"""
群益 SKQuoteLib 報價封裝，取代原本的華南 XQ RTD (rtd_client.py，已移除)。

跟 RTD 的「每個欄位各自一個 topic」不一樣，SKQuoteLib 是「每個商品一個索引
(nStockIdx)，一次給整包買價/賣價/成交價等欄位」：
    1. SKQuoteLib_EnterMonitorLONG() 連線報價主機——這是非同步的，呼叫時
       立刻回傳的 retCode 只代表「連線請求已送出」。

       *** 這一步之前踩過兩個坑，都是照著官方文件《13.國內報價.docx》
       修正過的，不是猜的： ***
       (a) 連線成功不是只等 nKind==3001 (Connected)。文件寫得很清楚：
           「請先 SKQuoteLib_EnterMonitorLONG，須等 OnConnection 收到
           SK_SUBJECT_CONNECTION_STOCKS_READY 後，方可進行訂閱商品報
           價」——SK_SUBJECT_CONNECTION_STOCKS_READY 對應的就是
           nKind==3003 (官方範例 Quote.py 印成 "Stocks ready!")，
           不是 3001。3001 只代表連線本身成功、商品資料表可能還在下載
           (SKQuoteLib_IsConnected() 回傳 2 = 下載中)，這時候呼叫
           RequestStocks 一樣會回 SK_ERROR_QUOTE_CONNECT_FIRST。
       (b) 文件在 OnConnection 事件的說明裡明講：「避免在此處直接進行
           RequestStocks and RequestTicks 等報價訂閱」。所以收到
           3003 之後，訂閱動作要排到事件callback 外面執行 (用
           QTimer.singleShot(0, ...) 丟到下一個事件圈)，不能在
           OnConnection callback 裡面直接呼叫。
    2. SKQuoteLib_RequestStocks(頁碼, "代碼1,代碼2,...") 訂閱一批商品，
       交易所會依序配好索引 (不需要我們自己對應)
    3. 報價變動時觸發 OnNotifyQuoteLONG(sMarketNo, nStockIdx) 事件，這個
       事件本身不帶報價值，要另外呼叫 GetStockByIndexLONG 把完整的
       SKSTOCKLONG 結構拉回來 (裡面就有 bstrStockNo，不用自己維護
       index -> 代碼的對照表)。這個事件只在報價「變動」時才會來，還沒
       成交過或非盤中時不會觸發，所以另外提供 fetch_snapshot() 直接用
       GetStockByNoLONG 隨時主動查詢目前快照，不用乾等事件
    4. 價格欄位(nBid/nAsk/nClose 等)是整數，要除以 10**sDecimal 還原成
       實際價格 (跟群益官方範例 Quote.py 的算法一致)

因為連線是非同步的，呼叫端 (main_window.py) 在還沒連上前就呼叫
subscribe() 是很正常的情況 (例如剛登入、視窗還在建構)，這裡的做法是把
還沒連線時的 subscribe() 要求先排進 _pending_symbols，等真的收到
SK_SUBJECT_CONNECTION_STOCKS_READY (nKind==3003) 才補送出去，呼叫端不需
要自己等待/重試。

*** comtypes 回傳值形狀是實測過的，不是照 IDL 猜的 ***
SKCOM 的 IDL 把某些參數同時標成 [in, out]（不是單純 [out]），comtypes 對
這種參數的規則是：呼叫時一樣要把值傳進去，但回傳值會變成一個
[該參數的值, retCode] 兩個元素的 list，不是單純一個 retCode 整數。用
`uv run python` 對已註冊好的 SKCOM.dll 實際呼叫確認過：
    SKQuoteLib_RequestStocks(page, symbols)      -> [page回聲, retCode]
    SKQuoteLib_GetStockByNoLONG(symbol, stock)   -> [stock(同一顆物件), retCode]
    SKQuoteLib_GetStockByIndexLONG(mkt, idx, s)  -> [stock(同一顆物件), retCode]
    SKQuoteLib_CancelRequestStocks(symbols)      -> retCode (純整數，這個
                                                     函式只吃 1 個參數，
                                                     不吃頁碼！)
上面這些之前都當成單一 retCode 整數在用，是錯的 (第一版沒有實測，只照
官方範例的呼叫語法照抄，但範例只把回傳值印出來看沒有拆開檢查型別)。

群益的基礎報價沒有華南 RTD 那種「隱波%/理論價/Delta/Theta」加值欄位
(SKQuoteLib_Delta 等函式其實是本地 Black-Scholes 計算機，不是即時報價，
需要自己先算出 IV 才能用)，所以這裡只提供買價/賣價/成交價/開高低。
"""
from typing import Dict, Set

import comtypes.client
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.models.capital_client import CapitalClient

CONN_CONNECTED = 3001
CONN_DISCONNECTED = 3002
CONN_STOCKS_READY = 3003
CONN_ERROR = 3021


