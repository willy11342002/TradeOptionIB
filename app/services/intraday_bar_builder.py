"""
把「1分鐘K」二次聚合成使用者選的週期(1/5/30分/日線)，週期以上一律用整
點/半點對齊——1分鐘K有兩個不同來源，都餵進同一個池子：
1. CapitalTickClient 送出的成交tick(今日回補+之後的即時)，用tick自己帶
   的時間戳記(不是 datetime.now()，回補的歷史tick發生時間都在過去，用
   now()分bucket會整批歸進「現在這一秒」，白白浪費回補資料)先疊成1分鐘K
   (見 on_tick)。
2. CapitalKLineClient 查回來的歷史1分鐘K，直接餵進來(見 load_minute_bars)。

*** 為什麼歷史也要拆成1分鐘K自己重疊，不能直接跟伺服器要5分/30分K ***
實測過(2026-09-09)：伺服器給的多分鐘K是照「各商品自己的開盤時間」對齊，
不是照整點/半點對齊——TX00期貨08:45開盤，30分K是09:15/09:45/10:15...；
TSEA現貨09:00開盤，30分K是09:30/10:00/10:30...，兩條線疊在同一張圖上
會對不齊(TX00的每根K棒都比TSEA的鄰居早15分鐘結束)。唯一解法是兩邊都只
跟伺服器要1分鐘K(1分鐘沒有這個對齊問題)，5分/30分完全自己在本地用「以
0分/30分為界」的固定規則聚合，不管是哪個商品、幾點開盤，聚合出來的
bucket邊界永遠一致，兩條線才能疊得上。

歷史(load_minute_bars)跟今天(on_tick)的1分鐘K放在同一個池子裡，用同一套
對齊邏輯聚合——這樣「歷史裡已經結束的盤」跟「今天tick組的、還在進行中的
盤」自然銜接，不會因為來源不同而彼此錯開。

週線/月線不用這個——這週/這月還沒走完的即時組棒，對短線選擇權交易沒什麼
即時盯盤的意義，一律用 CapitalKLineClient 查回來的最後一根完整歷史棒。
日線因為邊界是「日期」不是「時分」，不受這個對齊問題影響，也不透過這支
處理歷史，只有「今天」這一天用這支的day-bucket(period=1440)即時組棒，
維持原本「歷史(完整日K) + 今天(tick組)事後合併」的做法(見opening_tab.py)。
"""
import datetime
from typing import Dict, Optional

from PyQt5.QtCore import QObject, pyqtSignal

DAY_PERIOD_MINUTES = 1440


class MinuteBarAggregator(QObject):
    # 目前週期下，「今天」完整的bars清單(依時間排序，跟歷史(到昨天為止)
    # 串在一起就是完整的圖表資料)。每次有新tick或換週期都整批重送。
    bars_changed = pyqtSignal(list)

    def __init__(self, period_minutes: int = 1):
        super().__init__()
        self._period = period_minutes
        # key是那一分鐘的起點(datetime，秒以下歸零)，這份存的是「1分鐘K」，
        # 跟使用者選的週期無關，是聚合的最細粒度。
        self._minute_bars: Dict[datetime.datetime, dict] = {}

    def set_period(self, period_minutes: int) -> None:
        """換週期不清掉已經收到的1分鐘K——只是換一種疊法重新算，不然使用
        者切個週期，剛剛回補到的「開盤到現在」就白費了。"""
        self._period = period_minutes
        self._emit_aggregated()

    def reset(self) -> None:
        """真的要整批丟棄重來的情況才呼叫這個(例如切換盤別導致「今天」的
        資料集合本身就不一樣了——tick不分盤別，沒辦法只挑合乎新盤別的部
        分保留，只能整批清掉重來，等新的歷史查詢+新tick重新填)。"""
        self._minute_bars.clear()
        self._emit_aggregated()

    def load_minute_bars(self, bars: list) -> None:
        """直接餵入已經是完整的1分鐘K(來自CapitalKLineClient歷史查詢，
        sMinuteNumber=1)，跟on_tick累加的(今天即時/回補的)1分鐘K存進同一
        個池子，用同一套對齊邏輯一起參與二次聚合——見檔案開頭「為什麼歷
        史也要拆成1分鐘K自己重疊」。呼叫端負責保證餵進來的bar本身就是
        1分鐘K(不是5分/30分)，這裡不檢查。"""
        for bar in bars:
            minute_start = _parse_minute_start(bar)
            if minute_start is None:
                continue
            self._minute_bars[minute_start] = {
                "open": bar["open"], "high": bar["high"], "low": bar["low"],
                "close": bar["close"], "volume": bar["volume"],
            }
        self._emit_aggregated()

    def on_tick(self, dt: datetime.datetime, price: Optional[float], qty: Optional[float]) -> None:
        if not price or price <= 0 or not qty:
            return
        minute_start = dt.replace(second=0, microsecond=0)
        bar = self._minute_bars.get(minute_start)
        if bar is None:
            self._minute_bars[minute_start] = {
                "open": price, "high": price, "low": price, "close": price, "volume": qty,
            }
        else:
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += qty
        self._emit_aggregated()

    def _emit_aggregated(self) -> None:
        buckets: Dict[datetime.datetime, dict] = {}
        for minute_start in sorted(self._minute_bars):
            minute_bar = self._minute_bars[minute_start]
            bucket_start = self._bucket_start_for(minute_start)
            bucket = buckets.get(bucket_start)
            if bucket is None:
                buckets[bucket_start] = {
                    "date": self._label_for(bucket_start),
                    "calendar_date": bucket_start.strftime("%Y-%m-%d"),
                    "time": None if self._period >= DAY_PERIOD_MINUTES else bucket_start.strftime("%H:%M"),
                    "open": minute_bar["open"],
                    "high": minute_bar["high"],
                    "low": minute_bar["low"],
                    "close": minute_bar["close"],
                    "volume": minute_bar["volume"],
                }
            else:
                bucket["high"] = max(bucket["high"], minute_bar["high"])
                bucket["low"] = min(bucket["low"], minute_bar["low"])
                bucket["close"] = minute_bar["close"]
                bucket["volume"] += minute_bar["volume"]
        self.bars_changed.emit([buckets[key] for key in sorted(buckets)])

    def _bucket_start_for(self, minute_start: datetime.datetime) -> datetime.datetime:
        midnight = minute_start.replace(hour=0, minute=0)
        if self._period >= DAY_PERIOD_MINUTES:
            return midnight
        minutes_since_midnight = minute_start.hour * 60 + minute_start.minute
        bucket_minute = (minutes_since_midnight // self._period) * self._period
        return midnight + datetime.timedelta(minutes=bucket_minute)

    def _label_for(self, bucket_start: datetime.datetime) -> str:
        if self._period >= DAY_PERIOD_MINUTES:
            return bucket_start.strftime("%Y-%m-%d")
        return bucket_start.strftime("%Y-%m-%d %H:%M")


def _parse_minute_start(bar: dict) -> Optional[datetime.datetime]:
    """從 capital_kline_client._parse_bar 的輸出(calendar_date="YYYY-MM-DD",
    time="HH:MM")組回 datetime，餵給 load_minute_bars 用。"""
    calendar_date = bar.get("calendar_date")
    time_str = bar.get("time")
    if not calendar_date or not time_str:
        return None
    try:
        year, month, day = (int(x) for x in calendar_date.split("-"))
        hour, minute = (int(x) for x in time_str.split(":"))
        return datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        return None
