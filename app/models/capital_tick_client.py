"""
群益 SKQuoteLib_RequestTicks 訂閱封裝。

*** 重要澄清：文件寫的 OnNotifyLiveKLineData 事件，實際安裝的 SKCOM.dll
(2.13.59.0，版本號跟文件完全對得上) 裡根本不存在 ***
直接對真的COM介面重新查證過 (comtypes.client.GetModule 重新產生的
_ISKQuoteLibEvents 介面清單裡沒有這個方法，親自確認過，不是猜的、也不是
版本不合、更不是comtypes快取沒更新)——文件寫了，但這支DLL沒有實作出來
(或還沒對外開放)。所以這裡改用另外兩個查證過**真的存在**的事件，自己在
本地把tick組成K棒，不依賴那個不存在的現成「即時分K」。

1. SKQuoteLib_RequestTicks([in,out] SHORT* psPageNo, [in] BSTR bstrStockNo)
   文件4-4-3：「psPageNo請從0開始」「一個Page僅能索取一檔」——跟報價訂閱
   (RequestStocks，一個頁碼可以塞逗號分隔多檔)不一樣，這裡一檔要配一個
   獨立頁碼，多檔要呼叫多次、頁碼遞增。

2. 訂閱後會收到（都查過文件4-4-s/4-4-t，兩個事件欄位定義完全一樣）：
   - OnNotifyHistoryTicksLONG(sMarketNo, nIndex, nPtr, nDate, nTimehms,
     nTimemillismicros, nBid, nAsk, nClose, nQty, nSimulate)
     文件開頭：「當首次索取個股成交明細，此事件會回補當天Tick」——這就是
     「盤中才開程式也能拿到開盤到現在」的關鍵。
   - OnNotifyTicksLONG：訂閱之後的即時tick，欄位跟上面完全一樣。
   共同欄位意義：
     nIndex/nStockIdx: 系統索引代碼，不是商品代碼字串——這裡改成在
       subscribe() 當下呼叫 GetStockByNoLONG 先查一次該商品的 nStockIdx
       跟 sDecimal 存起來做對照表，不在事件裡呼叫查詢函式(文件備註明講
       「避免在OnNotifyHistoryTicksLONG事件裡進行GetTickLONG/
       GetStockByIndexLONG」)。
     nDate: 交易日期 YYYYMMDD。
     nTimehms: 時分秒 hh:mm:ss 打包成一個整數(例如 90025 代表 09:00:25，
       時位不足二位不補零，解析時要先補零成6碼再切)。
     nBid/nAsk/nClose/nQty: 買價/賣價/成交價/成交量，價格是原始值未還原
       小數位數，要自己除以10^sDecimal。
     nSimulate: 0=一般揭示 1=試算揭示(開盤前試搓，不是真的成交)——組K棒
       只採計nSimulate=0的tick，試算揭示不算真實成交，不能拿來當OHLC。

3. 備註「須使用SKQuoteLib_EnterMonitorLONG登入，該事件才會被觸發」——跟
   capital_quote_client.py／capital_kline_client.py 一樣要等 OnConnection
   的 STOCKS_READY(3003) 才能呼叫，還沒連線前呼叫的訂閱先排進佇列，連線
   後自動補送。
"""
import datetime
from typing import Dict, List

import comtypes.client
from PyQt5.QtCore import QObject, pyqtSignal

from app.models.capital_client import CapitalClient
from app.models.capital_quote_client import CONN_STOCKS_READY

_SIMULATE_REAL = 0  # 一般揭示(真的成交)，試算揭示(1)不採計


