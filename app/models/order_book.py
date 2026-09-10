"""
下單匣的狀態機：暫存(staged) → 送出(live/retrying) → 終態(filled/
rejected/cancelled)。

這裡刻意跟 capital_order_client.py 分開——CapitalOrderClient 只管「跟
SKOrderLib 講一次話」，這支檔案管「使用者到底送了哪些委託、現在什麼狀
態、連續IOC要不要繼續重送」，因為要同時支援好幾組獨立的連續IOC(例如鐵
鷹兩腳價差各自一組)，狀態機式的東西集中在一個地方比較不會亂。

*** 連續IOC 的正確邏輯 (照使用者明確糾正過的版本，不是盲目連發) ***
之前的版本用 QTimer(interval=0) 每個事件圈都不檢查價格、直接重送，這是
錯的，而且因為每次重送都會 emit records_changed 觸發整個表格重繪，
interval=0 等於每秒重繪表格幾百上千次，UI 執行緒被榨乾，使用者點暫停/
刪除的滑鼠事件根本排不進事件圈處理——這就是「暫停/刪單按了沒反應」的真
正原因，不是按鈕邏輯本身寫錯。

正確邏輯：
    1. 進入 STATUS_RETRYING 之後不是在跑計時器盲送，是「監看」這幾腳的
       即時報價 (訂閱 CapitalQuoteClient 的 quote_updated 訊號)，每次
       相關報價更新時檢查目前買賣價能不能滿足限價條件 (_condition_met)。
    2. 條件滿足才呼叫 SendOptionOrder/SendDuplexOrder 送一次 (_maybe_fire)。
    3. 委託回報 OrderErr=='Y' 或 Type=='S' (真的失敗/被交易所退單)：停
       在 STATUS_REJECTED，不會再自動送單。
    4. 委託回報 Type=='C' 且 OrderErr=='N' (單純沒成交被取消，IOC 正常
       現象)：立刻用目前快取的報價重新檢查一次條件 (可能同一個報價還沒
       變、但因為競爭排隊沒搓合成功，值得馬上再試一次)，沒滿足的話就回
       到「監看」狀態，等下一次報價異動再檢查。
    5. 暫停/刪除：沒有計時器可以停，只是把 status 從 RETRYING 改掉，
       _maybe_fire 呼叫前一定先檢查 status==RETRYING，改掉之後任何報價
       異動都不會再觸發送單，乾淨俐落不會有殘留的重送。

*** 連續IOC 的委託識別問題 ***
一般委託(ROD 掛單)送出後 SeqNo 終生不變，回報靠 SeqNo 比對就好。但連續
IOC 每次重送都是全新的委託 (IOC 瞬間成交或死亡，沒有「改單」這回事)，
每次重送 SeqNo 都不一樣。這裡改用「商品組合＋買賣別」比對回報屬於哪一筆
本地紀錄 (見 _match_record)：同一時間不會有兩組完全相同商品組合+方向的
委託在跑，這個比對足夠用，但不是絕對嚴謹 (已知限制，寫在這裡供之後參
考)。
"""
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from app.models.capital_order_client import (
    CapitalOrderClient, TIF_ROD, TIF_IOC, TIF_FOK, NEW_POSITION,
)
from app.models.capital_quote_client import CapitalQuoteClient
from app.services import order_book_store

STATUS_STAGED = "staged"
STATUS_LIVE = "live"           # 已送出，沒有自動重送 (ROD 掛單，或單發 IOC/FOK 等回報)
STATUS_RETRYING = "retrying"   # 連續IOC 監看中 (價格滿足才送)
STATUS_PAUSED = "paused"       # 連續IOC 暫停中
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED)

GET_ORDER_REPORT_COOLDOWN_SEC = 5.0  # 官方文件要求查詢間隔至少 5 秒
ORDER_REPORT_FORMAT_ALL = 1  # GetOrderReport nFormat：1=全部

