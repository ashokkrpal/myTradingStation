import os
import time
import logging
import requests
import threading
import json
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, time as dt_time
from nicegui import ui, app
from py5paisa import FivePaisaClient
from py5paisa.order import Order
from dotenv import load_dotenv
from options_math import calculate_iv, bs_price

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
    trades_executed = 0
    realized_pnl = 0.0
    positions = {}        
    simulated_cart = {}   
    ui_elements = {}
    last_known_qty_sum = 0
    current_spot = 22000.0 

client = None

# ==========================================
# 2. OAUTH LOGIC & INITIALIZATION
# ==========================================
def get_access_token(request_token):
    """Fetches the daily Access Token using the provided Request Token."""
    url = "https://Openapi.5paisa.com/VendorsAPI/Service1.svc/GetAccessToken"
    payload = {
        "head": {"Key": app.storage.user.get('API_KEY')},
        "body": {
            "RequestToken": request_token,
            "EncryKey": app.storage.user.get('ENCRYPTION_KEY'),
            "UserId": app.storage.user.get('USER_ID')
        }
    }
    return requests.post(url, json=payload).json()

def initialize_client():
    """Initializes the 5paisa client using session storage."""
    global client
    try:
        cred = {
            "APP_SOURCE": app.storage.user.get('APP_SOURCE', ''),
            "APP_NAME": "NiceGUI_Algo", 
            "USER_ID": app.storage.user.get('USER_ID', ''),
            "PASSWORD": app.storage.user.get('USER_PASSWORD', ''),
            "USER_KEY": app.storage.user.get('API_KEY', ''),
            "ENCRYPTION_KEY": app.storage.user.get('ENCRYPTION_KEY', '')
        }
        
        client = FivePaisaClient(email="dummy@example.com", passwd=cred["PASSWORD"], dob="19900101", cred=cred)
        client.access_token = app.storage.user.get('ACCESS_TOKEN')
        client.client_code = app.storage.user.get('CLIENT_CODE')
        
        # Test connection validity
        if not client.margin():
            return False
            
        logging.info("5paisa OAuth Session Valid & Connected!")
        return True
    except Exception as e:
        logging.error(f"Client initialization failed: {e}")
        return False

def is_logged_in():
    return "ACCESS_TOKEN" in app.storage.user

# ==========================================
# 3. AUTHENTICATION WEB PAGES
# ==========================================
@ui.page('/login')
def login_page():
    if is_logged_in():
        ui.navigate.to('/')
        return

    with ui.card().classes('absolute-center w-full max-w-md p-8 shadow-2xl rounded-xl border border-gray-200'):
        ui.label('🔐 5paisa Secure Login').classes('text-2xl font-bold mb-2 text-center w-full')
        ui.label('Copy and paste the exact fields from your 5paisa API Dashboard.').classes('text-sm text-gray-500 mb-6 text-center w-full')

        api_key = ui.input('API Key', value=app.storage.user.get('API_KEY', '')).classes('w-full mb-2')
        encry_key = ui.input('Encryption Key', value=app.storage.user.get('ENCRYPTION_KEY', '')).classes('w-full mb-2').props('type=password')
        user_id = ui.input('User ID', value=app.storage.user.get('USER_ID', '')).classes('w-full mb-2')
        app_source = ui.input('App Source', value=app.storage.user.get('APP_SOURCE', '')).classes('w-full mb-2')
        user_password = ui.input('User Password', value=app.storage.user.get('USER_PASSWORD', '')).classes('w-full mb-6').props('type=password')

        def initiate_oauth():
            # Save inputs to the user's browser session
            app.storage.user.update({
                'API_KEY': api_key.value.strip(),
                'ENCRYPTION_KEY': encry_key.value.strip(),
                'USER_ID': user_id.value.strip(),
                'APP_SOURCE': app_source.value.strip(),
                'USER_PASSWORD': user_password.value.strip()
            })
            
            callback_url = "http://localhost:8080/callback" 
            auth_url = f"https://openapi.5paisa.com/WebVendorLogin/VLogin/Index?VendorKey={api_key.value.strip()}&ResponseURL={callback_url}"
            ui.navigate.to(auth_url)

        ui.button('Login via 5paisa', on_click=initiate_oauth).classes('w-full h-12 text-lg font-bold bg-blue-600 text-white rounded')

@ui.page('/callback')
def callback_page(RequestToken: str = None):
    if not RequestToken:
        with ui.card().classes('absolute-center p-6'):
            ui.label("❌ Authentication Failed: No Request Token received.").classes('text-red-500 font-bold')
            ui.button("Try Again", on_click=lambda: ui.navigate.to('/login')).classes('mt-4')
        return

    try:
        res = get_access_token(RequestToken)
        if res.get('body', {}).get('Status') == 0:
            app.storage.user['ACCESS_TOKEN'] = res['body']['AccessToken']
            app.storage.user['CLIENT_CODE'] = res['body']['ClientCode']
            ui.navigate.to('/')
        else:
            with ui.card().classes('absolute-center p-6 text-center'):
                ui.label(f"Token Exchange Failed!").classes('text-red-500 font-bold text-lg')
                ui.label(res.get('body', {}).get('Message', 'Check your Encryption Key and User ID.')).classes('text-gray-700 mt-2')
                ui.button("Try Again", on_click=lambda: ui.navigate.to('/login')).classes('mt-4')
    except Exception as e:
        ui.label(f"API Error during authentication: {e}")

