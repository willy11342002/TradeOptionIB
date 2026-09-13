"""
IB 下單封裝，取代 app/models/capital_order_client.py。

跟群益 SKOrderLib 最大的不同：IB 的委託(含 BAG 複式單)整個生命週期都有
一個穩定的整數 orderId，改價/刪單都認這個 id，不需要像 SKCOM 那樣另外
維護一個 13 碼 SeqNo、也不需要 `_REPORT_FIELDS` 那套逗號字串解析表——
`ib.placeOrder()` 回傳的 `Trade` 物件本身就會被 `ib_async` 持續更新
(`trade.orderStatus`)，`orderStatusEvent`/`execDetailsEvent` 直接給結構
化欄位。

改價的做法：`ib.placeOrder()` 如果傳進去的 `order.orderId` 是既有委託
的 id，IB 會辨識成「修改」而不是新單(核對自 ib_async 原始碼
`IB.placeOrder`：`orderId = order.orderId or self.client.getReqId()`，
有給就沿用、觸發 modify 分支)，不是新開一張單——這裡的 send_order 直接
支援傳 order_id 做這件事，呼叫端(order_book.py)不用另外呼叫不同的函式。

*** 這支不做「連續IOC自動重送」那套狀態機 ***：那是 SKCOM 組合單只能
用 IOC、不能掛單的限制，IB 的 BAG combo 可以直接掛 LMT+DAY/GTC 跡在單
子上等成交，不需要重送。重送/暫停/恢復的狀態機邏輯(如果曾經在
order_book.py 裡)已經跟著這次改動整套移除，不是搬過來這裡。
"""
from typing import Dict, Optional

from ib_async import LimitOrder, Trade

from app.models.ib_client import IBClient
from app.services.signal import Signal


class IBOrderClient:
    def __init__(self, ib_client: IBClient):
        self.order_sent = Signal()     # orderId，剛送出(PendingSubmit)當下
        self.order_failed = Signal()   # 送出/改價/刪單失敗訊息
        self.order_report = Signal()   # 委託狀態變化，見 _trade_to_report()
        self._ib_client = ib_client
        self._ib = ib_client.ib
        # *** 重開程式後這個字典本來是空的，cancel_order 會找不到上一個
        # session 送出的既有委託 ***：ib_async 連線時自己會從 IB 同步這個
        # session 看得到的委託(ib.trades())，這裡在建構時把它們預先塞
        # 進來，讓 order_book.py 重新載入的 LIVE 紀錄一樣能改價/刪單，
        # 不用等使用者手動觸發什麼同步動作。
        self._trades: Dict[int, Trade] = {t.order.orderId: t for t in self._ib.trades()}
        self._ib.orderStatusEvent += self._on_order_status
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.errorEvent += self._on_error

    def send_order(
        self, contract, action: str, qty: float, price: float,
        tif: str = "DAY", order_id: Optional[int] = None,
    ) -> int:
        """送出一筆限價單；`order_id` 有給且是既有委託的話，IB 會辨識成
        改價/改量(同一個 orderId)，不是新單。單腳/複式單共用這支——呼叫
        端自己組好 `contract`(單腳用 `Option`，複式用
        `secType='BAG'` + `comboLegs`)。回傳 orderId。"""
        order = LimitOrder(action, qty, price)
        order.tif = tif
        if order_id:
            order.orderId = order_id
        trade = self._ib.placeOrder(contract, order)
        self._trades[trade.order.orderId] = trade
        self.order_sent.emit(trade.order.orderId)
        return trade.order.orderId

    def cancel_order(self, order_id: int) -> None:
        trade = self._trades.get(order_id)
        if trade is None:
            self.order_failed.emit(f"找不到 orderId={order_id} 的委託，無法刪單")
            return
        self._ib.cancelOrder(trade.order)

    def get_trade(self, order_id: int) -> Optional[Trade]:
        return self._trades.get(order_id)

    def _on_order_status(self, trade: Trade) -> None:
        if trade.order.orderId not in self._trades:
            return  # 不是這個 client 送出的委託(例如 TWS 手動下的單)，不處理
        self.order_report.emit(self._trade_to_report(trade))

    def _on_exec_details(self, trade: Trade, fill) -> None:
        if trade.order.orderId not in self._trades:
            return
        self.order_report.emit(self._trade_to_report(trade))

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        # IB 委託相關的錯誤，reqId 就是該筆委託的 orderId。
        if reqId in self._trades:
            self.order_failed.emit(f"{errorCode}: {errorString}")

    @staticmethod
    def _trade_to_report(trade: Trade) -> dict:
        status = trade.orderStatus
        return {
            "order_id": trade.order.orderId,
            "status": status.status,
            "filled": status.filled,
            "remaining": status.remaining,
            "avg_fill_price": status.avgFillPrice,
            "action": trade.order.action,
            "lmt_price": trade.order.lmtPrice,
            "symbol": trade.contract.symbol,
        }
