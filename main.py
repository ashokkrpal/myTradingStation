import os
import time
import logging
import threading
import json
import numpy as np
import plotly.graph_objects as go
from datetime import time as dt_time
from nicegui import ui
from dotenv import load_dotenv

import auth
from options_math import calculate_iv, bs_price

# Broker SDKs
from py5paisa import FivePaisaClient
from py5paisa.order import Order as FivePaisaOrder
try:
    from neo_api_client import NeoAPI
    KOTAK_SDK_AVAILABLE = True
except ImportError:
    KOTAK_SDK_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# 1. GLOBAL CONFIGURATION & STATE
# ==========================================
class AlgoConfig:
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", -5000.0))
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES", 20))
    TIME_EXIT = dt_time(15, 15)
    HALT_TRADING = False
    SLICE_DELAY_SEC = 1.5
    RISK_FREE_RATE = 0.07

class State:
    is_mock_mode = False
    active_broker_name = "None"
    trades_executed = 0
    realized_pnl = 0.0
    positions = {}        
    simulated_cart = {}   
    ui_elements = {}
    last_known_qty_sum = 0
    current_spot = 22000.0 

# ==========================================
# 2. BROKER ADAPTER FACTORY
# ==========================================
class BrokerAdapter:
    def get_positions(self): return []
    def place_order(self, scrip_code, qty, is_buy): pass
    def start_websocket(self, req_list, on_message_callback): pass

class FivePaisaAdapter(BrokerAdapter):
    def __init__(self, session):
        cred = {
            "APP_SOURCE": session.get('APP_SOURCE', ''), "APP_NAME": "NiceGUI_Algo", 
            "USER_ID": session.get('USER_ID', ''), "PASSWORD": session.get('USER_PASSWORD', ''),
            "USER_KEY": session.get('API_KEY', ''), "ENCRYPTION_KEY": session.get('ENCRYPTION_KEY', '')
        }
        self.client = FivePaisaClient(email="dummy@example.com", passwd=cred["PASSWORD"], dob="19900101", cred=cred)
        self.client.access_token = session.get('ACCESS_TOKEN')
        self.client.client_code = session.get('CLIENT_CODE')
        if not self.client.margin(): raise Exception("5paisa Margin/Auth check failed")

    def get_positions(self):
        raw = self.client.positions()
        return raw if raw else []

    def place_order(self, scrip_code, qty, is_buy):
        order_type = "B" if is_buy else "S"
        req = FivePaisaOrder(order_type=order_type, exchange="N", exchange_segment="D", scrip_code=scrip_code, quantity=qty, price=0, is_intraday=False)
        return self.client.place_order(req)

    def start_websocket(self, req_list, on_message_callback):
        ws = self.client.ws_client(on_message=on_message_callback)
        ws.connect()
        self.client.Request_Feed('mf', 's', req_list)

class KotakNeoAdapter(BrokerAdapter):
    def __init__(self, session):
        if not KOTAK_SDK_AVAILABLE: raise Exception("Kotak Neo SDK not installed. Run 'pip install neo-api-client'")
        self.client = NeoAPI(consumer_key=session.get('KOTAK_CONSUMER_KEY'), consumer_secret=session.get('KOTAK_CONSUMER_SECRET'), environment='prod')
        self.client.login(mobilenumber=session.get('KOTAK_MOBILE'), password=session.get('KOTAK_PASSWORD'))
        self.client.session_2fa(OTP=session.get('KOTAK_MPIN'))

    def get_positions(self):
        return self.client.positions()

    def place_order(self, scrip_code, qty, is_buy):
        transaction_type = "B" if is_buy else "S"
        return self.client.place_order(exchange_segment="nse_fo", product="NRML", price="", order_type="MKT", quantity=str(qty), validity="DAY", trading_symbol=scrip_code, transaction_type=transaction_type)
        
    def start_websocket(self, req_list, on_message_callback):
        # Implement Kotak WS logic here
        pass

# Global adapter instance
broker = None

def initialize_client(session_data):
    global broker
    try:
        broker_choice = session_data.get('ACTIVE_BROKER')
        if broker_choice == '5paisa':
            broker = FivePaisaAdapter(session_data)
        elif broker_choice == 'KOTAK':
            broker = KotakNeoAdapter(session_data)
        else:
            return False
        
        State.active_broker_name = broker_choice
        logging.info(f"{broker_choice} API Connection Established!")
        return True
    except Exception as e:
        logging.error(f"Broker initialization failed: {e}")
        return False

