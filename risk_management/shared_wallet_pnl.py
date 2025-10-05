from __future__ import annotations
from PySide6.QtCore import QObject, Signal
from collections import defaultdict
from math import isfinite
import pandas as pd
from typing import Dict


class SharedWalletPnL(QObject):
"""
Shared-cash, per-condition PnL aggregator.
Emits Qt signal `snapshot_ready` with schema compatible to UI.
"""
snapshot_ready = Signal(dict)


def __init__(self, *, base_cash: float = 100_000_000, tz: str = "Asia/Seoul"):
super().__init__()
self.tz = tz
self.base_cash = float(base_cash)


# Shared cash & realized PnL
self.cash = float(base_cash)
self.realized = 0.0
self.last_px: Dict[str, float] = defaultdict(float)


# Per-condition positions & realized
# positions[cond_id][code] = {"qty": float, "avg": float}
self.positions: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"qty": 0.0, "avg": 0.0}))
self.realized_by_cond: Dict[str, float] = defaultdict(float)


# Portfolio curves
self.port_equity_curve = [] # [{t, equity}]
self.port_daily_hist = defaultdict(float) # day -> pnl
self._last_equity = self.base_cash


# Simple exposure limits (can be overridden)
self.limits = {
"portfolio": {"daily_draw_pct": -3.0, "mdd_pct": -10.0, "max_gross_expo_pct": 120.0},
"per_cond": {"daily_draw_pct": -2.0, "mdd_pct": -8.0, "max_gross_expo_pct": 60.0, "max_symbol_pct": 30.0},
}


# ---------- Public API: fills & prices ----------
def on_fill(self, cond_id: str, code: str, side: str, qty: float, price: float, ts=None, fee: float = 0.0):
side = side.upper()
qty = float(qty); price = float(price); fee = float(fee)
pos = self.positions[cond_id][code]


if side == "BUY":
cost = qty * price + fee
if self.cash < cost:
# Reject or log as needed
return
new_qty = pos["qty"] + qty
pos["avg"] = (pos["qty"]*pos["avg"] + qty*price) / new_qty if new_qty != 0 else 0.0
pos["qty"] = new_qty
self.cash -= cost


elif side == "SELL":
if pos["qty"] <= 0:
return
sell_qty = min(qty, pos["qty"])
realized = (price - pos["avg"]) * sell_qty - fee
self.realized += realized
self.realized_by_cond[cond_id] += realized
pos["qty"] -= sell_qty
if pos["qty"] <= 0:
pos["qty"] = 0.0
pos["avg"] = 0.0
self.cash += sell_qty * price


}