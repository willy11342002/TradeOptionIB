"""
回測需要的兩條歷史序列，用 yfinance 抓，快取在 `pref/backtest/cache/`(整個 `pref/` 已 gitignore)：
  - 標的每日開高低收(如 SPY)
  - 對應標的的隱含波動率指數每日收盤(VIX/VXN/RVX)，當作市場實際隱含波動率的代理

不用歷史已實現波動率反推，是因為那樣會把賣方長期的風險溢酬(VRP)直接定義成零；用真實隱含波動率
才能讓合成價格反映真實存在的溢酬。波動率指數一定要對應到標的本身(見 spec.SUPPORTED_TICKERS)，不
能整個回測都套 VIX。

*** 這支檔案 import 了 pandas/yfinance，只能在使用者按下「執行回測」之後才 import(見 spec.py 開頭) ***
"""
import time
from datetime import date

import pandas as pd

from app.models.backtest.spec import SUPPORTED_TICKERS
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


def _cached_ohlc(ticker: str, start: str, end: str) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{ticker.replace('^', '_')}_{start}_{end}.csv"
    # 結束日期是今天或未來的話，快取的資料可能不完整(還沒收盤/還沒更新)，不能沿用。
    if cache_path.exists() and date.fromisoformat(end) < date.today():
        return pd.read_csv(cache_path, index_col=0, parse_dates=True)

    df = _download_with_retry(ticker, start, end)
    out = pd.DataFrame(index=df.index)
    for src, dst in (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close")):
        out[dst] = _column(df, src)
    out.to_csv(cache_path)
    return out


def load_market_data(ticker: str, start: str, end: str) -> pd.DataFrame:
    """回傳以日期為 index 的 DataFrame，欄位 open/high/low/close(標的當天開高低收)、vix(當天隱含
    波動率指數收盤，20.0 代表 20%)。沒有盤中波動率資料，當天波動率視為常數(已知簡化)。"""
    vol_index = SUPPORTED_TICKERS.get(ticker)
    if vol_index is None:
        raise ValueError(f"{ticker} 沒有對應的波動率指數，目前只支援：{'、'.join(SUPPORTED_TICKERS)}")

    underlying = _cached_ohlc(ticker, start, end)
    vol = _cached_ohlc(vol_index, start, end)["close"]

    out = underlying.copy()
    out["vix"] = vol
    return out.dropna().sort_index()