# ==========================================
# 3. MOCK DATA ENGINE (IRON CONDOR)
# ==========================================
def setup_mock_iron_condor():
    State.current_spot = 22000.0
    State.active_broker_name = "MOCK MODE"
    State.positions = {
        1: {'symbol': 'NIFTY 21500 PE', 'strike': 21500.0, 'opt_type': 'PE', 'qty': -50, 'entry': 85.5, 'ltp': 85.5, 'sl': 0.0, 'tp': 0.0, 'is_long': False, 'adjust_lot': 50, 'slice_size': 50},
        2: {'symbol': 'NIFTY 21300 PE', 'strike': 21300.0, 'opt_type': 'PE', 'qty': 50,  'entry': 45.0, 'ltp': 45.0, 'sl': 0.0, 'tp': 0.0, 'is_long': True,  'adjust_lot': 50, 'slice_size': 50},
        3: {'symbol': 'NIFTY 22500 CE', 'strike': 22500.0, 'opt_type': 'CE', 'qty': -50, 'entry': 90.0, 'ltp': 90.0, 'sl': 0.0, 'tp': 0.0, 'is_long': False, 'adjust_lot': 50, 'slice_size': 50},
        4: {'symbol': 'NIFTY 22700 CE', 'strike': 22700.0, 'opt_type': 'CE', 'qty': 50,  'entry': 50.0, 'ltp': 50.0, 'sl': 0.0, 'tp': 0.0, 'is_long': True,  'adjust_lot': 50, 'slice_size': 50},
    }

def mock_ws_worker():
    while State.is_mock_mode:
        if not AlgoConfig.HALT_TRADING:
            State.current_spot += np.random.normal(0, 1.5)
            for scrip, pos in list(State.positions.items()):
                if pos.get('is_closing'): continue
                new_ltp = max(0.05, pos['ltp'] + np.random.normal(0, 0.8))
                process_tick(scrip, new_ltp)
        time.sleep(1)

# ==========================================
# 4. TRADING EXECUTION ENGINE
# ==========================================
def fetch_live_positions():
    if not broker or State.is_mock_mode: return
    try:
        raw_positions = broker.get_positions()
        for pos in raw_positions:
            qty = int(pos.get('NetQty', 0))
            if qty == 0: continue
            
            scrip_code = int(pos.get('ScripCode', 0))
            symbol = pos.get('ScripName', pos.get('tradingSymbol', f"Scrip_{scrip_code}"))
            entry_price = float(pos.get('AveragePrice', pos.get('buyAmt', 0.0)))
            
            strike = 0.0; opt_type = 'XX'
            try:
                parts = symbol.split()
                strike = float(parts[-2])
                opt_type = parts[-1].upper()
            except: pass

            State.positions[scrip_code] = {
                'symbol': symbol, 'strike': strike, 'opt_type': opt_type,
                'qty': qty, 'entry': entry_price, 'ltp': entry_price,
                'sl': 0.0, 'tp': 0.0, 'is_long': qty > 0,
                'adjust_lot': 25, 'slice_size': abs(qty)
            }
    except Exception as e:
        logging.error(f"Live fetch failed: {e}")

def adjust_position(scrip_code, adjust_amount, is_increase):
    pos = State.positions.get(scrip_code)
    if not pos or AlgoConfig.HALT_TRADING or adjust_amount <= 0: return

    is_buy = not pos['is_long'] if not is_increase else pos['is_long']
    qty_change = adjust_amount if pos['is_long'] else -adjust_amount
    if not is_increase: qty_change = -qty_change

    if not is_increase and abs(qty_change) >= abs(pos['qty']): return 

    try:
        if not State.is_mock_mode: broker.place_order(scrip_code, abs(adjust_amount), is_buy)
        logging.info(f"🔄 {'[MOCK] ' if State.is_mock_mode else ''}ADJUST {'BUY' if is_buy else 'SELL'} {abs(adjust_amount)} for {scrip_code}")
        
        if is_increase:
            total_cost = (abs(pos['qty']) * pos['entry']) + (abs(adjust_amount) * pos['ltp'])
            pos['entry'] = round(total_cost / (abs(pos['qty']) + abs(adjust_amount)), 2)
        pos['qty'] += qty_change
        State.trades_executed += 1
    except Exception as e:
        logging.error(f"Adjustment failed: {e}")

