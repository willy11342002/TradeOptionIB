import datetime

import pythoncom
import win32event
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QHeaderView, QMessageBox, QTabWidget,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

from app.models import taifex_symbols as sym
from app.models.rtd_client import RTDClient
from app.services import theme

CALL_BG = QBrush(QColor("#fff2f2"))
PUT_BG = QBrush(QColor("#f0f7ff"))
STRIKE_BG = QBrush(QColor("#eeeeee"))
PRICE_BG = QBrush(QColor("white"))

COLOR_UP_TEXT = QColor("#cc0000")       # 上漲：白底紅字
COLOR_DOWN_TEXT = QColor("#008000")     # 下跌：白底綠字
COLOR_LIMIT_UP_BG = QColor("#cc0000")   # 漲停：紅底白字
COLOR_LIMIT_DOWN_BG = QColor("#008000")  # 跌停：綠底白字
COLOR_DEFAULT_TEXT = QColor("black")
COLOR_WHITE_TEXT = QColor("white")

COLUMNS = [
    "Delta", "Theta", "隱波%", "理論價", "買價", "賣價", "成交價",   # Call
    "履約價",
    "成交價", "買價", "賣價", "理論價", "隱波%", "Theta", "Delta",   # Put
]
CALL_COLS = {"delta": 0, "theta": 1, "iv": 2, "theory": 3, "bid": 4, "ask": 5, "last": 6}
STRIKE_COL = 7
PUT_COLS = {"last": 8, "bid": 9, "ask": 10, "theory": 11, "iv": 12, "theta": 13, "delta": 14}

FIELD_BID = "TF-Bid"
FIELD_ASK = "TF-Ask"
FIELD_LAST = "TF-Price"
FIELD_DELTA = "TF-Delta"
FIELD_THETA = "TF-Theta"
FIELD_IV = "TF-ImplyVolatility"
FIELD_THEORY = "TF-TheoryPrice"
FIELD_PRECLOSE = "TF-PreClose"
FIELD_UP_LIMIT = "TF-UpLimit"
FIELD_DOWN_LIMIT = "TF-DownLimit"

# CALL_COLS/PUT_COLS 共用的 key -> RTD 欄位名對照，訂閱時兩邊各自套用
FIELD_BY_KEY = {
    "bid": FIELD_BID,
    "ask": FIELD_ASK,
    "last": FIELD_LAST,
    "delta": FIELD_DELTA,
    "theta": FIELD_THETA,
    "iv": FIELD_IV,
    "theory": FIELD_THEORY,
}

