import datetime

import pythoncom
import win32event
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QHeaderView, QTabWidget, QPushButton,
    QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

from app.models import capital_symbols as sym
from app.models.capital_client import CapitalClient
from app.models.capital_order_client import CapitalOrderClient
from app.models.capital_quote_client import CapitalQuoteClient
from app.models.order_book import OrderBookManager
from app.services import black_scholes, theme
from app.views.opening_tab import OpeningTab
from app.views.order_book_window import OrderBookWindow
from app.views.order_dialog import OrderDialog

# 表格底色跟著淺色/深色模式切換；漲跌紅綠字、漲跌停紅綠底白字這些「語意」
# 顏色兩個主題共用，不受影響。
PALETTES = {
    "light": {
        "call_bg": QColor("#fff2f2"),
        "put_bg": QColor("#f0f7ff"),
        "strike_bg": QColor("#eeeeee"),
        "price_bg": QColor("#ffffff"),
        "default_text": QColor("black"),
    },
    "dark": {
        "call_bg": QColor("#4a2c2c"),
        "put_bg": QColor("#1f3444"),
        "strike_bg": QColor("#3a3a3a"),
        "price_bg": QColor("#1e1e1e"),
        "default_text": QColor("#e0e0e0"),
    },
}

COLOR_UP_TEXT = QColor("#e05050")       # 上漲：紅字 (淺色模式白底/深色模式深底都夠亮)
COLOR_DOWN_TEXT = QColor("#3ecf6e")     # 下跌：綠字
COLOR_LIMIT_UP_BG = QColor("#cc0000")   # 漲停：紅底白字
COLOR_LIMIT_DOWN_BG = QColor("#008000")  # 跌停：綠底白字
COLOR_WHITE_TEXT = QColor("white")
COLOR_ATM_TEXT = QColor("#ff8c00")      # 價平：履約價文字用醒目橙色標示

# 群益基礎報價沒有華南 XQ RTD 那種「隱波%/理論價/Delta/Theta」加值欄位
# (SKQuoteLib_Delta 等函式是本地 Black-Scholes 計算機，不是即時報價)。
# Delta 自己用 app/services/black_scholes.py 算：拿買賣中價反推隱含波動
# 率，再算 Delta，近似值 (歐式、無股利調整)，用來感受勝率，不是精確風控。
COLUMNS = ["買價", "賣價", "成交價", "Delta", "履約價", "Delta", "成交價", "買價", "賣價"]
CALL_COLS = {"bid": 0, "ask": 1, "last": 2, "delta": 3}
STRIKE_COL = 4
PUT_COLS = {"delta": 5, "last": 6, "bid": 7, "ask": 8}

PRICE_KEYS = ("bid", "ask", "last")
CALL_DELTA_COL = CALL_COLS["delta"]
PUT_DELTA_COL = PUT_COLS["delta"]

# Delta 反推用的無風險利率，近似值——短天期選擇權對這個數字很不敏感，不用
# 精確；不是即時資料，固定寫死即可。
RISK_FREE_RATE = 0.015

# 加權指數在群益 SKQuoteLib 的代碼，用來自動帶入中心履約價。
# 用群益官方範例 PythonExampleV2/Quote/Quote.py 的「個股資訊」查詢實測
# 核對過："TSEA" 查回來的 bstrStockName 就是「加權指」，不是猜的。
TAIEX_SYMBOL = "TSEA"

PUMP_INTERVAL_MS = 200