def execute_square_off(scrip_code, reason, price):
    pos = State.positions.get(scrip_code)
    if not pos or AlgoConfig.HALT_TRADING or pos.get('is_closing', False): return
    pos['is_closing'] = True
    
    slice_size = pos.get('slice_size', abs(pos['qty']))
    total_qty = abs(pos['qty'])
    is_buy = not pos['is_long']
    if slice_size >= total_qty or slice_size <= 0: slice_size = total_qty

    def slicer_thread():
        remaining = total_qty
        while remaining > 0 and scrip_code in State.positions:
            current_slice = min(slice_size, remaining)
            try:
                if not State.is_mock_mode: broker.place_order(scrip_code, current_slice, is_buy)
                logging.info(f"🚨 {'[MOCK] ' if State.is_mock_mode else ''}SLICE {'BUY' if is_buy else 'SELL'} {current_slice} for {scrip_code} | {reason}")
                
                remaining -= current_slice
                State.trades_executed += 1
                slice_pnl = (price - pos['entry']) * current_slice if pos['is_long'] else (pos['entry'] - price) * current_slice
                State.realized_pnl += slice_pnl
                
                pos['qty'] += -current_slice if pos['is_long'] else current_slice
                
                if remaining <= 0 or pos['qty'] == 0:
                    del State.positions[scrip_code]
                    State.ui_elements.pop(f'ltp_{scrip_code}', None)
                    break
                time.sleep(AlgoConfig.SLICE_DELAY_SEC)
            except Exception as e:
                logging.error(f"Slice failed: {e}")
                pos['is_closing'] = False
                break
    threading.Thread(target=slicer_thread, daemon=True).start()

def process_tick(scrip_code, ltp):
    if AlgoConfig.HALT_TRADING: return
    if State.realized_pnl <= AlgoConfig.MAX_DAILY_LOSS: AlgoConfig.HALT_TRADING = True; return
    
    pos = State.positions.get(scrip_code)
    if not pos or pos.get('is_closing', False): return 
    pos['ltp'] = round(ltp, 2)
    
    sl_hit = (pos['is_long'] and pos['sl'] > 0 and ltp <= pos['sl']) or (not pos['is_long'] and pos['sl'] > 0 and ltp >= pos['sl'])
    tp_hit = (pos['is_long'] and pos['tp'] > 0 and ltp >= pos['tp']) or (not pos['is_long'] and pos['tp'] > 0 and ltp <= pos['tp'])
    
    if sl_hit: execute_square_off(scrip_code, "SL Hit", ltp)
    elif tp_hit: execute_square_off(scrip_code, "TP Hit", ltp)

def on_message(msg):
    try:
        data = json.loads(msg)
        for tick in data:
            scrip = tick.get('Token')
            ltp = tick.get('LastTradedPrice') or tick.get('LastRate')
            if scrip and ltp: process_tick(scrip, float(ltp))
    except Exception: pass 

def ws_worker():
    if not broker: return
    try:
        req_list = [{"Exch": "N", "ExchType": "D", "ScripCode": scrip} for scrip in State.positions.keys()]
        if not req_list: return
        broker.start_websocket(req_list, on_message)
    except Exception as e: logging.error(f"WS Error: {e}")

# ==========================================
# 5. CHARTING ENGINE & UI DASHBOARD
# ==========================================
def generate_payoff_chart(positions_dict, days_to_expiry=3):
    fig = go.Figure()
    if not positions_dict:
        fig.update_layout(title="No Active Positions", template="plotly_white", height=350)
        return fig

    t_years = max(days_to_expiry / 365.0, 0.0001)
    strikes = [p['strike'] for p in positions_dict.values()]
    if not strikes: return fig
    
    spot_range = np.linspace(min(strikes) * 0.90, max(strikes) * 1.10, 200)
    expiry_payoff = np.zeros_like(spot_range)
    t0_payoff = np.zeros_like(spot_range)

    for pos in positions_dict.values():
        K, opt_type, entry, qty, ltp = pos['strike'], pos['opt_type'], pos['entry'], pos['qty'], pos['ltp']
        intrinsic = np.maximum(0, spot_range - K) if opt_type == 'CE' else np.maximum(0, K - spot_range)
        expiry_payoff += (intrinsic - entry) * qty
        iv = calculate_iv(ltp, State.current_spot, K, t_years, AlgoConfig.RISK_FREE_RATE, opt_type)
        t0_prices = np.array([bs_price(S, K, t_years, AlgoConfig.RISK_FREE_RATE, iv, opt_type) for S in spot_range])
        t0_payoff += (t0_prices - entry) * qty

    fig.add_trace(go.Scatter(x=spot_range, y=expiry_payoff, mode='lines', name='Expiry', line=dict(color='gray', dash='dash')))
    color = '#10B981' if np.max(t0_payoff) > 0 else '#EF4444'
    fig.add_trace(go.Scatter(x=spot_range, y=t0_payoff, mode='lines', name='T+0 Live', line=dict(color=color, width=3), fill='tozeroy'))
    fig.add_vline(x=State.current_spot, line_dash="dot", line_color="orange")
    fig.update_layout(title="Options Payoff Profile", hovermode="x unified", template="plotly_white", height=350, margin=dict(l=20, r=20, t=40, b=20))
    return fig

