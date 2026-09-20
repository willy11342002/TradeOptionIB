"""
回測逐筆交易紀錄。一列 = 一個單邊價差(put 邊或 call 邊)從開倉到平倉——整組同時平倉就拆成
兩列，這樣報表只需要處理一種格式。純標準庫(不 import pandas)，策略清單存檔/UI 都能直接用。
"""
from dataclasses import asdict, dataclass


@dataclass
class Trade:
    side: str            # "put" 或 "call"
    entry_date: str      # ISO 日期 YYYY-MM-DD
    exit_date: str
    K_short: float
    K_long: float
    entry_credit: float  # 每股，進場時收到的淨權利金
    exit_value: float    # 每股，平倉時的價差價值(平倉成本)
    pnl: float           # 每股，entry_credit - exit_value
    max_loss: float      # 每股，價差寬度 - entry_credit(拿來估保證金)
    exit_reason: str     # "{leg|group}_{take_profit|stop_loss|dte}"
    fill_mode: str       # "close" 或 "intraday"
    contracts: int
    pnl_usd: float       # pnl * 100 * 口數
    commission_usd: float  # 這一列開倉+平倉共 4 腳次執行的 IBKR 手續費(combo 每腳套用最低收費)
    net_pnl_usd: float   # pnl_usd - commission_usd

    def to_dict(self) -> dict:
        return asdict(self)
