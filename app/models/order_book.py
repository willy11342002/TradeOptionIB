"""
下單匣的狀態機：暫存(staged) → 送出(live) → 終態(filled/rejected/
cancelled)。

*** 跟舊版(SKCOM)最大的差異：IB 的委託(含 BAG 複式單)整個生命週期都有
一個穩定的整數 orderId，回報比對從「猜履約價組合」簡化成字典查找
(self._records_by_order_id)——舊版 `_match_record` 那套用 strike 集合
比對回報的 workaround 整套刪除，因為造成那個 workaround 存在的原因
(SKCOM 連續IOC每次重送都是全新委託、沒有穩定識別碼)在 IB 這邊不存在。

*** 連續IOC自動重送引擎整套移除(已跟使用者確認，不是遺漏) ***：那是
SKCOM 組合單只能用IOC、不能掛單的限制，IB的BAG combo可以直接掛
LMT+DAY/GTC跡在單子上等成交，不需要這套引擎。STATUS_RETRYING/
STATUS_PAUSED、auto_retry、condition_op、CONDITION_LE/GE、
_on_quote_updated/_condition_met/_maybe_fire、
pause_retry/resume_retry/change_condition 全部拿掉，狀態收斂成
staged→live→filled/rejected/cancelled。改價/改量直接對應 IB 原生的重新
placeOrder(同 orderId 觸發 IB 的 modify 分支，見
app/models/ib_order_client.py 的說明)，刪單對應 cancelOrder。

*** OrderLeg 只存 conId + 顯示用欄位，不存完整 Contract 物件 ***：conId
是 IB 對這個合約的唯一識別，重送/改價/組 BAG ComboLeg 都只需要 conId，
不需要完整的 Option 物件——`send_order`/BAG 的 ComboLeg 都只吃 conId。
只有第一次「送出」需要完整合約物件才能組單腳的 `Contract` 傳給
`ib.placeOrder`，這裡用 `Contract(conId=leg.con_id, exchange="SMART")`
這種只帶 conId 的最小合約重建(IB 官方行為：conId 已經能唯一決定合約，
不需要重新 qualify)，不需要在本地保存完整合約物件、也不需要處理
Contract/ComboLeg 巢狀 dataclass 的還原。
"""
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from ib_async import Bag, ComboLeg
from ib_async import Contract as IBContract

from app.models.ib_order_client import IBOrderClient
from app.services import order_book_store
from app.services.signal import Signal

STATUS_STAGED = "staged"
STATUS_LIVE = "live"       # 已送出，掛在 IB 上等成交(PendingSubmit/PreSubmitted/Submitted/PendingCancel 都算)
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED)

# IB Trade.orderStatus.status 對照到這裡收斂過的狀態機。
_IB_LIVE_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
_IB_CANCELLED_STATUSES = {"Cancelled", "ApiCancelled"}


@dataclass
class OrderLeg:
    con_id: int
    symbol: str
    buy: bool
    right: Optional[str] = None    # "C"/"P"，僅供 UI 顯示
    strike: Optional[float] = None
    local_symbol: str = ""         # IB qualified 後的顯示字串(例如 "SPY   261016C00760000")
    multiplier: str = "100"        # 給 payoff_chart_widget.py 算損益用，美股選擇權幾乎恆為 "100"


@dataclass
class OrderRecord:
    id: str
    kind: str  # "outright" | "duplex"
    legs: List[OrderLeg]
    price: float
    qty: float
    tif: str = "DAY"
    # 複式單這筆整體是淨買方(付debit)還是淨賣方(收credit)，決定 BAG
    # Order.action；裸買賣時就是 legs[0].buy 本身。這個欄位存在的理由跟
    # 舊版 net_buyer 一樣：兩腳各自的買賣方向(leg.buy)不等於整組委託的
    # 淨方向，價格條件/顯示要看這個，不能看 legs[0].buy。
    net_buyer: bool = True
    status: str = STATUS_STAGED
    order_id: Optional[int] = None
    error_msg: Optional[str] = None
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    filled_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

    def label(self) -> str:
        if self.kind == "outright":
            leg = self.legs[0]
            return f"{leg.local_symbol or leg.symbol} {'買' if leg.buy else '賣'}"
        leg1, leg2 = self.legs
        return (f"{leg1.local_symbol or leg1.symbol}{'買' if leg1.buy else '賣'} / "
                f"{leg2.local_symbol or leg2.symbol}{'買' if leg2.buy else '賣'}")


