"""
選擇權篩選器一系列 dialog/widget 共用的小型排版與元件 helper，原本是
ScreenerWidget 內部的 @staticmethod，因為 scanner_filter_picker.py／
scan_code_picker.py／filter_assistant_dialog.py 都要重用，搬出來當模組
級函式，避免跨類別呼叫 ScreenerWidget._field(...) 這種寫法。純搬移，行
為不變。
"""
from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QLayout, QVBoxLayout, QWidget


def field_card(caption: str, widget: QWidget, tooltip: str = "") -> QVBoxLayout:
    """「標題在上、控制項在下」的一張小卡片，取代 QFormLayout/inline
    QLabel 那種標籤跟輸入框硬擠在同一列的排法——字一多、dock 一窄就會全
    部黏在一起看不清楚。`tooltip`(通常是 rich_tooltip() 包過的 HTML)有
    帶就同時掛在標籤跟控制項上，滑到哪裡都看得到說明。"""
    col = QVBoxLayout()
    col.setSpacing(2)
    caption_label = QLabel(caption)
    font = caption_label.font()
    font.setBold(True)
    font.setPointSize(max(font.pointSize() - 1, 7))
    caption_label.setFont(font)
    if tooltip:
        caption_label.setToolTip(tooltip)
        widget.setToolTip(tooltip)
    col.addWidget(caption_label)
    col.addWidget(widget)
    return col


def range_widget(min_widget: QWidget, max_widget: QWidget) -> QWidget:
    """Min ~ Max 兩個輸入框放進同一張卡片，中間夾一個「~」，取代原本各
    自獨立佔一組標籤+輸入框的寫法。"""
    wrapper = QWidget()
    row = QHBoxLayout(wrapper)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(min_widget)
    row.addWidget(QLabel("~"))
    row.addWidget(max_widget)
    return wrapper


def make_optional_spin(maximum: float, suffix: str = "", decimals: int = 0) -> QDoubleSpinBox:
    """0 代表「不設這個門檻」——用 QDoubleSpinBox 的特殊值文字顯示成「不
    限」，比另外用 checkbox 開關簡單。"""
    spin = QDoubleSpinBox()
    spin.setRange(0, maximum)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setSpecialValueText("不限")
    spin.setValue(0)
    return spin


def rich_tooltip(text: str) -> str:
    """純文字的 tooltip 在 Qt 裡不會自動換行，一長串字會變成一整條很難
    看的橫向長條——包成簡單的 HTML(Qt 偵測到有 tag 就會走 QTextDocument
    排版)才會自動換行、限制寬度。跟 auto_close_dialog.py 的
    _rich_tooltip() 是同一個寫法。"""
    return f"<div style='max-width:320px; font-size:13pt;'>{text}</div>"


class FlowLayout(QLayout):
    """依可用寬度自動把子元件排成多行(超出寬度就換到下一行)，取代
    QHBoxLayout 那種只會無限往右延伸、元件一多就超出畫面(得靠橫向捲動才
    看得到)的排法——螢幕小螢幕一開始可以放差不多寬度，篩選條件一多，用
    QHBoxLayout就必然滿出去。PyQt/Qt 沒有內建這種排版，這是 Qt 官方
    「Flow Layout」範例的標準寫法(公開範例程式碼，非本專案獨創的演算
    法)，照搬過來給 screener_widget.py 的篩選條件列使用。

    *** 用法注意 ***：Qt 對「高度取決於寬度」(hasHeightForWidth)的
    layout 巢狀在另一個 layout 裡容易有重新計算高度的問題，穩妥的用法是
    包一層 QWidget 再用 addWidget()，不要直接對另一個 layout 呼叫
    addLayout(flow_layout)——見 screener_widget.py 的實際用法。"""

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 6, vspacing: int = 6):
        super().__init__(parent)
        self._hspacing = hspacing
        self._vspacing = vspacing
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def horizontalSpacing(self) -> int:
        return self._hspacing

    def verticalSpacing(self) -> int:
        return self._vspacing

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        x, y = effective_rect.x(), effective_rect.y()
        line_height = 0

        for item in self._items:
            item_size = item.sizeHint()
            next_x = x + item_size.width() + self._hspacing
            if next_x - self._hspacing > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + self._vspacing
                next_x = x + item_size.width() + self._hspacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y() + bottom