def build_ui():
    with ui.header().classes('bg-slate-900 items-center p-4 justify-between'):
        title = f'📈 Pro Terminal [{State.active_broker_name}]'
        ui.label(title).classes('text-2xl font-bold text-orange-400' if State.is_mock_mode else 'text-2xl font-bold text-white')
        
        with ui.row().classes('gap-4 items-center'):
            ui.label().bind_text_from(State, 'realized_pnl', backward=lambda p: f"Booked: ₹{p:.2f}").classes('text-gray-300 font-bold')
            State.ui_elements['total_running_pnl'] = ui.label("Running: ₹0.00").classes('text-yellow-400 font-bold')
            
            def hard_logout():
                if os.path.exists(auth.SESSION_FILE): os.remove(auth.SESSION_FILE)
                State.is_mock_mode = False
                State.positions.clear()
                ui.navigate.to('/login')
                
            ui.button(icon='logout', color='red', on_click=hard_logout).classes('ml-4 p-2')

    if not State.positions and not State.is_mock_mode:
        fetch_live_positions()

    with ui.tabs().classes('w-full mt-4') as tabs:
        live_tab = ui.tab('Live Execution')
        builder_tab = ui.tab('Strategy Builder')

    with ui.tab_panels(tabs, value=live_tab).classes('w-full max-w-7xl mx-auto'):
        with ui.tab_panel(live_tab):
            State.ui_elements['live_chart'] = ui.plotly(generate_payoff_chart(State.positions)).classes('w-full mb-4')
            
            with ui.row().classes('w-full bg-slate-200 p-2 font-bold text-center text-sm flex'):
                ui.label("Symbol").classes('w-2/12 text-left'); ui.label("Qty").classes('w-1/12')
                ui.label("LTP/PnL").classes('w-2/12'); ui.label("Scale").classes('w-2/12')
                ui.label("SL").classes('w-1/12'); ui.label("TP").classes('w-1/12')
                ui.label("Slice").classes('w-1/12'); ui.label("Action").classes('w-2/12')

            for scrip, data in State.positions.items():
                with ui.row().classes('w-full bg-white shadow p-2 items-center text-center text-sm flex'):
                    ui.label(data['symbol']).classes('w-2/12 font-bold text-left')
                    State.ui_elements[f'qty_{scrip}'] = ui.label(str(data['qty'])).classes('w-1/12 font-bold text-lg')
                    
                    with ui.column().classes('w-2/12 gap-0'):
                        State.ui_elements[f'ltp_{scrip}'] = ui.label(f"₹{data['ltp']}").classes('font-bold')
                        State.ui_elements[f'pnl_{scrip}'] = ui.label("₹0.00").classes('font-bold text-xs')
                    
                    with ui.row().classes('w-2/12 justify-center gap-1'):
                        ui.number(value=data['adjust_lot'], format='%d', on_change=lambda e, s=scrip: State.positions[s].update({'adjust_lot': int(e.value)})).classes('w-10').props('dense')
                        ui.button("+", color='green', on_click=lambda s=scrip: adjust_position(s, State.positions[s]['adjust_lot'], True)).classes('p-1 h-6 min-w-0')
                        ui.button("-", color='orange', on_click=lambda s=scrip: adjust_position(s, State.positions[s]['adjust_lot'], False)).classes('p-1 h-6 min-w-0')
                    
                    ui.number(value=data['sl'], format='%.2f', on_change=lambda e, s=scrip: State.positions[s].update({'sl': e.value})).classes('w-1/12').props('dense')
                    ui.number(value=data['tp'], format='%.2f', on_change=lambda e, s=scrip: State.positions[s].update({'tp': e.value})).classes('w-1/12').props('dense')
                    ui.number(value=data['slice_size'], format='%d', on_change=lambda e, s=scrip: State.positions[s].update({'slice_size': int(e.value)})).classes('w-1/12').props('dense')
                    ui.button("Square Off", color='red', on_click=lambda s=scrip: execute_square_off(s, "Manual", State.positions[s]['ltp'])).classes('w-2/12 h-8 text-xs font-bold')

        # --- OPTION CHAIN & STRATEGY BUILDER ---
        with ui.tab_panel(builder_tab):
            def add_to_sim(strike, opt_type, price, is_buy):
                code = f"SIM_{strike}_{opt_type}"
                qty = 50 if is_buy else -50
                if code in State.simulated_cart: State.simulated_cart[code]['qty'] += qty
                else: State.simulated_cart[code] = {'symbol': f"NIFTY {strike} {opt_type}", 'strike': strike, 'opt_type': opt_type, 'entry': price, 'ltp': price, 'qty': qty}
                State.ui_elements['sim_chart'].update_figure(generate_payoff_chart(State.simulated_cart))
                render_cart()
                
            def remove_from_cart(code):
                State.simulated_cart.pop(code, None)
                State.ui_elements['sim_chart'].update_figure(generate_payoff_chart(State.simulated_cart))
                render_cart()

            def update_cart_qty(code, qty):
                if qty == 0: remove_from_cart(code)
                else:
                    State.simulated_cart[code]['qty'] = qty
                    State.ui_elements['sim_chart'].update_figure(generate_payoff_chart(State.simulated_cart))

            with ui.row().classes('w-full gap-4'):
                with ui.column().classes('w-1/2'):
                    ui.label("Live Option Chain (NIFTY)").classes('font-bold text-lg bg-gray-200 p-2 w-full rounded')
                    with ui.row().classes('w-full font-bold text-xs bg-slate-100 p-2 text-center'):
                        ui.label("CE Buy").classes('w-1/6'); ui.label("CE Sell").classes('w-1/6')
                        ui.label("STRIKE").classes('w-2/6 text-blue-600')
                        ui.label("PE Sell").classes('w-1/6'); ui.label("PE Buy").classes('w-1/6')

                    for strike in [21800, 21900, 22000, 22100, 22200]:
                        with ui.row().classes('w-full border-b p-2 items-center text-center'):
                            ui.button("B", color='green', on_click=lambda s=strike: add_to_sim(s, 'CE', 120, True)).classes('w-1/6 h-8 min-w-0')
                            ui.button("S", color='red', on_click=lambda s=strike: add_to_sim(s, 'CE', 118, False)).classes('w-1/6 h-8 min-w-0')
                            ui.label(str(strike)).classes('w-2/6 font-bold text-lg')
                            ui.button("S", color='red', on_click=lambda s=strike: add_to_sim(s, 'PE', 95, False)).classes('w-1/6 h-8 min-w-0')
                            ui.button("B", color='green', on_click=lambda s=strike: add_to_sim(s, 'PE', 98, True)).classes('w-1/6 h-8 min-w-0')
                
                with ui.column().classes('w-5/12 bg-white shadow-lg p-4 rounded border'):
                    ui.label("Strategy Cart & Payoff").classes('font-bold text-lg mb-2')
                    State.ui_elements['sim_chart'] = ui.plotly(generate_payoff_chart({})).classes('w-full h-64')
                    ui.label("Adjust Legs").classes('font-bold text-sm mt-4 text-gray-500')
                    State.ui_elements['sim_cart_container'] = ui.column().classes('w-full gap-2')
                    
                    def render_cart():
                        State.ui_elements['sim_cart_container'].clear()
                        with State.ui_elements['sim_cart_container']:
                            for code, leg in list(State.simulated_cart.items()):
                                with ui.row().classes('w-full items-center justify-between text-sm bg-gray-50 p-2 rounded'):
                                    action_color = 'text-green-600' if leg['qty'] > 0 else 'text-red-600'
                                    ui.label(f"{'BUY' if leg['qty']>0 else 'SELL'} {leg['strike']} {leg['opt_type']}").classes(f'font-bold {action_color}')
                                    ui.number(value=leg['qty'], format='%d', on_change=lambda e, c=code: update_cart_qty(c, int(e.value))).classes('w-20').props('dense')
                                    ui.button(icon="delete", color="gray", on_click=lambda c=code: remove_from_cart(c)).props('flat dense')
                    
                    def execute_strategy():
                        if not State.simulated_cart:
                            ui.notify("Cart is empty!", type="warning")
                            return
                        if State.is_mock_mode:
                            ui.notify("Mock Order Placed Successfully!", type="positive")
                        else:
                            ui.notify(f"Executing Strategy to {State.active_broker_name}...", type="info")
                            # Loop through cart and fire to the active broker adapter!
                            for code, leg in State.simulated_cart.items():
                                broker.place_order(leg['symbol'], abs(leg['qty']), leg['qty'] > 0)
                        
                        State.simulated_cart.clear()
                        State.ui_elements['sim_chart'].update_figure(generate_payoff_chart({}))
                        render_cart()

                    with ui.row().classes('w-full mt-4 gap-2'):
                        ui.button("Clear", color='gray', on_click=lambda: [State.simulated_cart.clear(), render_cart(), State.ui_elements['sim_chart'].update_figure(generate_payoff_chart({}))]).classes('w-1/3 font-bold')
                        ui.button("EXECUTE LIVE", color='blue-800', on_click=execute_strategy).classes('w-7/12 font-bold shadow-lg')

