from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QVBoxLayout, QWidget, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QInputDialog, QColorDialog, QLabel, QPushButton, QHBoxLayout,
)

from app.models.positions import PositionManager, Position, UNGROUPED_ID
from app.services import position_groups_store

# 跟 main_window.py:44-45 的紅漲綠跌是同一組顏色常數，這裡不 import
# main_window(避免循環 import：main_window 要 import 這個檔案來建立
# dock)，數值保持同步即可。
COLOR_UP_TEXT = QColor("#e05050")
COLOR_DOWN_TEXT = QColor("#3ecf6e")

_COLUMNS = ["商品/群組", "買權/賣權", "方向", "口數", "均價", "現價", "損益", "動作"]
_CALL_PUT_LABELS = {"C": "買權", "P": "賣權"}

_DEFAULT_GROUP_COLOR = "#4a90d9"


def _position_payoff(position: Position, current_price: Optional[float]) -> Optional[float]:
    """目前浮動損益：直接拿「現價」(即時成交價/複式單淨價差，跟畫面上
    現價欄位同一個值，見 _current_price) 對比「均價」——不是拿加權指數
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
    if position.buy is None or current_price is None:
        return None
    diff = (current_price - position.avg_cost) if position.buy else (position.avg_cost - current_price)
    return diff * position.qty * position.legs[0].multiplier


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


def _current_price(manager: PositionManager, position: Position) -> Optional[float]:
    """單腳部位直接顯示該合約現價。複式單(TM合併列)不能只顯示其中一腳的
    成交價——之前這裡就是這樣做，數字(例如207點)完全沒辦法跟均價(24點,
    整組淨權利金)放在一起比較，這就是使用者回報「現價計算異常」的原因。

    改成顯示「淨價差現價」= legs[1]現價 - legs[0]現價，跟 avg_cost/均價
    用同一套「legs[1] 扛淨權利金、legs[0] 反向抵消」慣例算出來的(見
    Position.payoff_legs() 的說明)，這樣現價才能直接拿來跟均價比較(現價
    低於均價=賣方部位還在賺，反之則已經虧)。任一腳報價還沒訂閱到就回
    None，畫面顯示「—」，不要用單腳報價湊出一個誤導的數字。"""
    if not position.is_combo:
        return manager.latest_price(position.legs[0].symbol)
    leg0_price = manager.latest_price(position.legs[0].symbol)
    leg1_price = manager.latest_price(position.legs[1].symbol)
    if leg0_price is None or leg1_price is None:
        return None
    return leg1_price - leg0_price


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
        values = []
        for position in positions:
            price = _current_price(self._manager, position)
            pnl = _position_payoff(position, price)
            if pnl is not None:
                values.append(pnl)
        return sum(values) if values else None

    def _add_position_item(self, group_item: QTreeWidgetItem, position: Position) -> None:
        # 損益現在直接用現價比均價算(見 _position_payoff 的說明)，現價/
        # 損益兩個欄位共用同一個 price 值，才不會又各算各的兜不起來。
        price = _current_price(self._manager, position)
        pnl = _position_payoff(position, price)
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
            item.setToolTip(6, "複式單合併部位：損益是現價(淨價差)比均價(整組合計淨權利金)，見 _current_price/_position_payoff")
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
