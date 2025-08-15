# core/token_manager.py

import os
import json
import time
import requests
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")

TOKEN_FILE = "access_token.json"
TOKEN_URL = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"  # 실전 계좌용

def get_access_token():
    token_data = load_token_from_file()
    if is_token_valid(token_data):
        return token_data["access_token"]
    return request_new_token()

def is_token_valid(token_data):
    return "access_token" in token_data and time.time() < token_data.get("expires_at", 0)

def load_token_from_file():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_token_to_file(access_token, expires_at):
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "expires_at": expires_at
        }, f)

def request_new_token():
    headers = {"Content-Type": "application/json"}
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }

    response = requests.post(TOKEN_URL, headers=headers, data=json.dumps(payload))
    if response.status_code != 200:
        raise Exception(f"토큰 발급 실패: {response.status_code} - {response.text}")

    res_json = response.json()
    access_token = res_json.get("access_token")
    if not access_token:
        raise Exception(f"access_token 없음: {res_json}")

    expires_at = time.time() + 60 * 60 * 24  # 24시간 유효

    save_token_to_file(access_token, expires_at)
    return access_token