def update_ui_loop():
    total_running_pnl = 0.0; current_qty_sum = 0
    for scrip, data in list(State.positions.items()):
        current_qty_sum += data['qty']
        ltp_lbl, pnl_lbl, qty_lbl = State.ui_elements.get(f'ltp_{scrip}'), State.ui_elements.get(f'pnl_{scrip}'), State.ui_elements.get(f'qty_{scrip}')
        
        if ltp_lbl and pnl_lbl and qty_lbl:
            ltp_lbl.set_text(f"₹{data['ltp']}")
            qty_lbl.set_text(str(data['qty']))
            
            pnl = (data['ltp'] - data['entry']) * data['qty'] if data['is_long'] else (data['entry'] - data['ltp']) * abs(data['qty'])
            total_running_pnl += pnl
            pnl_lbl.set_text(f"₹{pnl:.2f}"); pnl_lbl.classes(replace='text-green-600 font-bold' if pnl >= 0 else 'text-red-600 font-bold')

    if current_qty_sum != State.last_known_qty_sum:
        if 'live_chart' in State.ui_elements: State.ui_elements['live_chart'].update_figure(generate_payoff_chart(State.positions))
        State.last_known_qty_sum = current_qty_sum

    if 'total_running_pnl' in State.ui_elements:
        State.ui_elements['total_running_pnl'].set_text(f"Running: ₹{total_running_pnl:.2f}")

