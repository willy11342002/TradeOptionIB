"""
真實 ThetaData 選擇權鏈：讀 `scripts/backfill_thetadata.py` 回補到本機的 EOD 買賣報價
(`pref/backtest/thetadata/<ticker>/YYYY-MM.parquet`)，取代舊版 Black-Scholes + VIX 的合成定價。
只有本機已經回補過的標的能回測，`spec.available_tickers()` 是這個限制的入口，這支檔案本身不做
「沒有資料就退回合成價」的 fallback——見 `spec.py` 開頭「價格資料」說明。

*** 這支檔案 import 了 polars，只能在使用者按下「執行回測」之後才 import(見 spec.py 開頭
的說明，跟 market_data.py 同樣理由) ***

核心概念：
- 買賣中價(mid) 是唯一的「公平價值」來源，進場信用、收盤出場都用它——`close` 欄位是當天最後一筆
  *成交價*，流動性差的合約(這幾種賣方策略的短腳/長腳常常是)可能是很早以前的成交，不能當報價用
  (這個坑 `scripts/backfill_thetadata.py` 開頭就寫了)。
- `open`/`high`/`low`/`close` 只在「盤中觸價」出場模式，用來判斷這支選擇權當天實際成交價有沒有
  越過門檻——只在 `volume > 0`(當天真的有成交)時才有意義；`volume == 0`(遠價外選擇權常見，完全
  沒有成交)時 ThetaData 這四欄全部回 0，`Quote.traded` 標記這種情況，呼叫端(`engine.py`)退回用
  買賣中價當作全天唯一的參考點(沒有真的盤中路徑資訊可用)。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import polars as pl

from app.models.backtest import cloud_store
from app.models.backtest.spec import THETADATA_DIR

RIGHT_OF_SIDE = {"put": "PUT", "call": "CALL"}


def _month_files(ticker: str, start: date, end: date) -> List[str]:
    """涵蓋 [start, end] 的月檔案路徑，只列出實際存在的檔案——本機沒有先試著從 R2 補(見
    `cloud_store.py`，沒設定雲端鏡像就整段跳過)，還是沒有就直接跳過、不當錯誤(可能還沒回補到那個
    月，或免費帳號抓不到更早的資料，見 backfill_thetadata.py 的 FREE_TIER_FIRST_DATE)。"""
    files = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        p = THETADATA_DIR / ticker / f"{year:04d}-{month:02d}.parquet"
        if not p.exists() and cloud_store.enabled():
            cloud_store.download(f"{ticker}/{year:04d}-{month:02d}.parquet", p)
        if p.exists():
            files.append(str(p))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return files


def _mid(bid: float, ask: float) -> Optional[float]:
    """買賣中價。bid=ask=0 代表這天這個履約價完全沒有報價(不是真的掛牌組合，或資料缺漏)，回傳
    None 讓呼叫端知道這個履約價不能用，不要誤當成免費的 0 元選擇權。"""
    if bid <= 0 and ask <= 0:
        return None
    return (bid + ask) / 2.0


@dataclass(frozen=True)
class Quote:
    """一天、一個履約價/到期日/買賣權的報價。"""
    date: date
    mid: float     # 買賣中價，任何時候的「公平價值」都用這個(entry_credit、收盤出場)
    open: float    # 當天實際成交價；volume=0 時全部是 0，不能單獨當報價用，見 `traded`
    high: float
    low: float
    close: float
    bid: float
    ask: float
    volume: int

    @property
    def traded(self) -> bool:
        """這天這支選擇權有沒有真的成交過——沒有的話 open/high/low/close 沒有意義(見模組開頭說明)。"""
        return self.volume > 0 and self.close > 0


def _row_to_quote(row: dict) -> Optional[Quote]:
    m = _mid(row["bid"], row["ask"])
    if m is None:
        return None
    return Quote(
        date=row["date"], mid=m, open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        bid=row["bid"], ask=row["ask"], volume=row["volume"],
    )


class OptionChain:
    """單一標的、一段期間的完整選擇權鏈。整段期間一次讀進記憶體(polars，欄位式操作快)，`engine.py`
    的回測主迴圈逐日呼叫 `expirations_on`/`strikes_on` 找進場履約價，開倉當下用 `contract_series`
    把那個履約價「接下來整段持有期間」的報價一次撈出來，存在 `_Spread` 上，之後逐日出場判斷直接查
    這個小表，不用每天重新過濾一次整個資料集(效能考量，實測 SPY 39 個月/807 萬列，載入+分區約 1
    秒，單次履約價查詢 <10ms)。"""

    def __init__(self, ticker: str, start: date, end: date):
        files = _month_files(ticker, start, end)
        if not files:
            raise ValueError(
                f"{ticker} 在 {start}~{end} 完全沒有本機 ThetaData 資料，"
                f"先跑 `uv run python scripts/backfill_thetadata.py --symbol {ticker}` 回補"
            )
        df = pl.concat([pl.scan_parquet(f) for f in files]).filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        ).collect()
        if df.is_empty():
            raise ValueError(f"{ticker} 在 {start}~{end} 沒有資料，檢查日期範圍是否在已回補的區間內")
        self._df = df.sort(["date", "expiration", "strike", "right"])
        self.trading_dates: List[date] = sorted(self._df["date"].unique().to_list())
        # 逐日分區：進場時「今天有哪些到期日/履約價」的查詢很頻繁(每個空手的交易日都要掃一次)，
        # 先切好比每次對全量資料做 filter 快得多(partition_by 一次性成本 <1 秒，換來單次查詢 <1ms)。
        self._by_day: Dict[date, pl.DataFrame] = {
            (k[0] if isinstance(k, tuple) else k): v
            for k, v in self._df.partition_by("date", as_dict=True, include_key=True).items()
        }

    def expirations_on(self, d: date) -> List[date]:
        day = self._by_day.get(d)
        if day is None:
            return []
        return sorted(day["expiration"].unique().to_list())

    def nearest_expiration(self, d: date, target_dte: int) -> Optional[date]:
        """該天實際掛牌的到期日裡，剩餘天數最接近 target_dte 的一個；找不到任何到期日回傳 None。"""
        exps = [e for e in self.expirations_on(d) if e > d]
        if not exps:
            return None
        return min(exps, key=lambda e: abs((e - d).days - target_dte))

    def strikes_on(self, d: date, expiration: date, side: str) -> List[Tuple[float, Quote]]:
        """該天、該到期日、該買賣權，由小到大列出所有「有報價」的履約價。沒有報價(bid=ask=0)的
        履約價直接濾掉，不會出現在結果裡。"""
        day = self._by_day.get(d)
        if day is None:
            return []
        sub = day.filter((pl.col("expiration") == expiration) & (pl.col("right") == RIGHT_OF_SIDE[side])).sort("strike")
        out = []
        for row in sub.iter_rows(named=True):
            q = _row_to_quote(row)
            if q is not None:
                out.append((row["strike"], q))
        return out

    def contract_series(
        self, expiration: date, strike: float, side: str, from_date: date, dates: List[date],
    ) -> Dict[date, Quote]:
        """一個履約價/到期日/買賣權，從 from_date 到到期日(含)的完整每日報價，key 是日期。`dates`
        是這次回測全程走訪的交易日清單(遞增排序)，用來補齊 ThetaData 本身的資料缺口——理論上每個
        交易日都該有報價(整條鏈是完整快照，不是只記錄有成交的)，萬一真的缺(回補中斷留下的洞)就沿
        用最近一次的報價，讓持倉期間不會有一天完全查不到值而讓回測直接掛掉。"""
        sub = self._df.filter(
            (pl.col("expiration") == expiration) & (pl.col("strike") == strike)
            & (pl.col("right") == RIGHT_OF_SIDE[side]) & (pl.col("date") >= from_date) & (pl.col("date") <= expiration)
        )
        quotes: Dict[date, Quote] = {}
        for row in sub.iter_rows(named=True):
            q = _row_to_quote(row)
            if q is not None:
                quotes[row["date"]] = q

        start = bisect.bisect_left(dates, from_date)
        end = bisect.bisect_right(dates, expiration)
        filled: Dict[date, Quote] = {}
        last: Optional[Quote] = None
        for d in dates[start:end]:
            if d in quotes:
                last = quotes[d]
            if last is not None:
                filled[d] = last
        return filled
