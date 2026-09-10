"""
未平倉部位彙總＋分組管理。三個輸入來源 reconcile 成畫面看到的部位列表：

  1. **Broker 真相**：GetOpenInterestGW 部位 (透過
     CapitalOrderClient.open_interest_rows 訊號一次拿到這次查詢的完整清
     單)，已經是交易所 netting 過的結果，數量/均價以這個為準。市場別=
     "TM" 的列是複式單合併列 (商品欄位格式「履約價1/履約價2」，見
     app/models/contracts.py 開頭的說明)。
  2. **本地下單紀錄**：OrderBookManager 已成交的複式單，只在當前 App
     session 有效 (OrderBookManager 沒有跨重啟持久化)，用來輔助標示「這
     兩腳原本是同一次複式單送出的」——只有 broker 沒有回傳 TM 合併列(拆
     成單腳分開回報)時才需要靠這個線索自動配對；TM 合併列本身已經是
     broker 端配對好的，不需要這個線索。
  3. **使用者手動分組覆蓋**：position_groups_store.py，持久化，優先權最
     高，每次 refresh() 重新 reconcile 之後都會覆蓋回去。

*** 已知未驗證、刻意不猜的風險 ***
GetOpenInterestGW 的「買賣別」欄位真正編碼方式 (見
capital_order_client.py 開頭關於 OPEN_INTEREST_FIELDS 的說明) 還沒有拿真
實資料核對過。這裡遇到無法辨識的值不會硬猜一個方向——Position.buy 會是
None，UI 必須把這種列明顯標示出來，不能悄悄當成某個方向處理(選擇權買賣
方向猜錯，損益方向會完全相反，比不顯示更危險)。

TM 合併列(複式單)的方向：*** 2026-09-10 曾經誤判成「兩腳同方向」，已經
被使用者當面糾正——這幾筆部位「都是價差單」，兩腳一定是一買一賣(不然就
不叫價差)，同方向那版是錯的，已經改回「一買一賣」。*** GetOpenInterestGW
只回傳一個 buy_sell 欄位套用在整組，商品欄位「履約價1/履約價2」裡，
legs[0](履約價1，斜線前面)的方向永遠跟 Position.buy 相反、legs[1](履約
價2，斜線後面)永遠跟 Position.buy 相同——用真實部位手算驗證過(put價差
45900/46000 收24點、call價差47900/47800 收16.5點，兩筆都算出跟「100點
寬價差收X點權利金」的標準公式(width-credit)*multiplier吻合的有限風險最
大虧損，不是無上限/無下限)。avg_cost(均價)是整組合計的淨權利金，只記一
次(記在 legs[0]，legs[1] 記0，見 Position.payoff_legs())，不是兩腳各自
的權利金。這個結論只驗證過「賣方」的樣本，還沒有「買方」的真實資料核對
對稱的另一半，如果之後遇到跟這裡假設不符的真實回傳資料，要重新核對，不
要預設一定通用。
"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from app.models.capital_order_client import CapitalOrderClient
from app.models.contracts import Contract, parse_combo_symbol, parse_symbol
from app.models.order_book import OrderBookManager, STATUS_FILLED, TERMINAL_STATUSES
from app.models.capital_quote_client import CapitalQuoteClient
from app.services import position_groups_store

OPEN_INTEREST_QUERY_COOLDOWN_SEC = 5.0  # 文件沒有明講這支的最低間隔，比照 GetOrderReport 保守處理

UNGROUPED_ID = "__ungrouped__"  # 固定的「未分組」虛擬群組 id，不會被使用者刪除

# 買賣別欄位可能的編碼——目前只是「猜測清單」不是確認過的對照表，任何一
# 個值只要不在這個清單裡就整列標成 buy=None，不要因為清單漏了什麼值就讓
# 使用者以為系統判斷得出方向。
_BUY_SELL_GUESSES = {
    "0": True, "1": False,
    "B": True, "S": False,
    "買": True, "賣": False,
}


@dataclass
class Position:
    legs: List[Contract]       # 一般部位 1 個，TM 複式單合併列 2 個 (見上方模組說明)
    buy: Optional[bool]        # None = 買賣別欄位無法判讀，畫面要標示不明，不能猜
    buy_sell_raw: str
    qty: int
    avg_cost: float
    market_type: str
    raw: dict

    @property
    def symbol_key(self) -> str:
        """跟本地委託紀錄/手動分組共用的識別碼。TM 合併列用兩腳 symbol
        排序後接起來，維持穩定(不受兩個履約價在字串裡的先後順序影響)。"""
        if len(self.legs) == 1:
            return self.legs[0].symbol
        return "+".join(sorted(leg.symbol for leg in self.legs))

    @property
    def is_combo(self) -> bool:
        return len(self.legs) > 1

    def payoff_legs(self) -> Optional[List[Tuple[Contract, bool, float]]]:
        """把這筆部位攤平成 payoff.py 用得到的「(Contract, buy, premium)」
        清單。self.buy 是 None(買賣別欄位無法判讀) 就回傳 None，呼叫端要
        整筆跳過，不能瞎猜方向硬算。

        複式單合併列(TM)兩腳方向：見本檔案開頭「TM 合併列(複式單)的方
        向」說明——兩腳一買一賣(價差組合)，legs[0](履約價1)方向跟
        self.buy 相反、legs[1](履約價2)跟 self.buy 相同。avg_cost 是整
        組合計的淨權利金，只記在「方向跟 self.buy 相同」的那一腳(這裡是
        legs[1]，legs[0] 記0)——這不是隨便選的，是跟
        payoff_chart_widget.py `_pending_legs` 處理下單匣淨價同一套規
        則：淨價只能記在跟「淨買方/淨賣方」方向一致的那一腳，記錯腳會導
        致兩腳的內含價值沒有正確互相抵消，算出無上限/無下限這種明顯不對
        的極端值(2026-09-10 實測過這個錯誤，見對話紀錄)。"""
        if self.buy is None:
            return None
        if not self.is_combo:
            leg = self.legs[0]
            return [(leg, self.buy, self.avg_cost)]
        leg1, leg2 = self.legs
        return [(leg1, not self.buy, 0.0), (leg2, self.buy, self.avg_cost)]


@dataclass
class PositionGroup:
    group_id: str
    name: str
    color: str
    positions: List[Position] = field(default_factory=list)


def current_price(manager: "PositionManager", position: Position) -> Optional[float]:
    """單腳部位直接顯示該合約現價。複式單(TM合併列)不能只顯示其中一腳的
    成交價——之前這樣做過，數字(例如207點)完全沒辦法跟均價(24點,整組淨權
    利金)放在一起比較。

    改成顯示「淨價差現價」= legs[1]現價 - legs[0]現價，跟 avg_cost/均價
    用同一套「legs[1] 扛淨權利金、legs[0] 反向抵消」慣例算出來的(見
    Position.payoff_legs() 的說明)，這樣現價才能直接拿來跟均價比較(現價
    低於均價=賣方部位還在賺，反之則已經虧)。任一腳報價還沒訂閱到就回
    None，不要用單腳報價湊出一個誤導的數字。

    這支函式是「畫面上的損益欄位」跟「自動平倉判斷」共用的唯一現價來
    源，故意放在 positions.py 而不是 position_widgets.py，避免兩邊各算
    一份、數字對不起來(自動下單這種會動到真錢的功能尤其不能有兩套算
    法)。"""
    if not position.is_combo:
        return manager.latest_price(position.legs[0].symbol)
    leg0_price = manager.latest_price(position.legs[0].symbol)
    leg1_price = manager.latest_price(position.legs[1].symbol)
    if leg0_price is None or leg1_price is None:
        return None
    return leg1_price - leg0_price


def pnl_points(position: Position, price: Optional[float]) -> Optional[float]:
    """目前浮動損益，換算成「點數」(不乘口數、不乘乘數)——賣方「現價<均
    價」賺，買方相反。跟 position_pnl() 共用同一套方向判斷，這裡只回傳點
    數版本，給自動平倉的門檻比較用(使用者輸入的門檻本來就是點數)；乘上
    口數/乘數才是畫面上顯示的新台幣損益，見 position_pnl()。

    buy=None(買賣別欄位無法判讀)或沒有現價就回 None，呼叫端不能瞎猜方
    向硬算。"""
    if position.buy is None or price is None:
        return None
    return (price - position.avg_cost) if position.buy else (position.avg_cost - price)


def position_pnl(position: Position, price: Optional[float]) -> Optional[float]:
    """目前浮動損益(新台幣)：直接拿「現價」對比「均價」——不是拿加權指數
    現貨價套履約內含價值公式。

    *** 這裡本來是用內含價值公式，已知有問題(2026-09-10 使用者實際回報
    過)：內含價值公式忽略時間價值，只看「如果現在到期會怎樣」，賣方部位
    即使現價已經比均價貴很多(對賣方不利、代表要付更多權利金才能回補)，
    只要還沒實質跌破/漲破履約價，內含價值算出來還是0，畫面照樣顯示獲利
    封頂的數字——使用者的真實例子：Call價差均價16.5、現價24(現價>均價，
    賣方應該是虧損)，但內含價值法算出來卻是+1650(獲利封頂)，兩個欄位互
    相矛盾。改成直接比現價，賣方「現價<均價」賺、「現價>均價」賠，買方
    相反，永遠跟現價欄位一致，不會再打架。

    這犧牲的是「多算了時間價值，不是單純的到期內含價值」，但這才是券商
    一般認知的「浮動損益」(比較現在市價 vs 進場成本)，履約內含價值那套
    邏輯留給 payoff_chart_widget.py 的到期損益圖(那裡問的是不同的問題：
    「如果現在到期會怎樣」，不是「現在的浮動損益是多少」)。"""
    diff = pnl_points(position, price)
    if diff is None:
        return None
    return diff * position.qty * position.legs[0].multiplier


def _parse_buy_sell(raw_value: str) -> Optional[bool]:
    return _BUY_SELL_GUESSES.get(raw_value)


def _row_to_position(row: dict) -> Optional[Position]:
    symbol = row.get("symbol", "")
    if not symbol:
        print(f"[PositionManager] 這一列沒有 symbol 欄位，丟掉不顯示，raw={row.get('raw')}")
        return None
    try:
        legs = parse_combo_symbol(symbol) if "/" in symbol else [parse_symbol(symbol)]
    except ValueError as exc:
        print(f"[PositionManager] 無法解析商品代碼 {symbol!r}，這一列先丟掉不顯示: {exc}")
        return None
    try:
        qty = int(row.get("open_qty", "0") or "0")
        avg_cost = float(row.get("avg_cost", "0") or "0")
    except ValueError:
        print(f"[PositionManager] 未平倉部位/均價欄位不是數字，raw={row.get('raw')}")
        return None
    return Position(
        legs=legs,
        buy=_parse_buy_sell(row.get("buy_sell", "")),
        buy_sell_raw=row.get("buy_sell", ""),
        qty=qty,
        avg_cost=avg_cost,
        market_type=row.get("market_type", ""),
        raw=row,
    )


class PositionManager(QObject):
    positions_changed = pyqtSignal()   # 整批重建，不做逐列 diff (比照 OrderBookManager.records_changed 的一貫風格)
    query_failed = pyqtSignal(str)

    def __init__(self, order_client: CapitalOrderClient, order_book_manager: OrderBookManager,
                 quote_client: CapitalQuoteClient):
        super().__init__()
        self._order_client = order_client
        self._order_client.open_interest_rows.connect(self._on_open_interest_rows)
        self._order_client.open_interest_failed.connect(self._on_query_failed)
        self._order_client.open_interest_query_status.connect(self._on_query_status)

        self._order_book_manager = order_book_manager
        self._quote_client = quote_client
        self._quote_client.quote_updated.connect(self._on_quote_updated)

        self._positions: Dict[str, Position] = {}   # symbol_key -> Position
        self._last_query_at = 0.0
        self._subscribed_symbols: set = set()
        self._latest_quotes: Dict[str, dict] = {}    # symbol -> {"bid":, "ask":, "last":, ...} (CapitalQuoteClient.quote_updated 的資料)
        self._underlying_price: Optional[float] = None  # 加權指數現貨價，算選擇權履約後內含價值(浮動損益)要用這個，不是選擇權自己的成交價

    @property
    def positions(self) -> List[Position]:
        return list(self._positions.values())

    @property
    def underlying_price(self) -> Optional[float]:
        return self._underlying_price

    def set_underlying_price(self, price: float) -> None:
        """main_window.py 收到加權指數(TSEA)即時成交價時呼叫。*** 這裡曾
        經誤用選擇權自己的成交價當這個角色去算履約內含價值(max(S-履約
        價,0))——選擇權的成交價(例如248點)跟履約價(例如45900)完全不同量
        級，套進去算出來的損益是垃圾數字，只是因為當時複式單部位被
        is_combo 擋掉沒顯示、單腳部位剛好還沒出現才沒被發現。現在改成用
        真正的加權指數現貨價，跟 payoff_chart_widget.py 用同一個來源。***"""
        self._underlying_price = price
        self.positions_changed.emit()

    # ------------------------------------------------------------------ 查詢
    def can_refresh(self) -> bool:
        return time.time() - self._last_query_at >= OPEN_INTEREST_QUERY_COOLDOWN_SEC

    def refresh(self) -> None:
        """呼叫端(UI)自己負責在冷卻中不要呼叫 (can_refresh)。非同步查詢。

        *** 2026-09-10 修正：原本設計是「逐列收集，等 OnOpenInterestGWStatus
        才整批換掉 self._positions」，但實測 OnOpenInterestGWStatus 會在
        OnOpenInterestJson(真正的部位資料) 之前就先觸發——用真實帳號資料
        (使用者提供的 console log) 核對過，status 事件到的時候資料都還
        沒進來，導致每次都拿空集合覆蓋掉，這是「成交後查不到部位」的根
        因。現在 open_interest_rows 訊號一次就帶「這次查詢的完整部位清
        單」(見 capital_order_client.py 的 _handle_open_interest)，收到
        就整批直接換掉 self._positions，不再依賴 status 事件的時機；
        status 事件只拿來判斷查詢本身有沒有失敗。***"""
        self._last_query_at = time.time()
        self._order_client.query_open_interest()

    def _on_open_interest_rows(self, rows: List[dict]) -> None:
        positions: Dict[str, Position] = {}
        for row in rows:
            position = _row_to_position(row)
            if position is None:
                continue
            positions[position.symbol_key] = position
        print(f"[PositionManager] 這次查詢收到 {len(positions)} 筆部位")
        self._positions = positions
        self._reconcile_manual_overrides()
        self._resubscribe_quotes()
        self.positions_changed.emit()

    def _on_query_status(self, n_query_status: int, bstr_error_msg: str) -> None:
        # 印出來是排查「查完卻看不到部位」的依據——這個事件本身不帶部位
        # 資料，只代表查詢請求本身成功/失敗，不能拿來判斷資料收完了沒
        # (見 refresh() 的說明)。
        print(f"[OnOpenInterestGWStatus] n_query_status={n_query_status} msg={bstr_error_msg!r}")
        if n_query_status != 0:
            self.query_failed.emit(bstr_error_msg or "未平倉查詢失敗(無訊息)")

    def _on_query_failed(self, message: str) -> None:
        self.query_failed.emit(message)

    # ------------------------------------------------------------------ 分組
    def _auto_group_key(self, position: Position) -> Optional[str]:
        """TM 合併列本身已經是 broker 端配對好的一組，直接用
        symbol_key(兩腳 symbol 排序後接起來) 當自動分組 key，不需要再靠本
        地下單紀錄猜。單腳部位才需要往下查本地複式單成交紀錄。"""
        if position.is_combo:
            return position.symbol_key
        symbol = position.legs[0].symbol
        for record in self._order_book_manager.records:
            if record.kind != "duplex" or record.status != STATUS_FILLED:
                continue
            leg_symbols = {leg.symbol for leg in record.legs}
            if symbol in leg_symbols:
                return "+".join(sorted(leg_symbols))
        return None

    def effective_group_for(self, symbol_key: str, auto_key: Optional[str]) -> str:
        overrides = position_groups_store.get_manual_overrides()
        if symbol_key in overrides:
            return overrides[symbol_key] or UNGROUPED_ID
        return auto_key or UNGROUPED_ID

    def _reconcile_manual_overrides(self) -> None:
        """部位已經歸零(這次查詢結果裡完全沒出現)的 symbol，手動覆蓋變孤
        兒，清掉；不要讓 json 檔案無限累積死掉的分組指定。"""
        current_keys = set(self._positions.keys())
        for symbol_key in list(position_groups_store.get_manual_overrides().keys()):
            if symbol_key not in current_keys:
                position_groups_store.clear_manual_override(symbol_key)

    @property
    def groups(self) -> List[PositionGroup]:
        stored_groups = position_groups_store.list_groups()
        result: Dict[str, PositionGroup] = {
            gid: PositionGroup(group_id=gid, name=info["name"], color=info["color"])
            for gid, info in stored_groups.items()
        }
        ungrouped = PositionGroup(group_id=UNGROUPED_ID, name="未分組", color="#888888")
        for position in self._positions.values():
            auto_key = self._auto_group_key(position)
            gid = self.effective_group_for(position.symbol_key, auto_key)
            target = result.get(gid, ungrouped)
            target.positions.append(position)
        return [g for g in result.values() if g.positions] + ([ungrouped] if ungrouped.positions else [])

    def create_group(self, name: str, color: str) -> str:
        return position_groups_store.create_group(name, color)

    def rename_group(self, group_id: str, name: str) -> None:
        position_groups_store.rename_group(group_id, name)

    def set_group_color(self, group_id: str, color: str) -> None:
        position_groups_store.set_group_color(group_id, color)

    def delete_group(self, group_id: str) -> None:
        position_groups_store.delete_group(group_id)

    def move_to_group(self, symbol_key: str, group_id: Optional[str]) -> None:
        """group_id=None 代表使用者主動取消分組(跟「從沒設定過」不同，寫
        入一筆 None，見 position_groups_store.get_manual_overrides 的說
        明)。"""
        position_groups_store.set_manual_override(symbol_key, group_id)

    # ------------------------------------------------------------------ 下單匣疊加(給損益圖用)
    def pending_legs(self):
        """讀 OrderBookManager 裡還沒到終態的委託(掛單中/監看連續IOC
        中)，攤平成逐腳清單，給損益圖曲線B(部位+下單匣)用。buy=None 的
        腳(理論上不會發生，本地紀錄的 OrderLeg.buy 是使用者下單時就決定
        好的，不是從 broker 猜的)在這裡不會出現，只有 Position 才有
        buy=None 的可能。"""
        legs = []
        for record in self._order_book_manager.records:
            if record.status in TERMINAL_STATUSES:
                continue
            for leg in record.legs:
                legs.append((leg, record.price, record.qty))
        return legs

    # ------------------------------------------------------------------ 現價訂閱(給群組彙總損益用)
    def _resubscribe_quotes(self) -> None:
        wanted = set()
        for position in self._positions.values():
            for leg in position.legs:
                wanted.add(leg.symbol)
        to_add = wanted - self._subscribed_symbols
        to_remove = self._subscribed_symbols - wanted
        if to_add:
            self._quote_client.subscribe(list(to_add))
        if to_remove:
            self._quote_client.unsubscribe(list(to_remove))
        self._subscribed_symbols = wanted

    def latest_price(self, symbol: str) -> Optional[float]:
        quote = self._latest_quotes.get(symbol)
        if not quote:
            return None
        return quote.get("last") or None

    def _on_quote_updated(self, symbol: str, data: dict) -> None:
        # quote_client 是整個 App 共用同一顆(main_window.py:84)，T字報價表
        # /開倉分頁自己也會訂閱一堆跟部位無關的商品，這裡只在「這顆報價
        # 是我們自己持有中的商品」才快取+重繪，不然畫面會被完全無關的商
        # 品跳動洗到一直重建。
        if symbol not in self._subscribed_symbols:
            return
        self._latest_quotes[symbol] = data
        self.positions_changed.emit()

    def shutdown(self) -> None:
        if self._subscribed_symbols:
            self._quote_client.unsubscribe(list(self._subscribed_symbols))
            self._subscribed_symbols = set()
