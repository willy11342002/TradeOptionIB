"""
群益 SKQuoteLib_RequestKLineAMByDate 歷史K線查詢封裝。

*** 這幾件事都是查文件+實測核對過的，不是猜的 (見 CLAUDE.md 規定) ***

1. 文件《策略王COM元件使用說明_V2.13.59.htm》4-4-24 SKQuoteLib_RequestKLineAMByDate
   開頭就寫明「（僅提供歷史資料）向報價伺服器提出」——這不是即時推播機制，
   是「呼叫一次 -> OnNotifyKLineData 事件陸續回傳一批歷史K棒」的模式，沒有
   「這批資料已經送完」的結束事件/結束標記。今天還在走的那根K棒要另外靠
   即時報價 tick 自己組 (見 app/services/intraday_bar_builder.py)，這支只
   負責歷史回補。

2. 宣告 (SHORT 開頭三個都是數值型參數，不是字串)：
       Long SKQuoteLib_RequestKLineAMByDate(
           BSTR bstrStockNo, SHORT sKLineType, SHORT sOutType,
           SHORT sTradeSession, BSTR bstrStartDate, BSTR bstrEndDate,
           SHORT sMinuteNumber)
   sKLineType: 0=分線 4=日線 5=週線 6=月線
   sOutType:   0=舊版 1=新版 (這裡固定用新版，價格已經是還原小數點後的值，
               不用自己再除 10^n)
   sTradeSession: 僅國內期權有效，0=全盤(含夜盤) 1=AM盤(僅日盤)
   bstrStartDate/bstrEndDate: YYYYMMDD
   sMinuteNumber: sKLineType=0 時才有意義，ex: 1=1分K, 5=5分K, 30=30分K
                  (伺服器端直接組好回傳，不用自己拿1分K疊)

3. 實測過 (2026-09-09，正式環境，TSEA/TX00 都測過)：
   - 新版格式的 OnNotifyKLineData(bstrStockNo, bstrData) 是逗號分隔字串，
     分線: "YYYY/MM/DD HH:MM, 開盤, 最高, 最低, 收盤, 量"
     日/週/月線: "YYYY/MM/DD, 開盤, 最高, 最低, 收盤, 量" (沒有時間欄位)
   - TSEA 日線可以一次查到 2000/01/04 (查了 20000101~今天，一次拿回6593筆，
     沒有踩到文件備註提到的「288天」限制——那個限制看起來只跟訂閱模式的
     RequestKLine/RequestKLineAM 有關，這支沒有踩到)。
   - TX00 是有效的近月期貨代碼；sTradeSession=0 查到的K棒，時間戳記是
     真實發生時間 (例如全盤查詢會多出前一晚夜盤的K棒，日期就是實際的
     前一天，不會被改寫成查詢區間那天)。

*** 重要限制：同一個代碼不能同時有兩筆查詢在飛行中 ***
OnNotifyKLineData(bstrStockNo, bstrData) 事件本身完全不帶「這是哪一次
RequestKLineAMByDate呼叫觸發的」這種關聯資訊，COM事件是廣播給這個
SKQuoteLib物件上所有掛的事件槽，不是只給發出請求的那個呼叫端。如果同一
個代碼(例如"TSEA")同時有兩筆不同用途的查詢在飛(例如互動圖表查5分K、背景
排程查日K)，兩邊收到的事件會混在一起，沒辦法事後分開——所以這裡用
(tag, symbol) 當作查詢的識別鍵，且同一個 symbol 只允許一筆查詢在飛行中，
呼叫端(不管是誰) 在對方還沒收完前對同一個代碼發第二筆查詢，會直接被拒絕
(emit kline_failed)，逼呼叫端自己序列化，而不是安靜地讓資料混在一起壞掉。
"""
import datetime
from typing import Dict, List, Optional, Tuple

import comtypes.client
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.models.capital_client import CapitalClient
from app.models.capital_quote_client import CONN_STOCKS_READY

KLINE_TYPE_MINUTE = 0
KLINE_TYPE_DAY = 4
KLINE_TYPE_WEEK = 5
KLINE_TYPE_MONTH = 6

SESSION_FULL = 0  # 全盤(含夜盤)，僅國內期權有效
SESSION_AM = 1    # AM盤(僅日盤)

_OUT_TYPE_NEW = 1  # 固定用新版格式，價格已還原小數點

# 收到最後一筆 OnNotifyKLineData 後，靜默這麼久沒有新事件進來，視為這次
# 查詢已經收完——文件沒有提供「這批資料送完了」的結束事件，只能用這種
# 方式判斷 (查過文件確認過沒有結束事件，不是漏看)。
_DEBOUNCE_MS = 700
# 保險上限：如果伺服器異常導致事件一直斷斷續續進來，不要無限期等下去。
_MAX_WAIT_MS = 30000

_PendingKey = Tuple[str, str]  # (tag, symbol)


