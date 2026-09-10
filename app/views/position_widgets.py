from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QVBoxLayout, QWidget, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QInputDialog, QColorDialog, QLabel, QPushButton, QHBoxLayout,
)

from app.models.positions import PositionManager, Position, UNGROUPED_ID
from app.services import position_groups_store
from app.services.payoff import PayoffLeg, leg_payoff_at

# 跟 main_window.py:44-45 的紅漲綠跌是同一組顏色常數，這裡不 import
# main_window(避免循環 import：main_window 要 import 這個檔案來建立
# dock)，數值保持同步即可。
COLOR_UP_TEXT = QColor("#e05050")
COLOR_DOWN_TEXT = QColor("#3ecf6e")

_COLUMNS = ["商品/群組", "買權/賣權", "方向", "口數", "均價", "現價", "損益", "動作"]
_CALL_PUT_LABELS = {"C": "買權", "P": "賣權"}

_DEFAULT_GROUP_COLOR = "#4a90d9"


def _position_payoff(position: Position, underlying_price: Optional[float]) -> Optional[float]:
    """用加權指數現貨價當 S 算目前浮動損益(履約後的內含價值，不是到期損
    益)——這裡只是給部位表格「損益」欄位一個粗略即時數字，跟
    payoff_chart_widget.py 的到期損益圖是兩回事，不要混用。

    *** S 一定要是加權指數現貨價，不是選擇權自己的成交價 ***：之前這裡
    錯把 latest_price(選擇權自己的合約代碼) 當 S 傳進來，選擇權成交價
    (例如248點)跟履約價(例如45900)量級完全不同，算出來的內含價值是垃圾
    數字——只是因為當時複式單被下面的邏輯擋掉沒顯示、單腳部位還沒出現才
    沒被發現。現在呼叫端要傳 PositionManager.underlying_price。

    逐腳方向/淨權利金怎麼分配交給 Position.payoff_legs() 統一處理(跟
    payoff_chart_widget.py `_position_legs` 共用同一個來源，不要在這裡
    重複一份邏輯)，這裡只負責把每一腳丟進 leg_payoff_at 加總。"""
    if underlying_price is None:
        return None
    payoff_legs = position.payoff_legs()
    if payoff_legs is None:
        return None
    total = 0.0
    for leg, buy, premium in payoff_legs:
        pleg = PayoffLeg(
            strike=leg.strike, call_put=leg.call_put, buy=buy,
            qty=position.qty, premium=premium, multiplier=leg.multiplier,
        )
        total += leg_payoff_at(underlying_price, pleg)
    return total


def _direction_text(position: Position) -> str:
    if position.buy is None:
        return "不明(買賣別待確認)"
    return "買進" if position.buy else "賣出"


def _call_put_text(position: Position) -> str:
    labels = {_CALL_PUT_LABELS.get(leg.call_put, leg.call_put) for leg in position.legs}
    return "/".join(sorted(labels))


def _symbol_text(position: Position) -> str:
    if position.is_combo:
        return " / ".join(f"{leg.symbol}(履約{int(leg.strike)})" for leg in position.legs)
    return position.legs[0].symbol


class PositionTreeWidget(QWidget):
    """未平倉部位：樹狀分組表格。頂層節點是群組(含固定的「未分組」)，子
    節點是逐列部位(TM 複式單合併列算一列，不會再往下拆兩腳個別顯示——
    parse_combo_symbol 拆出來的兩腳是給損益計算/自動分組用，畫面上使用者
    要看到的是「這是一組複式部位」，不是硬拆成兩列)。"""

    def __init__(self, manager: PositionManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._manager.positions_changed.connect(self._refresh)
        self._manager.query_failed.connect(self._on_query_failed)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_toolbar())

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(_COLUMNS)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
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
        toolbar.addStretch(1)
        return toolbar

    def _on_refresh_clicked(self) -> None:
        if not self._manager.can_refresh():
            self.status_label.setText("查詢太頻繁，請稍後再試(官方文件對這支查詢沒有明講最低間隔，這裡保守套用5秒節流)")
            return
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
            for position in group.positions:
                self._add_position_item(group_item, position)
            group_item.setExpanded(True)
        self.status_label.setText(f"共 {sum(len(g.positions) for g in groups)} 筆部位")

    def _group_pnl(self, positions) -> Optional[float]:
        underlying_price = self._manager.underlying_price
        values = []
        for position in positions:
            pnl = _position_payoff(position, underlying_price)
            if pnl is not None:
                values.append(pnl)
        return sum(values) if values else None

    def _add_position_item(self, group_item: QTreeWidgetItem, position: Position) -> None:
        # 「現價」欄位顯示選擇權自己這口合約目前的成交價(給使用者看行情
        # 用)，跟算損益要用的加權指數現貨價是兩回事，不要共用同一個變數
        # (先前的 bug 就是把這兩者混為一談)。
        price = self._manager.latest_price(position.legs[0].symbol)
        pnl = _position_payoff(position, self._manager.underlying_price)
        item = QTreeWidgetItem([
            _symbol_text(position),
            _call_put_text(position),
            _direction_text(position),
            str(position.qty),
            f"{position.avg_cost:g}",
            f"{price:g}" if price is not None else "—",
            "",
            "",
        ])
        item.setData(0, Qt.UserRole, ("position", position.symbol_key))
        self._set_pnl_cell(item, 6, pnl)
        if position.buy is None:
            for col in range(len(_COLUMNS)):
                item.setForeground(col, QBrush(QColor("#c0392b")))
        elif position.is_combo:
            item.setToolTip(6, "複式單合併部位：損益假設兩腳一買一賣(價差)、均價為整組合計淨權利金，見 Position.payoff_legs()")
        group_item.addChild(item)

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
        kind, key = item.data(0, Qt.UserRole)
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
