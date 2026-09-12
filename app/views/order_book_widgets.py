import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QFormLayout, QTableWidget,
    QTableWidgetItem, QWidget, QPushButton, QLabel, QHeaderView,
    QSpinBox, QDoubleSpinBox, QInputDialog, QDialog, QDialogButtonBox,
)

from app.models.order_book import OrderBookManager, STATUS_STAGED, STATUS_LIVE, STATUS_FILLED, STATUS_REJECTED

# *** 跟舊版(群益)的重要差異 ***
# 1. 沒有「連續送單中/已暫停」狀態：那套引擎已經整套移除(IB combo 可以
#    直接掛 DAY/GTC 跡在單子上等成交，不需要監看報價重送)，狀態收斂成
#    staged/live/filled/rejected/cancelled，動作也跟著簡化(改條件/暫停/
#    繼續都拿掉)。
# 2. 沒有「委託頻率保護(SetMaxQty/SetMaxCount)」：那是群益 SKCOM 特有的
#    client端限速設定，IB 沒有對應的 API，直接拿掉整個區塊。
# 3. 沒有「既有委託查詢(GetOrderReport)」按鈕：IB 的委託狀態本來就是即
#    時事件推送(orderStatusEvent)，不需要像群益那樣手動查、還要顧慮5秒
#    的查詢間隔限制。

_STATUS_LABELS = {
    STATUS_STAGED: "待送出",
    STATUS_LIVE: "掛單中",
    STATUS_FILLED: "已成交",
    STATUS_REJECTED: "失敗",
    "cancelled": "已取消",
}

_BOX_COLUMNS = ["商品", "買權/賣權", "方向", "價格", "口數", "委託條件", "狀態", "動作"]
_FILL_COLUMNS = ["商品", "買權/賣權", "方向", "成交價", "成交量", "時間"]

_RIGHT_LABELS = {"C": "買權", "P": "賣權"}


def _direction_text(record) -> str:
    # 複式單一定要看 net_buyer，不能看 legs[0].buy——兩者不是同一件事
    # (見 order_book.py::OrderRecord.net_buyer 的說明)。
    if record.kind == "duplex":
        return "買方" if record.net_buyer else "賣方"
    return "買進" if record.legs[0].buy else "賣出"


def _right_text(record) -> str:
    for leg in record.legs:
        if leg.right:
            return _RIGHT_LABELS.get(leg.right, leg.right)
    return ""


def _set_cell(table: QTableWidget, row: int, col: int, text: str):
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignCenter)
    table.setItem(row, col, item)


def _build_table(columns) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
    header.setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    return table


class OrderBookWidget(QWidget):
    """下單匣：尚未成交的委託表格(待送出/掛單中/失敗/已取消)。跟
    FillReportWidget 共用同一個 OrderBookManager，各自是獨立的 dock
    widget 內容，可以分開擺放。"""

    def __init__(self, manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._manager.records_changed.connect(self._refresh)
        self._manager.order_book_error.connect(self._on_error)

        layout = QVBoxLayout(self)

        self.table = _build_table(_BOX_COLUMNS)
        layout.addWidget(self.table)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._refresh()

    def _on_error(self, message: str):
        self.status_label.setText(f"操作失敗：{message}")

    # ------------------------------------------------------------- 表格重繪
    def _refresh(self):
        box_records = [r for r in self._manager.records if r.status != STATUS_FILLED]

        self.table.setRowCount(len(box_records))
        for row, record in enumerate(box_records):
            _set_cell(self.table, row, 0, record.label())
            _set_cell(self.table, row, 1, _right_text(record))
            _set_cell(self.table, row, 2, _direction_text(record))
            _set_cell(self.table, row, 3, f"{record.price:g}")
            _set_cell(self.table, row, 4, str(record.qty))
            _set_cell(self.table, row, 5, record.tif)
            status_text = _STATUS_LABELS.get(record.status, record.status)
            if record.status == STATUS_REJECTED and record.error_msg:
                status_text += f" ({record.error_msg})"
            _set_cell(self.table, row, 6, status_text)
            self.table.setCellWidget(row, 7, self._build_actions_widget(record))

    def _build_actions_widget(self, record) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        def add_btn(text, handler):
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            layout.addWidget(btn)

        if record.status == STATUS_STAGED:
            add_btn("送出", lambda: self._manager.confirm_send(record.id))
            add_btn("刪除", lambda: self._manager.discard_staged(record.id))
        elif record.status == STATUS_LIVE:
            add_btn("改價", lambda: self._prompt_amend_price(record.id))
            add_btn("改量", lambda: self._prompt_amend_qty(record.id))
            add_btn("刪單", lambda: self._manager.cancel(record.id))
        # rejected/cancelled：不給操作，純顯示

        return widget

    def _prompt_amend_price(self, record_id: str):
        record = next((r for r in self._manager.records if r.id == record_id), None)
        if record is None:
            return
        price, ok = QInputDialog.getDouble(self, "改價", "新委託價格", record.price, 0.01, 99999, 2, step=0.05)
        if ok:
            self._manager.amend_price(record_id, price)

    def _prompt_amend_qty(self, record_id: str):
        record = next((r for r in self._manager.records if r.id == record_id), None)
        if record is None:
            return
        qty, ok = QInputDialog.getInt(self, "改量", "新口數", int(record.qty), 1, 999)
        if ok:
            self._manager.amend_qty(record_id, qty)


class FillReportWidget(QWidget):
    """成交回報：唯讀表格，跟 OrderBookWidget 共用同一個 OrderBookManager
    的 records_changed 訊號，各自是獨立的 dock widget 內容。"""

    def __init__(self, manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._manager.records_changed.connect(self._refresh)

        layout = QVBoxLayout(self)
        self.table = _build_table(_FILL_COLUMNS)
        layout.addWidget(self.table)

        self._refresh()

    def _refresh(self):
        fill_records = [r for r in self._manager.records if r.status == STATUS_FILLED]

        self.table.setRowCount(len(fill_records))
        for row, record in enumerate(fill_records):
            _set_cell(self.table, row, 0, record.label())
            _set_cell(self.table, row, 1, _right_text(record))
            _set_cell(self.table, row, 2, _direction_text(record))
            _set_cell(self.table, row, 3, str(record.fill_price or ""))
            _set_cell(self.table, row, 4, str(record.fill_qty or ""))
            filled_at = ""
            if record.filled_at:
                filled_at = datetime.datetime.fromtimestamp(record.filled_at).strftime("%Y-%m-%d %H:%M:%S")
            _set_cell(self.table, row, 5, filled_at)
