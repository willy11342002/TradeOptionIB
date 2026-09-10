"""
未平倉部位自動平倉／停利停損管理器。規則本身(閾值/動作/口數/新履約價)
是使用者在對話中逐項拍板定案的，詳細規則表見
C:\\Users\\tingw\\.claude\\plans\\lazy-riding-hearth.md，這裡只記整體機
制設計：

- **判斷「要不要觸發」自己做，不外包給 order_book.py 的連續IOC條件引
  擎**：因為規則1(整組)橫跨兩個不同的複式單、規則5(加開對側裸賣腳)判斷
  用的是「原本那組價差」的價格、但真正送出的是另一個商品，沒辦法套進
  「一張委託自己的成交價條件」這個框架裡，所以統一用一個 watcher(接
  PositionManager.positions_changed，跟畫面的損益欄位算的是同一組函式
  `app.models.positions.current_price`/`pnl_points`，不會兩邊算出不同數
  字)自己判斷門檻有沒有到。
- **「怎麼把單送出去、盯著等成交」則整個借用 order_book.py 現成的連續
  IOC 引擎**：判斷觸發之後，呼叫 `OrderBookManager.stage_duplex`/
  `stage_outright` 立刻 `confirm_send`(不用使用者再按一次)，價格用門檻
  換算出的絕對限價、`auto_retry=True`，沒成交會自己一直重送，這段完全
  不重寫。
- **平倉單成交後才重開**：不是自己維護一套計時器，而是記一筆
  `pending_fill_actions[record_id] = 重開參數`，接
  `OrderBookManager.records_changed`，看到那筆 record 變成
  STATUS_FILLED 才真的送出重開單；被交易所真的拒絕(不是連續IOC還沒成
  交)才整條規則設 FAILED、跳通知，不會亂猜著重開。
- **App 重啟一律凍結成暫停**：跟 order_book.py 的 STATUS_RETRYING→
  STATUS_PAUSED 是同一個安全考量，見 `_load()`。
"""
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Union

from PyQt5.QtCore import QObject, pyqtSignal

from app.models.auto_close import (
    PositionRules, ReopenSpec, StopLossRule, TakeProfitRule,
    SL_MODE_ADD_LEG, SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE,
    STATUS_ARMED, STATUS_FAILED, STATUS_PAUSED, STATUS_TRIGGERED, STATUS_UNSET,
)
from app.models.capital_order_client import AUTO_POSITION
from app.models.contracts import build_leg_symbol, build_vertical_spread_legs
from app.models.order_book import (
    OrderBookManager, STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED, TIF_IOC,
    default_condition_op,
)
from app.models.positions import Position, PositionGroup, PositionManager, UNGROUPED_ID, current_price, pnl_points
from app.services import auto_close_store


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


