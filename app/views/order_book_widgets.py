from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QWidget, QPushButton, QLabel, QHeaderView,
    QComboBox, QSpinBox, QInputDialog, QGroupBox,
)

from app.models.order_book import (
    OrderBookManager, STATUS_STAGED, STATUS_LIVE, STATUS_RETRYING,
    STATUS_PAUSED, STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED,
)
from app.models.capital_order_client import TIF_ROD, TIF_IOC, TIF_FOK

_TIF_LABELS = {TIF_ROD: "ROD", TIF_IOC: "IOC", TIF_FOK: "FOK"}

# IOC/FOK 送出後幾乎瞬間就成交或死亡，不可能「掛單中」(那個狀態只有 ROD
# 掛單才有意義)。IOC/FOK 送出後狀態一直卡在這裡，代表委託回報沒被
# _match_record 配對到——這是真的 bug，不是正常情況，用不同的字樣跟真
# 正的 ROD 掛單分開，不要謊稱「掛單中」。
_STATUS_LABELS = {
    STATUS_STAGED: "待送出",
    STATUS_LIVE: "掛單中",
    STATUS_RETRYING: "連續送單中",
    STATUS_PAUSED: "已暫停",
    STATUS_FILLED: "已成交",
    STATUS_REJECTED: "失敗",
    STATUS_CANCELLED: "已取消",
}
_STATUS_LIVE_NON_ROD_LABEL = "已送出(等待回報，若一直停在這裡代表回報配對失敗，是bug)"

_MARKET_TYPE_LABELS = {
    "TS(證券)": 0, "TF(期貨)": 1, "TO(選擇權)": 2,
    "OS(複委託)": 3, "OF(海期)": 4, "OO(海選)": 5,
}

_BOX_COLUMNS = ["商品", "方向", "價格", "口數", "委託條件", "狀態", "已送次數", "動作"]
_FILL_COLUMNS = ["商品", "方向", "成交價", "成交量", "時間"]


def _direction_text(record) -> str:
    # 複式單的 legs[0] 是依期交所編碼規則(履約價高低)排的，不是「買方
    # /賣方」那個語意，一定要看 net_buyer，不能看 legs[0].buy——這正
    # 是先前「下賣方價差顯示成買方」那個 bug 的成因，legs 重新排序後
    # 用 legs[0].buy 判斷方向的寫法都要避免。
    if record.kind == "duplex":
        return "買方" if record.net_buyer else "賣方"
    return "買進" if record.legs[0].buy else "賣出"


def _set_cell(table: QTableWidget, row: int, col: int, text: str):
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignCenter)
    table.setItem(row, col, item)


def _build_table(columns) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    return table


