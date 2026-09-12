"""
未平倉部位彙總＋分組管理。

*** 跟舊版(SKCOM GetOpenInterestGW)最大的差異 ***：IB 的 `ib.positions()`
每一筆都是單一合約(conId)的乾淨部位，方向(多/空)直接看 `position` 欄位
正負號——正=多頭(買方持有)、負=空頭(賣方持有)，這是 IB 官方文件化的行
為，不是像 SKCOM 那樣要猜買賣別欄位怎麼編碼(`_BUY_SELL_GUESSES` 那整套
「猜不出來就顯示 None，不要瞎猜方向」的防禦邏輯，連同 `buy: Optional[bool]`
這個型別，這次全部拿掉——IB 不會給出「看不懂方向」的部位)。

*** IB 不會像 SKCOM 偶爾給的 TM 列那樣，把一組價差兩腳直接合併回報成一
列 ***：每一腳(每個 conId)都是獨立一筆。這裡沒有做「把兩個獨立部位自動
合併成一個複式顯示列」——`Position.legs` 目前恆為 1 個；`is_combo`/
`payoff_legs()` 的兩腳分支保留只是介面形狀(給以後真的要做合併顯示時延
伸)，目前不會被觸發。分組(`_auto_group_key`)還是可以把價差兩腳歸進同一
個 `PositionGroup`——只是畫面上是同一個群組底下兩列，不是合併成一列。

`Position.legs` 現在直接放 IB 的 `Contract`(通常是 `Option`)物件，不再
需要 app/models/contracts.py 那套 TAIFEX 符號字串解析——履約價/買賣權/
乘數都是 IB 給的結構化欄位(`leg.strike`/`leg.right`/`leg.multiplier`)。

symbol_key 改用 `str(contract.conId)`，跟 `ib_quote_client.py` 的
symbol_key 用同一套，才能對得起來查現價。
"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from app.models.ib_client import IBClient
from app.models.ib_quote_client import IBQuoteClient
from app.models.order_book import OrderBookManager, STATUS_FILLED, TERMINAL_STATUSES
from app.services import position_groups_store

UNGROUPED_ID = "__ungrouped__"  # 固定的「未分組」虛擬群組 id，不會被使用者刪除


@dataclass
class Position:
    legs: List[object]     # IB Contract(通常是 Option)物件，目前恆為 1 個
    buy: bool               # True=多頭(買方持有) False=空頭(賣方持有)；直接來自 IB position 正負號，不會是 None
    qty: float
    avg_cost: float
    account: str
    raw: object              # 原始 ib_async.Position 物件，除錯用

    @property
    def symbol_key(self) -> str:
        if len(self.legs) == 1:
            return str(self.legs[0].conId)
        return "+".join(sorted(str(leg.conId) for leg in self.legs))

    @property
    def is_combo(self) -> bool:
        return len(self.legs) > 1

    def payoff_legs(self) -> List[Tuple[object, bool, float]]:
        """把這筆部位攤平成 payoff.py 用得到的「(Contract, buy, premium)」
        清單。IB 給的方向永遠明確，不會有 SKCOM 那種「買賣別欄位無法判
        讀」要整筆跳過的情況。"""
        if not self.is_combo:
            leg = self.legs[0]
            return [(leg, self.buy, self.avg_cost)]
        # 目前恆不會走到這裡(legs 恆為 1 個)，保留只是介面形狀。
        leg1, leg2 = self.legs
        return [(leg1, not self.buy, 0.0), (leg2, self.buy, self.avg_cost)]


@dataclass
class PositionGroup:
    group_id: str
    name: str
    color: str
    positions: List[Position] = field(default_factory=list)


def _leg_multiplier(leg) -> float:
    try:
        return float(leg.multiplier or 100)
    except (TypeError, ValueError):
        return 100.0


def current_price(manager: "PositionManager", position: Position) -> Optional[float]:
    """單腳部位直接顯示該合約現價。任一腳報價還沒訂閱到就回 None，不要
    用單腳報價湊出一個誤導的數字。"""
    if not position.is_combo:
        return manager.latest_price(position.symbol_key)
    leg0_price = manager.latest_price(str(position.legs[0].conId))
    leg1_price = manager.latest_price(str(position.legs[1].conId))
    if leg0_price is None or leg1_price is None:
        return None
    return leg1_price - leg0_price


def pnl_points(position: Position, price: Optional[float]) -> Optional[float]:
    """目前浮動損益，換算成「點數」(不乘口數、不乘乘數)——賣方(buy=False)
    「現價<均價」賺，買方相反。跟 position_pnl() 共用同一套方向判斷。"""
    if price is None:
        return None
    return (price - position.avg_cost) if position.buy else (position.avg_cost - price)


def position_pnl(position: Position, price: Optional[float]) -> Optional[float]:
    """目前浮動損益(美金)：直接拿「現價」對比「均價」，不是拿內含價值公
    式(那個只回答「如果現在到期會怎樣」，不是「現在的浮動損益是多少」，
    這個區分沿用舊版 group益 版本已經修正過的結論，見到期損益圖
    payoff_chart_widget.py)。"""
    diff = pnl_points(position, price)
    if diff is None:
        return None
    return diff * position.qty * _leg_multiplier(position.legs[0])


def _ib_position_to_position(ib_pos) -> Optional[Position]:
    """ib_pos 是 ib_async 的 Position 物件(account, contract, position,
    avgCost)。position==0 代表剛平倉完的殘留列，不顯示。"""
    if ib_pos.position == 0:
        return None
    return Position(
        legs=[ib_pos.contract],
        buy=ib_pos.position > 0,
        qty=abs(ib_pos.position),
        avg_cost=ib_pos.avgCost,
        account=ib_pos.account,
        raw=ib_pos,
    )


class PositionManager(QObject):
    positions_changed = pyqtSignal()   # 整批重建，不做逐列 diff
    query_failed = pyqtSignal(str)     # 目前 ib.positions()/positionEvent 是本地同步讀取，不會失敗；保留訊號給未來需要時用

    def __init__(self, ib_client: IBClient, order_book_manager: OrderBookManager, quote_client: IBQuoteClient):
        super().__init__()
        self._ib = ib_client.ib
        self._ib.positionEvent += self._on_position_event

        self._order_book_manager = order_book_manager
        self._quote_client = quote_client
        self._quote_client.quote_updated.connect(self._on_quote_updated)

        self._positions: Dict[str, Position] = {}
        self._subscribed_contracts: Dict[str, object] = {}  # symbol_key -> Contract，目前訂閱中
        self._latest_quotes: Dict[str, dict] = {}

        self.refresh()

    @property
    def positions(self) -> List[Position]:
        return list(self._positions.values())

    # ------------------------------------------------------------------ 查詢
    def can_refresh(self) -> bool:
        return True  # ib.positions() 是本地同步讀取，沒有 SKCOM 那種查詢間隔限制

    def refresh(self) -> None:
        rows = self._ib.positions()
        positions: Dict[str, Position] = {}
        for ib_pos in rows:
            position = _ib_position_to_position(ib_pos)
            if position is None:
                continue
            positions[position.symbol_key] = position
        self._positions = positions
        self._reconcile_manual_overrides()
        self._resubscribe_quotes()
        self.positions_changed.emit()

    def _on_position_event(self, ib_pos) -> None:
        """ib.positionEvent 是事件驅動、每次某一筆部位變動就觸發一次(不
        是整批)，比群益 GetOpenInterestGW 每次都要整批重查好——直接更
        新/移除對應那一筆就好。"""
        position = _ib_position_to_position(ib_pos)
        key = str(ib_pos.contract.conId)
        if position is None:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position
        self._reconcile_manual_overrides()
        self._resubscribe_quotes()
        self.positions_changed.emit()

    # ------------------------------------------------------------------ 分組
    def _auto_group_key(self, position: Position) -> Optional[str]:
        """IB 不會給合併好的複式列，全部部位都要靠這裡「查本地已成交的
        複式單紀錄」自動配對——這是現在唯一的自動分組來源(不像舊版還有
        「broker 已經幫你合併好」這個分支可以走)。"""
        if position.is_combo:
            return position.symbol_key
        con_id = position.legs[0].conId
        for record in self._order_book_manager.records:
            if record.kind != "duplex" or record.status != STATUS_FILLED:
                continue
            leg_con_ids = {leg.con_id for leg in record.legs}
            if con_id in leg_con_ids:
                return "+".join(str(c) for c in sorted(leg_con_ids))
        return None

    def effective_group_for(self, symbol_key: str, auto_key: Optional[str]) -> str:
        overrides = position_groups_store.get_manual_overrides()
        if symbol_key in overrides:
            return overrides[symbol_key] or UNGROUPED_ID
        return auto_key or UNGROUPED_ID

    def _reconcile_manual_overrides(self) -> None:
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
        position_groups_store.set_manual_override(symbol_key, group_id)

    # ------------------------------------------------------------------ 下單匣疊加(給損益圖用)
    def pending_legs(self):
        legs = []
        for record in self._order_book_manager.records:
            if record.status in TERMINAL_STATUSES:
                continue
            for leg in record.legs:
                legs.append((leg, record.price, record.qty))
        return legs

    # ------------------------------------------------------------------ 現價訂閱(給群組彙總損益用)
    def _resubscribe_quotes(self) -> None:
        wanted: Dict[str, object] = {}
        for position in self._positions.values():
            for leg in position.legs:
                wanted[str(leg.conId)] = leg
        to_add_keys = set(wanted) - set(self._subscribed_contracts)
        to_remove_keys = set(self._subscribed_contracts) - set(wanted)
        if to_add_keys:
            self._quote_client.subscribe([wanted[k] for k in to_add_keys])
        if to_remove_keys:
            self._quote_client.unsubscribe([self._subscribed_contracts[k] for k in to_remove_keys])
        self._subscribed_contracts = wanted

    def latest_price(self, symbol_key: str) -> Optional[float]:
        quote = self._latest_quotes.get(symbol_key)
        if not quote:
            return None
        return quote.get("last")

    def _on_quote_updated(self, symbol_key: str, data: dict) -> None:
        if symbol_key not in self._subscribed_contracts:
            return
        self._latest_quotes[symbol_key] = data
        self.positions_changed.emit()

    def shutdown(self) -> None:
        if self._subscribed_contracts:
            self._quote_client.unsubscribe(list(self._subscribed_contracts.values()))
            self._subscribed_contracts = {}