class AutoCloseManager(QObject):
    rules_changed = pyqtSignal()     # 規則設定/狀態有變動，UI 重繪
    auto_close_error = pyqtSignal(str)  # 鏈結中的平倉單被交易所真的拒絕，需要人工排查

    def __init__(self, position_manager: PositionManager, order_book_manager: OrderBookManager):
        super().__init__()
        self._position_manager = position_manager
        self._order_book_manager = order_book_manager

        self._position_rules: Dict[str, PositionRules] = {}
        self._group_rules: Dict[str, TakeProfitRule] = {}
        self._pending_fill_actions: Dict[str, Union[dict, List[dict]]] = {}

        self._load()

        self._position_manager.positions_changed.connect(self._on_positions_changed)
        self._order_book_manager.records_changed.connect(self._check_chained_fills)

    # ------------------------------------------------------------ 讀寫
    def _load(self) -> None:
        data = auto_close_store.load()
        for symbol_key, rules in data.get("position_rules", {}).items():
            tp = _tp_from_dict(rules.get("take_profit"))
            sl = _sl_from_dict(rules.get("stop_loss"))
            # *** 重啟一律凍結成暫停，比照 order_book.py 的
            # STATUS_RETRYING -> STATUS_PAUSED，不管存檔當下是不是武裝
            # 中，都要使用者自己按「啟用」才會重新開始監控送單 ***
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
    def _on_positions_changed(self) -> None:
        self._reconcile_orphans()
        self._evaluate_positions()
        self._evaluate_groups()

    def _reconcile_orphans(self) -> None:
        """部位/群組已經從查詢結果消失(完全平倉/群組被刪除)，規則跟著清
        掉，不留孤兒設定——邏輯比照 positions.py 的
        _reconcile_manual_overrides()。"""
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

    def _evaluate_positions(self) -> None:
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
                    self._fire_take_profit(position, rules.take_profit)
                    continue  # 停利/停損互斥觸發，這輪不用再檢查停損
            if rules.stop_loss is not None and rules.stop_loss.status == STATUS_ARMED:
                if points <= -rules.stop_loss.threshold_points:
                    self._fire_stop_loss(position, rules.stop_loss)

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
        """新開的重開/加開部位，併回觸發部位當下所屬的群組(使用者定案：
        規則4「新開一整組，併入同樣群組」，其餘規則也比照辦理，一致比較
        不會讓使用者困惑)。"""
        for group in self._position_manager.groups:
            if any(p.symbol_key == position.symbol_key for p in group.positions):
                target = None if group.group_id == UNGROUPED_ID else group.group_id
                self._position_manager.move_to_group(new_symbol_key, target)
                return

    def _fire_take_profit(self, position: Position, rule: TakeProfitRule) -> None:
        price = _trigger_price(position, rule.threshold_points, is_take_profit=True)
        record_id = self._close_position(position, price)
        rule.status = STATUS_TRIGGERED
        rule.triggered_at = time.time()
        if rule.reopen is not None and record_id is not None:
            action = self._build_reopen_action(position, rule.reopen, position.qty)
            self._pending_fill_actions[record_id] = {
                "actions": action, "symbol_key": position.symbol_key, "kind": "take_profit",
            }
        self._pause_position_rules(position.symbol_key)
        self._pause_group_for(position)
        self._save()
        self.rules_changed.emit()

    def _fire_stop_loss(self, position: Position, rule: StopLossRule) -> None:
        rule.status = STATUS_TRIGGERED
        rule.triggered_at = time.time()
        if rule.mode == SL_MODE_ADD_LEG:
            # 規則5：虧損邊不平倉，直接加開對側裸賣一支腳，沒有「平倉成
            # 交後才重開」這個鏈結，武裝條件一到就直接送。
            if rule.add_leg is not None:
                self._open_add_leg(position, rule.add_leg)
        else:
            price = _trigger_price(position, rule.threshold_points, is_take_profit=False)
            record_id = self._close_position(position, price)
            if record_id is not None:
                queued = None
                if rule.mode == SL_MODE_REOPEN_DOUBLE and rule.reopen is not None:
                    queued = self._build_reopen_action(position, rule.reopen, position.qty * 2)
                elif rule.mode == SL_MODE_NEW_GROUP:
                    sibling = self._find_sibling(position)
                    actions = []
                    if rule.new_put is not None:
                        actions.append(self._build_new_group_action(position, sibling, rule.new_put, "P", position.qty))
                    if rule.new_call is not None:
                        actions.append(self._build_new_group_action(position, sibling, rule.new_call, "C", position.qty))
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

    def _find_sibling(self, position: Position) -> Optional[Position]:
        """同群組裡跟 position 不同 call_put 類型的另一個複式部位，規則4
        「新put沿用原put寬度、新call沿用原call寬度」要用；找不到(理論上
        鐵禿鷹一定兩邊都在，這裡防呆)就回 None，呼叫端會退回用觸發部位
        自己頂著用。"""
        for group in self._position_manager.groups:
            members = group.positions
            if not any(p.symbol_key == position.symbol_key for p in members):
                continue
            for other in members:
                if other.symbol_key != position.symbol_key and other.is_combo:
                    return other
        return None

    # ------------------------------------------------------------ 實際送單
    def _close_position(self, position: Position, price: float) -> Optional[str]:
        """送出平倉單(方向跟原部位相反)，回傳 OrderRecord id 給呼叫端串
        「成交後才重開」的鏈結用。position.buy 是 None 的部位不會被武裝
        到規則(pnl_points 一定回 None，_evaluate_* 永遠不會走到這裡)，這
        裡不用再防一次。"""
        payoff_legs = position.payoff_legs()
        if payoff_legs is None:
            return None
        net_buyer = not position.buy
        condition_op = default_condition_op(net_buyer)
        if position.is_combo:
            (leg1, buy1, _), (leg2, buy2, _) = payoff_legs
            record_id = self._order_book_manager.stage_duplex(
                leg1.symbol, not buy1, leg2.symbol, not buy2,
                price, position.qty, tif=TIF_IOC, new_close=AUTO_POSITION, auto_retry=True,
                call_put1=leg1.call_put, strike1=leg1.strike,
                call_put2=leg2.call_put, strike2=leg2.strike,
                net_buyer=net_buyer, condition_op=condition_op,
            )
        else:
            leg, buy, _ = payoff_legs[0]
            record_id = self._order_book_manager.stage_outright(
                leg.symbol, not buy, price, position.qty, tif=TIF_IOC, new_close=AUTO_POSITION,
                auto_retry=True, call_put=leg.call_put, strike=leg.strike, condition_op=condition_op,
            )
        self._order_book_manager.confirm_send(record_id)
        return record_id

    def _build_reopen_action(self, position: Position, spec: ReopenSpec, qty: int) -> dict:
        """規則2/3：原地重開同類型價差，寬度沿用原部位自己的寬度、方向
        沿用原部位自己的方向(同一種價差，只是換履約價)。"""
        leg0 = position.legs[0]
        width = abs(position.legs[0].strike - position.legs[1].strike) if position.is_combo else 0.0
        leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2 = build_vertical_spread_legs(
            leg0.product_code, spec.strike, leg0.call_put, leg0.expiry_month, leg0.expiry_year_digit,
            width, position.buy,
        )
        symbol_key = "+".join(sorted([leg1_symbol, leg2_symbol]))
        self._assign_group(position, symbol_key)
        return {
            "leg1_symbol": leg1_symbol, "buy1": buy1, "call_put1": leg0.call_put, "strike1": strike1,
            "leg2_symbol": leg2_symbol, "buy2": buy2, "call_put2": leg0.call_put, "strike2": strike2,
            "price": spec.price, "qty": qty, "net_buyer": position.buy,
        }

    def _build_new_group_action(
        self, position: Position, sibling: Optional[Position], spec: ReopenSpec, call_put: str, qty: int,
    ) -> dict:
        """規則4其中一組新價差。寬度/方向的範本：跟 call_put 類型相同的
        既有部位(觸發部位自己，或同群組另一邊)，都找不到才退回用觸發部
        位自己頂著(見 _find_sibling 的說明)。"""
        template = position if position.legs[0].call_put == call_put else sibling
        if template is None or not template.is_combo:
            template = position
        width = abs(template.legs[0].strike - template.legs[1].strike)
        buy_spread = template.buy
        leg0 = position.legs[0]
        leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2 = build_vertical_spread_legs(
            leg0.product_code, spec.strike, call_put, leg0.expiry_month, leg0.expiry_year_digit,
            width, buy_spread,
        )
        symbol_key = "+".join(sorted([leg1_symbol, leg2_symbol]))
        self._assign_group(position, symbol_key)
        return {
            "leg1_symbol": leg1_symbol, "buy1": buy1, "call_put1": call_put, "strike1": strike1,
            "leg2_symbol": leg2_symbol, "buy2": buy2, "call_put2": call_put, "strike2": strike2,
            "price": spec.price, "qty": qty, "net_buyer": buy_spread,
        }

    def _open_add_leg(self, position: Position, spec: ReopenSpec) -> None:
        """規則5：虧損邊不平倉，裸賣加開對側買賣權一支腳，履約價/委託價
        使用者手動填、不套寬度公式，方向固定賣出(收權利金換空間，見規則
        定案說明)。"""
        leg0 = position.legs[0]
        opposite_call_put = "P" if leg0.call_put == "C" else "C"
        symbol = build_leg_symbol(leg0.product_code, spec.strike, opposite_call_put, leg0.expiry_month, leg0.expiry_year_digit)
        record_id = self._order_book_manager.stage_outright(
            symbol, False, spec.price, position.qty, tif=TIF_IOC, new_close=AUTO_POSITION,
            auto_retry=True, call_put=opposite_call_put, strike=spec.strike,
            condition_op=default_condition_op(net_buyer=False),
        )
        self._order_book_manager.confirm_send(record_id)
        self._assign_group(position, symbol)

    # ------------------------------------------------------------ 平倉成交後才重開
    def _check_chained_fills(self) -> None:
        if not self._pending_fill_actions:
            return
        done = []
        for record_id, entry in list(self._pending_fill_actions.items()):
            record = self._order_book_manager.get_record(record_id)
            if record is None:
                done.append(record_id)  # 理論上不會發生(委託被刪掉)，別讓鏈結卡住不放
                continue
            if record.status == STATUS_FILLED:
                self._execute_reopen(entry["actions"])
                done.append(record_id)
            elif record.status in (STATUS_REJECTED, STATUS_CANCELLED):
                # *** 連續IOC沒成交不會走到這裡(order_book.py 對
                # auto_retry=True 的委託，IOC沒成交會留在 RETRYING 繼續
                # 試，不會變成 CANCELLED)——看到這兩個狀態代表平倉單真的
                # 被交易所拒絕或被使用者手動刪掉，使用者原話："除非下錯
                # 口數(bug)，不然平倉單不會失敗，如果真的失敗那就跳通
                # 知，要修bug"，這裡不猜測後續怎麼補救，只中止鏈結+通知、
                # 把觸發那條規則標成 FAILED 讓畫面看得出來。
                self._mark_failed(entry.get("symbol_key"), entry.get("kind"))
                self.auto_close_error.emit(f"自動平倉委託未能完成({record.status})，重開/加開動作已取消，請手動確認：{record.label()}")
                done.append(record_id)
        if done:
            for record_id in done:
                self._pending_fill_actions.pop(record_id, None)
            self._save()

    def _mark_failed(self, symbol_key: Optional[str], kind: Optional[str]) -> None:
        rules = self._position_rules.get(symbol_key) if symbol_key else None
        if rules is None:
            return
        rule = rules.take_profit if kind == "take_profit" else rules.stop_loss
        if rule is not None:
            rule.status = STATUS_FAILED

    def _execute_reopen(self, action: Union[dict, List[dict]]) -> None:
        actions = action if isinstance(action, list) else [action]
        for spec in actions:
            record_id = self._order_book_manager.stage_duplex(
                spec["leg1_symbol"], spec["buy1"], spec["leg2_symbol"], spec["buy2"],
                spec["price"], spec["qty"], tif=TIF_IOC, new_close=AUTO_POSITION, auto_retry=True,
                call_put1=spec["call_put1"], strike1=spec["strike1"],
                call_put2=spec["call_put2"], strike2=spec["strike2"],
                net_buyer=spec["net_buyer"], condition_op=default_condition_op(spec["net_buyer"]),
            )
            self._order_book_manager.confirm_send(record_id)