class CapitalKLineClient(QObject):
    kline_ready = pyqtSignal(str, str, list)  # tag, 商品代碼, [bar,...]
    kline_failed = pyqtSignal(str, str, str)  # tag, 商品代碼, 錯誤訊息

    def __init__(self, client: CapitalClient):
        super().__init__()
        self._quote = client.quote
        self._center = client.center
        self._events = _KLineEvents(self)
        self._handler = comtypes.client.GetEvents(self._quote, self._events)

        # RequestKLineAMByDate 跟 RequestStocks 一樣，要等 EnterMonitorLONG
        # 連線真的到 STOCKS_READY(3003) 才能呼叫，太早呼叫會直接拿到
        # SK_ERROR_QUOTE_CONNECT_FIRST(1095)——這裡不自己呼叫
        # EnterMonitorLONG(那是 CapitalQuoteClient 建構時做的事，同一個
        # SKQuoteLib 物件只需要連一次)，只是掛一個 OnConnection 事件槽跟著
        # 判斷連線狀態，還沒連上前呼叫 request_range() 的話先排進佇列，連
        # 上後才真的送出，呼叫端不用自己等待。
        self._connected = False
        self._queued_requests: List[tuple] = []

        self._pending: Dict[_PendingKey, list] = {}
        self._debounce_timers: Dict[_PendingKey, QTimer] = {}
        self._deadline_timers: Dict[_PendingKey, QTimer] = {}

    def is_pending(self, symbol: str) -> bool:
        if self._active_key_for(symbol) is not None:
            return True
        return any(queued[1] == symbol for queued in self._queued_requests)

    def request_range(
        self,
        tag: str,
        symbol: str,
        kline_type: int,
        trade_session: int,
        start_date: datetime.date,
        end_date: datetime.date,
        minute_number: int = 1,
    ) -> None:
        """tag 是呼叫端自己的識別字串(例如"chart"/"reconcile")，同一個
        symbol 同時只接受一筆查詢(不管是已經送出還是還在排隊等連線)——如果
        已經有其他查詢(不管是誰、什麼tag)還沒收完，直接回報失敗，不會讓
        兩邊的資料混在一起(見檔案開頭說明)。呼叫端要自己用 is_pending()
        檢查後再發，或是處理 kline_failed 重試。"""
        if self.is_pending(symbol):
            self.kline_failed.emit(tag, symbol, f"{symbol} 已經有其他查詢在進行中，請稍後再試")
            return

        if not self._connected:
            self._queued_requests.append((tag, symbol, kline_type, trade_session, start_date, end_date, minute_number))
            return

        self._send_request(tag, symbol, kline_type, trade_session, start_date, end_date, minute_number)

    def _send_request(
        self,
        tag: str,
        symbol: str,
        kline_type: int,
        trade_session: int,
        start_date: datetime.date,
        end_date: datetime.date,
        minute_number: int,
    ) -> None:
        key = (tag, symbol)
        self._pending[key] = []
        code = self._quote.SKQuoteLib_RequestKLineAMByDate(
            symbol, kline_type, _OUT_TYPE_NEW, trade_session,
            start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), minute_number,
        )
        if code != 0:
            self._pending.pop(key, None)
            self.kline_failed.emit(tag, symbol, self._center_msg(code))
            return
        self._arm_debounce(key)
        self._arm_deadline(key)

    def _handle_connection(self, n_kind: int) -> None:
        if n_kind != CONN_STOCKS_READY:
            return
        self._connected = True
        queued = self._queued_requests
        self._queued_requests = []
        for args in queued:
            self._send_request(*args)

    def _active_key_for(self, symbol: str) -> Optional[_PendingKey]:
        for key in self._pending:
            if key[1] == symbol:
                return key
        return None

    def _arm_debounce(self, key: _PendingKey) -> None:
        timer = self._debounce_timers.get(key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._finish(key))
            self._debounce_timers[key] = timer
        timer.start(_DEBOUNCE_MS)

    def _arm_deadline(self, key: _PendingKey) -> None:
        timer = self._deadline_timers.get(key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._finish(key))
            self._deadline_timers[key] = timer
        timer.start(_MAX_WAIT_MS)

    def _finish(self, key: _PendingKey) -> None:
        bars = self._pending.pop(key, None)
        if bars is None:
            return  # 已經結束過一次了(debounce跟deadline其中一個先觸發)
        self._debounce_timers[key].stop()
        self._deadline_timers[key].stop()
        tag, symbol = key
        self.kline_ready.emit(tag, symbol, bars)

    def _handle_kline(self, symbol: str, raw: str) -> None:
        key = self._active_key_for(symbol)
        if key is None:
            return  # 沒有在等這個代碼的查詢結果，忽略(可能是殘留的舊事件)
        bar = _parse_bar(raw)
        if bar is not None:
            self._pending[key].append(bar)
        self._arm_debounce(key)

    def _center_msg(self, code: int) -> str:
        try:
            return f"{code} ({self._center.SKCenterLib_GetReturnCodeMessage(code)})"
        except Exception:  # noqa: BLE001
            return str(code)


def _parse_bar(raw: str) -> Optional[dict]:
    """新版格式固定6欄，分線帶時間、日週月線不帶——用有沒有空白分辨，不用
    再另外傳 kline_type 進來判斷。日期統一轉成 YYYY-MM-DD (跟 finmind_client
    的日期格式一致，方便共用比較/排序邏輯)。"""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 6:
        return None

    date_part = parts[0]
    if " " in date_part:
        date_str, time_str = date_part.split(" ", 1)
    else:
        date_str, time_str = date_part, None
    date_str = date_str.replace("/", "-")
    label = f"{date_str} {time_str}" if time_str else date_str

    try:
        open_, high, low, close = (float(x) for x in parts[1:5])
        volume = float(parts[5])
    except ValueError:
        return None

    return {
        "date": label,
        "calendar_date": date_str,
        "time": time_str,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


class _KLineEvents:
    """跟 capital_quote_client._QuoteEvents 一樣掛在同一個 client.quote
    物件上，comtypes 允許同一個 COM 物件掛多個獨立事件槽，兩邊各自收到
    完整的 OnConnection 廣播、互不影響 (那邊也有自己的 OnConnection 處理，
    用來管報價訂閱的排隊；這裡是另一份獨立的排隊，管K線查詢)。"""

    def __init__(self, owner: CapitalKLineClient):
        self._owner = owner

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        self._owner._handle_kline(bstrStockNo, bstrData)

    def OnConnection(self, nKind, nCode):
        self._owner._handle_connection(nKind)