class CapitalQuoteClient(QObject):
    quote_updated = pyqtSignal(str, dict)  # 代碼, {"bid":, "ask":, "last":, "open":, "high":, "low":}
    quote_error = pyqtSignal(str, str)     # 代碼或動作說明, 錯誤訊息
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self, client: CapitalClient):
        super().__init__()
        self._client = client
        self._quote = client.quote
        self._sk = client.sk
        self._next_page = 0
        self._subscribed_symbols: Set[str] = set()
        self._pending_symbols: Set[str] = set()
        self._connected = False

        self._events = _QuoteEvents(self)
        self._handler = comtypes.client.GetEvents(self._quote, self._events)

        # 連線是非同步的 (見檔案開頭說明)，這裡只是送出連線請求。
        code = self._quote.SKQuoteLib_EnterMonitorLONG()
        if code != 0:
            self.quote_error.emit("EnterMonitorLONG", self._center_msg(code))

    def subscribe(self, symbols) -> None:
        """一次訂閱一批商品代碼 (逗號分隔字串上限由 SKCOM 決定，這裡假設
        T 字報價一次的檔數不會超過，超過的話要自己分批呼叫)。還沒連上報
        價主機的話會先排進待訂閱清單，連線成功後自動補送，呼叫端不用等
        待/重試。"""
        symbols = [s for s in symbols if s not in self._subscribed_symbols and s not in self._pending_symbols]
        if not symbols:
            return
        if not self._connected:
            self._pending_symbols.update(symbols)
            return
        self._do_subscribe(symbols)

    def _do_subscribe(self, symbols) -> None:
        page = self._next_page
        self._next_page += 1
        _echo_page, code = self._quote.SKQuoteLib_RequestStocks(page, ",".join(symbols))
        if code != 0:
            self.quote_error.emit(",".join(symbols), self._center_msg(code))
            return
        for s in symbols:
            self._subscribed_symbols.add(s)
            self.fetch_snapshot(s)

    def fetch_snapshot(self, symbol: str) -> None:
        """主動查一次目前的報價快照 (不需要等事件觸發)，隨時可以呼叫。"""
        stock, code = self._quote.SKQuoteLib_GetStockByNoLONG(symbol, self._sk.SKSTOCKLONG())
        if code != 0:
            self.quote_error.emit(symbol, self._center_msg(code))
            return
        self.quote_updated.emit(symbol, self._stock_to_data(stock))

    def unsubscribe(self, symbols) -> None:
        self._pending_symbols.difference_update(symbols)
        symbols = [s for s in symbols if s in self._subscribed_symbols]
        if not symbols:
            return
        try:
            self._quote.SKQuoteLib_CancelRequestStocks(",".join(symbols))
        except Exception:  # noqa: BLE001
            pass
        self._subscribed_symbols.difference_update(symbols)

    def unsubscribe_all(self) -> None:
        self.unsubscribe(list(self._subscribed_symbols) + list(self._pending_symbols))

    def _handle_quote(self, market_no: int, stock_idx: int) -> None:
        stock, code = self._quote.SKQuoteLib_GetStockByIndexLONG(market_no, stock_idx, self._sk.SKSTOCKLONG())
        if code != 0:
            return
        symbol = stock.bstrStockNo
        if symbol not in self._subscribed_symbols:
            return
        self.quote_updated.emit(symbol, self._stock_to_data(stock))

    def _handle_connection(self, n_kind: int, n_code: int) -> None:
        # 文件明講「避免在 OnConnection 事件裡直接呼叫 RequestStocks」，
        # 所以真正訂閱的動作用 QTimer.singleShot(0, ...) 丟到這個事件處理
        # 完之後、下一個事件圈才執行，不在這個 callback 裡面直接送出。
        if n_kind == CONN_STOCKS_READY:
            self._connected = True
            self.connected.emit()
            if self._pending_symbols:
                pending = list(self._pending_symbols)
                self._pending_symbols.clear()
                QTimer.singleShot(0, lambda: self._do_subscribe(pending))
        elif n_kind == CONN_DISCONNECTED:
            self._connected = False
            self.disconnected.emit()
        elif n_kind == CONN_ERROR:
            self.quote_error.emit("OnConnection", self._center_msg(n_code))

    def _center_msg(self, code: int) -> str:
        try:
            return f"{code} ({self._client.center.SKCenterLib_GetReturnCodeMessage(code)})"
        except Exception:  # noqa: BLE001
            return str(code)

    @staticmethod
    def _stock_to_data(stock) -> Dict[str, float]:
        scale = 10 ** stock.sDecimal
        return {
            "bid": stock.nBid / scale,
            "ask": stock.nAsk / scale,
            "last": stock.nClose / scale,
            "open": stock.nOpen / scale,
            "high": stock.nHigh / scale,
            "low": stock.nLow / scale,
            "preclose": stock.nRef / scale,   # 昨收參考價
            "up_limit": stock.nUp / scale,    # 漲停價
            "down_limit": stock.nDown / scale,  # 跌停價
        }


class _QuoteEvents:
    def __init__(self, owner: CapitalQuoteClient):
        self._owner = owner

    def OnNotifyQuoteLONG(self, sMarketNo, nStockIdx):
        self._owner._handle_quote(sMarketNo, nStockIdx)

    def OnConnection(self, nKind, nCode):
        self._owner._handle_connection(nKind, nCode)