class MainWindow(QMainWindow):
    def __init__(self, capital_client: CapitalClient = None):
        super().__init__()
        account_suffix = f" - 帳號 {capital_client.account}" if capital_client and capital_client.account else ""
        self.setWindowTitle(f"台指選擇權 T 字報價 (群益 API){account_suffix}")
        self.resize(1000, 700)

        self.capital_client = capital_client
        self.quote_client = CapitalQuoteClient(capital_client)
        self.quote_client.quote_updated.connect(self._on_quote_updated)
        self.quote_client.quote_error.connect(self._on_quote_error)
        self.quote_client.connected.connect(self._on_quote_connected)
        self.quote_client.disconnected.connect(self._on_quote_disconnected)
        self.capital_client.report_connect_status.connect(self._on_report_connect_status)
        self.capital_client.report_ready.connect(self._on_report_ready)
        self.order_client = CapitalOrderClient(capital_client)
        self.order_book_manager = OrderBookManager(self.order_client, self.quote_client)
        # 委託被交易所退單/失敗一定要跳出來，不能只寫在下單匣表格裡等
        # 使用者剛好開著那個視窗才看得到——接在 MainWindow 上，不管下單
        # 匣視窗有沒有開過都會跳。
        self.order_book_manager.record_rejected.connect(self._on_order_rejected)
        self.order_book_window = None  # 單例，開過一次就重複使用同一個視窗

        self.row_meta = {}          # row -> {"strike":, "call_symbol":, "put_symbol":}
        self.symbol_row_side = {}   # 商品代碼 -> (row, "call"/"put")
        self.price_ref = {}         # (row, side) -> {"preclose":, "up":, "down":}
        self.price_last_value = {}  # (row, col) -> 最後一次收到的原始價格值，供收到參考值時重新上色
        self.center_auto_filled = False
        self.center_value = None    # 中心履約價，完全由加權指數開盤價自動算出，不給手動改
        self.strike_atm_row = None  # 目前「價平」(現貨即時價四捨五入)所在的列
        self.strike_to_row = {}     # 履約價數值 -> 該列的 row index
        self.underlying_price = None  # TSEA 即時成交價，Delta 反推用

        # 價格欄位 col -> 屬於 call 還是 put，漲跌停/漲跌顏色要分開比對
        self.col_side = {}
        for key in PRICE_KEYS:
            self.col_side[CALL_COLS[key]] = "call"
            self.col_side[PUT_COLS[key]] = "put"

        self.pump_timer = QTimer(self)
        self.pump_timer.setInterval(PUMP_INTERVAL_MS)
        self.pump_timer.timeout.connect(self._pump_messages)
        self.pump_timer.start()

        self._build_ui()
        self._populate_expiry_list()
        self._subscribe_taiex_open()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        self.theme_toggle = theme.make_theme_toggle(self)
        self.theme_toggle.toggled.connect(lambda _checked: self._apply_table_theme())
        self.tabs.setCornerWidget(self.theme_toggle, Qt.TopRightCorner)
        root.addWidget(self.tabs)

        option_tab = QWidget()
        option_layout = QVBoxLayout(option_tab)
        option_layout.addWidget(self._build_query_box())
        option_layout.addWidget(self._build_side_header_box())
        option_layout.addWidget(self._build_table())
        self.tabs.addTab(option_tab, "選擇權報價")

        self.opening_tab = OpeningTab()
        self.tabs.addTab(self.opening_tab, "開倉")

        bottom_row = QHBoxLayout()
        self.status_label = QLabel("已連接群益 API，可以開始查詢/訂閱")
        bottom_row.addWidget(self.status_label, 1)
        order_book_btn = QPushButton("下單匣 / 成交回報")
        order_book_btn.clicked.connect(self._open_order_book_window)
        bottom_row.addWidget(order_book_btn)
        root.addLayout(bottom_row)

    def _open_order_book_window(self):
        if self.order_book_window is None:
            self.order_book_window = OrderBookWindow(self.order_book_manager, parent=self)
        self.order_book_window.show()
        self.order_book_window.raise_()
        self.order_book_window.activateWindow()

    def _on_order_rejected(self, label: str, error_msg: str):
        QMessageBox.critical(self, "委託失敗", f"{label}\n\n{error_msg}")

    def _build_query_box(self) -> QGroupBox:
        box = QGroupBox("選擇權合約查詢")
        layout = QHBoxLayout(box)

        self.expiry_combo = QComboBox()
        self.expiry_combo.currentIndexChanged.connect(self._on_query_params_changed)

        self.center_label = QLabel("(等待加權指數即時成交價...)")

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
        """買權(Call)/賣權(Put)分組標題列，用伸縮比例(3:1:3)對齊表格底下的
        Call 3欄 / 履約價 1欄 / Put 3欄，不是塞進 QTableWidget 本身(表格表頭
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

        layout.addWidget(call_label, 3)
        layout.addWidget(self.days_label, 1)
        layout.addWidget(put_label, 3)
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
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        return self.table

    # ----------------------------------------------------------- 報價訂閱
    def _subscribe_taiex_open(self):
        self.quote_client.subscribe([TAIEX_SYMBOL])

    def _on_quote_error(self, symbol_or_action: str, message: str):
        self.status_label.setText(f"報價查詢失敗 [{symbol_or_action}]: {message}")

    def _on_quote_connected(self):
        self.status_label.setText("報價伺服器已連線，開始訂閱...")

    def _on_quote_disconnected(self):
        self.status_label.setText("報價伺服器斷線")

    def _on_report_connect_status(self, ok: bool, message: str):
        # 委託/成交回報要靠這個連線才收得到 (OnNewData)，之前漏呼叫
        # SKReplyLib_ConnectByID，導致回報永遠進不來、下單匣狀態卡在「掛
        # 單中」出不來——連線失敗一定要讓使用者看到，不能默默吞掉。
        if not ok:
            self.status_label.setText(f"回報主機連線異常：{message}（委託/成交回報可能收不到）")

    def _on_report_ready(self):
        # 文件：收到 OnComplete 才代表回報回補完成、真的能正常收委託/成
        # 交回報了；沒收到的話代表連線異常 (不是這裡處理，OnConnect 失敗
        # 已經有訊息了)。
        self.status_label.setText("回報主機連線完成，可正常接收委託/成交回報")

    def _try_apply_taiex_open(self, data: dict):
        price = data.get("last") or data.get("open")
        if not price:
            self.status_label.setText(
                f"{TAIEX_SYMBOL} 查得到商品但成交價/開盤價目前是 0 或空值"
                f"(可能非盤中，或代碼不對)，原始資料: {data}"
            )
            return
        # 選擇權履約價是以「價格間距」為級距報價的整數(例如百位)，指數
        # 本身(帶小數)不是有效履約價，要先取整到最近的級距倍數。
        step = self.step_spin.value() or 100
        rounded = int(round(price / step) * step)

        if not self.center_auto_filled:
            self.center_value = rounded
            self.center_label.setText(str(rounded))
            self.center_auto_filled = True
            self.status_label.setText(f"已自動帶入加權指數即時成交價 {price} → 中心履約價 {rounded}")
            self._do_subscribe()

        # 價平 = 現貨即時價四捨五入到百位，跟中心履約價一次性帶入不同，
        # 這個每次跳動都要重算，畫面上的橙字標示才會跟著現貨移動。
        self._highlight_atm_strike(rounded)

        # Delta 反推要用現貨價，現貨每跳一次全部列都要重算 (不是只有中心
        # 履約價那一次)。
        self.underlying_price = price
        for row in self.row_meta:
            self._recompute_delta(row, "call")
            self._recompute_delta(row, "put")

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

        symbols = []
        for row, strike in enumerate(strikes):
            self.strike_to_row[strike] = row
            call_symbol = sym.build_symbol(expiry.product_code, strike, expiry.expiry_date, is_call=True)
            put_symbol = sym.build_symbol(expiry.product_code, strike, expiry.expiry_date, is_call=False)

            self.row_meta[row] = {
                "strike": strike,
                "call_symbol": call_symbol,
                "put_symbol": put_symbol,
                "product_code": expiry.product_code,
                "expiry_date": expiry.expiry_date,
            }
            self.symbol_row_side[call_symbol] = (row, "call")
            self.symbol_row_side[put_symbol] = (row, "put")
            symbols.append(call_symbol)
            symbols.append(put_symbol)

            palette = self._palette()
            self._set_cell(row, STRIKE_COL, str(strike), QBrush(palette["strike_bg"]), bold=True)
            self._init_side(row, CALL_COLS, QBrush(palette["call_bg"]))
            self._init_side(row, PUT_COLS, QBrush(palette["put_bg"]))

        self.quote_client.subscribe(symbols)

        self.status_label.setText(
            f"已訂閱 {len(strikes)} 檔履約價 (共 {len(symbols)} 個商品)｜"
            f"例如第一檔代碼: {symbols[0]} / {symbols[1]}"
        )

    def _clear_subscriptions(self):
        old_symbols = list(self.symbol_row_side.keys())
        if old_symbols:
            self.quote_client.unsubscribe(old_symbols)
        self.row_meta.clear()
        self.symbol_row_side.clear()
        self.price_ref.clear()
        self.price_last_value.clear()
        self.strike_atm_row = None
        self.strike_to_row.clear()

    def _palette(self) -> dict:
        return PALETTES["dark" if theme.load_theme() == "dark" else "light"]

    def _init_side(self, row: int, cols: dict, bg: QBrush):
        price_bg = QBrush(self._palette()["price_bg"])
        for key, col in cols.items():
            cell_bg = price_bg if key in PRICE_KEYS else bg
            self._set_cell(row, col, "", cell_bg)

    def _set_cell(self, row: int, col: int, text: str, bg: QBrush, bold: bool = False):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        item.setBackground(bg)
        item.setForeground(QBrush(self._palette()["default_text"]))
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------- 行情更新
    def _pump_messages(self):
        """comtypes 的 COM 事件 (報價/委託回報) 靠訊息幫浦驅動，這裡定時抽
        訊息讓 SKQuoteLib/SKReplyLib 的事件能真的被呼叫到；跟原本 RTD 版本
        不同的是不用自己再手動 refresh 拉資料，事件本身就會直接推送。"""
        try:
            win32event.MsgWaitForMultipleObjects([], False, 0, win32event.QS_ALLINPUT)
            pythoncom.PumpWaitingMessages()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"訊息幫浦失敗: {exc}")
            self.pump_timer.stop()

    def _on_quote_updated(self, symbol: str, data: dict):
        if symbol == TAIEX_SYMBOL:
            self._try_apply_taiex_open(data)
            return

        entry = self.symbol_row_side.get(symbol)
        if entry is None:
            return
        row, side = entry
        cols = CALL_COLS if side == "call" else PUT_COLS

        self.price_ref[(row, side)] = {
            "preclose": data.get("preclose"),
            "up": data.get("up_limit"),
            "down": data.get("down_limit"),
        }
        for key, col in cols.items():
            value = data.get(key)
            if value is None:
                continue
            self._update_cell(row, col, value)

        self._recompute_delta(row, side)

    def _recompute_delta(self, row: int, side: str) -> None:
        """拿買賣中價反推隱含波動率，算出 Delta 填進表格。群益基礎報價
        沒有現成的 Delta/IV，這是我們自己用 Black-Scholes 算的近似值 (見
        app/services/black_scholes.py)，不是交易所/券商提供的即時資料。"""
        delta_col = CALL_DELTA_COL if side == "call" else PUT_DELTA_COL
        item = self.table.item(row, delta_col)
        if item is None:
            return
        meta = self.row_meta.get(row)
        if meta is None or self.underlying_price is None:
            item.setText("")
            return

        cols = CALL_COLS if side == "call" else PUT_COLS
        bid = self.price_last_value.get((row, cols["bid"]))
        ask = self.price_last_value.get((row, cols["ask"]))
        last = self.price_last_value.get((row, cols["last"]))
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif last and last > 0:
            mid = last
        else:
            item.setText("")
            return

        days = (meta["expiry_date"] - datetime.date.today()).days
        if days <= 0:
            item.setText("")
            return
        time_to_expiry = days / 365.0
        is_call = side == "call"

        iv = black_scholes.implied_vol(is_call, self.underlying_price, meta["strike"], RISK_FREE_RATE, time_to_expiry, mid)
        if iv is None:
            item.setText("")
            return
        d = black_scholes.delta(is_call, self.underlying_price, meta["strike"], RISK_FREE_RATE, time_to_expiry, iv)
        item.setText(f"{d:.2f}" if d is not None else "")

    def _update_cell(self, row: int, col: int, value):
        item = self.table.item(row, col)
        if item is None:
            return
        item.setText(self._fmt(value))

        side = self.col_side.get(col)
        if side is not None:
            self.price_last_value[(row, col)] = value
            self._recolor_cell(row, col, value, side)

    def _recolor_cell(self, row: int, col: int, value, side: str):
        palette = self._palette()
        try:
            v = float(value)
        except (TypeError, ValueError):
            self._paint_cell(row, col, palette["price_bg"], palette["default_text"])
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
                self._paint_cell(row, col, palette["price_bg"], COLOR_UP_TEXT)
                return
            if v < preclose:
                self._paint_cell(row, col, palette["price_bg"], COLOR_DOWN_TEXT)
                return
        self._paint_cell(row, col, palette["price_bg"], palette["default_text"])

    def _paint_cell(self, row: int, col: int, bg: QColor, fg: QColor):
        item = self.table.item(row, col)
        if item is None:
            return
        item.setBackground(QBrush(bg))
        item.setForeground(QBrush(fg))

    def _apply_table_theme(self):
        """深色/淺色模式切換時，把表格裡已經存在的儲存格重新上色一次，
        新查詢的話 _do_subscribe 本來就會用目前主題的顏色，這裡是處理
        「查詢完之後才切換主題」的情況。"""
        palette = self._palette()
        for row in range(self.table.rowCount()):
            strike_item = self.table.item(row, STRIKE_COL)
            if strike_item is not None:
                strike_item.setBackground(QBrush(palette["strike_bg"]))
                strike_item.setForeground(QBrush(palette["default_text"]))
            for side, cols in (("call", CALL_COLS), ("put", PUT_COLS)):
                side_bg = palette["call_bg"] if side == "call" else palette["put_bg"]
                for key, col in cols.items():
                    if key in PRICE_KEYS:
                        value = self.price_last_value.get((row, col))
                        if value is not None:
                            self._recolor_cell(row, col, value, side)
                        else:
                            self._paint_cell(row, col, palette["price_bg"], palette["default_text"])
                    else:
                        item = self.table.item(row, col)
                        if item is not None:
                            item.setBackground(QBrush(side_bg))
                            item.setForeground(QBrush(palette["default_text"]))

        if self.strike_atm_row is not None:
            atm_item = self.table.item(self.strike_atm_row, STRIKE_COL)
            if atm_item is not None:
                atm_item.setForeground(QBrush(COLOR_ATM_TEXT))

    def _highlight_atm_strike(self, strike_value: int):
        """價平 = 加權指數即時成交價四捨五入到價格間距的整數，橙字標示
        履約價等於這個值的那一列 (跟著現貨跳動即時更新)。"""
        target_row = self.strike_to_row.get(strike_value)
        if target_row == self.strike_atm_row:
            return

        default_text = self._palette()["default_text"]
        if self.strike_atm_row is not None:
            old_item = self.table.item(self.strike_atm_row, STRIKE_COL)
            if old_item is not None:
                old_item.setForeground(QBrush(default_text))

        if target_row is not None:
            new_item = self.table.item(target_row, STRIKE_COL)
            if new_item is not None:
                new_item.setForeground(QBrush(COLOR_ATM_TEXT))

        self.strike_atm_row = target_row

    # --------------------------------------------------------------- 下單
    def _on_cell_double_clicked(self, row: int, col: int):
        meta = self.row_meta.get(row)
        if meta is None:
            return
        if col in CALL_COLS.values():
            is_call = True
        elif col in PUT_COLS.values():
            is_call = False
        else:
            return  # 履約價欄位本身不觸發下單

        call_bid = self.price_last_value.get((row, CALL_COLS["bid"]), 0.0) or 0.0
        call_ask = self.price_last_value.get((row, CALL_COLS["ask"]), 0.0) or 0.0
        put_bid = self.price_last_value.get((row, PUT_COLS["bid"]), 0.0) or 0.0
        put_ask = self.price_last_value.get((row, PUT_COLS["ask"]), 0.0) or 0.0

        dialog = OrderDialog(
            self.order_book_manager,
            meta["product_code"],
            meta["expiry_date"],
            meta["strike"],
            is_call,
            float(call_bid),
            float(call_ask),
            float(put_bid),
            float(put_ask),
            self.step_spin.value(),
            parent=self,
        )
        dialog.exec_()

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
        super().closeEvent(event)
