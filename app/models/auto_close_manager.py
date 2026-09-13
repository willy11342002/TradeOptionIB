"""
未平倉部位自動平倉／停利停損管理器。規則本身(閾值/動作/口數/新履約價)
是使用者在對話中逐項拍板定案的，這裡只記整體機制設計：

- **判斷「要不要觸發」自己做，不外包給 order_book.py**：因為規則1(整組)
  橫跨兩個不同的複式單、規則5(加開對側裸賣腳)判斷用的是「原本那組價
  差」的價格、但真正送出的是另一個商品，沒辦法套進「一張委託自己的成交
  價條件」這個框架裡，所以統一用一個 watcher(接
  PositionManager.positions_changed，跟畫面的損益欄位算的是同一組函式
  `app.models.positions.current_price`/`pnl_points`，不會兩邊算出不同數
  字)自己判斷門檻有沒有到。
- **平倉/重開直接掛限價單，不做連續IOC重送**：IB 的 BAG combo 可以直接
  掛 LMT+DAY 跡在單子上等成交，跟群益 SKCOM 组合單只能用IOC、必須靠連
  續重送不一樣(這條引擎已經整套從 order_book.py 移除，這裡跟著簡化，不
  再傳 auto_retry/condition_op/TIF_IOC/AUTO_POSITION 這些群益專屬概念)。
- **平倉單成交後才重開**：不是自己維護一套計時器，而是記一筆
  `pending_fill_actions[record_id] = 重開參數`，接
  `OrderBookManager.records_changed`，看到那筆 record 變成
  STATUS_FILLED 才真的送出重開單；被交易所真的拒絕(不是還沒成交)才整條
  規則設 FAILED、跳通知，不會亂猜著重開。
- **App 重啟一律凍結成暫停**：跟 order_book.py 的重啟安全設計同一個考
  量，見 `_load()`。
- **重開/加開新履約價需要現場跟 IB 要 conId**：IB 不像群益是純字串編
  碼、可以無 IO 直接組出商品代碼，換一個新履約價一定要
  `ib.qualifyContractsAsync()` 才拿得到可以下單的合約——`pending_fill_
  actions` 裡只存 symbol/expiry/strike/right 這些 JSON 安全的原始欄位
  (不存 Contract 物件本身，那個沒辦法直接 json.dumps)，真正要送單時
  (`_execute_reopen`/`_open_add_leg`)才重新 qualify 一次。
- **凡是會 qualify 合約的路徑都要是 async**：這支 app 全程只在單一
  asyncio 事件迴圈上跑(不管是舊版 qasync 的迴圈還是新版 NiceGUI/uvicorn
  的迴圈)，同步版 `ib.qualifyContracts()` 內部的
  `loop.run_until_complete()` 在這個架構下一定會撞上「這個事件迴圈已經
  在跑了」——所以整條從 `positions_changed`/`records_changed` 訊號進來、
  到真正送出重開/加開單的呼叫鏈(`_on_positions_changed`→
  `_evaluate_positions`→`_fire_take_profit`/`_fire_stop_loss`→
  `_build_reopen_action`/`_build_new_group_action`/`_open_add_leg`→
  `_assign_group_for_action`；`_check_chained_fills`→`_execute_reopen`)
  都是 async def。規則觸發後的 `rule.status = STATUS_TRIGGERED` 都刻意
  寫在第一個 `await`(qualify)之前，這樣同一個規則不會因為兩次訊號重疊
  觸發而被送兩次重複的單。
- **`_on_positions_changed`/`_check_chained_fills` 用 `spawn()` 接訊號，
  不是舊版的 `@asyncSlot()`**：`app.services.signal.Signal.emit()` 是同
  步呼叫 callback，傳一個 async def 進去只會拿到一個沒人 await 的
  coroutine 物件，什麼都不會執行——用
  `app/services/background_tasks.py` 的 `spawn()` 包一層，把訊號觸發
  的當下排程成一個有強參照保護、不會被 GC 中途回收的 Task(這個模組原本
  用 qasync 的 `@asyncSlot()` 也是為了同一個目的，但 `@asyncSlot()`
  本身就踩在「Task 只有區域變數撐著、slot 一返回就可能被 GC 回收」這個
  陷阱上，`spawn()` 是這支專案已經修過這個陷阱的正確做法，詳見
  `background_tasks.py` 開頭的說明)。
"""
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Union

