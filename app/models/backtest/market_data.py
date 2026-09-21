"""
回測需要的標的每日開高低收，用 yfinance 抓，快取在 `pref/backtest/cache/`(整個 `pref/` 已 gitignore)。

選擇權本身的價格改用真實 ThetaData 報價(`option_chain.py`)，這支檔案只負責「現價 S」這一件事——
`engine.py` 選履約價的距現價百分比條件、保證金估算都需要它，ThetaData 的 EOD 選擇權快照本身不含
正股/ETF 的價格。

*** 這支檔案 import 了 pandas/yfinance，只能在使用者按下「執行回測」之後才 import(見 spec.py 開頭) ***
"""
import time
from datetime import date

import pandas as pd

from app.paths import PREF_DIR

CACHE_DIR = PREF_DIR / "backtest" / "cache"


def _download_with_retry(ticker: str, start: str, end: str, attempts: int = 4) -> pd.DataFrame:
    """yfinance 打的是 Yahoo 沒公開文件的內部 API，連續打很容易被暫時限流、回傳空結果(錯誤訊息會誤
    導地講成「possibly delisted」)，重試幾次通常就過了。"""
    import yfinance as yf

    for i in range(attempts):
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if not df.empty:
            return df
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"yfinance 重試 {attempts} 次仍抓不到 {ticker} 在 {start}~{end} 的資料，檢查代號或日期範圍，或稍後再試")


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    col = df[name]
    if isinstance(col, pd.DataFrame):  # 有些 yfinance 版本單一標的也會回 MultiIndex 欄位
        col = col.iloc[:, 0]
    return col


def load_market_data(ticker: str, start: str, end: str) -> pd.DataFrame:
    """回傳以日期為 index 的 DataFrame，欄位 open/high/low/close(標的當天開高低收)。快取用檔案，
    結束日期是今天或未來的話不沿用(可能還沒收盤/還沒更新)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{ticker}_{start}_{end}.csv"
    if cache_path.exists() and date.fromisoformat(end) < date.today():
        return pd.read_csv(cache_path, index_col=0, parse_dates=True).dropna().sort_index()

    df = _download_with_retry(ticker, start, end)
    out = pd.DataFrame(index=df.index)
    for src, dst in (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close")):
        out[dst] = _column(df, src)
    out.to_csv(cache_path)
    return out.dropna().sort_index()
