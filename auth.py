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

def get_access_token(request_token, session_data):
    """Fetches the 5paisa daily Access Token."""
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
            error_preview = response.text[:300] if response.text else "Empty response"
            return {"body": {"Status": -1, "Message": f"Broker returned invalid data (HTTP {response.status_code}). Raw: {error_preview}"}}
    except Exception as e:
        return {"body": {"Status": -1, "Message": f"Network Connection Error: {str(e)}"}}

# ==========================================
# 2. AUTHENTICATION WEB PAGES
# ==========================================
@ui.page('/login')
def login_page():
    session = load_session()
    
    # Check if already logged in via Mock, Kotak, or a fresh 5paisa token
    if session.get('MOCK_MODE'):
        ui.navigate.to('/')
        return
    if session.get('ACTIVE_BROKER') == 'KOTAK':
        ui.navigate.to('/')
        return
    if session.get('ACTIVE_BROKER') == '5paisa' and session.get('ACCESS_TOKEN') and not is_token_expired(session):
        ui.navigate.to('/')
        return

    with ui.card().classes('absolute-center w-full max-w-md p-8 shadow-2xl rounded-xl border border-gray-200'):
        ui.label('Select Your Broker').classes('text-2xl font-bold mb-6 text-center w-full')

        with ui.tabs().classes('w-full') as tabs:
            tab_5paisa = ui.tab('5paisa')
            tab_kotak = ui.tab('Kotak Neo')

        with ui.tab_panels(tabs, value=tab_5paisa).classes('w-full mt-4 bg-transparent p-0'):
            # --- 5PAISA LOGIN TAB ---
            with ui.tab_panel(tab_5paisa):
                ui.label('5paisa Credentials').classes('font-bold mb-2 text-gray-700')
                api_key = ui.input('API Key', value=session.get('API_KEY', '')).classes('w-full mb-2')
                encry_key = ui.input('Encryption Key', value=session.get('ENCRYPTION_KEY', '')).classes('w-full mb-2').props('type=password')
                user_id = ui.input('User ID', value=session.get('USER_ID', '')).classes('w-full mb-2')
                app_source = ui.input('App Source', value=session.get('APP_SOURCE', '')).classes('w-full mb-2')
                user_password = ui.input('User Password', value=session.get('USER_PASSWORD', '')).classes('w-full mb-6').props('type=password')

                def initiate_5paisa_oauth():
                    ak = (api_key.value or '').strip()
                    ek = (encry_key.value or '').strip()
                    uid = (user_id.value or '').strip()
                    app_src = (app_source.value or '').strip()
                    pwd = (user_password.value or '').strip()

                    session.update({
                        'ACTIVE_BROKER': '5paisa',
                        'API_KEY': ak, 'ENCRYPTION_KEY': ek, 'USER_ID': uid,
                        'APP_SOURCE': app_src, 'USER_PASSWORD': pwd
                    })
                    save_session(session)
                    
                    callback_url = os.getenv("REDIRECT_URL", "http://140.245.249.255:8080/callback") 
                    safe_callback = urllib.parse.quote(callback_url, safe='')
                    auth_url = f"https://dev-openapi.5paisa.com/WebVendorLogin/VLogin/Index?VendorKey={ak}&ResponseURL={safe_callback}"
                    ui.navigate.to(auth_url)

                ui.button('Login via 5paisa', on_click=initiate_5paisa_oauth).classes('w-full h-12 text-lg font-bold bg-blue-600 text-white rounded')

            # --- KOTAK NEO LOGIN TAB ---
            with ui.tab_panel(tab_kotak):
                ui.label('Kotak Neo Credentials').classes('font-bold mb-2 text-gray-700')
                k_consumer = ui.input('Consumer Key', value=session.get('KOTAK_CONSUMER_KEY', '')).classes('w-full mb-2')
                k_secret = ui.input('Consumer Secret', value=session.get('KOTAK_CONSUMER_SECRET', '')).classes('w-full mb-2').props('type=password')
                k_mobile = ui.input('Mobile Number', value=session.get('KOTAK_MOBILE', '')).classes('w-full mb-2')
                k_pass = ui.input('Password', value=session.get('KOTAK_PASSWORD', '')).props('type=password').classes('w-full mb-2')
                k_mpin = ui.input('MPIN', value=session.get('KOTAK_MPIN', '')).props('type=password').classes('w-full mb-6')
                
                def kotak_login():
                    session.update({
                        'ACTIVE_BROKER': 'KOTAK',
                        'KOTAK_CONSUMER_KEY': (k_consumer.value or '').strip(),
                        'KOTAK_CONSUMER_SECRET': (k_secret.value or '').strip(),
                        'KOTAK_MOBILE': (k_mobile.value or '').strip(),
                        'KOTAK_PASSWORD': (k_pass.value or '').strip(),
                        'KOTAK_MPIN': (k_mpin.value or '').strip()
                    })
                    save_session(session)
                    ui.navigate.to('/') # Kotak verifies connection on the main page, not via redirect
                    
                ui.button('Login via Kotak Neo', on_click=kotak_login).classes('w-full h-12 text-lg font-bold bg-red-600 text-white rounded')

@ui.page('/callback')
def callback_page(RequestToken: str = None):
    if not RequestToken:
        with ui.card().classes('absolute-center p-6'):
            ui.label("❌ Authentication Failed: No Request Token received.").classes('text-red-500 font-bold')
            ui.button("Try Again", on_click=lambda: ui.navigate.to('/login')).classes('mt-4')
        return

    try:
        session = load_session()
        res = get_access_token(RequestToken, session)
        
        body = res.get('body') or {}
        
        if body.get('Status') == 0:
            session['ACCESS_TOKEN'] = body.get('AccessToken')
            session['CLIENT_CODE'] = body.get('ClientCode')
            session['LOGIN_TIME'] = datetime.now().isoformat()
            save_session(session)
            ui.navigate.to('/')
        else:
            error_msg = body.get('Message', 'Unknown 5paisa API Error.')
            with ui.card().classes('absolute-center p-6 text-center'):
                ui.label("Token Exchange Failed!").classes('text-red-500 font-bold text-lg')
                ui.label(error_msg).classes('text-gray-700 mt-2')
                ui.label(f"Raw Output: {res}").classes('text-xs text-gray-400 mt-4 break-words')
                
                def activate_mock():
                    session['MOCK_MODE'] = True
                    save_session(session)
                    ui.navigate.to('/')
                
                ui.button("Proceed with Sample Data", color="orange", on_click=activate_mock).classes('mt-6 w-full font-bold')
                ui.button("Try Again", color="gray", on_click=lambda: ui.navigate.to('/login')).classes('mt-2 w-full')
    except Exception as e:
        ui.label(f"Python Error during authentication: {e}")
