"""
「＋ 新增篩選條件」搜尋新增對話框，跟每一列已加入的篩選條件元件——仿 IB
自己 TWS 桌面版 MultiSort 篩選器「搜尋後動態新增」的互動方式，取代舊版
screener_widget.py 寫死 3 個欄位(股價/市值/選擇權成交量)的做法。

*** 只收 value_type 是 "double"/"int" 的篩選欄位，"bool" 型的不列進來
***：IB 的篩選欄位裡有少數(7 個，例如 HASOPTIONS)是布林值，TagValue 的
value 慣例上要傳 "true"/"false" 字串，跟這裡 ScanFilterValue.value 統一
用 float 的設計不相容；這幾個大多也跟股票篩選關聯不大(多半是債券專
用)，先跳過，之後真的需要再另外處理成 checkbox。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app.models.scanner_catalog import FilterDef
from app.models.screener import ScanFilterValue
from app.views.widget_helpers import field_card, make_optional_spin, range_widget, rich_tooltip

# 沒有從 IB 的參數 XML 拿到每個欄位實際合理的數值上限，這裡統一給一個夠
# 寬鬆的上限，只是擋輸入框不要打出離譜的天文數字，不影響送給 IB 的實際
# 篩選行為(IB 自己會驗證/忽略不合理的值)。
_SPIN_MAX = 1_000_000_000.0


def _pickable(catalog: list[FilterDef]) -> list[FilterDef]:
    return [f for f in catalog if f.value_type in ("double", "int")]


class AddFilterDialog(QDialog):
    """搜尋＋依分類分組瀏覽的篩選條件挑選對話框。"""

    def __init__(self, catalog: list[FilterDef], already_added: set[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增篩選條件")
        self.resize(480, 560)
        self._catalog = [f for f in _pickable(catalog) if f.id not in already_added]
        self._selected_id: str | None = None

        layout = QVBoxLayout(self)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜尋篩選條件名稱或分類...")
        self.search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        layout.addWidget(self.buttons)

        self._populate_tree(self._catalog)

    def _populate_tree(self, filters: list[FilterDef]) -> None:
        self.tree.clear()
        by_category: dict[str, list[FilterDef]] = {}
        for f in filters:
            by_category.setdefault(f.category_zh or f.category_en, []).append(f)

        for category in sorted(by_category):
            cat_item = QTreeWidgetItem([category])
            cat_item.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(cat_item)
            for f in sorted(by_category[category], key=lambda x: x.label_zh or x.id):
                leaf = QTreeWidgetItem([f.label_zh or f.id])
                leaf.setData(0, Qt.UserRole, f.id)
                leaf.setToolTip(0, rich_tooltip(self._leaf_tooltip(f, category)))
                cat_item.addChild(leaf)
        self.tree.expandAll()

    @staticmethod
    def _leaf_tooltip(f: FilterDef, category_label: str) -> str:
        parts = [f"<b>{f.label_zh or f.id}</b>", f"分類：{category_label}（{f.category_en}）"]
        field_names = "、".join(field.name_en for field in f.fields)
        parts.append(f"IB 原始欄位：{field_names}")
        if f.tooltip_zh:
            parts.append(f.tooltip_zh)
        return "<br>".join(parts)

    def _on_search_changed(self, text: str) -> None:
        text = text.strip().lower()
        if not text:
            self._populate_tree(self._catalog)
            return
        matched = [
            f for f in self._catalog
            if text in (f.label_zh or "").lower()
            or text in (f.category_zh or "").lower()
            or text in f.id.lower()
            or text in (f.category_en or "").lower()
        ]
        self._populate_tree(matched)

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        is_leaf = bool(items) and items[0].data(0, Qt.UserRole) is not None
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(is_leaf)
        if is_leaf:
            self._selected_id = items[0].data(0, Qt.UserRole)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.data(0, Qt.UserRole) is not None:
            self._selected_id = item.data(0, Qt.UserRole)
            self.accept()

    def selected_filter_id(self) -> str | None:
        return self._selected_id


class FilterRowWidget(QWidget):
    """已加入的一列篩選條件——依 FilterDef.kind 建一或兩個數值輸入框，
    右邊一顆移除鈕。"""

    removed = pyqtSignal(str)  # 帶 filter_def.id

    def __init__(self, filter_def: FilterDef, parent=None):
        super().__init__(parent)
        self.filter_def = filter_def
        decimals = 2 if filter_def.value_type == "double" else 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tooltip = rich_tooltip(filter_def.tooltip_zh or filter_def.label_zh or filter_def.id)
        label = filter_def.label_zh or filter_def.id

        self._spins: list[tuple[str, object]] = []
        if filter_def.kind == "range":
            min_field, max_field = filter_def.fields
            min_spin = make_optional_spin(_SPIN_MAX, suffix=_suffix(min_field.unit_zh), decimals=decimals)
            max_spin = make_optional_spin(_SPIN_MAX, suffix=_suffix(max_field.unit_zh), decimals=decimals)
            self._spins = [(min_field.code, min_spin), (max_field.code, max_spin)]
            content = range_widget(min_spin, max_spin)
        else:
            (only_field,) = filter_def.fields
            spin = make_optional_spin(_SPIN_MAX, suffix=_suffix(only_field.unit_zh), decimals=decimals)
            self._spins = [(only_field.code, spin)]
            content = spin

        layout.addLayout(field_card(label, content, tooltip=tooltip), 1)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(24)
        remove_btn.setToolTip("移除這個篩選條件")
        remove_btn.clicked.connect(lambda: self.removed.emit(self.filter_def.id))
        layout.addWidget(remove_btn)

    def values(self) -> list[ScanFilterValue]:
        """只回傳有實際填值(非 0/不限)的欄位，跟舊版
        ScreenerWidget._optional_value() 的語意一致。"""
        result = []
        for code, spin in self._spins:
            value = spin.value()
            if value > 0:
                result.append(ScanFilterValue(code=code, value=value))
        return result

    def set_values(self, above: float | None, below: float | None) -> None:
        """給 filter_assistant_dialog.py 的 AI 建議預覽用——把 (above,
        below) 這種語意的值填回對應的輸入框，不用管 kind 是 range 還是
        simple。"""
        if self.filter_def.kind == "range":
            if above is not None:
                self._spins[0][1].setValue(above)
            if below is not None:
                self._spins[1][1].setValue(below)
        else:
            value = above if above is not None else below
            if value is not None:
                self._spins[0][1].setValue(value)

    def get_values_raw(self) -> tuple[float | None, float | None]:
        """回傳目前 (above, below) 兩個原始輸入值(0/不限視為 None)——跟
        values() 不同，這裡不濾掉未填的，是給「把這一列原封不動搬到另一
        個 FilterRowWidget」這種情境(AI 建議預覽套用到正式表單)用的。"""
        if self.filter_def.kind == "range":
            min_v, max_v = self._spins[0][1].value(), self._spins[1][1].value()
            return (min_v if min_v > 0 else None, max_v if max_v > 0 else None)
        value = self._spins[0][1].value()
        return (value if value > 0 else None, None)


def _suffix(unit_zh: str) -> str:
    return f" {unit_zh}" if unit_zh else ""
