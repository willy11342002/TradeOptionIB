"""
群益 SKOrderLib 下單封裝：裸買賣 (SendOptionOrder) 跟價差複式單
(SendDuplexOrder)。

兩者共用同一個 FUTUREORDER 結構 (comtypes.gen.SKCOMLib.FUTUREORDER)，複式
單只是多填 bstrStockNo2/sBuySell2 兩個欄位。價差複式單是交易所端原生的兩
腳合併委託，兩腳同進同出由交易所撮合引擎保證、不會有「一腳成交一腳沒成
交」的裸露風險 (跟凱基 kgisuperpy 只能拆成兩筆單式單完全不同)，所以複式
單「沒成交就重送」單純是照原樣重送整張單，不需要處理腿風險。

委託/成交回報走 SKReplyLib 的 OnNewData(userID, bstrData) 事件，是逗號分
隔字串；欄位的精確定義在群益文件《12.回報.docx》，這裡先把原始字串整包
往外送 (order_report 訊號)，等實測拿到真實回報字串後再視需要解析特定欄位
(例如委託書號、成交價、成交量)，避免照猜的欄位位置寫死。
"""
from typing import Optional

import comtypes.client
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.models.capital_client import CapitalClient

BUY = 0
SELL = 1

TIF_ROD = 0
TIF_IOC = 1
TIF_FOK = 2

NEW_POSITION = 0
CLOSE_POSITION = 1

# 價差複式單重送：因為是交易所端原生複式單、沒有腿風險，「多次IOC」的實際
# 做法是沒成交就連續送出、中間不等待 (不是每隔幾秒送一次)，所以這裡沒有
# 重送間隔，只用 QTimer(interval=0) 讓事件圈盡快跑下一輪。
DUPLEX_RETRY_INTERVAL_MS = 0


class CapitalOrderClient(QObject):
    order_sent = pyqtSignal(str)       # 送出當下的訊息 (SendXxxOrder 回傳的 bstrMessage)
    order_failed = pyqtSignal(str)     # 送出失敗 (retCode != 0)
    order_report = pyqtSignal(str)     # OnNewData 原始回報字串，待實測後再拆欄位
    duplex_retry_progress = pyqtSignal(int)  # 已送出第幾次 (沒有上限)
    duplex_retry_stopped = pyqtSignal(str)   # 停止原因

    def __init__(self, client: CapitalClient):
        super().__init__()
        self._client = client
        self._order = client.order
        self._reply = client.reply
        self._sk = client.sk

        self._reply_events = _ReplyEvents(self)
        self._reply_handler = comtypes.client.GetEvents(self._reply, self._reply_events)

        self._retry_timer: Optional[QTimer] = None
        self._retry_attempt = 0
        self._retry_builder = None  # 沒成交時重呼叫這個 callable 重建/重送 FUTUREORDER

    # ------------------------------------------------------------ 裸買賣
    def send_option_order(
        self, symbol: str, buy: bool, price: float, qty: int,
        tif: int = TIF_ROD, new_close: int = NEW_POSITION,
    ):
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = self._client.account
        order.bstrStockNo = symbol
        order.sBuySell = BUY if buy else SELL
        order.sTradeType = tif
        order.bstrPrice = str(price)
        order.nQty = int(qty)
        order.sNewClose = new_close

        message, code = self._order.SendOptionOrder(self._client.user_id, True, order)
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
        else:
            self.order_sent.emit(str(message))

    # ------------------------------------------------------------ 價差複式單
    def _build_duplex_order(
        self, symbol1: str, buy1: bool, symbol2: str, buy2: bool,
        net_price: float, qty: int, tif: int, new_close: int,
    ):
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = self._client.account
        order.bstrStockNo = symbol1
        order.bstrStockNo2 = symbol2
        order.sBuySell = BUY if buy1 else SELL
        order.sBuySell2 = BUY if buy2 else SELL
        order.sTradeType = tif
        order.bstrPrice = str(net_price)
        order.nQty = int(qty)
        order.sNewClose = new_close
        return order

    def send_duplex_order(
        self, symbol1: str, buy1: bool, symbol2: str, buy2: bool,
        net_price: float, qty: int, tif: int = TIF_IOC,
        new_close: int = NEW_POSITION, auto_retry: bool = True,
    ):
        """tif 只接受 TIF_IOC/TIF_FOK，交易所規則不開放複式單用 ROD。"""
        if tif not in (TIF_IOC, TIF_FOK):
            raise ValueError("價差複式單只能用 IOC 或 FOK")

        def build():
            return self._build_duplex_order(symbol1, buy1, symbol2, buy2, net_price, qty, tif, new_close)

        self._stop_retry("送出新單")
        self._send_duplex_once(build)

        if auto_retry:
            self._retry_builder = build
            self._retry_attempt = 1
            self._retry_timer = QTimer(self)
            self._retry_timer.setInterval(DUPLEX_RETRY_INTERVAL_MS)
            self._retry_timer.timeout.connect(self._on_retry_tick)
            self._retry_timer.start()
            self.duplex_retry_progress.emit(self._retry_attempt)

    def _send_duplex_once(self, build) -> None:
        order = build()
        message, code = self._order.SendDuplexOrder(self._client.user_id, True, order)
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
        else:
            self.order_sent.emit(str(message))

    def _on_retry_tick(self) -> None:
        self._retry_attempt += 1
        self._send_duplex_once(self._retry_builder)
        self.duplex_retry_progress.emit(self._retry_attempt)

    def stop_duplex_retry(self) -> None:
        """目前這個 dialog 沒有暴露停止按鈕 (照要求拿掉了)，之後的下單匣/
        委託追蹤頁面要提供停止功能的話，呼叫這個方法即可。"""
        self._stop_retry("使用者手動停止")

    def _stop_retry(self, reason: str) -> None:
        if self._retry_timer is not None:
            self._retry_timer.stop()
            self._retry_timer = None
            self._retry_builder = None
            self.duplex_retry_stopped.emit(reason)

    def _center_msg(self, code: int) -> str:
        try:
            return self._client.center.SKCenterLib_GetReturnCodeMessage(code)
        except Exception:  # noqa: BLE001
            return f"錯誤碼 {code}"


class _ReplyEvents:
    def __init__(self, owner: CapitalOrderClient):
        self._owner = owner

    def OnNewData(self, bstrUserID, bstrData):
        self._owner.order_report.emit(bstrData)