# 需要漲跌顏色的欄位 (只有價格，不含 Delta/Theta/隱波/理論價)
PRICE_KEYS = ("bid", "ask", "last")

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
        self.ref_topic_info = {}  # topic_id -> (row, side, "preclose"/"up"/"down")
        self.price_ref = {}  # (row, side) -> {"preclose":.., "up":.., "down":..}
        self.price_last_value = {}  # (row, col) -> 最後一次收到的原始價格值，供收到參考值時重新上色
        self.taiex_open_topic_id = None
        self.center_auto_filled = False
        self.center_value = None  # 中心履約價，完全由 TSE 開盤價自動算出，不給手動改

        # 價格欄位 col -> 屬於 call 還是 put，漲跌停/漲跌顏色要分開比對
        self.col_side = {}
        for key in PRICE_KEYS:
            self.col_side[CALL_COLS[key]] = "call"
            self.col_side[PUT_COLS[key]] = "put"

        self.pump_timer = QTimer(self)
        self.pump_timer.setInterval(PUMP_INTERVAL_MS)
        self.pump_timer.timeout.connect(self._pump_and_refresh)

        self._build_ui()
        self._populate_expiry_list()
        self._connect_rtd()
        theme.apply_titlebar_theme(self)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        option_tab = QWidget()
        option_layout = QVBoxLayout(option_tab)
        option_layout.addWidget(self._build_query_box())
        option_layout.addWidget(self._build_side_header_box())
        option_layout.addWidget(self._build_table())
        self.tabs.addTab(option_tab, "選擇權報價")

        self.status_label = QLabel("尚未連接 RTD")
        root.addWidget(self.status_label)

    def _build_query_box(self) -> QGroupBox:
        box = QGroupBox("選擇權合約查詢")
        layout = QHBoxLayout(box)

        self.expiry_combo = QComboBox()
        self.expiry_combo.currentIndexChanged.connect(self._on_query_params_changed)

        self.center_label = QLabel("(等待加權指數開盤價...)")

        self.step_spin = QSpinBox()
        self.step_spin.setRange(1, 5000)
        self.step_spin.setSingleStep(50)
        self.step_spin.setValue(100)
        self.step_spin.valueChanged.connect(self._on_query_params_changed)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 40)
        self.rows_spin.setValue(10)
        self.rows_spin.valueChanged.connect(self._on_query_params_changed)

        form = QFormLayout()
        form.addRow("到期別", self.expiry_combo)

        layout.addLayout(form)
        layout.addWidget(QLabel("中心履約價"))
        layout.addWidget(self.center_label)
        layout.addWidget(QLabel("價格間距"))
        layout.addWidget(self.step_spin)
        layout.addWidget(QLabel("上下各幾檔"))
        layout.addWidget(self.rows_spin)
        return box

    def _build_side_header_box(self) -> QWidget:
        """買權(Call)/賣權(Put)分組標題列，用伸縮比例(7:1:7)對齊表格底下的
        Call 7欄 / 履約價 1欄 / Put 7欄，不是塞進 QTableWidget 本身(表格表頭
        不支援合併儲存格)。"""
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        call_label = QLabel("買權 (Call)")
        call_label.setAlignment(Qt.AlignCenter)
        call_label.setStyleSheet("background-color:#c0392b; color:white; font-weight:bold; padding:4px;")

        self.days_label = QLabel("")
        self.days_label.setAlignment(Qt.AlignCenter)
        self.days_label.setStyleSheet("background-color:#2c3e50; color:#ff6b6b; font-weight:bold; padding:4px;")

        put_label = QLabel("賣權 (Put)")
        put_label.setAlignment(Qt.AlignCenter)
        put_label.setStyleSheet("background-color:#16a085; color:white; font-weight:bold; padding:4px;")

        layout.addWidget(call_label, 7)
        layout.addWidget(self.days_label, 1)
        layout.addWidget(put_label, 7)
        return box

    def _update_days_label(self):
        expiry = self.expiry_combo.currentData()
        if expiry is None:
            self.days_label.setText("")
            return
        days = (expiry.expiry_date - datetime.date.today()).days
        self.days_label.setText(f"{days}天到期" if days >= 0 else "已到期")

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
        self.center_value = center
        self.center_label.setText(str(center))
        self.center_auto_filled = True
        self.status_label.setText(f"已自動帶入加權指數開盤價 {price} → 中心履約價 {center}")
        self._do_subscribe()

    # ------------------------------------------------------------- 查詢邏輯
    def _populate_expiry_list(self):
        self.expiry_combo.clear()
        for expiry in sym.list_all_expiries():
            self.expiry_combo.addItem(expiry.label, expiry)

    def _on_query_params_changed(self):
        """到期別/價格間距/上下幾檔任何一個變動時直接查詢，不需要按鈕。"""
        self._update_days_label()
        self._do_subscribe()

    def _do_subscribe(self):
        if not self.rtd_connected:
            self.status_label.setText("RTD 尚未連接，連上後會自動查詢")
            return

        expiry = self.expiry_combo.currentData()
        if expiry is None:
            return

        if self.center_value is None:
            self.status_label.setText("中心履約價還沒抓到加權指數開盤價，抓到後會自動查詢")
            return

        center = self.center_value
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

            for key, field in FIELD_BY_KEY.items():
                self._subscribe_field(row, CALL_COLS[key], call_symbol, field)
                self._subscribe_field(row, PUT_COLS[key], put_symbol, field)

            self._subscribe_ref(row, "call", call_symbol, "preclose", FIELD_PRECLOSE)
            self._subscribe_ref(row, "call", call_symbol, "up", FIELD_UP_LIMIT)
            self._subscribe_ref(row, "call", call_symbol, "down", FIELD_DOWN_LIMIT)
            self._subscribe_ref(row, "put", put_symbol, "preclose", FIELD_PRECLOSE)
            self._subscribe_ref(row, "put", put_symbol, "up", FIELD_UP_LIMIT)
            self._subscribe_ref(row, "put", put_symbol, "down", FIELD_DOWN_LIMIT)

        preview_call = sym.build_symbol(expiry.product_code, strikes[0], expiry.expiry_date, True)
        preview_put = sym.build_symbol(expiry.product_code, strikes[0], expiry.expiry_date, False)
        topics_per_row = len(FIELD_BY_KEY) * 2 + 6
        self.status_label.setText(
            f"已訂閱 {len(strikes)} 檔履約價 (共 {len(strikes) * topics_per_row} 個 RTD topic)｜"
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

    def _subscribe_ref(self, row: int, side: str, symbol: str, kind: str, field: str):
        topic_str = f"{symbol}.{field}"
        try:
            topic_id, initial = self.rtd.subscribe(topic_str)
        except Exception:  # noqa: BLE001
            return
        self.ref_topic_info[topic_id] = (row, side, kind)
        self._apply_ref_value(row, side, kind, initial)

    def _clear_subscriptions(self):
        for topic_id in list(self.topic_row_col.keys()):
            try:
                self.rtd.unsubscribe(topic_id)
            except Exception:  # noqa: BLE001
                pass
        self.topic_row_col.clear()
        for topic_id in list(self.ref_topic_info.keys()):
            try:
                self.rtd.unsubscribe(topic_id)
            except Exception:  # noqa: BLE001
                pass
        self.ref_topic_info.clear()
        self.price_ref.clear()
        self.price_last_value.clear()

    def _init_side(self, row: int, cols: dict, bg: QBrush):
        for key, col in cols.items():
            cell_bg = PRICE_BG if key in PRICE_KEYS else bg
            self._set_cell(row, col, "", cell_bg)

    def _set_cell(self, row: int, col: int, text: str, bg: QBrush, bold: bool = False):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        item.setBackground(bg)
        # 這幾欄背景是固定的淺色(粉紅/淺藍/淺灰)，不會跟著深色模式變深，
        # 文字顏色也要固定用深色，不然深色模式下字會變成淺灰、疊在淺色
        # 底上完全看不清楚。價格欄位之後會被 _recolor_cell 蓋掉，這裡的
        # 顏色只是暫時的初始值。
        item.setForeground(QBrush(COLOR_DEFAULT_TEXT))
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
            value = data[topic_str]
            entry = self.topic_row_col.get(topic_id)
            if entry is not None:
                row, col = entry
                self._update_cell(row, col, value)
                continue
            ref_entry = self.ref_topic_info.get(topic_id)
            if ref_entry is not None:
                row, side, kind = ref_entry
                self._apply_ref_value(row, side, kind, value)

    def _update_cell(self, row: int, col: int, value):
        if value is None:
            return
        item = self.table.item(row, col)
        if item is None:
            return
        item.setText(self._fmt(value))

        side = self.col_side.get(col)
        if side is not None:
            self.price_last_value[(row, col)] = value
            self._recolor_cell(row, col, value, side)

    def _apply_ref_value(self, row: int, side: str, kind: str, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.price_ref.setdefault((row, side), {})[kind] = v
        self._recolor_side(row, side)

    def _recolor_side(self, row: int, side: str):
        cols = CALL_COLS if side == "call" else PUT_COLS
        for key in PRICE_KEYS:
            col = cols[key]
            value = self.price_last_value.get((row, col))
            if value is not None:
                self._recolor_cell(row, col, value, side)

    def _recolor_cell(self, row: int, col: int, value, side: str):
        try:
            v = float(value)
        except (TypeError, ValueError):
            self._paint_cell(row, col, PRICE_BG.color(), COLOR_DEFAULT_TEXT)
            return

        ref = self.price_ref.get((row, side), {})
        up = ref.get("up")
        down = ref.get("down")
        preclose = ref.get("preclose")

        if up is not None and v >= up:
            self._paint_cell(row, col, COLOR_LIMIT_UP_BG, COLOR_WHITE_TEXT)
            return
        if down is not None and v <= down:
            self._paint_cell(row, col, COLOR_LIMIT_DOWN_BG, COLOR_WHITE_TEXT)
            return
        if preclose is not None:
            if v > preclose:
                self._paint_cell(row, col, PRICE_BG.color(), COLOR_UP_TEXT)
                return
            if v < preclose:
                self._paint_cell(row, col, PRICE_BG.color(), COLOR_DOWN_TEXT)
                return
        self._paint_cell(row, col, PRICE_BG.color(), COLOR_DEFAULT_TEXT)

    def _paint_cell(self, row: int, col: int, bg: QColor, fg: QColor):
        item = self.table.item(row, col)
        if item is None:
            return
        item.setBackground(QBrush(bg))
        item.setForeground(QBrush(fg))

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