# ==========================================
# 6. MAIN ROUTING & EXECUTION
# ==========================================
@ui.page('/')
def main_page():
    session = auth.load_session()
    is_mock = session.get('MOCK_MODE', False)
    
    if not is_mock:
        active_broker = session.get('ACTIVE_BROKER')
        
        if active_broker == '5paisa' and (not session.get('ACCESS_TOKEN') or auth.is_token_expired(session)):
            ui.navigate.to('/login')
            return
        elif not active_broker:
            ui.navigate.to('/login')
            return

        is_connected = initialize_client(session)
        
        if not is_connected:
            with ui.card().classes('absolute-center w-full max-w-md p-8 shadow-2xl rounded-xl border border-red-200 text-center'):
                ui.label(f"🚨 {active_broker} Connection Failed!").classes('text-2xl font-bold text-red-600 mb-2')
                ui.label("The background API connection test failed. Check your credentials.").classes('text-gray-700')
                
                def reset_and_retry():
                    session.clear() 
                    auth.save_session(session)
                    ui.navigate.to('/login')
                    
                def activate_mock():
                    session['MOCK_MODE'] = True
                    auth.save_session(session)
                    ui.navigate.to('/')
                    
                ui.button("Proceed with Sample Data", color="orange", on_click=activate_mock).classes('mt-6 w-full font-bold')
                ui.button("Clear Data & Try Again", color="red", on_click=reset_and_retry).classes('mt-2 w-full font-bold')
            return
            
    else:
        State.is_mock_mode = True
        if not State.positions:
            setup_mock_iron_condor()
    
    build_ui()
    
    if is_mock:
        if not any(thread.name == "mock_ws_thread" for thread in threading.enumerate()):
            threading.Thread(target=mock_ws_worker, name="mock_ws_thread", daemon=True).start()
    else:
        if not any(thread.name == f"{session.get('ACTIVE_BROKER')}_ws_thread" for thread in threading.enumerate()):
            threading.Thread(target=ws_worker, name=f"{session.get('ACTIVE_BROKER')}_ws_thread", daemon=True).start()
    
    ui.timer(0.5, update_ui_loop)

if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.getenv("PORT", 8080))
    ui.run(host="0.0.0.0", port=port, reload=False, title="Options Pro Terminal")
