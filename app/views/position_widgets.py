import time
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QVBoxLayout, QWidget, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QInputDialog, QColorDialog, QLabel, QPushButton, QHBoxLayout, QDialog,
)

from app.models.auto_close import (
    SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE,
    STATUS_ARMED, STATUS_FAILED, STATUS_PAUSED, STATUS_TRIGGERED, STATUS_UNSET,
)
from app.models.auto_close_manager import AutoCloseManager
from app.models.positions import PositionManager, Position, PositionGroup, UNGROUPED_ID, current_price, position_pnl
from app.services import position_groups_store
from app.views.auto_close_dialog import GroupTakeProfitDialog, StopLossDialog, TakeProfitDialog

# 跟 main_window.py 的 COLOR_UP_TEXT/COLOR_DOWN_TEXT 是同一組顏色常數，
# 這裡不 import main_window(避免循環 import：main_window 要 import 這個
# 檔案來建立 dock)，數值保持同步即可。*** 美股慣例：漲=綠、跌=紅，跟台
# 股相反，這是這次改動故意翻過來的地方。***
COLOR_UP_TEXT = QColor("#3ecf6e")
COLOR_DOWN_TEXT = QColor("#e05050")

_COLUMNS = ["商品/群組", "買權/賣權", "方向", "口數", "均價", "現價", "損益", "動作"]
_RIGHT_LABELS = {"C": "買權", "P": "賣權"}

_DEFAULT_GROUP_COLOR = "#4a90d9"

_RULE_KIND_LABELS = {"take_profit": "停利", "stop_loss": "停損"}
_STATUS_ICONS = {
    STATUS_UNSET: "⚪", STATUS_PAUSED: "⏸", STATUS_ARMED: "▶",
    STATUS_TRIGGERED: "✅", STATUS_FAILED: "❌",
}


def _rich_tooltip(text: str) -> str:
    """純文字 tooltip 不會自動換行、字體也是系統預設的小字，包成簡單
    HTML 才能換行+限制寬度+放大字體，跟 auto_close_dialog.py 的同名函式
    是同一招，這裡獨立一份避免跨檔案 import 私有名稱。"""
    return f"<div style='max-width:320px; font-size:13pt;'>{text}</div>"


def _direction_text(position: Position) -> str:
    return "買進" if position.buy else "賣出"


def _right_text(position: Position) -> str:
    labels = {_RIGHT_LABELS.get(leg.right, leg.right) for leg in position.legs}
    return "/".join(sorted(labels))


def _symbol_text(position: Position) -> str:
    if position.is_combo:
        return " / ".join(f"{leg.localSymbol or leg.symbol}" for leg in position.legs)
    leg = position.legs[0]
    return leg.localSymbol or leg.symbol


