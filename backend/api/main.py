# backend/api/main.py
from fastapi import FastAPI, WebSocket, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import asyncio
from typing import Dict, List

app = FastAPI(title="Auto Trading Platform API")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 프로덕션에서는 특정 도메인만 허용
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 기존 모듈 재사용
from trade_pro.auto_trader import AutoTrader
from risk_management.risk_dashboard import RiskDashboard
from core.detail_information_getter import DetailInformationGetter

# 전역 인스턴스 관리
class TradingService:
    def __init__(self):
        self.traders: Dict[str, AutoTrader] = {}  # user_id별 trader 인스턴스
        self.active_connections: Dict[str, List[WebSocket]] = {}
    
    def get_trader(self, user_id: str) -> AutoTrader:
        if user_id not in self.traders:
            self.traders[user_id] = AutoTrader()
        return self.traders[user_id]

trading_service = TradingService()

# WebSocket 연결 (실시간 데이터)
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    
    if user_id not in trading_service.active_connections:
        trading_service.active_connections[user_id] = []
    trading_service.active_connections[user_id].append(websocket)
    
    try:
        while True:
            # 실시간 종목 데이터 전송
            data = await websocket.receive_text()
            # 처리 로직
            await websocket.send_json({
                "type": "stock_update",
                "data": {...}
            })
    except Exception as e:
        trading_service.active_connections[user_id].remove(websocket)

# REST API 엔드포인트
@app.get("/api/v1/candidates")
async def get_candidates(user_id: str = Depends(get_current_user)):
    """후보 종목 목록 조회"""
    import pandas as pd
    df = pd.read_csv("candidate_stocks.csv")
    return df.to_dict(orient="records")

@app.post("/api/v1/conditions/{condition_id}/start")
async def start_condition(
    condition_id: str,
    user_id: str = Depends(get_current_user)
):
    """조건식 시작"""
    trader = trading_service.get_trader(user_id)
    # 기존 engine 로직 재사용
    trader.engine.send_condition_search_request(condition_id)
    return {"status": "started", "condition_id": condition_id}

@app.get("/api/v1/risk/dashboard")
async def get_risk_dashboard(user_id: str = Depends(get_current_user)):
    """리스크 대시보드 데이터"""
    store = TradingResultStore(f"data/trading_results_{user_id}.json")
    snapshot = store.get_latest_snapshot()
    return snapshot

@app.post("/api/v1/trade")
async def execute_trade(
    symbol: str,
    side: str,
    quantity: int,
    user_id: str = Depends(get_current_user)
):
    """매매 주문 실행"""
    trader = trading_service.get_trader(user_id)
    result = trader.execute_order(symbol, side, quantity)
    
    # WebSocket으로 실시간 알림
    await broadcast_to_user(user_id, {
        "type": "trade_executed",
        "data": result
    })
    return result

# 실시간 브로드캐스트 헬퍼
async def broadcast_to_user(user_id: str, message: dict):
    connections = trading_service.active_connections.get(user_id, [])
    for ws in connections:
        try:
            await ws.send_json(message)
        except:
            connections.remove(ws)