import os
import time
import logging
import threading
import json
import requests
import urllib.parse
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta, time as dt_time
from nicegui import ui
from dotenv import load_dotenv

import auth
from options_math import calculate_iv, bs_price

# Brokers
from py5paisa import FivePaisaClient
from py5paisa.order import Order as FivePaisaOrder
try:
    from neo_api_client import NeoAPI
    KOTAK_SDK_AVAILABLE = True
except ImportError:
    KOTAK_SDK_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Helper to dynamically get the next weekly expiry for Option Chain
def get_next_thursday():
    today = datetime.today()
    days_ahead = 3 - today.weekday()
    if days_ahead < 0: days_ahead += 7
    return (today + timedelta(days_ahead)).strftime('%Y-%m-%d')

# ==========================================
# 1. GLOBAL CONFIGURATION & STATE
# ==========================================
class AlgoConfig:
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", -5000.0))
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES", 20))
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
    def get_option_chain(self): return []

class UpstoxAdapter(BrokerAdapter):
    def __init__(self, session):
        self.access_token = session.get('ACCESS_TOKEN')
        self.headers = {'accept': 'application/json', 'Api-Version': '2.0', 'Authorization': f'Bearer {self.access_token}'}
        res = requests.get("https://api.upstox.com/v2/user/profile", headers=self.headers)
        if res.status_code != 200: raise Exception(f"Upstox Profile Auth failed: {res.text}")

    def get_positions(self):
        res = requests.get("https://api.upstox.com/v2/portfolio/short-term-positions", headers=self.headers)
        if res.status_code == 200:
            return [{'NetQty': p.get('net_quantity', 0), 'ScripCode': p.get('instrument_token', ''),
                     'ScripName': p.get('trading_symbol', ''), 'AveragePrice': p.get('average_price', 0.0)}
                    for p in res.json().get('data', [])]
        return []

    def place_order(self, scrip_code, qty, is_buy):
        data = {
            "quantity": abs(qty), "product": "D", "validity": "DAY", "price": 0.0,
            "instrument_token": str(scrip_code), "order_type": "MARKET",
            "transaction_type": "BUY" if is_buy else "SELL",
            "disclosed_quantity": 0, "trigger_price": 0.0, "is_amo": False
        }
        res = requests.post("https://api.upstox.com/v2/order/regular", headers=self.headers, json=data)
        return res.json()

    def get_option_chain(self):
        # Fetch real-time NIFTY chain for the closest expiry
        url = f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote('NSE_INDEX|Nifty 50')}&expiry_date={get_next_thursday()}"
        try:
            res = requests.get(url, headers=self.headers)
            if res.status_code == 200: return res.json().get('data', [])
        except: pass
        return []

class FivePaisaAdapter(BrokerAdapter):
    def __init__(self, session):
        cred = {"APP_SOURCE": session.get('APP_SOURCE', ''), "APP_NAME": "NiceGUI_Algo", "USER_ID": session.get('USER_ID', ''), 
                "PASSWORD": session.get('USER_PASSWORD', ''), "USER_KEY": session.get('API_KEY', ''), "ENCRYPTION_KEY": session.get('ENCRYPTION_KEY', '')}
        self.client = FivePaisaClient(email="dummy@example.com", passwd=cred["PASSWORD"], dob="19900101", cred=cred)
        self.client.access_token = session.get('ACCESS_TOKEN')
        self.client.client_code = session.get('CLIENT_CODE')
        if not self.client.margin(): raise Exception("5paisa Auth failed")

    def get_positions(self): return self.client.positions() or []
    def place_order(self, scrip_code, qty, is_buy):
        req = FivePaisaOrder(order_type="B" if is_buy else "S", exchange="N", exchange_segment="D", scrip_code=scrip_code, quantity=qty, price=0, is_intraday=False)
        return self.client.place_order(req)