class PositionTreeWidget(QWidget):
    """未平倉部位：樹狀分組表格。頂層節點是群組(含固定的「未分組」)，子
    節點是逐列部位(TM 複式單合併列算一列，不會再往下拆兩腳個別顯示——
    parse_combo_symbol 拆出來的兩腳是給損益計算/自動分組用，畫面上使用者
    要看到的是「這是一組複式部位」，不是硬拆成兩列)。"""

    def __init__(self, manager: PositionManager, auto_close: AutoCloseManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._auto_close = auto_close
        self._manager.positions_changed.connect(self._refresh)
        self._manager.query_failed.connect(self._on_query_failed)
        self._auto_close.rules_changed.connect(self._refresh)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_toolbar())

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(_COLUMNS)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        # 「商品/群組」欄位內容長度差很多(單腳代碼 vs 複式單兩腳合併字
        # 串)，用 Interactive 的話每次重繪都要使用者自己手動拖寬，改成
        # 自動依內容撐開；其餘欄位維持可手動調整。
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._refresh()

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        refresh_btn = QPushButton("重新查詢未平倉")
        refresh_btn.clicked.connect(self._on_refresh_clicked)
        toolbar.addWidget(refresh_btn)
        # 規則設定完是暫停狀態，重開機後也全部凍結成暫停(見
        # auto_close_manager.py 開頭說明)，筆數一多要一個一個點「啟用」
        # 太累人，這裡一次把所有「已設定但暫停中」的規則全部武裝。
        arm_all_btn = QPushButton("全部啟動")
        arm_all_btn.clicked.connect(self._auto_close.arm_all_paused)
        toolbar.addWidget(arm_all_btn)
        toolbar.addStretch(1)
        return toolbar

    def _on_refresh_clicked(self) -> None:
        # IB 的 ib.positions() 是本地同步讀取，沒有群益那種查詢間隔限
        # 制，不需要節流。
        self.status_label.setText("查詢中...")
        self._manager.refresh()

    def _on_query_failed(self, message: str) -> None:
        self.status_label.setText(f"未平倉查詢失敗：{message}")

    # ------------------------------------------------------------------ 畫面重繪
    def _refresh(self) -> None:
        self.tree.clear()
        groups = self._manager.groups
        for group in groups:
            group_item = QTreeWidgetItem([group.name, "", "", "", "", "", "", ""])
            group_item.setData(0, Qt.UserRole, ("group", group.group_id))
            group_item.setBackground(0, QBrush(QColor(group.color)))
            total_qty = sum(p.qty for p in group.positions)
            group_pnl = self._group_pnl(group.positions)
            group_item.setText(3, str(total_qty))
            self._set_pnl_cell(group_item, 6, group_pnl)
            self.tree.addTopLevelItem(group_item)
            self.tree.setItemWidget(group_item, 7, self._build_group_actions_widget(group))
            for position in group.positions:
                self._add_position_item(group_item, position)
            group_item.setExpanded(True)
        self.status_label.setText(f"共 {sum(len(g.positions) for g in groups)} 筆部位")

    def _group_pnl(self, positions) -> Optional[float]:
        values = []
        for position in positions:
            price = current_price(self._manager, position)
            pnl = position_pnl(position, price)
            if pnl is not None:
                values.append(pnl)
        return sum(values) if values else None

    def _add_position_item(self, group_item: QTreeWidgetItem, position: Position) -> None:
        # 損益現在直接用現價比均價算(見 app/models/positions.py position_pnl
        # 的說明)，現價/損益兩個欄位共用同一個 price 值，才不會又各算各的兜不起來。
        price = current_price(self._manager, position)
        pnl = position_pnl(position, price)
        item = QTreeWidgetItem([
            _symbol_text(position),
            _right_text(position),
            _direction_text(position),
            str(position.qty),
            f"{position.avg_cost:g}",
            f"{price:g}" if price is not None else "—",
            "",
            "",
        ])
        item.setData(0, Qt.UserRole, ("position", position.symbol_key))
        self._set_pnl_cell(item, 6, pnl)
        if position.is_combo:
            item.setToolTip(6, "複式單合併部位：損益是現價(淨價差)比均價(整組合計淨權利金)，見 app/models/positions.py 的 current_price/position_pnl")
        group_item.addChild(item)
        self._add_rule_child(item, position, "take_profit")
        self._add_rule_child(item, position, "stop_loss")
        item.setExpanded(True)

    # ------------------------------------------------------------------ 停利/停損子節點
    def _add_rule_child(self, parent_item: QTreeWidgetItem, position: Position, kind: str) -> None:
        rules = self._auto_close.get_position_rules(position.symbol_key)
        rule = rules.take_profit if kind == "take_profit" else rules.stop_loss
        icon = _STATUS_ICONS.get(rule.status if rule else STATUS_UNSET, "⚪")
        label = _RULE_KIND_LABELS[kind]
        child = QTreeWidgetItem([
            f"{icon} {label}：{self._rule_summary(kind, rule)}", "", "", "", "", "",
            self._rule_status_text(rule), "",
        ])
        child.setData(0, Qt.UserRole, ("rule", position.symbol_key, kind))
        child.setToolTip(0, _rich_tooltip("門檻比較的是「現價跟均價」的點數差本身，不會乘口數，這個價差不管幾口門檻判斷都一樣。"))
        if rule is not None and rule.status == STATUS_FAILED:
            child.setForeground(6, QBrush(QColor("#c0392b")))
        parent_item.addChild(child)
        self.tree.setItemWidget(child, 7, self._build_rule_actions_widget(position, kind, rule))

    def _rule_summary(self, kind: str, rule) -> str:
        if rule is None:
            return "未設定"
        if kind == "take_profit":
            text = f"獲利達 {rule.threshold_points:g} 點 → 平倉"
            if rule.reopen is not None:
                text += f"+原地重開@{rule.reopen.strike:g}/{rule.reopen.price:g}"
            return text
        text = f"虧損達 {rule.threshold_points:g} 點 → "
        if rule.mode == SL_MODE_REOPEN_DOUBLE and rule.reopen is not None:
            return text + f"規則3，重開@{rule.reopen.strike:g}/{rule.reopen.price:g}(雙倍口數)"
        if rule.mode == SL_MODE_NEW_GROUP:
            return text + "規則4，平倉+另開一整組新價差"
        return text + "規則5，不平倉+加開對側裸賣一支腳"

    def _rule_status_text(self, rule) -> str:
        if rule is None:
            return "未設定"
        if rule.status == STATUS_ARMED:
            return "監控中"
        if rule.status == STATUS_PAUSED:
            return "已暫停"
        if rule.status == STATUS_TRIGGERED:
            ts = time.strftime("%H:%M:%S", time.localtime(rule.triggered_at)) if rule.triggered_at else "?"
            return f"已觸發於 {ts}"
        if rule.status == STATUS_FAILED:
            return "失敗，需人工排查"
        return "未設定"

    def _build_rule_actions_widget(self, position: Position, kind: str, rule) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        edit_btn = QPushButton("設定")
        edit_btn.clicked.connect(lambda: self._on_edit_rule(position, kind))
        layout.addWidget(edit_btn)
        if rule is not None and rule.status in (STATUS_ARMED, STATUS_PAUSED):
            toggle = QPushButton("暫停" if rule.status == STATUS_ARMED else "啟用")
            new_status = STATUS_PAUSED if rule.status == STATUS_ARMED else STATUS_ARMED
            toggle.clicked.connect(lambda: self._auto_close.set_position_rule_status(position.symbol_key, kind, new_status))
            layout.addWidget(toggle)
        return widget

    def _on_edit_rule(self, position: Position, kind: str) -> None:
        rules = self._auto_close.get_position_rules(position.symbol_key)
        if kind == "take_profit":
            dialog = TakeProfitDialog(position, rules.take_profit, self)
            if dialog.exec_() == QDialog.Accepted:
                self._auto_close.set_take_profit(position.symbol_key, dialog.result_rule())
        else:
            sibling = self._auto_close.find_sibling(position)
            dialog = StopLossDialog(position, sibling, rules.stop_loss, self)
            if dialog.exec_() == QDialog.Accepted:
                self._auto_close.set_stop_loss(position.symbol_key, dialog.result_rule())

    # ------------------------------------------------------------------ 群組停利(規則1)
    def _build_group_actions_widget(self, group: PositionGroup) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        edit_btn = QPushButton("整組停利設定")
        edit_btn.setToolTip(_rich_tooltip("門檻比較的是群組內各價差「點數×口數」加權合計，跟單一價差停利/停損用純點數不一樣。"))
        edit_btn.clicked.connect(lambda: self._on_edit_group_rule(group))
        layout.addWidget(edit_btn)
        rule = self._auto_close.get_group_rule(group.group_id)
        status_label = QLabel(self._rule_status_text(rule))
        layout.addWidget(status_label)
        if rule is not None and rule.status in (STATUS_ARMED, STATUS_PAUSED):
            toggle = QPushButton("暫停" if rule.status == STATUS_ARMED else "啟用")
            new_status = STATUS_PAUSED if rule.status == STATUS_ARMED else STATUS_ARMED
            toggle.clicked.connect(lambda: self._auto_close.set_group_rule_status(group.group_id, new_status))
            layout.addWidget(toggle)
        return widget

    def _on_edit_group_rule(self, group: PositionGroup) -> None:
        existing = self._auto_close.get_group_rule(group.group_id)
        dialog = GroupTakeProfitDialog(group.name, existing, self)
        if dialog.exec_() == QDialog.Accepted:
            self._auto_close.set_group_rule(group.group_id, dialog.result_rule())

    def _set_pnl_cell(self, item: QTreeWidgetItem, col: int, pnl: Optional[float]) -> None:
        if pnl is None:
            item.setText(col, "—")
            return
        item.setText(col, f"{pnl:,.0f}")
        item.setForeground(col, QBrush(COLOR_UP_TEXT if pnl >= 0 else COLOR_DOWN_TEXT))

    # ------------------------------------------------------------------ 分組操作
    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if data is None or data[0] == "rule":
            return  # 停利/停損子節點沒有右鍵選單，操作都在「動作」欄的按鈕上
        kind, key = data
        menu = QMenu(self)
        if kind == "position":
            self._build_position_menu(menu, key)
        else:
            self._build_group_menu(menu, key)
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def _build_position_menu(self, menu: QMenu, symbol_key: str) -> None:
        move_menu = menu.addMenu("移到群組")
        for group_id, info in self._list_group_infos():
            action = move_menu.addAction(info["name"])
            action.triggered.connect(lambda _checked, gid=group_id: self._move_to_group(symbol_key, gid))
        move_menu.addSeparator()
        new_action = move_menu.addAction("新群組...")
        new_action.triggered.connect(lambda: self._create_group_and_move(symbol_key))
        ungroup_action = menu.addAction("取消分組")
        ungroup_action.triggered.connect(lambda: self._move_to_group(symbol_key, None))

    def _list_group_infos(self):
        return list(position_groups_store.list_groups().items())

    def _build_group_menu(self, menu: QMenu, group_id: str) -> None:
        if group_id == UNGROUPED_ID:
            return  # 「未分組」是固定虛擬群組，不能改名/改色/刪除
        rename_action = menu.addAction("重新命名...")
        rename_action.triggered.connect(lambda: self._rename_group(group_id))
        color_action = menu.addAction("設定顏色...")
        color_action.triggered.connect(lambda: self._set_group_color(group_id))
        delete_action = menu.addAction("刪除群組")
        delete_action.triggered.connect(lambda: self._delete_group(group_id))

    def _move_to_group(self, symbol_key: str, group_id) -> None:
        self._manager.move_to_group(symbol_key, group_id)
        self._refresh()

    def _create_group_and_move(self, symbol_key: str) -> None:
        name, ok = QInputDialog.getText(self, "新群組", "群組名稱")
        name = name.strip() if ok else ""
        if not name:
            return
        group_id = self._manager.create_group(name, _DEFAULT_GROUP_COLOR)
        self._move_to_group(symbol_key, group_id)

    def _rename_group(self, group_id: str) -> None:
        name, ok = QInputDialog.getText(self, "重新命名群組", "群組名稱")
        name = name.strip() if ok else ""
        if not name:
            return
        self._manager.rename_group(group_id, name)
        self._refresh()

    def _set_group_color(self, group_id: str) -> None:
        color = QColorDialog.getColor(QColor(_DEFAULT_GROUP_COLOR), self, "設定群組顏色")
        if not color.isValid():
            return
        self._manager.set_group_color(group_id, color.name())
        self._refresh()

    def _delete_group(self, group_id: str) -> None:
        self._manager.delete_group(group_id)
        self._refresh()