from app.models.auto_close import (
    PositionRules, ReopenSpec, StopLossRule, TakeProfitRule,
    SL_MODE_ADD_LEG, SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE,
    STATUS_ARMED, STATUS_FAILED, STATUS_PAUSED, STATUS_TRIGGERED, STATUS_UNSET,
)
from app.models.ib_client import IBClient
from app.models.option_utils import build_option, vertical_spread_legs
from app.models.order_book import OrderBookManager, STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED
from app.models.positions import Position, PositionGroup, PositionManager, UNGROUPED_ID, current_price, pnl_points
from app.services import auto_close_store
from app.services.background_tasks import spawn
from app.services.signal import Signal


def _trigger_price(position: Position, threshold_points: float, is_take_profit: bool) -> float:
    """把「點數門檻」換算成絕對限價——這個限價同時是「要不要觸發」的判
    斷基準，也直接拿去當平倉/加開單的限價(不用另外算一個，見模組開頭說
    明)。"""
    if is_take_profit:
        return position.avg_cost + threshold_points if position.buy else position.avg_cost - threshold_points
    return position.avg_cost - threshold_points if position.buy else position.avg_cost + threshold_points


def _reopen_from_dict(d: Optional[dict]) -> Optional[ReopenSpec]:
    return ReopenSpec(**d) if d else None


def _reopen_to_dict(spec: Optional[ReopenSpec]) -> Optional[dict]:
    return asdict(spec) if spec else None


def _tp_from_dict(d: Optional[dict]) -> Optional[TakeProfitRule]:
    if d is None:
        return None
    return TakeProfitRule(
        threshold_points=d["threshold_points"],
        reopen=_reopen_from_dict(d.get("reopen")),
        status=d.get("status", STATUS_UNSET),
        paused_reason=d.get("paused_reason"),
        triggered_at=d.get("triggered_at"),
    )


def _tp_to_dict(rule: Optional[TakeProfitRule]) -> Optional[dict]:
    if rule is None:
        return None
    return {
        "threshold_points": rule.threshold_points,
        "reopen": _reopen_to_dict(rule.reopen),
        "status": rule.status,
        "paused_reason": rule.paused_reason,
        "triggered_at": rule.triggered_at,
    }


def _sl_from_dict(d: Optional[dict]) -> Optional[StopLossRule]:
    if d is None:
        return None
    return StopLossRule(
        threshold_points=d["threshold_points"],
        mode=d.get("mode", SL_MODE_REOPEN_DOUBLE),
        reopen=_reopen_from_dict(d.get("reopen")),
        new_put=_reopen_from_dict(d.get("new_put")),
        new_call=_reopen_from_dict(d.get("new_call")),
        add_leg=_reopen_from_dict(d.get("add_leg")),
        status=d.get("status", STATUS_UNSET),
        triggered_at=d.get("triggered_at"),
    )


def _sl_to_dict(rule: Optional[StopLossRule]) -> Optional[dict]:
    if rule is None:
        return None
    return {
        "threshold_points": rule.threshold_points,
        "mode": rule.mode,
        "reopen": _reopen_to_dict(rule.reopen),
        "new_put": _reopen_to_dict(rule.new_put),
        "new_call": _reopen_to_dict(rule.new_call),
        "add_leg": _reopen_to_dict(rule.add_leg),
        "status": rule.status,
        "triggered_at": rule.triggered_at,
    }