class CapitalTickClient(QObject):
    # 代碼, 成交時間(datetime), 成交價, 成交量 —— 回補/即時都從這個訊號出，
    # 呼叫端(MinuteBarBuilder)不需要區分來源，用時間戳記自然分類就對了。
    tick_received = pyqtSignal(str, object, float, float)
    subscribe_failed = pyqtSignal(str, str)

    def __init__(self, client: CapitalClient):
        super().__init__()
        self._quote = client.quote
        self._center = client.center
        self._sk = client.sk

        self._connected = False
        self._next_page = 0
        self._subscribed: Dict[str, int] = {}      # 代碼 -> 頁碼
        self._decimals: Dict[str, int] = {}        # 代碼 -> sDecimal
        self._index_to_symbol: Dict[int, str] = {}  # nStockIdx -> 代碼
        self._queued_symbols: List[str] = []

        self._events = _TickEvents(self)
        self._handler = comtypes.client.GetEvents(self._quote, self._events)

    def subscribe(self, symbol: str) -> None:
        if symbol in self._subscribed or symbol in self._queued_symbols:
            return
        if not self._connected:
            self._queued_symbols.append(symbol)
            return
        self._do_subscribe(symbol)

    def _do_subscribe(self, symbol: str) -> None:
        stock, code = self._quote.SKQuoteLib_GetStockByNoLONG(symbol, self._sk.SKSTOCKLONG())
        if code != 0:
            self.subscribe_failed.emit(symbol, self._center_msg(code))
            return
        self._decimals[symbol] = stock.sDecimal
        self._index_to_symbol[stock.nStockIdx] = symbol

        page = self._next_page
        self._next_page += 1
        echoed_page, code = self._quote.SKQuoteLib_RequestTicks(page, symbol)
        if code != 0:
            self.subscribe_failed.emit(symbol, self._center_msg(code))
            return
        self._subscribed[symbol] = echoed_page

    def _handle_connection(self, n_kind: int) -> None:
        if n_kind != CONN_STOCKS_READY:
            return
        self._connected = True
        queued = self._queued_symbols
        self._queued_symbols = []
        for symbol in queued:
            self._do_subscribe(symbol)

    def _handle_tick(
        self, n_index: int, n_date: int, n_timehms: int,
        n_close: int, n_qty: int, n_simulate: int,
    ) -> None:
        if n_simulate != _SIMULATE_REAL:
            return  # 試算揭示，不是真的成交
        symbol = self._index_to_symbol.get(n_index)
        if symbol is None:
            return  # 不是我們訂閱的代碼(理論上不會發生，防呆)
        scale = 10 ** self._decimals.get(symbol, 0)
        dt = _parse_tick_datetime(n_date, n_timehms)
        if dt is None:
            return
        self.tick_received.emit(symbol, dt, n_close / scale, float(n_qty))

    def _center_msg(self, code: int) -> str:
        try:
            return f"{code} ({self._center.SKCenterLib_GetReturnCodeMessage(code)})"
        except Exception:  # noqa: BLE001
            return str(code)


def _parse_tick_datetime(n_date: int, n_timehms: int):
    try:
        date_str = f"{n_date:08d}"
        time_str = f"{n_timehms:06d}"
        return datetime.datetime(
            int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]),
            int(time_str[0:2]), int(time_str[2:4]), int(time_str[4:6]),
        )
    except ValueError:
        return None


class _TickEvents:
    def __init__(self, owner: CapitalTickClient):
        self._owner = owner

    def OnConnection(self, nKind, nCode):
        self._owner._handle_connection(nKind)

    def OnNotifyHistoryTicksLONG(self, sMarketNo, nIndex, nPtr, nDate, nTimehms, nTimemillismicros, nBid, nAsk, nClose, nQty, nSimulate):
        self._owner._handle_tick(nIndex, nDate, nTimehms, nClose, nQty, nSimulate)

    def OnNotifyTicksLONG(self, sMarketNo, nIndex, nPtr, nDate, nTimehms, nTimemillismicros, nBid, nAsk, nClose, nQty, nSimulate):
        self._owner._handle_tick(nIndex, nDate, nTimehms, nClose, nQty, nSimulate)