class KotakNeoAdapter(BrokerAdapter):
    def __init__(self, session):
        if not KOTAK_SDK_AVAILABLE: raise Exception("Kotak SDK not installed.")
        self.client = NeoAPI(consumer_key=session.get('KOTAK_CONSUMER_KEY'), consumer_secret=session.get('KOTAK_CONSUMER_SECRET'), environment='prod')
        self.client.login(mobilenumber=session.get('KOTAK_MOBILE'), password=session.get('KOTAK_PASSWORD'))
        self.client.session_2fa(OTP=session.get('KOTAK_MPIN'))
    def get_positions(self): return self.client.positions()
    def place_order(self, scrip_code, qty, is_buy):
        return self.client.place_order(exchange_segment="nse_fo", product="NRML", price="", order_type="MKT", quantity=str(qty), validity="DAY", trading_symbol=scrip_code, transaction_type="B" if is_buy else "S")

broker = None
def initialize_client(session_data):
    global broker
    try:
        b_choice = session_data.get('ACTIVE_BROKER')
        if b_choice == 'UPSTOX': broker = UpstoxAdapter(session_data)
        elif b_choice == '5paisa': broker = FivePaisaAdapter(session_data)
        elif b_choice == 'KOTAK': broker = KotakNeoAdapter(session_data)
        else: return False
        State.active_broker_name = b_choice
        return True
    except Exception as e:
        logging.error(f"Broker init failed: {e}")
        return False

# ==========================================
# 3. MOCK DATA & EXECUTION ENGINE
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
                pos['ltp'] = round(max(0.05, pos['ltp'] + np.random.normal(0, 0.8)), 2)
        time.sleep(1)

def fetch_live_positions():
    if not broker or State.is_mock_mode: return
    try:
        raw_positions = broker.get_positions()
        for pos in raw_positions:
            qty = int(pos.get('NetQty', 0))
            if qty == 0: continue
            scrip_code = str(pos.get('ScripCode', 0)) # Standardized as string for broker keys
            symbol = pos.get('ScripName', pos.get('tradingSymbol', f"Scrip_{scrip_code}"))
            entry_price = float(pos.get('AveragePrice', pos.get('buyAmt', 0.0)))
            
            strike = 0.0; opt_type = 'XX'
            try:
                parts = symbol.split()
                strike = float(parts[-2])
                opt_type = parts[-1].upper()
            except: pass

            State.positions[scrip_code] = {
                'symbol': symbol, 'strike': strike, 'opt_type': opt_type, 'qty': qty, 'entry': entry_price, 
                'ltp': entry_price, 'sl': 0.0, 'tp': 0.0, 'is_long': qty > 0, 'adjust_lot': 25, 'slice_size': abs(qty)
            }
    except Exception as e:
        logging.error(f"Live fetch failed: {e}")

# ==========================================
# 4. CHARTING & UI DASHBOARD
# ==========================================
def generate_payoff_chart(positions_dict, days_to_expiry=3):
    fig = go.Figure()
    if not positions_dict:
        fig.update_layout(title="No Active Positions", template="plotly_white", height=280)
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
    fig.update_layout(title="Options Payoff Profile", hovermode="x unified", template="plotly_white", height=280, margin=dict(l=20, r=20, t=40, b=20))
    return fig