# 連續IOC 的限價條件比較方向。_condition_met 算出來的 total_cost/threshold
# 是「買方視角的淨成本」(買方=正、賣方=負，見 _condition_met 說明)，這裡的
# le/ge 就是直接比較這兩個數字，跟買賣方向本身無關：
#   CONDITION_LE：total_cost <= threshold —— 現在的成本比預期「更好」才送
#       (買方付得比預期少、賣方收得比預期多)，適合新倉「追一個有利的價
#       格」。這是原本唯一支援、寫死的行為。
#   CONDITION_GE：total_cost >= threshold —— 反過來，成本比預期「更差」也
#       送，適合平倉「停損出場」(價格往不利的方向走也要出場，不是隨便亂
#       跳都送——只有跳到比設定門檻更差才送)。
# 使用者要選的是「真實成交價」跟委託價的 ≦/≧ 關係 (order_entry_widget.py
# 的下拉選單)，那個符號在買方/賣方時對應到這裡的 le/ge 會相反(因為賣方
# 那一腳算成本時用的是 -bid)，換算邏輯在 order_entry_widget.py 做，這裡
# 只認 le/ge 兩個值，不處理買賣方向轉換。
CONDITION_LE = "le"
CONDITION_GE = "ge"


@dataclass
class OrderLeg:
    symbol: str
    buy: bool
    call_put: Optional[str] = None  # "C"/"P"，僅供 UI 顯示
    strike: Optional[float] = None


@dataclass
class OrderRecord:
    id: str
    kind: str  # "outright" | "duplex"
    legs: List[OrderLeg]
    price: float
    qty: int
    tif: int
    new_close: int = NEW_POSITION
    net_buyer: bool = True  # 這筆委託整體是「買方(付權利金)」還是「賣方(收權利金)」；
                             # 複式單兩腳為了配合期交所編碼規則會依履約價高低決定誰放
                             # legs[0]/legs[1]，買賣方向因此不一定對得上 legs[0].buy，
                             # 限價條件判斷 (_condition_met) 一定要看這個欄位，不能看
                             # legs[0].buy。裸買賣則等於 legs[0].buy 本身。
    auto_retry: bool = False
    condition_op: str = CONDITION_LE  # 連續IOC 限價條件比較方向，見上面 CONDITION_LE/GE 說明
    status: str = STATUS_STAGED
    seq_no: Optional[str] = None
    retry_count: int = 0
    awaiting_report: bool = False  # 上一次 _send_once 送出後，這筆委託的終態回報
                                    # (Type=D/C 或 S/OrderErr=Y) 還沒回來——回來之前
                                    # 不能再送下一張。沒有這個鎖的話，quote 事件觸發
                                    # 頻率如果快過送單→回報的往返時間，會在同一張還
                                    # 沒收到終態回報前又送出下一張，兩張都可能各自成
                                    # 交，變成使用者要的 1 口變成 2 口(實際發生過：兩
                                    # 個不同委託書號、同時間、都顯示全部成交1口)。
                                    # Type=N(委託確認，交易所已受理但還沒撮合結果)不
                                    # 是終態，不能清這個鎖。
    last_report: Optional[dict] = None
    error_msg: Optional[str] = None
    fill_price: Optional[str] = None
    fill_qty: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def label(self) -> str:
        if self.kind == "outright":
            leg = self.legs[0]
            return f"{leg.symbol} {'買' if leg.buy else '賣'}"
        leg1, leg2 = self.legs
        return f"{leg1.symbol}{'買' if leg1.buy else '賣'} / {leg2.symbol}{'買' if leg2.buy else '賣'}"


