import os
import json
import requests
import urllib.parse
from datetime import datetime
from nicegui import ui

# ==========================================
# 1. FILE-BASED SESSION MANAGEMENT
# ==========================================
SESSION_FILE = "session.json"

def load_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_session(data):
    with open(SESSION_FILE, 'w') as f:
        json.dump(data, f)

def is_token_expired(session):
    login_time = session.get("LOGIN_TIME")
    if not login_time:
        return True
    try:
        return datetime.now().date() != datetime.fromisoformat(login_time).date()
    except:
        return True

# --- 5PAISA AUTH ---
def get_5paisa_access_token(request_token, session_data):
    url = "https://Openapi.5paisa.com/VendorsAPI/Service1.svc/GetAccessToken"
    headers = {"Content-Type": "application/json"}
    payload = {
        "head": {"Key": session_data.get('API_KEY')},
        "body": {
            "RequestToken": request_token,
            "EncryKey": session_data.get('ENCRYPTION_KEY'),
            "UserId": session_data.get('USER_ID')
        }
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        try:
            return response.json()
        except ValueError:
            return {"body": {"Status": -1, "Message": f"Invalid data (HTTP {response.status_code})."}}
    except Exception as e:
        return {"body": {"Status": -1, "Message": f"Connection Error: {str(e)}"}}

# --- UPSTOX AUTH ---
def get_upstox_access_token(code, session_data):
    url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'code': code,
        'client_id': session_data.get('UPSTOX_API_KEY'),
        'client_secret': session_data.get('UPSTOX_API_SECRET'),
        'redirect_uri': session_data.get('UPSTOX_REDIRECT_URI'),
        'grant_type': 'authorization_code'
    }
    try:
        response = requests.post(url, headers=headers, data=data)
        return response.json()
    except Exception as e:
        return {"errors": [{"message": f"Connection Error: {str(e)}"}]}

# ==========================================
# 2. AUTHENTICATION WEB PAGES
# ==========================================
@ui.page('/login')
def login_page():
    session = load_session()
    
    # Bypass if mock mode or active token exists
    if session.get('MOCK_MODE') or session.get('ACTIVE_BROKER') == 'KOTAK':
        ui.navigate.to('/')
        return
    if session.get('ACTIVE_BROKER') in ['5paisa', 'UPSTOX'] and session.get('ACCESS_TOKEN') and not is_token_expired(session):
        ui.navigate.to('/')
        return

    with ui.card().classes('absolute-center w-full max-w-md p-8 shadow-2xl rounded-xl border border-gray-200'):
        ui.label('Select Your Broker').classes('text-2xl font-bold mb-6 text-center w-full')

        with ui.tabs().classes('w-full') as tabs:
            tab_5paisa = ui.tab('5paisa')
            tab_upstox = ui.tab('Upstox')
            tab_kotak = ui.tab('Kotak')

        with ui.tab_panels(tabs, value=tab_upstox).classes('w-full mt-4 bg-transparent p-0'):
            
            # --- UPSTOX LOGIN TAB ---
            with ui.tab_panel(tab_upstox):
                ui.label('Upstox API v2 Credentials').classes('font-bold mb-2 text-gray-700')
                u_key = ui.input('API Key (Client ID)', value=session.get('UPSTOX_API_KEY', '')).classes('w-full mb-2')
                u_secret = ui.input('API Secret', value=session.get('UPSTOX_API_SECRET', '')).classes('w-full mb-2').props('type=password')
                u_redirect = ui.input('Redirect URI', value=session.get('UPSTOX_REDIRECT_URI', 'http://127.0.0.1:8080/callback')).classes('w-full mb-6 text-xs')

                def initiate_upstox_oauth():
                    ak, ek, r_uri = (u_key.value or '').strip(), (u_secret.value or '').strip(), (u_redirect.value or '').strip()
                    session.update({'ACTIVE_BROKER': 'UPSTOX', 'UPSTOX_API_KEY': ak, 'UPSTOX_API_SECRET': ek, 'UPSTOX_REDIRECT_URI': r_uri})
                    save_session(session)
                    
                    safe_callback = urllib.parse.quote(r_uri, safe='')
                    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={ak}&redirect_uri={safe_callback}"
                    ui.navigate.to(auth_url)

                ui.button('Login via Upstox', on_click=initiate_upstox_oauth).classes('w-full h-12 text-lg font-bold bg-purple-600 text-white rounded')

            # --- 5PAISA LOGIN TAB ---
            with ui.tab_panel(tab_5paisa):
                ui.label('5paisa Credentials').classes('font-bold mb-2 text-gray-700')
                api_key = ui.input('API Key', value=session.get('API_KEY', '')).classes('w-full mb-2')
                encry_key = ui.input('Encryption Key', value=session.get('ENCRYPTION_KEY', '')).classes('w-full mb-2').props('type=password')
                user_id = ui.input('User ID', value=session.get('USER_ID', '')).classes('w-full mb-2')
                app_source = ui.input('App Source', value=session.get('APP_SOURCE', '')).classes('w-full mb-2')
                user_password = ui.input('User Password', value=session.get('USER_PASSWORD', '')).classes('w-full mb-6').props('type=password')

                def initiate_5paisa_oauth():
                    session.update({
                        'ACTIVE_BROKER': '5paisa',
                        'API_KEY': (api_key.value or '').strip(), 'ENCRYPTION_KEY': (encry_key.value or '').strip(),
                        'USER_ID': (user_id.value or '').strip(), 'APP_SOURCE': (app_source.value or '').strip(),
                        'USER_PASSWORD': (user_password.value or '').strip()
                    })
                    save_session(session)
                    cb_url = os.getenv("REDIRECT_URL", "http://127.0.0.1:8080/callback") 
                    ui.navigate.to(f"https://dev-openapi.5paisa.com/WebVendorLogin/VLogin/Index?VendorKey={api_key.value.strip()}&ResponseURL={urllib.parse.quote(cb_url, safe='')}")

                ui.button('Login via 5paisa', on_click=initiate_5paisa_oauth).classes('w-full h-12 text-lg font-bold bg-blue-600 text-white rounded')

            # --- KOTAK LOGIN TAB ---
            with ui.tab_panel(tab_kotak):
                ui.label('Kotak Neo Credentials').classes('font-bold mb-2 text-gray-700')
                k_consumer = ui.input('Consumer Key').classes('w-full mb-2')
                k_secret = ui.input('Consumer Secret').classes('w-full mb-2').props('type=password')
                k_mobile = ui.input('Mobile Number').classes('w-full mb-2')
                k_pass = ui.input('Password').props('type=password').classes('w-full mb-2')
                k_mpin = ui.input('MPIN').props('type=password').classes('w-full mb-6')
                
                def kotak_login():
                    session.update({
                        'ACTIVE_BROKER': 'KOTAK', 'KOTAK_CONSUMER_KEY': (k_consumer.value or '').strip(),
                        'KOTAK_CONSUMER_SECRET': (k_secret.value or '').strip(), 'KOTAK_MOBILE': (k_mobile.value or '').strip(),
                        'KOTAK_PASSWORD': (k_pass.value or '').strip(), 'KOTAK_MPIN': (k_mpin.value or '').strip()
                    })
                    save_session(session)
                    ui.navigate.to('/')
                    
                ui.button('Login via Kotak', on_click=kotak_login).classes('w-full h-12 text-lg font-bold bg-red-600 text-white rounded')

