import datetime

import pythoncom
import win32event
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QHeaderView, QPushButton,
    QMessageBox, QToolBar, QDockWidget, QInputDialog, QStyledItemDelegate,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush, QPen

from app.models import capital_symbols as sym
from app.models.capital_client import CapitalClient
from app.models.capital_order_client import CapitalOrderClient
from app.models.capital_quote_client import CapitalQuoteClient
from app.models.order_book import OrderBookManager
from app.models.positions import PositionManager
from app.services import black_scholes, layout_store, query_pref, theme
from app.views.equity_widget import EquityWidget
from app.views.opening_tab import OpeningTab
from app.views.order_book_widgets import FillReportWidget, OrderBookWidget
from app.views.order_entry_widget import OrderEntryWidget
from app.views.payoff_chart_widget import PayoffChartWidget
from app.views.position_widgets import PositionTreeWidget

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
# Call/Put 兩側要以履約價為中心輻射對稱：離中心由近到遠都是
# Delta、成交價、買價、賣價，所以 Call 側「賣價」要放在離中心最遠的位
# 置(index 0)、「買價」放內側(index 1)——跟 Put 側「買價」在內側
# (index 7)、「賣價」在外側(index 8)對稱。
COLUMNS = ["賣價", "買價", "成交價", "Delta", "履約價", "Delta", "成交價", "買價", "賣價"]
CALL_COLS = {"bid": 1, "ask": 0, "last": 2, "delta": 3}
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

ACTIVE_PRICE_CELL_BORDER = QColor("#ff8c00")  # 跟價平橙字同色系，但用框線不動背景色


