import pythoncom
import win32event
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QComboBox, QSpinBox, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QHeaderView, QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

import taifex_symbols as sym
from rtd_client import RTDClient

CALL_BG = QBrush(QColor("#fff2f2"))
PUT_BG = QBrush(QColor("#f0f7ff"))
STRIKE_BG = QBrush(QColor("#eeeeee"))

COLUMNS = ["買價", "賣價", "成交價", "履約價", "成交價", "買價", "賣價"]
CALL_COLS = {"bid": 0, "ask": 1, "last": 2}
STRIKE_COL = 3
PUT_COLS = {"last": 4, "bid": 5, "ask": 6}

FIELD_BID = "TF-Bid"
FIELD_ASK = "TF-Ask"
FIELD_LAST = "TF-Price"

# 加權指數(TSE)在 RTD 上的商品代碼跟欄位，用來自動帶入中心履約價。
# 注意欄位前綴是 TW- 不是 TF-（TF- 是期貨/選擇權專用，TW- 是大盤指數專用），
# 已用 uv run python 實測驗證過：TSE.TW-Open 真的會回傳當天開盤指數。
TAIEX_INDEX_SYMBOL = "TSE"
TAIEX_OPEN_FIELD = "TW-Open"

PUMP_INTERVAL_MS = 200


