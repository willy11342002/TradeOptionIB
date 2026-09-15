"""
用 yfinance 抓日K/週K/月K的完整歷史，給技術分析頁籤第一次查某個標的/週
期(本機還沒有 `historical_bars_store` 快取)時做「最長回補」用。

跟 IB `reqHistoricalDataAsync` 不一樣：Yahoo Finance 對 "1d"/"1wk"/"1mo"
這幾個粒度沒有「一次最多能抓多長」的限制，`period="max"` 一次 HTTP 請求
就把 Yahoo 資料庫存的完整歷史(通常回溯到上市或 Yahoo 涵蓋範圍起點)整段
吐回來，比 IB 那邊只能用猜的大 duration(`30 Y`/`50 Y`，見
`app/views/web_technical_analysis_panel.py::_TIMEFRAMES`)去逼近「最長」
精確，也不吃 IB 的 pacing 配額。

*** 只用在日K/週K/月K，不含分K ***：Yahoo 對分K(1分/5分/30分這幾個粒
度)有更嚴格的回溯上限(1分K只能抓最近7天、5~30分K只能抓最近60天)，都比
IB 現在放寬後能給的少，分K的回補繼續交給 `web_technical_analysis_panel.
py` 原本那條 IB 路徑。

跟 `fundamentals_client.py` 同一個理由：yfinance 是同步、真的會發 HTTP
請求的 library，一定要透過 `run_blocking()` 丟到背景執行緒呼叫，不能直
接在 async 函式裡呼叫。
"""
from __future__ import annotations

import datetime

from app.services.background_tasks import run_blocking

# 技術分析頁籤的 timeframe key -> yfinance interval 字串，只列有支援的
# 三種(分K不支援，見 module docstring)。
_INTERVAL_BY_TIMEFRAME = {"1day": "1d", "1week": "1wk", "1month": "1mo"}


def _fetch_sync(symbol: str, timeframe_key: str) -> list[dict]:
    import yfinance as yf

    interval = _INTERVAL_BY_TIMEFRAME[timeframe_key]
    # auto_adjust=False：只做股票分割調整、不做股利調整，跟 IB
    # reqHistoricalData(whatToShow="TRADES") 的原始成交價口徑一致——
    # auto_adjust=True(yfinance 預設)會連股利一起調整收盤價，之後增量
    # 更新改查 IB 原始成交價時兩段資料會對不起來，圖上會在資料源切換的
    # 那天出現一個不連續的跳空。
    df = yf.Ticker(symbol).history(period="max", interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return []

    bars = []
    for ts, row in df.iterrows():
        x = ts.strftime("%Y-%m-%d")
        bars.append({
            # ts 用重新 parse x 算，不要直接拿 pandas Timestamp 本身的
            # epoch(那顆物件帶交易所時區，算出來的 epoch 跟 IB 那邊
            # `_parse_ts()` 對同一個日期字串算出來的值可能對不上，
            # `_visible_bars()` 用 ts 篩可視範圍會篩錯)。
            "x": x, "ts": datetime.datetime.fromisoformat(x).timestamp(),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row["Volume"]),
        })
    return bars


async def fetch_max_history(symbol: str, timeframe_key: str) -> list[dict]:
    """回傳格式跟技術分析頁籤 `state["bars"]`/`historical_bars_store` 用
    的K棒 dict 一樣(`x`/`ts`/`open`/`high`/`low`/`close`/`volume`)。查失
    敗、查無資料、或這個 timeframe 不支援(不是日/週/月K)一律回傳空清
    單，呼叫端(`web_technical_analysis_panel.py::_refresh()`)退回原本的
    IB 大 duration 回補，不當成整體查詢失敗。"""
    if timeframe_key not in _INTERVAL_BY_TIMEFRAME:
        return []
    try:
        return await run_blocking(_fetch_sync, symbol, timeframe_key)
    except Exception:
        return []