# ==========================================
# 4. BUSINESS LOGIC & EXECUTION ENGINE
# ==========================================
def fetch_live_positions():
    if not client: return
    try:
        raw_positions = client.positions()
        if not raw_positions: return

        for pos in raw_positions:
            qty = int(pos.get('NetQty', 0))
            if qty == 0: continue
                
            scrip_code = int(pos.get('ScripCode'))
            symbol = pos.get('ScripName', f"Scrip_{scrip_code}")
            entry_price = float(pos.get('AveragePrice', 0.0))
            
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

    if is_increase:
        order_type = "B" if pos['is_long'] else "S"
        qty_change = adjust_amount if pos['is_long'] else -adjust_amount
    else:
        order_type = "S" if pos['is_long'] else "B"
        qty_change = -adjust_amount if pos['is_long'] else adjust_amount

    if not is_increase and abs(qty_change) >= abs(pos['qty']): return 

    req = Order(order_type=order_type, exchange="N", exchange_segment="D", scrip_code=scrip_code, quantity=abs(adjust_amount), price=0, is_intraday=False)
    try:
        # client.place_order(req) # UNCOMMENT FOR LIVE EXECUTION
        logging.info(f"🔄 ADJUST {order_type} {abs(adjust_amount)} for {scrip_code}")
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
    order_type = "S" if pos['is_long'] else "B"
    if slice_size >= total_qty or slice_size <= 0: slice_size = total_qty

    def slicer_thread():
        remaining = total_qty
        while remaining > 0 and scrip_code in State.positions:
            current_slice = min(slice_size, remaining)
            req = Order(order_type=order_type, exchange="N", exchange_segment="D", scrip_code=scrip_code, quantity=current_slice, price=0, is_intraday=False)
            try:
                # client.place_order(req) # UNCOMMENT FOR LIVE EXECUTION
                logging.info(f"🚨 SLICE {order_type} {current_slice} for {scrip_code} | {reason}")
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
    pos['ltp'] = ltp
    
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
    if not client: return
    try:
        req_list = [{"Exch": "N", "ExchType": "D", "ScripCode": scrip} for scrip in State.positions.keys()]
        if not req_list: return
        ws = client.ws_client(on_message=on_message)
        ws.connect()
        client.Request_Feed('mf', 's', req_list)
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

def add_to_sim(strike, opt_type, price, is_buy):
    code = f"SIM_{strike}_{opt_type}"
    qty = 50 if is_buy else -50
    if code in State.simulated_cart: State.simulated_cart[code]['qty'] += qty
    else: State.simulated_cart[code] = {'symbol': f"NIFTY {strike} {opt_type}", 'strike': strike, 'opt_type': opt_type, 'entry': price, 'ltp': price, 'qty': qty}
    State.ui_elements['sim_chart'].update_figure(generate_payoff_chart(State.simulated_cart))

def build_ui():
    with ui.header().classes('bg-slate-900 items-center p-4 justify-between'):
        ui.label('📈 Pro Algo Terminal').classes('text-2xl font-bold text-white')
        with ui.row().classes('gap-4 items-center'):
            ui.label().bind_text_from(State, 'realized_pnl', backward=lambda p: f"Booked: ₹{p:.2f}").classes('text-gray-300 font-bold')
            State.ui_elements['total_running_pnl'] = ui.label("Running: ₹0.00").classes('text-yellow-400 font-bold')
            # Clears the session logic securely
            ui.button(icon='logout', color='red', on_click=lambda: [app.storage.user.clear(), ui.navigate.to('/login')]).classes('ml-4 p-2')

    if not State.positions:
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

        with ui.tab_panel(builder_tab):
            with ui.row().classes('w-full gap-4'):
                with ui.column().classes('w-1/2'):
                    ui.label("Mock Option Chain").classes('font-bold text-lg')
                    for strike in [21800, 21900, 22000, 22100, 22200]:
                        with ui.row().classes('w-full border-b p-2 items-center text-center'):
                            ui.button("B", color='green', on_click=lambda s=strike: add_to_sim(s, 'CE', 120, True)).classes('w-1/6 h-6')
                            ui.button("S", color='red', on_click=lambda s=strike: add_to_sim(s, 'CE', 118, False)).classes('w-1/6 h-6')
                            ui.label(str(strike)).classes('w-2/6 font-bold')
                            ui.button("S", color='red', on_click=lambda s=strike: add_to_sim(s, 'PE', 95, False)).classes('w-1/6 h-6')
                            ui.button("B", color='green', on_click=lambda s=strike: add_to_sim(s, 'PE', 98, True)).classes('w-1/6 h-6')
                
                with ui.column().classes('w-5/12'):
                    ui.label("Simulation Payoff").classes('font-bold text-lg')
                    State.ui_elements['sim_chart'] = ui.plotly(generate_payoff_chart({})).classes('w-full')
                    ui.button("Clear Cart", color='gray', on_click=lambda: [State.simulated_cart.clear(), State.ui_elements['sim_chart'].update_figure(generate_payoff_chart({}))]).classes('w-full mt-2')

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
# 6. APPLICATION ROUTING
# ==========================================
@ui.page('/')
def main_page():
    if not is_logged_in():
        ui.navigate.to('/login')
        return

    is_connected = initialize_client()
    if not is_connected:
        app.storage.user.pop('ACCESS_TOKEN', None) # Clear invalid token
        ui.navigate.to('/login')
        return
    
    build_ui()
    
    if "ws_started" not in app.storage.user:
        app.storage.user["ws_started"] = True
        threading.Thread(target=ws_worker, name="5paisa_ws_thread", daemon=True).start()
    
    ui.timer(0.5, update_ui_loop)

if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.getenv("PORT", 8080))
    # CRITICAL: storage_secret is mandatory when using app.storage.user
    ui.run(host="0.0.0.0", port=port, reload=False, title="5paisa Pro Terminal", storage_secret="5paisa_algo_secret_key_123")