class MainWindow(QMainWindow):
    def __init__(self, kgi_client=None):
        super().__init__()
        self.setWindowTitle("台指選擇權 T 字報價 (華南 XQ RTD)")
        self.resize(1100, 700)

        self.kgi_client = kgi_client  # 登入後的凱基 api 物件，之後下單功能會用到

        self.rtd = RTDClient(on_update=self._on_rtd_update)
        self.rtd_connected = False
        self.topic_row_col = {}  # topic_id -> (row, col)
        self.taiex_open_topic_id = None
        self.center_auto_filled = False

        self.pump_timer = QTimer(self)
        self.pump_timer.setInterval(PUMP_INTERVAL_MS)
        self.pump_timer.timeout.connect(self._pump_and_refresh)

        self._build_ui()
        self._populate_expiry_list()
        self._connect_rtd()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addWidget(self._build_query_box())
        root.addWidget(self._build_table())

        self.status_label = QLabel("尚未連接 RTD")
        root.addWidget(self.status_label)

    def _build_query_box(self) -> QGroupBox:
        box = QGroupBox("選擇權合約查詢")
        layout = QHBoxLayout(box)

        self.expiry_combo = QComboBox()

        self.center_spin = QSpinBox()
        self.center_spin.setRange(0, 100000)
        self.center_spin.setSingleStep(100)
        self.center_spin.setValue(22000)

        self.step_spin = QSpinBox()
        self.step_spin.setRange(1, 5000)
        self.step_spin.setSingleStep(50)
        self.step_spin.setValue(100)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 40)
        self.rows_spin.setValue(10)

        self.subscribe_btn = QPushButton("查詢並訂閱")
        self.subscribe_btn.clicked.connect(self._on_subscribe_clicked)

        self.unsubscribe_btn = QPushButton("停止")
        self.unsubscribe_btn.clicked.connect(self._on_unsubscribe_clicked)

        form = QFormLayout()
        form.addRow("到期別", self.expiry_combo)

        layout.addLayout(form)
        layout.addWidget(QLabel("中心履約價"))
        layout.addWidget(self.center_spin)
        layout.addWidget(QLabel("價格間距"))
        layout.addWidget(self.step_spin)
        layout.addWidget(QLabel("上下各幾檔"))
        layout.addWidget(self.rows_spin)
        layout.addWidget(self.subscribe_btn)
        layout.addWidget(self.unsubscribe_btn)
        return box

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        return self.table

    # ----------------------------------------------------------- RTD 連線
    def _connect_rtd(self):
        """登入後自動連接，不需要使用者按按鈕。"""
        try:
            self.rtd.start()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"RTD 連接失敗: {exc}")
            QMessageBox.critical(self, "RTD 連接失敗", str(exc))
            return
        self.rtd_connected = True
        self.pump_timer.start()
        self.status_label.setText("已連接 RTD，可以開始查詢/訂閱")
        self._subscribe_taiex_open()

    def _subscribe_taiex_open(self):
        if not TAIEX_INDEX_SYMBOL:
            return
        try:
            topic_id, initial = self.rtd.subscribe(f"{TAIEX_INDEX_SYMBOL}.{TAIEX_OPEN_FIELD}")
        except Exception:  # noqa: BLE001
            return
        self.taiex_open_topic_id = topic_id
        self._try_apply_taiex_open(initial)

    def _try_apply_taiex_open(self, value):
        if self.center_auto_filled or value in (None, "", "--", "#N/A"):
            return
        try:
            price = float(value)
        except (TypeError, ValueError):
            return
        # 選擇權履約價是以「價格間距」為級距報價的整數(例如百位)，
        # 開盤價本身(帶小數)不是有效履約價，要先取整到最近的級距倍數，
        # 不然算出來的 Call/Put 代碼全部對不上實際合約。
        step = self.step_spin.value() or 100
        center = int(round(price / step) * step)
        self.center_spin.setValue(center)
        self.center_auto_filled = True
        self.status_label.setText(f"已自動帶入加權指數開盤價 {price} → 中心履約價 {center}")

    # ------------------------------------------------------------- 查詢邏輯
    def _populate_expiry_list(self):
        self.expiry_combo.clear()
        for expiry in sym.list_all_expiries():
            self.expiry_combo.addItem(expiry.label, expiry)

    def _on_subscribe_clicked(self):
        if not self.rtd_connected:
            QMessageBox.warning(self, "提醒", "RTD 尚未連接")
            return

        expiry = self.expiry_combo.currentData()
        if expiry is None:
            QMessageBox.warning(self, "提醒", "請先選擇到期別")
            return

        center = self.center_spin.value()
        step = self.step_spin.value()
        rows = self.rows_spin.value()
        strikes = [center + i * step for i in range(-rows, rows + 1)]

        self._clear_subscriptions()
        self.table.setRowCount(len(strikes))

        for row, strike in enumerate(strikes):
            call_symbol = sym.build_symbol(expiry.product_code, strike, expiry.expiry_date, is_call=True)
            put_symbol = sym.build_symbol(expiry.product_code, strike, expiry.expiry_date, is_call=False)

            self._set_cell(row, STRIKE_COL, str(strike), STRIKE_BG, bold=True)
            self._init_side(row, CALL_COLS, CALL_BG)
            self._init_side(row, PUT_COLS, PUT_BG)

            self._subscribe_field(row, CALL_COLS["bid"], call_symbol, FIELD_BID)
            self._subscribe_field(row, CALL_COLS["ask"], call_symbol, FIELD_ASK)
            self._subscribe_field(row, CALL_COLS["last"], call_symbol, FIELD_LAST)
            self._subscribe_field(row, PUT_COLS["last"], put_symbol, FIELD_LAST)
            self._subscribe_field(row, PUT_COLS["bid"], put_symbol, FIELD_BID)
            self._subscribe_field(row, PUT_COLS["ask"], put_symbol, FIELD_ASK)

        preview_call = sym.build_symbol(expiry.product_code, strikes[0], expiry.expiry_date, True)
        preview_put = sym.build_symbol(expiry.product_code, strikes[0], expiry.expiry_date, False)
        self.status_label.setText(
            f"已訂閱 {len(strikes)} 檔履約價 (共 {len(strikes) * 6} 個 RTD topic)｜"
            f"例如第一檔代碼: {preview_call} / {preview_put}"
        )

    def _subscribe_field(self, row: int, col: int, symbol: str, field: str):
        topic_str = f"{symbol}.{field}"
        try:
            topic_id, initial = self.rtd.subscribe(topic_str)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"訂閱 {topic_str} 失敗: {exc}")
            return
        self.topic_row_col[topic_id] = (row, col)
        if initial not in (None, "", "--"):
            self._update_cell(row, col, initial)

    def _on_unsubscribe_clicked(self):
        self._clear_subscriptions()
        self.table.setRowCount(0)
        self.status_label.setText("已取消全部訂閱")

    def _clear_subscriptions(self):
        for topic_id in list(self.topic_row_col.keys()):
            try:
                self.rtd.unsubscribe(topic_id)
            except Exception:  # noqa: BLE001
                pass
        self.topic_row_col.clear()

    def _init_side(self, row: int, cols: dict, bg: QBrush):
        for col in cols.values():
            self._set_cell(row, col, "", bg)

    def _set_cell(self, row: int, col: int, text: str, bg: QBrush, bold: bool = False):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        item.setBackground(bg)
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------- 行情更新
    def _on_rtd_update(self):
        # 由 RTD Server 的原生 COM callback 觸發 (透過 pump_timer 幫忙抽訊息才會送達)，
        # 真正拉資料的動作交給 _pump_and_refresh 統一做，這裡不用做事。
        pass

    def _pump_and_refresh(self):
        if not self.rtd_connected:
            return
        try:
            win32event.MsgWaitForMultipleObjects([], False, 0, win32event.QS_ALLINPUT)
            pythoncom.PumpWaitingMessages()
            data = self.rtd.refresh()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"RTD 讀取失敗: {exc}")
            self.pump_timer.stop()
            return

        if not data:
            return

        if self.taiex_open_topic_id is not None and not self.center_auto_filled:
            taiex_topic_str = self.rtd.topics.get(self.taiex_open_topic_id)
            if taiex_topic_str in data:
                self._try_apply_taiex_open(data[taiex_topic_str])

        for topic_id, topic_str in list(self.rtd.topics.items()):
            if topic_str not in data:
                continue
            entry = self.topic_row_col.get(topic_id)
            if entry is None:
                continue
            row, col = entry
            self._update_cell(row, col, data[topic_str])

    def _update_cell(self, row: int, col: int, value):
        if value is None:
            return
        item = self.table.item(row, col)
        if item is None:
            return
        item.setText(self._fmt(value))

    @staticmethod
    def _fmt(value) -> str:
        if isinstance(value, float):
            if value == int(value):
                return str(int(value))
            return f"{value:.2f}"
        return str(value)

    def closeEvent(self, event):
        self.pump_timer.stop()
        self._clear_subscriptions()
        if self.rtd_connected:
            self.rtd.stop()
        super().closeEvent(event)
