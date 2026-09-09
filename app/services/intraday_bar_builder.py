"""
拿即時報價 tick 自己組「今天還在走的那根K棒」。

CapitalKLineClient(SKQuoteLib_RequestKLineAMByDate)文件開頭寫明「僅提供
歷史資料」，沒有即時推播——今天這根還沒走完的K棒沒辦法跟伺服器要，只能
靠 CapitalQuoteClient.quote_updated 訊號的即時成交價/單量，依照目前時間
落在哪個週期區間裡自己累加 OHLCV。

只處理「分鐘」跟「日」兩種週期。週線/月線「這週/這月還沒走完」的即時組棒
對短線選擇權交易沒什麼意義 (最後一根還沒走完的週K/月K，波動判斷上不如
直接看日K)，這裡不處理，週/月線一律用 CapitalKLineClient 查回來的最後
一根完整歷史棒，不即時跳動。
"""
import datetime
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal

DAY_PERIOD_MINUTES = 1440


class IntradayBarBuilder(QObject):
    # 累積中的那根K棒(dict，欄位跟 capital_kline_client._parse_bar 一致)，
    # 以及這次更新是不是換到了新的一根(代表前一根已經走完、定案了)。
    bar_updated = pyqtSignal(dict, bool)

    def __init__(self, period_minutes: int = 1):
        super().__init__()
        self._period = period_minutes
        self._bucket_start: Optional[datetime.datetime] = None
        self._bar: Optional[dict] = None

    def set_period(self, period_minutes: int) -> None:
        """換週期(例如下拉選單從5分切到30分)要整個重新開始累積，不能沿用
        舊週期正在組的那根棒。"""
        self._period = period_minutes
        self._bucket_start = None
        self._bar = None

    def reset(self) -> None:
        self._bucket_start = None
        self._bar = None

    def on_tick(self, price: Optional[float], tick_qty: Optional[float]) -> None:
        """tick_qty 是這次報價事件裡的「單量」(這一筆成交的量)，None 或 0
        代表這次報價變動沒有伴隨新成交 (例如純粹買賣價變動)，不當一次
        新的成交來源，避免沒有真的成交也把 OHLC 往前推、量也重複累加。"""
        if not price or price <= 0 or not tick_qty:
            return

        now = datetime.datetime.now()
        bucket_start = self._bucket_start_for(now)
        is_new_bar = self._bucket_start != bucket_start

        if is_new_bar:
            self._bucket_start = bucket_start
            self._bar = {
                "date": self._label_for(bucket_start),
                "calendar_date": bucket_start.strftime("%Y-%m-%d"),
                "time": None if self._period >= DAY_PERIOD_MINUTES else bucket_start.strftime("%H:%M"),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": tick_qty,
            }
        else:
            bar = self._bar
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += tick_qty

        self.bar_updated.emit(dict(self._bar), is_new_bar)

    def _bucket_start_for(self, now: datetime.datetime) -> datetime.datetime:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._period >= DAY_PERIOD_MINUTES:
            return midnight
        minutes_since_midnight = now.hour * 60 + now.minute
        bucket_minute = (minutes_since_midnight // self._period) * self._period
        return midnight + datetime.timedelta(minutes=bucket_minute)

    def _label_for(self, bucket_start: datetime.datetime) -> str:
        if self._period >= DAY_PERIOD_MINUTES:
            return bucket_start.strftime("%Y-%m-%d")
        return bucket_start.strftime("%Y-%m-%d %H:%M")