class AutoCloseManager:
    def __init__(self, ib_client: IBClient, position_manager: PositionManager, order_book_manager: OrderBookManager):
        self.rules_changed = Signal()      # 規則設定/狀態有變動，UI 重繪
        self.auto_close_error = Signal()   # 鏈結中的平倉單被交易所真的拒絕，需要人工排查
        self._ib = ib_client.ib
        self._position_manager = position_manager
        self._order_book_manager = order_book_manager

        self._position_rules: Dict[str, PositionRules] = {}
        self._group_rules: Dict[str, TakeProfitRule] = {}
        self._pending_fill_actions: Dict[str, Union[dict, List[dict]]] = {}

        self._load()

        self._position_manager.positions_changed.connect(lambda: spawn(self._on_positions_changed()))
        self._order_book_manager.records_changed.connect(lambda: spawn(self._check_chained_fills()))

    # ------------------------------------------------------------ 讀寫
    def _load(self) -> None:
        data = auto_close_store.load()
        for symbol_key, rules in data.get("position_rules", {}).items():
            tp = _tp_from_dict(rules.get("take_profit"))
            sl = _sl_from_dict(rules.get("stop_loss"))
            # *** 重啟一律凍結成暫停，不管存檔當下是不是武裝中，都要使用
            # 者自己按「啟用」才會重新開始監控送單 ***
            if tp is not None and tp.status not in (STATUS_UNSET, STATUS_TRIGGERED, STATUS_FAILED):
                tp.status = STATUS_PAUSED
            if sl is not None and sl.status not in (STATUS_UNSET, STATUS_TRIGGERED, STATUS_FAILED):
                sl.status = STATUS_PAUSED
            self._position_rules[symbol_key] = PositionRules(take_profit=tp, stop_loss=sl)
        for group_id, rules in data.get("group_rules", {}).items():
            tp = _tp_from_dict(rules.get("take_profit"))
            if tp is not None and tp.status not in (STATUS_UNSET, STATUS_TRIGGERED, STATUS_FAILED):
                tp.status = STATUS_PAUSED
            if tp is not None:
                self._group_rules[group_id] = tp
        self._pending_fill_actions = dict(data.get("pending_fill_actions", {}))

    def _save(self) -> None:
        auto_close_store.save({
            "position_rules": {
                k: {"take_profit": _tp_to_dict(v.take_profit), "stop_loss": _sl_to_dict(v.stop_loss)}
                for k, v in self._position_rules.items()
            },
            "group_rules": {k: {"take_profit": _tp_to_dict(v)} for k, v in self._group_rules.items()},
            "pending_fill_actions": self._pending_fill_actions,
        })

    # ------------------------------------------------------------ 給 UI 用的公開介面
    def get_position_rules(self, symbol_key: str) -> PositionRules:
        return self._position_rules.get(symbol_key, PositionRules())

    def set_take_profit(self, symbol_key: str, rule: Optional[TakeProfitRule]) -> None:
        self._position_rules.setdefault(symbol_key, PositionRules()).take_profit = rule
        self._save()
        self.rules_changed.emit()

    def set_stop_loss(self, symbol_key: str, rule: Optional[StopLossRule]) -> None:
        self._position_rules.setdefault(symbol_key, PositionRules()).stop_loss = rule
        self._save()
        self.rules_changed.emit()

    def set_position_rule_status(self, symbol_key: str, kind: str, status: str) -> None:
        """kind: "take_profit"/"stop_loss"。給 UI 的啟用/暫停按鈕用。"""
        rules = self._position_rules.get(symbol_key)
        if rules is None:
            return
        rule = rules.take_profit if kind == "take_profit" else rules.stop_loss
        if rule is None:
            return
        rule.status = status
        self._save()
        self.rules_changed.emit()

    def get_group_rule(self, group_id: str) -> Optional[TakeProfitRule]:
        return self._group_rules.get(group_id)

    def set_group_rule(self, group_id: str, rule: Optional[TakeProfitRule]) -> None:
        if rule is None:
            self._group_rules.pop(group_id, None)
        else:
            self._group_rules[group_id] = rule
        self._save()
        self.rules_changed.emit()

    def set_group_rule_status(self, group_id: str, status: str) -> None:
        rule = self._group_rules.get(group_id)
        if rule is None:
            return
        rule.status = status
        if status == STATUS_ARMED:
            rule.paused_reason = None
        self._save()
        self.rules_changed.emit()

    def arm_all_paused(self) -> None:
        for rules in self._position_rules.values():
            for rule in (rules.take_profit, rules.stop_loss):
                if rule is not None and rule.status == STATUS_PAUSED:
                    rule.status = STATUS_ARMED
        for rule in self._group_rules.values():
            if rule.status == STATUS_PAUSED:
                rule.status = STATUS_ARMED
                rule.paused_reason = None
        self._save()
        self.rules_changed.emit()

    # ------------------------------------------------------------ 觸發判斷
    async def _on_positions_changed(self) -> None:
        self._reconcile_orphans()
        await self._evaluate_positions()
        self._evaluate_groups()

    def _reconcile_orphans(self) -> None:
        changed = False
        current_keys = {p.symbol_key for p in self._position_manager.positions}
        for symbol_key in list(self._position_rules.keys()):
            if symbol_key not in current_keys:
                del self._position_rules[symbol_key]
                changed = True
        current_group_ids = {g.group_id for g in self._position_manager.groups}
        for group_id in list(self._group_rules.keys()):
            if group_id not in current_group_ids:
                del self._group_rules[group_id]
                changed = True
        if changed:
            self._save()

    async def _evaluate_positions(self) -> None:
        for position in self._position_manager.positions:
            rules = self._position_rules.get(position.symbol_key)
            if rules is None:
                continue
            price = current_price(self._position_manager, position)
            points = pnl_points(position, price)
            if points is None:
                continue
            if rules.take_profit is not None and rules.take_profit.status == STATUS_ARMED:
                if points >= rules.take_profit.threshold_points:
                    await self._fire_take_profit(position, rules.take_profit)
                    continue  # 停利/停損互斥觸發，這輪不用再檢查停損
            if rules.stop_loss is not None and rules.stop_loss.status == STATUS_ARMED:
                if points <= -rules.stop_loss.threshold_points:
                    await self._fire_stop_loss(position, rules.stop_loss)

    def _evaluate_groups(self) -> None:
        for group in self._position_manager.groups:
            rule = self._group_rules.get(group.group_id)
            if rule is None or rule.status != STATUS_ARMED:
                continue
            total = 0.0
            for position in group.positions:
                points = pnl_points(position, current_price(self._position_manager, position))
                if points is None:
                    total = None
                    break
                total += points * position.qty
            if total is not None and total >= rule.threshold_points:
                self._fire_group_take_profit(group, rule)

    # ------------------------------------------------------------ 觸發動作
    def _pause_position_rules(self, symbol_key: str) -> None:
        rules = self._position_rules.get(symbol_key)
        if rules is None:
            return
        for rule in (rules.take_profit, rules.stop_loss):
            if rule is not None and rule.status == STATUS_ARMED:
                rule.status = STATUS_PAUSED

    def _pause_group_for(self, position: Position) -> None:
        for group in self._position_manager.groups:
            if any(p.symbol_key == position.symbol_key for p in group.positions):
                rule = self._group_rules.get(group.group_id)
                if rule is not None and rule.status == STATUS_ARMED:
                    rule.status = STATUS_PAUSED
                    rule.paused_reason = f"{position.symbol_key} 觸發了單邊規則，自動暫停"
                return

    def _assign_group(self, position: Position, new_symbol_key: str) -> None:
        """新開的重開/加開部位，併回觸發部位當下所屬的群組。"""
        for group in self._position_manager.groups:
            if any(p.symbol_key == position.symbol_key for p in group.positions):
                target = None if group.group_id == UNGROUPED_ID else group.group_id
                self._position_manager.move_to_group(new_symbol_key, target)
                return

    async def _fire_take_profit(self, position: Position, rule: TakeProfitRule) -> None:
        price = _trigger_price(position, rule.threshold_points, is_take_profit=True)
        record_id = self._close_position(position, price)
        rule.status = STATUS_TRIGGERED
        rule.triggered_at = time.time()
        if rule.reopen is not None and record_id is not None:
            action = await self._build_reopen_action(position, rule.reopen, position.qty)
            self._pending_fill_actions[record_id] = {
                "actions": action, "symbol_key": position.symbol_key, "kind": "take_profit",
            }
        self._pause_position_rules(position.symbol_key)
        self._pause_group_for(position)
        self._save()
        self.rules_changed.emit()

    async def _fire_stop_loss(self, position: Position, rule: StopLossRule) -> None:
        rule.status = STATUS_TRIGGERED
        rule.triggered_at = time.time()
        if rule.mode == SL_MODE_ADD_LEG:
            # 規則5：虧損邊不平倉，直接加開對側裸賣一支腳，武裝條件一到
            # 就直接送，沒有「平倉成交後才重開」這個鏈結。
            if rule.add_leg is not None:
                await self._open_add_leg(position, rule.add_leg)
        else:
            price = _trigger_price(position, rule.threshold_points, is_take_profit=False)
            record_id = self._close_position(position, price)
            if record_id is not None:
                queued = None
                if rule.mode == SL_MODE_REOPEN_DOUBLE and rule.reopen is not None:
                    queued = await self._build_reopen_action(position, rule.reopen, position.qty * 2)
                elif rule.mode == SL_MODE_NEW_GROUP:
                    sibling = self.find_sibling(position)
                    actions = []
                    if rule.new_put is not None:
                        actions.append(await self._build_new_group_action(position, sibling, rule.new_put, "P", position.qty))
                    if rule.new_call is not None:
                        actions.append(await self._build_new_group_action(position, sibling, rule.new_call, "C", position.qty))
                    if actions:
                        queued = actions
                if queued is not None:
                    self._pending_fill_actions[record_id] = {
                        "actions": queued, "symbol_key": position.symbol_key, "kind": "stop_loss",
                    }
        self._pause_position_rules(position.symbol_key)
        self._pause_group_for(position)
        self._save()
        self.rules_changed.emit()

    def _fire_group_take_profit(self, group: PositionGroup, rule: TakeProfitRule) -> None:
        for position in group.positions:
            price = current_price(self._position_manager, position)
            if price is None:
                continue  # 沒有現價就不硬平倉這一腳，寧可漏平不要用猜的價格送單
            self._close_position(position, price)
            self._pause_position_rules(position.symbol_key)
        rule.status = STATUS_TRIGGERED
        rule.triggered_at = time.time()
        self._save()
        self.rules_changed.emit()

    def find_sibling(self, position: Position) -> Optional[Position]:
        """同群組裡跟 position 不同買賣權類型的另一個複式部位，規則4
        「新put沿用原put寬度、新call沿用原call寬度」要用。"""
        if not position.is_combo:
            return None
        target_right = "P" if position.legs[0].right == "C" else "C"
        for group in self._position_manager.groups:
            members = group.positions
            if not any(p.symbol_key == position.symbol_key for p in members):
                continue
            for other in members:
                if (other.symbol_key != position.symbol_key and other.is_combo
                        and other.legs[0].right == target_right):
                    return other
        return None

    # ------------------------------------------------------------ 實際送單
    def _close_position(self, position: Position, price: float) -> Optional[str]:
        """送出平倉單(方向跟原部位相反)，回傳 OrderRecord id 給呼叫端串
        「成交後才重開」的鏈結用。position.legs 已經是 IB 已配對過的合約
        物件，直接拿去下單，不用再重建/重新qualify。"""
        payoff_legs = position.payoff_legs()
        net_buyer = not position.buy
        if position.is_combo:
            (leg1, buy1, _), (leg2, buy2, _) = payoff_legs
            record_id = self._order_book_manager.stage_duplex(
                leg1, not buy1, leg2, not buy2, price, position.qty, tif="DAY", net_buyer=net_buyer,
            )
        else:
            leg, buy, _ = payoff_legs[0]
            record_id = self._order_book_manager.stage_outright(leg, not buy, price, position.qty, tif="DAY")
        self._order_book_manager.confirm_send(record_id)
        return record_id

    async def _build_reopen_action(self, position: Position, spec: ReopenSpec, qty: float) -> dict:
        """規則2/3：原地重開同類型價差，寬度沿用原部位自己的寬度、方向
        沿用原部位自己的方向(同一種價差，只是換履約價)。"""
        leg0 = position.legs[0]
        width = abs(position.legs[0].strike - position.legs[1].strike) if position.is_combo else 0.0
        right = leg0.right
        (low_strike, low_action), (high_strike, high_action) = vertical_spread_legs(
            spec.strike, width, right, position.buy,
        )
        action = {
            "symbol": leg0.symbol, "expiry": leg0.lastTradeDateOrContractMonth, "right": right,
            "leg1_strike": low_strike, "leg1_action": low_action,
            "leg2_strike": high_strike, "leg2_action": high_action,
            "price": spec.price, "qty": qty, "net_buyer": position.buy,
        }
        await self._assign_group_for_action(position, action)
        return action

    async def _build_new_group_action(
        self, position: Position, sibling: Optional[Position], spec: ReopenSpec, right: str, qty: float,
    ) -> dict:
        """規則4其中一組新價差。寬度/方向的範本：跟買賣權類型相同的既有
        部位(觸發部位自己，或同群組另一邊)，都找不到才退回用觸發部位自
        己頂著(見 find_sibling 的說明)。"""
        template = position if position.legs[0].right == right else sibling
        if template is None or not template.is_combo:
            template = position
        width = abs(template.legs[0].strike - template.legs[1].strike)
        buy_spread = template.buy
        leg0 = position.legs[0]
        (low_strike, low_action), (high_strike, high_action) = vertical_spread_legs(
            spec.strike, width, right, buy_spread,
        )
        action = {
            "symbol": leg0.symbol, "expiry": leg0.lastTradeDateOrContractMonth, "right": right,
            "leg1_strike": low_strike, "leg1_action": low_action,
            "leg2_strike": high_strike, "leg2_action": high_action,
            "price": spec.price, "qty": qty, "net_buyer": buy_spread,
        }
        await self._assign_group_for_action(position, action)
        return action

    async def _assign_group_for_action(self, position: Position, action: dict) -> None:
        """重開/加開的新部位還沒送單，不知道真正的 conId(symbol_key)——
        這裡先跟 IB qualify 一次履約價換成 conId，算出真正下單成交後
        Position.symbol_key 會用到的 key，才能正確預先歸群組；不能用履
        約價字串本身當 key，跟 positions.py 的 symbol_key(str(conId)) 對
        不起來。"""
        leg1 = build_option(action["symbol"], action["expiry"], action["leg1_strike"], action["right"])
        leg2 = build_option(action["symbol"], action["expiry"], action["leg2_strike"], action["right"])
        await self._ib.qualifyContractsAsync(leg1, leg2)
        symbol_key = "+".join(sorted([str(leg1.conId), str(leg2.conId)]))
        self._assign_group(position, symbol_key)

    async def _open_add_leg(self, position: Position, spec: ReopenSpec) -> None:
        """規則5：虧損邊不平倉，裸賣加開對側買賣權一支腳，履約價/委託價
        使用者手動填、不套寬度公式，方向固定賣出(收權利金換空間)。"""
        leg0 = position.legs[0]
        opposite_right = "P" if leg0.right == "C" else "C"
        contract = build_option(leg0.symbol, leg0.lastTradeDateOrContractMonth, spec.strike, opposite_right)
        await self._ib.qualifyContractsAsync(contract)
        record_id = self._order_book_manager.stage_outright(contract, False, spec.price, position.qty, tif="DAY")
        self._order_book_manager.confirm_send(record_id)
        self._assign_group(position, str(contract.conId))

    # ------------------------------------------------------------ 平倉成交後才重開
    async def _check_chained_fills(self) -> None:
        # *** 每筆一定要在 await _execute_reopen() 之前就先從
        # _pending_fill_actions 移除、存檔 ***：_execute_reopen() 自己
        # 會送出重開單，那張新單透過 order_book_manager 也會再觸發一次
        # records_changed → 這個方法被重新排程執行一次；如果還沒清掉,
        # 舊的那個還在 await 中的呼叫恢復執行時，跟新排進來的這次呼叫
        # 會看到同一筆還沒清掉的 pending action，變成同一組重開動作被
        # 送兩次單。先 pop 再 await，兩邊看到的都是已經清空的狀態。
        if not self._pending_fill_actions:
            return
        for record_id, entry in list(self._pending_fill_actions.items()):
            record = self._order_book_manager.get_record(record_id)
            if record is None:
                self._pending_fill_actions.pop(record_id, None)  # 理論上不會發生(委託被刪掉)，別讓鏈結卡住不放
                self._save()
                continue
            if record.status == STATUS_FILLED:
                self._pending_fill_actions.pop(record_id, None)
                self._save()
                await self._execute_reopen(entry["actions"])
            elif record.status in (STATUS_REJECTED, STATUS_CANCELLED):
                self._pending_fill_actions.pop(record_id, None)
                self._mark_failed(entry.get("symbol_key"), entry.get("kind"))
                self._save()
                self.auto_close_error.emit(
                    f"自動平倉委託未能完成({record.status})，重開/加開動作已取消，請手動確認：{record.label()}",
                )

    def _mark_failed(self, symbol_key: Optional[str], kind: Optional[str]) -> None:
        rules = self._position_rules.get(symbol_key) if symbol_key else None
        if rules is None:
            return
        rule = rules.take_profit if kind == "take_profit" else rules.stop_loss
        if rule is not None:
            rule.status = STATUS_FAILED

    async def _execute_reopen(self, action: Union[dict, List[dict]]) -> None:
        actions = action if isinstance(action, list) else [action]
        for spec in actions:
            leg1 = build_option(spec["symbol"], spec["expiry"], spec["leg1_strike"], spec["right"])
            leg2 = build_option(spec["symbol"], spec["expiry"], spec["leg2_strike"], spec["right"])
            await self._ib.qualifyContractsAsync(leg1, leg2)
            record_id = self._order_book_manager.stage_duplex(
                leg1, spec["leg1_action"] == "BUY", leg2, spec["leg2_action"] == "BUY",
                spec["price"], spec["qty"], tif="DAY", net_buyer=spec["net_buyer"],
            )
            self._order_book_manager.confirm_send(record_id)