class OrderBookWidget(QWidget):
    """下單匣：委託頻率保護設定 + 尚未成交的委託表格 (待送出/掛單中/
    連續送單中/已暫停/失敗/已取消)。跟 FillReportWidget 共用同一個
    OrderBookManager，各自是獨立的 dock widget 內容，可以分開擺放。"""

    def __init__(self, manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._manager.records_changed.connect(self._refresh)
        self._manager.order_book_error.connect(self._on_error)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_safety_box())

        self.table = _build_table(_BOX_COLUMNS)
        layout.addWidget(self.table)

        # 查詢既有委託的原始字串可能很長，單行 QLabel 不換行的話會硬把
        # 整個視窗撐寬；開自動換行，長度也砍短，不讓內容決定視窗尺寸。
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._refresh()
        # 開窗當下先讓畫面畫出來，下一輪事件圈再做阻塞式查詢，不要卡在
        # 建構子裡讓整個畫面連畫都畫不出來。
        QTimer.singleShot(0, self._fetch_existing_orders)

    def _build_safety_box(self) -> QGroupBox:
        box = QGroupBox("委託頻率保護 (SetMaxQty/SetMaxCount，自己設的異常斷路器)")
        layout = QHBoxLayout(box)

        self.market_combo = QComboBox()
        self.market_combo.addItems(list(_MARKET_TYPE_LABELS.keys()))
        self.market_combo.setCurrentText("TO(選擇權)")

        self.max_qty_spin = QSpinBox()
        self.max_qty_spin.setRange(1, 999999)
        self.max_qty_spin.setValue(100)
        max_qty_btn = QPushButton("設定每秒委託量上限")
        max_qty_btn.clicked.connect(self._on_set_max_qty)

        self.max_count_spin = QSpinBox()
        self.max_count_spin.setRange(1, 999999)
        self.max_count_spin.setValue(20)
        max_count_btn = QPushButton("設定每秒委託筆數上限")
        max_count_btn.clicked.connect(self._on_set_max_count)

        unlock_btn = QPushButton("解鎖 (超過上限被鎖住時用)")
        unlock_btn.clicked.connect(self._on_unlock)

        refresh_btn = QPushButton("重新整理 (查既有委託，至少間隔5秒)")
        refresh_btn.clicked.connect(self._fetch_existing_orders)

        layout.addWidget(QLabel("市場別"))
        layout.addWidget(self.market_combo)
        layout.addWidget(self.max_qty_spin)
        layout.addWidget(max_qty_btn)
        layout.addWidget(self.max_count_spin)
        layout.addWidget(max_count_btn)
        layout.addWidget(unlock_btn)
        layout.addWidget(refresh_btn)
        return box

    # ------------------------------------------------------------- 頻率保護
    def _selected_market_type(self) -> int:
        return _MARKET_TYPE_LABELS[self.market_combo.currentText()]

    def _on_set_max_qty(self):
        err = self._manager.set_max_qty(self._selected_market_type(), self.max_qty_spin.value())
        self.status_label.setText("已設定每秒委託量上限" if err is None else f"設定失敗：{err}")

    def _on_set_max_count(self):
        err = self._manager.set_max_count(self._selected_market_type(), self.max_count_spin.value())
        self.status_label.setText("已設定每秒委託筆數上限" if err is None else f"設定失敗：{err}")

    def _on_unlock(self):
        err = self._manager.unlock_order(self._selected_market_type())
        self.status_label.setText("已解鎖" if err is None else f"解鎖失敗：{err}")

    # ------------------------------------------------------------- 既有委託
    def _fetch_existing_orders(self):
        if not self._manager.can_fetch_existing_orders():
            self.status_label.setText("查詢間隔要至少 5 秒，請稍後再試")
            return
        self.status_label.setText("查詢既有委託中...")
        try:
            result = self._manager.fetch_existing_orders()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"查詢既有委託失敗：{exc}")
            return
        # GetOrderReport 逐欄位格式官方文件沒查到對照表，先整包顯示，
        # 不試著解析成 OrderRecord (避免用猜的欄位位置寫錯資料)。字串可
        # 能很長，這裡只截一小段給個提示，長度砍短+有換行，不讓它撐開
        # 視窗；完整內容印在主控台方便對照。
        print(f"[GetOrderReport] {result}")
        preview = result[:200] + ("..." if len(result) > 200 else "")
        self.status_label.setText(f"既有委託查詢結果 (原始字串前 200 字，完整內容已印到主控台)：{preview}")

    def _on_error(self, message: str):
        self.status_label.setText(f"操作失敗：{message}")

    # ------------------------------------------------------------- 表格重繪
    def _refresh(self):
        box_records = [r for r in self._manager.records if r.status not in (STATUS_FILLED,)]

        self.table.setRowCount(len(box_records))
        for row, record in enumerate(box_records):
            _set_cell(self.table, row, 0, record.label())
            _set_cell(self.table, row, 1, _direction_text(record))
            _set_cell(self.table, row, 2, f"{record.price:g}")
            _set_cell(self.table, row, 3, str(record.qty))
            _set_cell(self.table, row, 4, _TIF_LABELS.get(record.tif, str(record.tif)))
            if record.status == STATUS_LIVE and record.tif != TIF_ROD:
                # IOC/FOK 送出後瞬間成交或死亡，不可能「掛單中」(那是 ROD
                # 掛單才有的狀態)；卡在這裡代表回報沒配對到，用不同字樣
                # 誠實顯示，不要謊稱掛單中。
                status_text = _STATUS_LIVE_NON_ROD_LABEL
            else:
                status_text = _STATUS_LABELS.get(record.status, record.status)
            if record.status == STATUS_REJECTED and record.error_msg:
                status_text += f" ({record.error_msg})"
            _set_cell(self.table, row, 5, status_text)
            _set_cell(self.table, row, 6, str(record.retry_count) if record.auto_retry else "")
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
            add_btn("改條件", lambda: self._prompt_change_condition(record.id))
            add_btn("刪除", lambda: self._manager.discard_staged(record.id))
        elif record.status == STATUS_RETRYING:
            add_btn("暫停", lambda: self._manager.pause_retry(record.id))
            add_btn("改條件", lambda: self._prompt_change_condition(record.id))
            add_btn("刪除", lambda: self._manager.delete(record.id))
        elif record.status == STATUS_PAUSED:
            add_btn("繼續", lambda: self._manager.resume_retry(record.id))
            add_btn("改條件", lambda: self._prompt_change_condition(record.id))
            add_btn("刪除", lambda: self._manager.delete(record.id))
        elif record.status == STATUS_LIVE and record.tif == TIF_ROD:
            add_btn("改價", lambda: self._prompt_amend_price(record.id))
            add_btn("減量", lambda: self._prompt_amend_qty(record.id))
            add_btn("刪單", lambda: self._manager.cancel(record.id))
        # rejected/cancelled/其他 live(單發IOC等回報中)：不給操作，純顯示

        return widget

    def _prompt_change_condition(self, record_id: str):
        record = next((r for r in self._manager.records if r.id == record_id), None)
        if record is None:
            return
        price, ok = QInputDialog.getDouble(self, "改條件", "新的權利金限價", record.price, 0.1, 99999, 1)
        if ok:
            self._manager.change_condition(record_id, price=price)

    def _prompt_amend_price(self, record_id: str):
        record = next((r for r in self._manager.records if r.id == record_id), None)
        if record is None:
            return
        price, ok = QInputDialog.getDouble(self, "改價", "新委託價格", record.price, 0.1, 99999, 1)
        if ok:
            self._manager.amend_price(record_id, price)

    def _prompt_amend_qty(self, record_id: str):
        record = next((r for r in self._manager.records if r.id == record_id), None)
        if record is None:
            return
        max_decrease = max(record.qty - 1, 1)
        qty, ok = QInputDialog.getInt(self, "減量", "要減少的口數 (只能減不能加)", 1, 1, max_decrease)
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
            _set_cell(self.table, row, 1, _direction_text(record))
            _set_cell(self.table, row, 2, str(record.fill_price or ""))
            _set_cell(self.table, row, 3, str(record.fill_qty or ""))
            report = record.last_report or {}
            _set_cell(self.table, row, 4, f"{report.get('date', '')} {report.get('time', '')}")