class _ActivePriceCellDelegate(QStyledItemDelegate):
    """在下單面板目前實際用來算價格(委託價欄位的預設值/現價)的那幾格
    (買價或賣價) 疊一層框線，故意不改背景色——背景色已經用來表示商品類
    別(call/put)、漲跌、漲跌停，改背景色會跟這些既有語意衝突，疊框線可
    以不干擾它們，一眼就看出「這格現在算進委託價了」。
    哪幾格算「目前在用」由 MainWindow._active_price_cells 決定，來源是
    OrderEntryWidget.active_legs_changed (裸買賣一格、價差單兩格)。"""

    def __init__(self, get_active_cells, parent=None):
        super().__init__(parent)
        self._get_active_cells = get_active_cells

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if (index.row(), index.column()) not in self._get_active_cells():
            return
        painter.save()
        pen = QPen(ACTIVE_PRICE_CELL_BORDER)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(option.rect.adjusted(1, 1, -2, -2))
        painter.restore()


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
        self.position_manager = PositionManager(self.order_client, self.order_book_manager, self.quote_client)
        self.position_manager.query_failed.connect(self._on_position_query_failed)

        self.row_meta = {}          # row -> {"strike":, "call_symbol":, "put_symbol":}
        self.symbol_row_side = {}   # 商品代碼 -> (row, "call"/"put")
        self.price_ref = {}         # (row, side) -> {"preclose":, "up":, "down":}
        self.price_last_value = {}  # (row, col) -> 最後一次收到的原始價格值，供收到參考值時重新上色
        self.center_auto_filled = False
        self.center_value = None    # 中心履約價，完全由加權指數開盤價自動算出，不給手動改
        self.strike_atm_row = None  # 目前「價平」(現貨即時價四捨五入)所在的列
        self.strike_to_row = {}     # 履約價數值 -> 該列的 row index
        self.underlying_price = None  # TSEA 即時成交價，Delta 反推用
        self._active_price_cells = set()  # {(row, col)}：下單面板目前算價格用到的儲存格，畫框線用
        # 填清單/還原上次設定的過程中，expiry_combo 的 index 從 -1 變成
        # 0 (清單第一筆)、以及 setCurrentIndex/setValue 都會觸發
        # currentIndexChanged/valueChanged，如果不擋住，_on_query_params_
        # changed 會在真正讀到上次存檔之前，先把「清單第一筆」存檔覆蓋
        # 掉——這是之前「明明改成月選，一開視窗又跑回週選」的成因。這段
        # 期間一律不存檔，全部弄完 (_restore_query_params 結束) 再存一次
        # 乾淨的最終狀態。
        self._restoring_query_params = True

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
        self._restore_query_params()
        self._restoring_query_params = False
        self._update_days_label()
        expiry = self.expiry_combo.currentData()
        if expiry is not None:
            query_pref.save(expiry.label, self.step_spin.value(), self.rows_spin.value())
        self._subscribe_taiex_open()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # 不設 central widget：整個視窗讓給 dock 區域，T字報價/開倉/下單/
        # 下單匣/成交回報全部是 QDockWidget，使用者自己拖動排列、拉出去
        # 變獨立視窗，或用「視窗」選單重新叫回來。
        self.setDockNestingEnabled(True)
        self._build_toolbar()
        self._build_docks()
        self.status_label = QLabel("已連接群益 API，可以開始查詢/訂閱")
        self.statusBar().addWidget(self.status_label, 1)

        # 先記住「剛排好的預設版面」，「重設為預設版面」按鈕才有東西可還原；
        # 之後才套用上次使用者自己存過的版面 (如果有的話)。
        self._default_geometry = self.saveGeometry()
        self._default_state = self.saveState()
        self._restore_last_layout()

    def _build_toolbar(self):
        toolbar = QToolBar("工具列", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.theme_toggle = theme.make_theme_toggle(self)
        self.theme_toggle.toggled.connect(lambda _checked: self._apply_table_theme())
        toolbar.addWidget(self.theme_toggle)
        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" 版面配置："))
        self.layout_combo = QComboBox()
        self.layout_combo.setMinimumWidth(140)
        toolbar.addWidget(self.layout_combo)

        apply_btn = QPushButton("套用")
        apply_btn.clicked.connect(self._on_apply_layout)
        toolbar.addWidget(apply_btn)

        save_btn = QPushButton("儲存目前版面...")
        save_btn.clicked.connect(self._on_save_layout)
        toolbar.addWidget(save_btn)

        delete_btn = QPushButton("刪除版面")
        delete_btn.clicked.connect(self._on_delete_layout)
        toolbar.addWidget(delete_btn)

        reset_btn = QPushButton("重設為預設版面")
        reset_btn.clicked.connect(self._on_reset_layout)
        toolbar.addWidget(reset_btn)

        self._reload_layout_combo()

    def _build_docks(self):
        self.quote_dock = self._make_dock("dock_quote", "T 字報價", self._build_option_quote_widget())

        self.opening_tab = OpeningTab(self.capital_client)
        self.opening_dock = self._make_dock("dock_opening", "開倉", self.opening_tab)
        # dock 沒打開(關掉，或疊在分頁裡但不是目前顯示的那個分頁)就不要
        # 打 K 線查詢/訂閱即時 tick——見 opening_tab.py 的 activate()/
        # deactivate()。visibilityChanged 涵蓋「用選單勾掉關閉」跟「疊在
        # 分頁裡切到別的分頁」兩種情況，Qt 都算「不可見」。
        self.opening_dock.visibilityChanged.connect(self._on_opening_dock_visibility_changed)

        self.order_entry_widget = OrderEntryWidget(self.order_book_manager)
        self.order_entry_widget.active_legs_changed.connect(self._on_active_legs_changed)
        self.order_entry_dock = self._make_dock("dock_order_entry", "下單", self.order_entry_widget)

        self.order_book_widget = OrderBookWidget(self.order_book_manager)
        self.order_book_dock = self._make_dock("dock_order_book", "下單匣", self.order_book_widget)

        self.fill_report_widget = FillReportWidget(self.order_book_manager)
        self.fill_report_dock = self._make_dock("dock_fill_report", "成交回報", self.fill_report_widget)

        self.equity_widget = EquityWidget(self.order_client)
        self.equity_dock = self._make_dock("dock_equity", "權益查詢", self.equity_widget)

        self.position_widget = PositionTreeWidget(self.position_manager)
        self.position_dock = self._make_dock("dock_positions", "未平倉部位", self.position_widget)

        self.payoff_chart_widget = PayoffChartWidget(self.position_manager, self.order_book_manager)
        self.payoff_dock = self._make_dock("dock_payoff", "到期損益圖", self.payoff_chart_widget)

        # 預設版面：T字報價/開倉分頁在左邊(跟改版前的 QTabWidget 分頁習慣
        # 一致)，下單/下單匣/成交回報/權益查詢/未平倉疊在右邊，損益圖放
        # 最下面(圖表要寬，不適合疊在右側窄欄裡)；使用者可以再自己拖動
        # 調整，這只是初次啟動、還沒存過版面時的起點。
        self.addDockWidget(Qt.LeftDockWidgetArea, self.quote_dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.opening_dock)
        self.tabifyDockWidget(self.quote_dock, self.opening_dock)
        self.quote_dock.raise_()

        self.addDockWidget(Qt.RightDockWidgetArea, self.order_entry_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.order_book_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.fill_report_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.equity_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.position_dock)
        self.tabifyDockWidget(self.order_book_dock, self.fill_report_dock)
        self.tabifyDockWidget(self.fill_report_dock, self.equity_dock)
        self.tabifyDockWidget(self.equity_dock, self.position_dock)
        self.order_book_dock.raise_()

        self.addDockWidget(Qt.BottomDockWidgetArea, self.payoff_dock)

        self.resizeDocks([self.quote_dock, self.order_entry_dock], [650, 350], Qt.Horizontal)

        view_menu = self.menuBar().addMenu("視窗")
        for dock in (
            self.quote_dock, self.opening_dock, self.order_entry_dock,
            self.order_book_dock, self.fill_report_dock, self.equity_dock,
            self.position_dock, self.payoff_dock,
        ):
            view_menu.addAction(dock.toggleViewAction())

    def _make_dock(self, object_name: str, title: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)  # saveState() 靠 objectName 認回每個 dock，一定要設
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable
        )
        return dock

    def _build_option_quote_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._build_query_box())
        layout.addWidget(self._build_side_header_box())
        layout.addWidget(self._build_table())
        return widget

    # ------------------------------------------------------------ 版面配置
    def _reload_layout_combo(self):
        current = self.layout_combo.currentText()
        names = layout_store.list_layouts()
        self.layout_combo.clear()
        self.layout_combo.addItems(names)
        if current in names:
            self.layout_combo.setCurrentText(current)

    def _on_apply_layout(self):
        name = self.layout_combo.currentText()
        if not name:
            return
        result = layout_store.load_layout(name)
        if result is None:
            QMessageBox.warning(self, "套用版面失敗", f"找不到版面「{name}」")
            return
        geometry, state = result
        self.restoreGeometry(geometry)
        self.restoreState(state)
        layout_store.set_last_layout_name(name)
        self.status_label.setText(f"已套用版面「{name}」")

    def _on_save_layout(self):
        name, ok = QInputDialog.getText(self, "儲存版面", "版面名稱", text=self.layout_combo.currentText() or "預設")
        name = name.strip() if ok else ""
        if not name:
            return
        layout_store.save_layout(name, bytes(self.saveGeometry()), bytes(self.saveState()))
        self._reload_layout_combo()
        self.layout_combo.setCurrentText(name)
        self.status_label.setText(f"已儲存版面「{name}」")

    def _on_delete_layout(self):
        name = self.layout_combo.currentText()
        if not name:
            return
        confirm = QMessageBox.question(
            self, "刪除版面", f"確定要刪除版面「{name}」嗎？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        layout_store.delete_layout(name)
        self._reload_layout_combo()
        self.status_label.setText(f"已刪除版面「{name}」")

    def _on_reset_layout(self):
        self.restoreGeometry(self._default_geometry)
        self.restoreState(self._default_state)
        self.status_label.setText("已重設為預設版面")

    def _restore_last_layout(self):
        name = layout_store.get_last_layout_name()
        if not name:
            return
        result = layout_store.load_layout(name)
        if result is None:
            return
        geometry, state = result
        self.restoreGeometry(geometry)
        self.restoreState(state)
        self.layout_combo.setCurrentText(name)

    def _on_order_rejected(self, label: str, error_msg: str):
        QMessageBox.critical(self, "委託失敗", f"{label}\n\n{error_msg}")

    def _on_opening_dock_visibility_changed(self, visible: bool):
        if visible:
            self.opening_tab.activate()
        else:
            self.opening_tab.deactivate()

    def _on_position_query_failed(self, message: str):
        self.status_label.setText(f"未平倉查詢失敗：{message}")

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
        self.table.setItemDelegate(_ActivePriceCellDelegate(lambda: self._active_price_cells, self.table))
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
        self.payoff_chart_widget.set_underlying_price(price)
        self.order_entry_widget.set_underlying_price(price)
        self.position_manager.set_underlying_price(price)
        for row in self.row_meta:
            self._recompute_delta(row, "call")
            self._recompute_delta(row, "put")

    # ------------------------------------------------------------- 查詢邏輯
    def _populate_expiry_list(self):
        self.expiry_combo.clear()
        for expiry in sym.list_all_expiries():
            self.expiry_combo.addItem(expiry.label, expiry)

    def _restore_query_params(self):
        """重開視窗自動套用上次的到期別/價格間距/上下幾檔。到期別存的是
        label (相對位置，如「第2週三選」)，如果那個 label 已經不在最新清
        單裡 (那份合約到期下架、捲到下一輪了)，依序改選最近的週三選、找
        不到再選最近的月選——都是清單裡「最近到期」的那筆，因為
        list_all_expiries() 已經照到期日排序過。"""
        expiry_label, step, rows = query_pref.load()

        target_index = -1
        if expiry_label is not None:
            target_index = self.expiry_combo.findText(expiry_label)
        if target_index < 0:
            target_index = self._first_index_by_category(sym.CATEGORY_WED)
        if target_index < 0:
            target_index = self._first_index_by_category(sym.CATEGORY_MONTHLY)
        if target_index >= 0:
            self.expiry_combo.setCurrentIndex(target_index)

        if step is not None:
            self.step_spin.setValue(step)
        if rows is not None:
            self.rows_spin.setValue(rows)

    def _first_index_by_category(self, category: str) -> int:
        for i in range(self.expiry_combo.count()):
            expiry = self.expiry_combo.itemData(i)
            if expiry is not None and expiry.category == category:
                return i
        return -1

    def _on_query_params_changed(self):
        """到期別/價格間距/上下幾檔任何一個變動時直接查詢，不需要按鈕。"""
        self._update_days_label()
        self._do_subscribe()
        if self._restoring_query_params:
            # 填清單/還原上次設定的過程中間狀態不存檔，見 __init__ 的說
            # 明；還原結束後 __init__ 自己會存一次最終狀態。
            return
        expiry = self.expiry_combo.currentData()
        if expiry is not None:
            query_pref.save(expiry.label, self.step_spin.value(), self.rows_spin.value())

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
        # 表格重新查詢後 row 編號可能整個重排，舊的 (row, col) 框線座標
        # 沒有意義了，不清掉的話重查後可能框到不相干的儲存格。
        if self._active_price_cells:
            self._active_price_cells = set()
            self.table.viewport().update()

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

    def _on_active_legs_changed(self, legs: list) -> None:
        """下單面板目前用哪幾格算價格變了 (換履約價、切買賣別/新倉平
        倉、切裸買賣/價差單分頁、改價差單的買權賣權/點數都會觸發)，重算
        T字表格要框哪幾格。legs 是 [(symbol, "bid"/"ask"), ...]，查不到
        對應的 row (例如這檔已經不在目前訂閱範圍內) 就跳過那一筆，不報
        錯——下單面板換履約價時本來就會暫時對不上。"""
        new_cells = set()
        for symbol, side in legs:
            entry = self.symbol_row_side.get(symbol)
            if entry is None:
                continue
            row, leg_side = entry
            cols = CALL_COLS if leg_side == "call" else PUT_COLS
            col = cols.get(side)
            if col is not None:
                new_cells.add((row, col))
        if new_cells == self._active_price_cells:
            return
        self._active_price_cells = new_cells
        self.table.viewport().update()

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

        self.order_entry_widget.set_context(
            meta["product_code"],
            meta["expiry_date"],
            meta["strike"],
            is_call,
            float(call_bid),
            float(call_ask),
            float(put_bid),
            float(put_ask),
            self.step_spin.value(),
        )
        # 下單面板是常駐的 dock，不是彈出視窗：確保它是可見/最上層的，
        # 不然使用者雙擊了報價卻看不到面板換了商品。
        self.order_entry_dock.setVisible(True)
        self.order_entry_dock.raise_()

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
        self.position_manager.shutdown()
        super().closeEvent(event)
