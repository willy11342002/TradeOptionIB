"""
用 yfinance 抓回測需要的兩條歷史序列，並快取在本機(不進版控)：
  - 標的每日收盤價(如 SPY)
  - VIX 每日收盤(當作市場實際隱含波動率的代理，不是用歷史已實現波動率反推——
    用已實現波動率會把賣方長期的風險溢酬(VRP)直接定義成零，用 VIX 才能讓
    合成回測反映出真實存在的溢酬，見 black_scholes.py 開頭的說明)
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_DIR = Path(__file__).parent / ".cache"


def _download_with_retry(ticker: str, start: str, end: str, attempts: int = 4) -> pd.DataFrame:
    """yfinance 打的是 Yahoo 沒公開文件的內部 API，連續打很容易被暫時限流、
    回傳空結果(錯誤訊息會誤導地講成「possibly delisted」)，重試幾次通常就過了。"""
    last_empty = False
    for i in range(attempts):
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if not df.empty:
            return df
        last_empty = True
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    if last_empty:
        raise RuntimeError(f"yfinance 重試{attempts}次仍抓不到 {ticker} 在 {start}~{end} 的資料，檢查代號或日期範圍，或稍後再試")


def _cached_download(ticker: str, start: str, end: str) -> pd.Series:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = ticker.replace("^", "_")
    cache_path = CACHE_DIR / f"{safe_name}_{start}_{end}.csv"
    if cache_path.exists():
        cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        return cached["close"]

    df = _download_with_retry(ticker, start, end)

    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # 有些 yfinance 版本單一標的也會回 MultiIndex 欄位
        close = close.iloc[:, 0]
    close = close.rename("close")

    close.to_frame().to_csv(cache_path)
    return close


def load_underlying_and_vix(ticker: str, start: str, end: str) -> pd.DataFrame:
    """回傳以日期為 index 的 DataFrame，欄位: close(標的收盤價)、vix(當天VIX，例如20.0代表20%)。"""
    underlying = _cached_download(ticker, start, end)
    vix = _cached_download("^VIX", start, end)

    out = pd.DataFrame({"close": underlying, "vix": vix}).dropna()
    out = out.sort_index()
    return out