class OrderBookManager(QObject):
    records_changed = pyqtSignal()  # 任何一筆的內容變了 (新增/刪除/狀態更新)，UI 重新整個表格
    order_book_error = pyqtSignal(str)         # 送出/改價/減量/刪單「當下」失敗 (SendXxxOrder 呼叫本身 retCode!=0)
    record_rejected = pyqtSignal(str, str)     # (委託描述, 錯誤訊息)：委託送出後被交易所回報退單/失敗，一定要跳出來，不能只寫在表格裡沒人看到

    def __init__(self, order_client: CapitalOrderClient, quote_client: CapitalQuoteClient):
        super().__init__()
        self._order_client = order_client
        self._order_client.order_report.connect(self._on_report)
        self._order_client.order_failed.connect(self.order_book_error.emit)

        self._quote_client = quote_client
        self._quote_client.quote_updated.connect(self._on_quote_updated)
        self._latest_quotes: Dict[str, dict] = {}

        self._records: Dict[str, OrderRecord] = {}
        self._last_get_order_report_at = 0.0
        self._load_persisted()
        # 一定要在 _load_persisted() 之後才接，不然載入當下逐筆塞進
        # self._records 不會經過 emit，但也不需要——widget 建構子自己會
        # 呼叫一次 _refresh()，第一次畫面本來就會畫出載入好的內容(見
        # order_book_widgets.py)。之後才接上，任何異動都自動存檔，不用在
        # 每個會改到 _records 的地方各自補一行存檔呼叫。
        self.records_changed.connect(self._persist)

    @property
    def records(self) -> List[OrderRecord]:
        return list(self._records.values())

    def get_quote(self, symbol: str) -> Optional[dict]:
        """回傳目前快取的最新報價 (bid/ask)。_on_quote_updated 是「不管
        這檔商品跟哪一筆委託有沒有關係，quote_client 推送過就存」，所以
        只要 T字報價表格訂閱過這個商品 (main_window 用的是同一個
        quote_client 實例)，這裡就查得到——下單面板用這個查價差單另一腳
        的即時報價，自動算出淨權利金現價，不用使用者自己心算。查不到回
        傳 None (從沒收過這檔報價，或還沒訂閱)。"""
        return self._latest_quotes.get(symbol)

    # --------------------------------------------------------------- 本地保存
    def _persist(self) -> None:
        order_book_store.save([asdict(record) for record in self._records.values()])

    def _load_persisted(self) -> None:
        """*** 重開機後不能直接把 STATUS_RETRYING 原封不動地恢復 ***：連
        續IOC監看靠的是「這個 session 一直訂閱著報價」(_on_quote_updated
        收到就檢查 _maybe_fire)，重開機後這個訂閱從零開始，就算把狀態原
        樣搬回來，只要剛好有其他地方(例如T字報價表)也訂閱了同一個商品、
        任何一次報價跳動都可能在使用者還沒看過畫面、還沒確認這筆單現在
        到底該不該繼續追價之前，就把新單默默送出去——這是絕對不能接受的
        行為。所以載入時把「原本在自動監看中」的單一律凍結成
        STATUS_PAUSED，要使用者自己按「恢復」才會重新開始監看送單；但
        還是先把這幾腳的報價訂閱補回去(訂閱本身只是被動收報價，沒有送單
        風險)，不然使用者按恢復的當下手上完全沒有報價快取，會直接卡住送
        不出去。STATUS_STAGED(還沒送出過，未曾對交易所產生任何動作)跟
        STATUS_LIVE(沒有自動重送行為綁在這個狀態上)原樣載入沒有風險。"""
        symbols_to_resubscribe: List[str] = []
        for raw in order_book_store.load():
            try:
                raw = dict(raw)
                raw["legs"] = [OrderLeg(**leg) for leg in raw["legs"]]
                record = OrderRecord(**raw)
            except (TypeError, KeyError) as exc:
                print(f"[OrderBook] 本地保存的委託格式對不上目前的欄位定義，這筆跳過不載入: {exc}")
                continue
            if record.status in (STATUS_RETRYING, STATUS_PAUSED):
                record.status = STATUS_PAUSED
                # 上一個 session 若剛好卡在「送出但終態回報還沒回來」就關
                # 程式，這個鎖永遠不會有回報來解——重開機後這個 session
                # 沒送過任何一張，不可能還有本地未完成的送單在飛，清掉避
                # 免使用者按恢復後被卡死。
                record.awaiting_report = False
                symbols_to_resubscribe.extend(leg.symbol for leg in record.legs)
            self._records[record.id] = record
        if symbols_to_resubscribe:
            self._quote_client.subscribe(symbols_to_resubscribe)

    # --------------------------------------------------------------- 暫存
    def stage_outright(
        self, symbol: str, buy: bool, price: float, qty: int,
        tif: int = TIF_ROD, new_close: int = NEW_POSITION, auto_retry: bool = False,
        call_put: Optional[str] = None, strike: Optional[float] = None,
        condition_op: str = CONDITION_LE,
    ) -> str:
        if auto_retry and tif == TIF_ROD:
            raise ValueError("ROD 不能連續重送 (會一直停在委託簿上疊單)，只有 IOC/FOK 可以")
        record = OrderRecord(
            id=str(uuid.uuid4()),
            kind="outright",
            legs=[OrderLeg(symbol=symbol, buy=buy, call_put=call_put, strike=strike)],
            price=price, qty=qty, tif=tif, new_close=new_close, net_buyer=buy, auto_retry=auto_retry,
            condition_op=condition_op,
        )
        self._records[record.id] = record
        self.records_changed.emit()
        return record.id

    def stage_duplex(
        self, symbol1: str, buy1: bool, symbol2: str, buy2: bool,
        net_price: float, qty: int, tif: int = TIF_IOC,
        new_close: int = NEW_POSITION, auto_retry: bool = True,
        call_put1: Optional[str] = None, strike1: Optional[float] = None,
        call_put2: Optional[str] = None, strike2: Optional[float] = None,
        net_buyer: Optional[bool] = None,
        condition_op: str = CONDITION_LE,
    ) -> str:
        if tif not in (TIF_IOC, TIF_FOK):
            raise ValueError("價差複式單只能用 IOC 或 FOK")
        # symbol1/buy1 已經依期交所編碼規則(履約價高低)排好順序，跟「整
        # 體是買方還是賣方」是兩回事，呼叫端(order_entry_widget.py)一定要明確
        # 傳 net_buyer，不能靠 buy1 反推——沒傳的話退回舊行為(等於 buy1)
        # 只是保底，不應該被依賴。
        record = OrderRecord(
            id=str(uuid.uuid4()),
            kind="duplex",
            legs=[
                OrderLeg(symbol=symbol1, buy=buy1, call_put=call_put1, strike=strike1),
                OrderLeg(symbol=symbol2, buy=buy2, call_put=call_put2, strike=strike2),
            ],
            price=net_price, qty=qty, tif=tif, new_close=new_close,
            net_buyer=buy1 if net_buyer is None else net_buyer, auto_retry=auto_retry,
            condition_op=condition_op,
        )
        self._records[record.id] = record
        self.records_changed.emit()
        return record.id

    def discard_staged(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is not None and record.status == STATUS_STAGED:
            del self._records[record_id]
            self.records_changed.emit()

    def edit_staged(self, record_id: str, price: Optional[float] = None, qty: Optional[int] = None) -> None:
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
        if record.auto_retry:
            # 不直接送單：進入監看狀態，訂閱這幾腳的報價，價格滿足限價
            # 條件才真的送出 (可能訂閱後馬上就滿足，_maybe_fire 裡面會
            # 立刻用目前手上的快取報價檢查一次)。
            self._quote_client.subscribe([leg.symbol for leg in record.legs])
            record.status = STATUS_RETRYING
            self.records_changed.emit()
            self._maybe_fire(record)
        else:
            self._send_once(record)
            record.status = STATUS_LIVE
            self.records_changed.emit()

    def _send_once(self, record: OrderRecord) -> None:
        record.retry_count += 1
        record.awaiting_report = True
        # auto_retry(連續IOC) 這條路徑會被報價更新高頻觸發(_maybe_fire)，
        # 一定要用非同步(is_async=True)，不然重送愈頻繁 UI 卡愈久，會重
        # 現先前「暫停/刪除按了沒反應」的問題(PyQt 沒有安全的方式把這個
        # COM 呼叫丟到背景執行緒，見 capital_order_client.py 的說明)。
        # 一次性送單(auto_retry=False，使用者按送出的單一動作)才用同
        # 步，換取送出當下就能拿到 SeqNo，讓 ROD 掛單改價/刪單不用等回
        # 報就能用；非同步模式沒有 SeqNo 可拿(回傳 None)，這是預期行為。
        is_async = record.auto_retry
        if record.kind == "outright":
            leg = record.legs[0]
            seq_no = self._order_client.send_option_order_once(
                leg.symbol, leg.buy, record.price, record.qty, record.tif, record.new_close,
                is_async=is_async,
            )
        else:
            leg1, leg2 = record.legs
            seq_no = self._order_client.send_duplex_order_once(
                leg1.symbol, leg1.buy, leg2.symbol, leg2.buy,
                record.price, record.qty, record.tif, record.new_close,
                is_async=is_async,
            )
        if seq_no:
            record.seq_no = seq_no

    # --------------------------------------------------------------- 條件監看
    def _on_quote_updated(self, symbol: str, data: dict) -> None:
        self._latest_quotes[symbol] = data
        for record in self._records.values():
            if record.status == STATUS_RETRYING and any(leg.symbol == symbol for leg in record.legs):
                self._maybe_fire(record)

    def _condition_met(self, record: OrderRecord) -> bool:
        """把每一腳算成「現在成交要付出的成本」(買=用賣價買進、賣=用買
        價賣出記成負的成本)，加總起來就是「現在能不能用這個淨價成交」。
        買方限價是「最多付這麼多」，賣方限價是「最少收這麼多」，兩者統一
        寫成 cost <= threshold 一個比較式：
            買方(leg1.buy=True)：threshold = price (最多付 price)
            賣方(leg1.buy=False)：threshold = -price (賣方希望 cost 是
                負的、絕對值 >= price，等價於 cost <= -price)
        """
        total_cost = 0.0
        for leg in record.legs:
            quote = self._latest_quotes.get(leg.symbol)
            if quote is None:
                return False
            bid, ask = quote.get("bid"), quote.get("ask")
            if not bid or not ask or bid <= 0 or ask <= 0:
                return False
            total_cost += ask if leg.buy else -bid
        threshold = record.price if record.net_buyer else -record.price
        if record.condition_op == CONDITION_GE:
            return total_cost >= threshold
        return total_cost <= threshold

    def _maybe_fire(self, record: OrderRecord) -> None:
        if record.status != STATUS_RETRYING:
            return
        if record.awaiting_report:
            # 上一張還沒收到終態回報，不能再送——見 OrderRecord.awaiting_report
            # 的說明，這是「連續IOC多成交一口」那個 bug 的防線。
            return
        if not self._condition_met(record):
            return
        self._send_once(record)
        self.records_changed.emit()

    # --------------------------------------------------------------- 連續IOC 管理
    def pause_retry(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None or record.status != STATUS_RETRYING:
            return
        record.status = STATUS_PAUSED
        self.records_changed.emit()

    def resume_retry(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None or record.status != STATUS_PAUSED:
            return
        record.status = STATUS_RETRYING
        self.records_changed.emit()
        self._maybe_fire(record)  # 恢復當下價格可能已經滿足，立刻檢查一次

    def change_condition(self, record_id: str, price: Optional[float] = None, qty: Optional[int] = None) -> None:
        """改連續IOC的限價/口數。IOC 瞬間成交或死亡，交易所端沒有「掛著
        的單」可以改，這裡只是更新之後 _condition_met/_send_once 要用的
        參數，改完立刻用新條件檢查一次 (可能新價格馬上就滿足)。"""
        record = self._records.get(record_id)
        if record is None:
            return
        if price is not None:
            record.price = price
        if qty is not None:
            record.qty = qty
        self.records_changed.emit()
        if record.status == STATUS_RETRYING:
            self._maybe_fire(record)

    def delete(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None:
            return
        if record.status == STATUS_LIVE and record.seq_no and record.tif == TIF_ROD:
            self._order_client.cancel_order_by_seq_no(record.seq_no)
        del self._records[record_id]
        self.records_changed.emit()

    # --------------------------------------------------------------- ROD 掛單管理
    def amend_price(self, record_id: str, new_price: float) -> None:
        record = self._records.get(record_id)
        if record is None or not record.seq_no:
            return
        self._order_client.correct_price_by_seq_no(record.seq_no, new_price, record.tif)

    def amend_qty(self, record_id: str, decrease_by: int) -> None:
        record = self._records.get(record_id)
        if record is None or not record.seq_no:
            return
        self._order_client.decrease_order_by_seq_no(record.seq_no, decrease_by)

    def cancel(self, record_id: str) -> None:
        record = self._records.get(record_id)
        if record is None or not record.seq_no:
            return
        self._order_client.cancel_order_by_seq_no(record.seq_no)

    # --------------------------------------------------------------- 回報比對
    @staticmethod
    def _parse_strike(value) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _match_record(self, report: dict) -> Optional[OrderRecord]:
        # *** 用履約價比對，不是用商品代碼字串比對 ***：用真實回報字串逐
        # 欄位核對過 (見 capital_order_client.py 的 _REPORT_FIELDS 說
        # 明)，複式單回報裡沒有跟 leg.symbol 完全一樣的商品代碼欄位可以
        # 直接比對 (原本比對的 com_id1 欄位其實是兩腳合併的顯示字串，根
        # 本比不出來)，但 strike_price1/strike_price2 這兩個欄位是分開
        # 的、乾淨的數字，逐欄位核對過對得起來，用這個比對可靠得多。
        strike1 = self._parse_strike(report.get("strike_price1"))
        strike2 = self._parse_strike(report.get("strike_price2"))
        report_strikes = {s for s in (strike1, strike2) if s is not None}
        if not report_strikes:
            return None
        for record in self._records.values():
            if record.status in TERMINAL_STATUSES:
                continue
            record_strikes = {leg.strike for leg in record.legs if leg.strike is not None}
            if not record_strikes:
                continue
            if record.kind == "outright" and len(record_strikes) == 1:
                if record_strikes <= report_strikes:
                    return record
            elif record.kind == "duplex" and record_strikes == report_strikes:
                return record
        return None

    def _on_report(self, report: dict) -> None:
        record = self._match_record(report)
        if record is None:
            # 收到回報但配對不到任何本地委託——代表回報連線本身沒問題
            # (OnNewData 真的有觸發)，是 _match_record 的比對條件或
            # _REPORT_FIELDS 欄位索引對不上，不是連線問題。跟完全沒印出
            # [OnNewData] 這行(代表連線根本沒建立)要分開看。
            print(f"[OrderBook] 回報配對失敗，找不到對應委託：strike_price1={report.get('strike_price1')} "
                  f"strike_price2={report.get('strike_price2')} "
                  f"目前追蹤中的委託：{[(r.kind, [(l.symbol, l.strike) for l in r.legs], r.status) for r in self._records.values() if r.status not in TERMINAL_STATUSES]}")
            return

        record.last_report = report
        seq_no = report.get("seq_no")
        if seq_no:
            record.seq_no = seq_no

        report_type = report.get("type")
        order_err = report.get("order_err")

        if report_type == "D":
            record.awaiting_report = False
            record.status = STATUS_FILLED
            record.fill_price = report.get("price1") or report.get("price")
            record.fill_qty = report.get("qty")
        elif order_err == "Y" or report_type == "S":
            record.awaiting_report = False
            record.status = STATUS_REJECTED
            record.error_msg = report.get("error_msg") or report.get("raw")
            self.record_rejected.emit(record.label(), record.error_msg)
        elif report_type == "C":
            record.awaiting_report = False
            if not record.auto_retry:
                record.status = STATUS_CANCELLED
            # auto_retry 的情況：IOC 沒成交被取消是正常現象，留在
            # STATUS_RETRYING，立刻用目前快取的報價再檢查一次條件——可能
            # 只是排隊搓合輸掉、價格其實還在，值得馬上再試。這裡已經把
            # awaiting_report 清掉了，_maybe_fire 才可能真的送出下一張。
            elif record.status == STATUS_RETRYING:
                self._maybe_fire(record)
        # Type=='N'(委託確認，交易所已受理但還沒有撮合結果)：不是終態，
        # awaiting_report 保持 True，避免下一次報價跳動又送出下一張。

        self.records_changed.emit()

    # --------------------------------------------------------------- 既有委託查詢
    def can_fetch_existing_orders(self) -> bool:
        return time.time() - self._last_get_order_report_at >= GET_ORDER_REPORT_COOLDOWN_SEC

    def fetch_existing_orders(self) -> str:
        """*** 阻塞式呼叫，官方文件要求至少間隔 5 秒 ***。呼叫端(UI)自己
        負責在冷卻中不要呼叫 (can_fetch_existing_orders)，也自己負責先讓
        視窗畫出「查詢中...」再呼叫這個方法 (例如包一層
        QTimer.singleShot(0, ...))，這裡不做真正的背景執行緒 (COM 物件
        跨執行緒呼叫需要正確 marshal，這次先不做)。"""
        self._last_get_order_report_at = time.time()
        return self._order_client.get_order_report(ORDER_REPORT_FORMAT_ALL)

    # --------------------------------------------------------------- 頻率保護
    def set_max_qty(self, market_type: int, max_qty: int) -> Optional[str]:
        return self._order_client.set_max_qty(market_type, max_qty)

    def set_max_count(self, market_type: int, max_count: int) -> Optional[str]:
        return self._order_client.set_max_count(market_type, max_count)

    def unlock_order(self, market_type: int) -> Optional[str]:
        return self._order_client.unlock_order(market_type)