@ui.page('/callback')
def callback_page(RequestToken: str = None, code: str = None):
    if not RequestToken and not code:
        with ui.card().classes('absolute-center p-6'):
            ui.label("❌ Authentication Failed: No OAuth token received.").classes('text-red-500 font-bold')
            ui.button("Try Again", on_click=lambda: ui.navigate.to('/login')).classes('mt-4')
        return

    try:
        session = load_session()
        active_broker = session.get('ACTIVE_BROKER')
        is_success = False
        error_msg = 'Unknown API Error.'
        
        # Route exchange logic based on broker
        if active_broker == '5paisa' and RequestToken:
            res = get_5paisa_access_token(RequestToken, session)
            if res.get('body', {}).get('Status') == 0:
                is_success = True
                session['ACCESS_TOKEN'] = res['body'].get('AccessToken')
                session['CLIENT_CODE'] = res['body'].get('ClientCode')
            else:
                error_msg = res.get('body', {}).get('Message', error_msg)
                
        elif active_broker == 'UPSTOX' and code:
            res = get_upstox_access_token(code, session)
            if 'access_token' in res:
                is_success = True
                session['ACCESS_TOKEN'] = res.get('access_token')
                session['CLIENT_CODE'] = 'UPSTOX_USER'
            else:
                error_msg = res.get('errors', [{}])[0].get('message', 'Upstox Auth Failed')

        if is_success:
            session['LOGIN_TIME'] = datetime.now().isoformat()
            save_session(session)
            ui.navigate.to('/')
        else:
            with ui.card().classes('absolute-center w-full max-w-sm p-6 text-center'):
                ui.label(f"{active_broker} Exchange Failed!").classes('text-red-500 font-bold text-lg')
                ui.label(error_msg).classes('text-gray-700 mt-2 text-xs')
                ui.button("Proceed with Sample Data", color="orange", on_click=lambda: [session.update({'MOCK_MODE': True}), save_session(session), ui.navigate.to('/')]).classes('mt-6 w-full font-bold')
                ui.button("Try Again", color="gray", on_click=lambda: ui.navigate.to('/login')).classes('mt-2 w-full')
    except Exception as e:
        ui.label(f"Python Error during authentication: {e}")