class OrderBookManager:
    def __init__(self, order_client: IBOrderClient):
        self.records_changed = Signal()    # 任何一筆的內容變了(新增/刪除/狀態更新)，UI 重新整個表格
        self.order_book_error = Signal()   # 送出/改價/改量/刪單「當下」失敗
        self.record_rejected = Signal()    # (委託描述, 錯誤訊息)：委託被交易所判定無效，一定要跳出來
        self._order_client = order_client
        self._order_client.order_report.connect(self._on_report)
        self._order_client.order_failed.connect(self.order_book_error.emit)

        self._records: Dict[str, OrderRecord] = {}
        self._records_by_order_id: Dict[int, str] = {}
        self._load_persisted()
        # 一定要在 _load_persisted() 之後才接，不然載入當下逐筆塞進
        # self._records 不會經過 emit，但也不需要——widget 建構子自己會
        # 呼叫一次 _refresh()，第一次畫面本來就會畫出載入好的內容。
        self.records_changed.connect(self._persist)

    @property
    def records(self) -> List[OrderRecord]:
        return list(self._records.values())

    def get_record(self, record_id: str) -> Optional[OrderRecord]:
        """給 auto_close_manager.py 追蹤「我排的平倉單成交了沒」用。"""
        return self._records.get(record_id)

    # --------------------------------------------------------------- 本地保存
    def _persist(self) -> None:
        order_book_store.save([asdict(record) for record in self._records.values()])

    def _load_persisted(self) -> None:
        for raw in order_book_store.load():
            try:
                raw = dict(raw)
                raw["legs"] = [OrderLeg(**leg) for leg in raw["legs"]]
                record = OrderRecord(**raw)
            except (TypeError, KeyError) as exc:
                print(f"[OrderBook] 本地保存的委託格式對不上目前的欄位定義，這筆跳過不載入: {exc}")
                continue
            self._records[record.id] = record
            if record.order_id is not None:
                self._records_by_order_id[record.order_id] = record.id

    # --------------------------------------------------------------- 暫存
    def stage_outright(self, contract, buy: bool, price: float, qty: float, tif: str = "DAY") -> str:
        leg = self._contract_to_leg(contract, buy)
        record = OrderRecord(
            id=str(uuid.uuid4()), kind="outright", legs=[leg],
            price=price, qty=qty, tif=tif, net_buyer=buy,
        )
        self._records[record.id] = record
        self.records_changed.emit()
        return record.id

    def stage_duplex(
        self, contract1, buy1: bool, contract2, buy2: bool,
        net_price: float, qty: float, tif: str = "DAY", net_buyer: Optional[bool] = None,
    ) -> str:
        leg1 = self._contract_to_leg(contract1, buy1)
        leg2 = self._contract_to_leg(contract2, buy2)
        record = OrderRecord(
            id=str(uuid.uuid4()), kind="duplex", legs=[leg1, leg2],
            price=net_price, qty=qty, tif=tif, net_buyer=buy1 if net_buyer is None else net_buyer,
        )
        self._records[record.id] = record
        self.records_changed.emit()
        return record.id

    @staticmethod
    def _contract_to_leg(contract, buy: bool) -> OrderLeg:
        return OrderLeg(
            con_id=contract.conId, symbol=contract.symbol, buy=buy,
            right=getattr(contract, "right", None) or None,
            strike=getattr(contract, "strike", None) or None,
            local_symbol=getattr(contract, "localSymbol", "") or "",
            multiplier=getattr(contract, "multiplier", "") or "100",
        )

    def discard_staged(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is not None and record.status == STATUS_STAGED:
            del self._records[record_id]
            self.records_changed.emit()

    def edit_staged(self, record_id: str, price: Optional[float] = None, qty: Optional[float] = None) -> None:
        record = self._records.get(record_id)
        if record is None or record.status != STATUS_STAGED:
            return
        if price is not None:
            record.price = price
        if qty is not None:
            record.qty = qty
        self.records_changed.emit()

    # --------------------------------------------------------------- 送出
    def confirm_send(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None or record.status != STATUS_STAGED:
            return
        self._send_once(record)
        record.status = STATUS_LIVE
        self.records_changed.emit()

    def _send_once(self, record: OrderRecord) -> None:
        if record.kind == "outright":
            leg = record.legs[0]
            contract = self._leg_contract(leg)
            action = "BUY" if leg.buy else "SELL"
        else:
            leg1, leg2 = record.legs
            contract = Bag(
                symbol=leg1.symbol, exchange="SMART", currency="USD",
                comboLegs=[
                    ComboLeg(conId=leg1.con_id, ratio=1, action="BUY" if leg1.buy else "SELL", exchange="SMART"),
                    ComboLeg(conId=leg2.con_id, ratio=1, action="BUY" if leg2.buy else "SELL", exchange="SMART"),
                ],
            )
            action = "BUY" if record.net_buyer else "SELL"
        order_id = self._order_client.send_order(
            contract, action, record.qty, record.price, record.tif, order_id=record.order_id,
        )
        record.order_id = order_id
        self._records_by_order_id[order_id] = record.id

    @staticmethod
    def _leg_contract(leg: OrderLeg):
        return IBContract(conId=leg.con_id, exchange="SMART")

    # --------------------------------------------------------------- 改價/改量/刪單
    def amend_price(self, record_id: str, new_price: float) -> None:
        record = self._records.get(record_id)
        if record is None or record.order_id is None:
            return
        record.price = new_price
        self._send_once(record)  # 同一個 order_id 重新 placeOrder，IB 視為改價，不是新單
        self.records_changed.emit()

    def amend_qty(self, record_id: str, new_qty: float) -> None:
        record = self._records.get(record_id)
        if record is None or record.order_id is None:
            return
        record.qty = new_qty
        self._send_once(record)
        self.records_changed.emit()

    def cancel(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None or record.order_id is None:
            return
        self._order_client.cancel_order(record.order_id)

    def delete(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None:
            return
        if record.status == STATUS_LIVE and record.order_id is not None:
            self._order_client.cancel_order(record.order_id)
        if record.order_id is not None:
            self._records_by_order_id.pop(record.order_id, None)
        del self._records[record_id]
        self.records_changed.emit()

    # --------------------------------------------------------------- 回報比對
    def _on_report(self, report: dict) -> None:
        order_id = report.get("order_id")
        record_id = self._records_by_order_id.get(order_id)
        if record_id is None:
            return  # 不是這個 manager 追蹤的委託(例如 Gateway 上手動下的單)，不處理
        record = self._records[record_id]

        status = report.get("status")
        if status == "Filled":
            record.status = STATUS_FILLED
            record.fill_price = report.get("avg_fill_price")
            record.fill_qty = report.get("filled")
            if record.filled_at is None:
                record.filled_at = time.time()
        elif status in _IB_CANCELLED_STATUSES:
            record.status = STATUS_CANCELLED
        elif status == "Inactive":
            record.status = STATUS_REJECTED
            record.error_msg = "委託被交易所判定無效(Inactive)"
            self.record_rejected.emit(record.label(), record.error_msg)
        elif status in _IB_LIVE_STATUSES:
            record.status = STATUS_LIVE

        self.records_changed.emit()