def build_ui():
    with ui.header().classes('bg-slate-900 items-center p-4 justify-between flex-nowrap'):
        title = f'📈 Pro Terminal [{State.active_broker_name}]'
        ui.label(title).classes('text-2xl font-bold text-orange-400' if State.is_mock_mode else 'text-2xl font-bold text-white')
        
        with ui.row().classes('gap-4 items-center flex-nowrap'):
            ui.label().bind_text_from(State, 'realized_pnl', backward=lambda p: f"Booked: ₹{p:.2f}").classes('text-gray-300 font-bold')
            State.ui_elements['total_running_pnl'] = ui.label("Running: ₹0.00").classes('text-yellow-400 font-bold')
            ui.button(icon='logout', color='red', on_click=lambda: [os.remove(auth.SESSION_FILE) if os.path.exists(auth.SESSION_FILE) else None, setattr(State, 'is_mock_mode', False), State.positions.clear(), ui.navigate.to('/login')]).classes('ml-4 p-2')

    if not State.positions and not State.is_mock_mode:
        fetch_live_positions()

    with ui.tabs().classes('w-full mt-4') as tabs:
        live_tab = ui.tab('Live Execution')
        builder_tab = ui.tab('Strategy Builder')

    with ui.tab_panels(tabs, value=builder_tab).classes('w-full max-w-7xl mx-auto'):
        
        # --- LIVE TAB ---
        with ui.tab_panel(live_tab):
            State.ui_elements['live_chart'] = ui.plotly(generate_payoff_chart(State.positions)).classes('w-full mb-4 h-[300px]')
            with ui.row().classes('w-full bg-slate-200 p-2 font-bold text-center text-sm flex-nowrap'):
                ui.label("Symbol").classes('w-3/12 text-left'); ui.label("Qty").classes('w-1/12'); ui.label("LTP/PnL").classes('w-2/12')
                ui.label("Scale").classes('w-3/12'); ui.label("Action").classes('w-3/12')

            for scrip, data in State.positions.items():
                with ui.row().classes('w-full bg-white shadow p-2 items-center text-center text-sm flex-nowrap'):
                    ui.label(data['symbol']).classes('w-3/12 font-bold text-left truncate')
                    State.ui_elements[f'qty_{scrip}'] = ui.label(str(data['qty'])).classes('w-1/12 font-bold text-lg')
                    with ui.column().classes('w-2/12 gap-0'):
                        State.ui_elements[f'ltp_{scrip}'] = ui.label(f"₹{data['ltp']}").classes('font-bold')
                        State.ui_elements[f'pnl_{scrip}'] = ui.label("₹0.00").classes('font-bold text-xs')
                    with ui.row().classes('w-3/12 justify-center gap-1 flex-nowrap'):
                        ui.number(value=data['adjust_lot'], format='%d').classes('w-12').props('dense')
                        ui.button("+", color='green').classes('p-1 h-6 min-w-0')
                        ui.button("-", color='orange').classes('p-1 h-6 min-w-0')
                    ui.button("Square Off", color='red').classes('w-3/12 h-8 text-xs font-bold')

        # --- OPTION CHAIN & STRATEGY BUILDER ---
        with ui.tab_panel(builder_tab):
            def add_to_sim(strike, opt_type, price, is_buy, instrument_code):
                qty = 50 if is_buy else -50
                if instrument_code in State.simulated_cart: State.simulated_cart[instrument_code]['qty'] += qty
                else: State.simulated_cart[instrument_code] = {'symbol': instrument_code, 'strike': strike, 'opt_type': opt_type, 'entry': price, 'ltp': price, 'qty': qty}
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

            with ui.row().classes('w-full gap-4 flex-nowrap'):
                
                # LEFT COLUMN: 60% Width
                with ui.column().classes('w-[60%] min-w-[550px]'):
                    ui.label("Live Option Chain (NIFTY)").classes('font-bold text-lg bg-gray-200 p-2 w-full rounded')
                    
                    with ui.row().classes('w-full font-bold text-[11px] bg-slate-100 p-2 text-center flex-nowrap items-center'):
                        ui.label("CE Buy").classes('flex-1'); ui.label("CE Sell").classes('flex-1')
                        ui.label("LTP").classes('flex-1'); ui.label("OI").classes('flex-1')
                        ui.label("STRIKE").classes('flex-1 text-blue-600 text-sm')
                        ui.label("OI").classes('flex-1'); ui.label("LTP").classes('flex-1')
                        ui.label("PE Sell").classes('flex-1'); ui.label("PE Buy").classes('flex-1')

                    # 1. Ask broker for chain. 2. If none, generate mock data
                    raw_chain = broker.get_option_chain() if hasattr(broker, 'get_option_chain') and not State.is_mock_mode else []
                    mock_chain_data = {}
                    
                    if raw_chain:
                        # Convert Upstox chain into UI mapping
                        raw_chain = sorted(raw_chain, key=lambda x: x.get('strike_price', 0))
                        center_idx = len(raw_chain)//2 # Center on spot roughly
                        for item in raw_chain[max(0, center_idx-3) : center_idx+3]:
                            strike = item.get('strike_price')
                            ce = item.get('call_options', {}).get('market_data', {})
                            pe = item.get('put_options', {}).get('market_data', {})
                            mock_chain_data[strike] = {
                                'ce_ltp': ce.get('ltp', 0.0), 'ce_oi': str(ce.get('oi', 0)),
                                'pe_ltp': pe.get('ltp', 0.0), 'pe_oi': str(pe.get('oi', 0)),
                                'ce_code': item.get('call_options', {}).get('instrument_key', f'CE_{strike}'),
                                'pe_code': item.get('put_options', {}).get('instrument_key', f'PE_{strike}')
                            }
                    else:
                        mock_chain_data = {
                            21800: {'ce_ltp': 245.5, 'ce_oi': '12.5k', 'pe_ltp': 12.2,  'pe_oi': '2.1k', 'ce_code': 'SIM_21800_CE', 'pe_code': 'SIM_21800_PE'},
                            21900: {'ce_ltp': 175.0, 'ce_oi': '18.2k', 'pe_ltp': 34.5,  'pe_oi': '1.5k', 'ce_code': 'SIM_21900_CE', 'pe_code': 'SIM_21900_PE'},
                            22000: {'ce_ltp': 115.0, 'ce_oi': '25.0k', 'pe_ltp': 95.0,  'pe_oi': '5.8k', 'ce_code': 'SIM_22000_CE', 'pe_code': 'SIM_22000_PE'},
                            22100: {'ce_ltp': 68.5,  'ce_oi': '10.1k', 'pe_ltp': 148.0, 'pe_oi': '18.9k', 'ce_code': 'SIM_22100_CE', 'pe_code': 'SIM_22100_PE'},
                            22200: {'ce_ltp': 35.0,  'ce_oi': '4.5k',  'pe_ltp': 215.5, 'pe_oi': '22.4k', 'ce_code': 'SIM_22200_CE', 'pe_code': 'SIM_22200_PE'},
                        }

                    for strike, data in mock_chain_data.items():
                        with ui.row().classes('w-full border-b p-1 items-center text-center flex-nowrap'):
                            ui.button("B", color='green', on_click=lambda s=strike, p=data['ce_ltp'], c=data['ce_code']: add_to_sim(s, 'CE', p, True, c)).classes('flex-1 h-8 min-w-0 p-0 text-xs')
                            ui.button("S", color='red', on_click=lambda s=strike, p=data['ce_ltp'], c=data['ce_code']: add_to_sim(s, 'CE', p, False, c)).classes('flex-1 h-8 min-w-0 p-0 text-xs')
                            ui.label(str(data['ce_ltp'])).classes('flex-1 text-xs font-semibold')
                            ui.label(data['ce_oi']).classes('flex-1 text-[10px] text-gray-500')
                            
                            ui.label(str(strike)).classes('flex-1 font-bold text-sm bg-gray-50 py-1 rounded')
                            
                            ui.label(data['pe_oi']).classes('flex-1 text-[10px] text-gray-500')
                            ui.label(str(data['pe_ltp'])).classes('flex-1 text-xs font-semibold')
                            ui.button("S", color='red', on_click=lambda s=strike, p=data['pe_ltp'], c=data['pe_code']: add_to_sim(s, 'PE', p, False, c)).classes('flex-1 h-8 min-w-0 p-0 text-xs')
                            ui.button("B", color='green', on_click=lambda s=strike, p=data['pe_ltp'], c=data['pe_code']: add_to_sim(s, 'PE', p, True, c)).classes('flex-1 h-8 min-w-0 p-0 text-xs')
                
                # RIGHT COLUMN: 38% Width
                with ui.column().classes('w-[38%] min-w-[350px] bg-white shadow-lg p-4 rounded border'):
                    ui.label("Strategy Cart & Payoff").classes('font-bold text-lg mb-2')
                    State.ui_elements['sim_chart'] = ui.plotly(generate_payoff_chart({})).classes('w-full h-[280px]')
                    
                    ui.label("Adjust Legs").classes('font-bold text-sm mt-4 text-gray-500')
                    with ui.scroll_area().classes('w-full max-h-[200px] border rounded p-2'):
                        State.ui_elements['sim_cart_container'] = ui.column().classes('w-full gap-2')
                    
                    def render_cart():
                        State.ui_elements['sim_cart_container'].clear()
                        with State.ui_elements['sim_cart_container']:
                            for code, leg in list(State.simulated_cart.items()):
                                with ui.row().classes('w-full items-center justify-between text-sm bg-gray-50 p-2 rounded border flex-nowrap'):
                                    action_color = 'text-green-600' if leg['qty'] > 0 else 'text-red-600'
                                    ui.label(f"{'BUY' if leg['qty']>0 else 'SELL'} {leg['strike']} {leg['opt_type']}").classes(f'font-bold {action_color} truncate w-1/3')
                                    ui.number(value=leg['qty'], format='%d', on_change=lambda e, c=code: update_cart_qty(c, int(e.value))).classes('w-20').props('dense')
                                    ui.button(icon="delete", color="gray", on_click=lambda c=code: remove_from_cart(c)).props('flat dense size=sm').classes('w-8')
                    
                    def execute_strategy():
                        if not State.simulated_cart:
                            ui.notify("Cart is empty!", type="warning")
                            return
                        if State.is_mock_mode:
                            ui.notify("Mock Strategy Placed Successfully!", type="positive")
                        else:
                            ui.notify(f"Executing Strategy to {State.active_broker_name}...", type="info")
                            for code, leg in State.simulated_cart.items():
                                broker.place_order(code, abs(leg['qty']), leg['qty'] > 0)
                        
                        State.simulated_cart.clear()
                        State.ui_elements['sim_chart'].update_figure(generate_payoff_chart({}))
                        render_cart()

                    with ui.row().classes('w-full mt-4 gap-2 flex-nowrap'):
                        ui.button("Clear", color='gray', on_click=lambda: [State.simulated_cart.clear(), render_cart(), State.ui_elements['sim_chart'].update_figure(generate_payoff_chart({}))]).classes('flex-1 font-bold')
                        ui.button("EXECUTE LIVE", color='blue-800', on_click=execute_strategy).classes('flex-[2] font-bold shadow-lg')

