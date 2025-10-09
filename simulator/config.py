from dataclasses import dataclass

@dataclass
class SimConfig:
    trigger: str = "last"          # 'last' | 'bidask' | 'low'
    partial_fill_ratio: float = 1.0
    slippage_bps: float = 0.0
    slippage_ticks: int = 0
    fee_bps: float = 0.0
    lot_size: int = 1
    allow_cross_improve: bool = True