def update_ui_loop():
    total_running_pnl = 0.0; current_qty_sum = 0
    for scrip, data in list(State.positions.items()):
        current_qty_sum += data['qty']
        ltp_lbl, pnl_lbl = State.ui_elements.get(f'ltp_{scrip}'), State.ui_elements.get(f'pnl_{scrip}')
        qty_lbl = State.ui_elements.get(f'qty_{scrip}')
        
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
# 5. MAIN ROUTING & EXECUTION
# ==========================================
@ui.page('/')
def main_page():
    session = auth.load_session()
    is_mock = session.get('MOCK_MODE', False)
    
    if not is_mock:
        active_broker = session.get('ACTIVE_BROKER')
        if not active_broker or (active_broker in ['5paisa', 'UPSTOX'] and (not session.get('ACCESS_TOKEN') or auth.is_token_expired(session))):
            ui.navigate.to('/login')
            return

        is_connected = initialize_client(session)
        if not is_connected:
            with ui.card().classes('absolute-center w-full max-w-md p-8 shadow-2xl rounded-xl border border-red-200 text-center'):
                ui.label(f"🚨 {active_broker} Connection Failed!").classes('text-2xl font-bold text-red-600 mb-2')
                ui.label("The background API test failed. Check your API Keys.").classes('text-gray-700')
                ui.button("Proceed with Sample Data", color="orange", on_click=lambda: [session.update({'MOCK_MODE': True}), auth.save_session(session), ui.navigate.to('/')]).classes('mt-6 w-full font-bold')
                ui.button("Clear Data & Try Again", color="red", on_click=lambda: [session.clear(), auth.save_session(session), ui.navigate.to('/login')]).classes('mt-2 w-full font-bold')
            return
            
    else:
        State.is_mock_mode = True
        if not State.positions: setup_mock_iron_condor()
    
    build_ui()
    
    if is_mock:
        if not any(thread.name == "mock_ws_thread" for thread in threading.enumerate()):
            threading.Thread(target=mock_ws_worker, name="mock_ws_thread", daemon=True).start()
    
    ui.timer(0.5, update_ui_loop)

if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.getenv("PORT", 8080))
    ui.run(host="0.0.0.0", port=port, reload=False, title="Options Pro Terminal")
