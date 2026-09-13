import asyncio
import datetime

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QLabel, QTableWidget, QLineEdit,
    QTableWidgetItem, QGroupBox, QHeaderView, QPushButton,
    QMessageBox, QToolBar, QDockWidget, QInputDialog, QStyledItemDelegate,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush, QPen
from qasync import asyncSlot

from app.models.auto_close_manager import AutoCloseManager
from app.models.ib_client import IBClient
from app.models.ib_order_client import IBOrderClient
from app.models.ib_quote_client import IBQuoteClient
from app.models.option_utils import build_option, build_stock
from app.models.order_book import OrderBookManager
from app.models.positions import PositionManager
from app.services import black_scholes, layout_store, query_pref, theme
from app.views.equity_widget import EquityWidget
from app.views.order_book_widgets import FillReportWidget, OrderBookWidget
from app.views.order_entry_widget import OrderEntryWidget
from app.views.payoff_chart_widget import PayoffChartWidget
from app.views.position_widgets import PositionTreeWidget
from app.views.screener_widget import ScreenerWidget

# 表格底色跟著淺色/深色模式切換；漲跌紅綠字、Delta 這些「語意」顏色兩個
# 主題共用，不受影響。
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

# *** 美股慣例：漲=綠、跌=紅，跟台股(漲紅跌綠)相反，這是這次改動故意翻
# 過來的地方，不是保留舊值。***
COLOR_UP_TEXT = QColor("#3ecf6e")
COLOR_DOWN_TEXT = QColor("#e05050")
COLOR_ATM_TEXT = QColor("#ff8c00")

# IB 選擇權沒有台股那種漲跌停(limit up/down)機制，這兩個顏色/欄位不需要了。

COLUMNS = ["賣價", "買價", "成交價", "Delta", "履約價", "Delta", "成交價", "買價", "賣價"]
CALL_COLS = {"bid": 1, "ask": 0, "last": 2, "delta": 3}
STRIKE_COL = 4
PUT_COLS = {"delta": 5, "last": 6, "bid": 7, "ask": 8}

PRICE_KEYS = ("bid", "ask", "last")
CALL_DELTA_COL = CALL_COLS["delta"]
PUT_DELTA_COL = PUT_COLS["delta"]

# Delta 反推用的無風險利率，近似值——短天期選擇權對這個數字很不敏感，不用
# 精確；不是即時資料，固定寫死即可。
RISK_FREE_RATE = 0.04  # 美債短天期利率量級，跟台股版本(0.015)不同，這裡改用美股市場的近似值

DEFAULT_ROWS = 10

ACTIVE_PRICE_CELL_BORDER = QColor("#ff8c00")


class _ActivePriceCellDelegate(QStyledItemDelegate):
    """在下單面板目前實際用來算價格(委託價欄位的預設值/現價)的那幾格
    (買價或賣價) 疊一層框線，故意不改背景色。"""

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
    def __init__(self, ib_client: IBClient):
        super().__init__()
        self.setWindowTitle("美股選擇權下單 (Interactive Brokers)")
        self.resize(1100, 750)

        self.ib_client = ib_client
        self.quote_client = IBQuoteClient(ib_client)
        self.order_client = IBOrderClient(ib_client)
        self.order_book_manager = OrderBookManager(self.order_client)
        self.order_book_manager.record_rejected.connect(self._on_order_rejected)
        self.position_manager = PositionManager(ib_client, self.order_book_manager, self.quote_client)
        self.position_manager.query_failed.connect(self._on_position_query_failed)
        self.auto_close_manager = AutoCloseManager(ib_client, self.position_manager, self.order_book_manager)
        self.auto_close_manager.auto_close_error.connect(self._on_auto_close_error)

        self.row_meta = {}          # row -> {"strike":, "call_contract":, "put_contract":}
        self.symbol_row_side = {}   # symbol_key(conId字串) -> (row, "call"/"put")
        self.price_last_value = {}  # (row, col) -> 最後一次收到的原始價格值
        self.strike_atm_row = None  # 目前「價平」所在的列
        self.strike_to_row = {}     # 履約價數值 -> 該列的 row index
        self._active_price_cells = set()

        self._current_symbol = None      # 目前查詢的標的代碼(股票，例如 "SPY")
        self._underlying_contract = None
        self._underlying_symbol_key = None
        self._underlying_price = None
        self._expirations = []           # 目前標的可用的到期日清單(YYYYMMDD字串)，已排序
        self._strikes = []               # 目前標的可用的履約價清單，已排序
        self._strike_step = 1.0          # 履約價間距的估計值，給價差單分頁的預設寬度用
        self._restoring_query_params = True
        self._busy = False               # 防止查詢/訂閱動作重疊觸發，見 _on_query_symbol() 開頭的說明

        self.col_side = {}
        for key in PRICE_KEYS:
            self.col_side[CALL_COLS[key]] = "call"
            self.col_side[PUT_COLS[key]] = "put"

        self.quote_client.quote_updated.connect(self._on_quote_updated)
        self.quote_client.quote_error.connect(self._on_quote_error)

        self._current_layout_name = None  # 目前套用中的版面名稱，給「視窗」選單打勾用，見 _reload_layout_menus()
        self._build_ui()
        self._restore_query_params()
        self.position_manager.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.setDockNestingEnabled(True)
        self._build_toolbar()
        self._build_docks()
        self.status_label = QLabel("已連線 IB，輸入標的代碼開始查詢")
        self.statusBar().addWidget(self.status_label, 1)

        self._default_geometry = self.saveGeometry()
        self._default_state = self.saveState()
        # *** 不能在這裡直接呼叫 _restore_last_layout() ***：這時候視窗
        # 還沒 show()/showMaximized()(main.py 是先建完 MainWindow 才呼叫
        # showMaximized())，QMainWindow 內部的 dock 分割區還沒真的排版
        # 過、量不到真實可用空間，restoreState() 在這個時間點還原出來的
        # dock 寬度比例就會是錯的——這也是「手動按套用版面是對的、但一
        # 開視窗就不對」的原因：手動按的時候視窗早就 show 過了，量得到
        # 正確尺寸。改用 QTimer.singleShot(0, ...) 排到下一輪事件迴圈，
        # 讓 main.py 的 showMaximized() 先跑完，這裡才真正還原——跟
        # main.py::_show_connect_dialog() 為什麼要用同一招是一樣的道理。
        QTimer.singleShot(0, self._restore_last_layout)

    def _build_toolbar(self):
        toolbar = QToolBar("工具列", self)
        toolbar.setObjectName("main_toolbar")  # saveState() 要靠這個認回工具列，沒設會噴警告(不影響功能，但訊息很煩)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.theme_toggle = theme.make_theme_toggle(self)
        self.theme_toggle.toggled.connect(lambda _checked: self._apply_table_theme())
        toolbar.addWidget(self.theme_toggle)

    def _build_docks(self):
        self.quote_dock = self._make_dock("dock_quote", "選擇權報價", self._build_option_quote_widget())

        self.screener_widget = ScreenerWidget(self.ib_client)
        self.screener_widget.symbol_selected.connect(self._on_screener_symbol_selected)
        self.screener_dock = self._make_dock("dock_screener", "選擇權篩選器", self.screener_widget)

        self.order_entry_widget = OrderEntryWidget(
            self.order_book_manager, self.quote_client, self._get_contract,
        )
        self.order_entry_widget.active_legs_changed.connect(self._on_active_legs_changed)
        self.order_entry_dock = self._make_dock("dock_order_entry", "下單", self.order_entry_widget)

        self.order_book_widget = OrderBookWidget(self.order_book_manager)
        self.order_book_dock = self._make_dock("dock_order_book", "下單匣", self.order_book_widget)

        self.fill_report_widget = FillReportWidget(self.order_book_manager)
        self.fill_report_dock = self._make_dock("dock_fill_report", "成交回報", self.fill_report_widget)

        self.equity_widget = EquityWidget(self.ib_client)
        self.equity_dock = self._make_dock("dock_equity", "權益查詢", self.equity_widget)

        self.position_widget = PositionTreeWidget(self.position_manager, self.auto_close_manager)
        self.position_dock = self._make_dock("dock_positions", "未平倉部位", self.position_widget)

        self.payoff_chart_widget = PayoffChartWidget(self.position_manager, self.order_book_manager)
        self.payoff_dock = self._make_dock("dock_payoff", "到期損益圖", self.payoff_chart_widget)

        self.addDockWidget(Qt.LeftDockWidgetArea, self.quote_dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.screener_dock)
        self.tabifyDockWidget(self.quote_dock, self.screener_dock)
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

        self.resizeDocks([self.quote_dock, self.order_entry_dock], [700, 350], Qt.Horizontal)

        view_menu = self.menuBar().addMenu("視窗")
        for dock in (
            self.quote_dock, self.screener_dock, self.order_entry_dock, self.order_book_dock,
            self.fill_report_dock, self.equity_dock, self.position_dock, self.payoff_dock,
        ):
            view_menu.addAction(dock.toggleViewAction())

        # *** 版面配置改放進「視窗」選單，不再用工具列上一整排 combo+按
        # 鈕 ***：常見應用程式(Visual Studio 的「視窗」選單、IntelliJ 的
        # 「Window」選單)都是把「套用/儲存/管理版面」放在選單裡的子選單
        # /動作，不是常駐佔掉工具列空間的一排控制項——工具列只留使用者
        # 高頻互動的深色模式切換。
        view_menu.addSeparator()
        self._apply_layout_menu = view_menu.addMenu("套用版面")
        save_layout_action = view_menu.addAction("儲存目前版面...")
        save_layout_action.triggered.connect(self._on_save_layout)
        self._delete_layout_menu = view_menu.addMenu("刪除版面")
        reset_layout_action = view_menu.addAction("重設為預設版面")
        reset_layout_action.triggered.connect(self._on_reset_layout)
        self._reload_layout_menus()

    def _make_dock(self, object_name: str, title: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable
        )
        return dock

    def _ensure_screener_tabbed(self):
        """套用/還原版面之後補呼叫一次——這支 app 支援使用者自己存版面
        (layout_store)，新增 screener_dock 這個 dock 之後，使用者機器上
        既有的舊版面(存檔當時這個 dock 還不存在)完全不知道它該分到哪一
        組：restoreState() 對「版面裡沒記錄過」的 dock 不會主動幫忙分
        組，套用舊版面後 quote_dock 會被搬回存檔當時的位置，screener_dock
        卻留在原地，兩個因此被拆開、不再是頁籤(這是實測踩到的真實案
        例，不是假設性防呆)。每次套用版面後都補一次 tabify，已經是同一
        組的話這行是no-op，不會有副作用。"""
        if self.screener_dock not in self.tabifiedDockWidgets(self.quote_dock):
            self.tabifyDockWidget(self.quote_dock, self.screener_dock)

    def _build_option_quote_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._build_query_box())
        layout.addWidget(self._build_side_header_box())
        layout.addWidget(self._build_table())
        return widget

    # ------------------------------------------------------------ 版面配置
    def _reload_layout_menus(self):
        """重新列出「視窗」選單裡「套用版面」/「刪除版面」兩個子選單的
        內容——存檔/刪除版面之後都要呼叫，保持選單跟 layout_store 的內
        容一致。「套用版面」裡目前套用中的那個打勾，純粹提示用，跟
        combo box 當年顯示目前選取項目是同一個用途。"""
        names = layout_store.list_layouts()

        self._apply_layout_menu.clear()
        for name in names:
            action = self._apply_layout_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == self._current_layout_name)
            action.triggered.connect(lambda checked=False, n=name: self._on_apply_layout(n))
        self._apply_layout_menu.setEnabled(bool(names))

        self._delete_layout_menu.clear()
        for name in names:
            action = self._delete_layout_menu.addAction(name)
            action.triggered.connect(lambda checked=False, n=name: self._on_delete_layout(n))
        self._delete_layout_menu.setEnabled(bool(names))

    def _on_apply_layout(self, name: str):
        result = layout_store.load_layout(name)
        if result is None:
            QMessageBox.warning(self, "套用版面失敗", f"找不到版面「{name}」")
            return
        geometry, state = result
        self.restoreGeometry(geometry)
        self.restoreState(state)
        self._ensure_screener_tabbed()
        layout_store.set_last_layout_name(name)
        self._current_layout_name = name
        self._reload_layout_menus()
        self.status_label.setText(f"已套用版面「{name}」")

    def _on_save_layout(self):
        name, ok = QInputDialog.getText(self, "儲存版面", "版面名稱", text=self._current_layout_name or "預設")
        name = name.strip() if ok else ""
        if not name:
            return
        layout_store.save_layout(name, bytes(self.saveGeometry()), bytes(self.saveState()))
        self._current_layout_name = name
        self._reload_layout_menus()
        self.status_label.setText(f"已儲存版面「{name}」")

    def _on_delete_layout(self, name: str):
        confirm = QMessageBox.question(
            self, "刪除版面", f"確定要刪除版面「{name}」嗎？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        layout_store.delete_layout(name)
        if self._current_layout_name == name:
            self._current_layout_name = None
        self._reload_layout_menus()
        self.status_label.setText(f"已刪除版面「{name}」")

    def _on_reset_layout(self):
        self.restoreGeometry(self._default_geometry)
        self.restoreState(self._default_state)
        self._ensure_screener_tabbed()
        self._current_layout_name = None
        self._reload_layout_menus()
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
        self._ensure_screener_tabbed()
        self._current_layout_name = name
        self._reload_layout_menus()

    def _on_order_rejected(self, label: str, error_msg: str):
        QMessageBox.critical(self, "委託失敗", f"{label}\n\n{error_msg}")

    def _on_auto_close_error(self, message: str):
        QMessageBox.critical(self, "自動平倉異常", message)

    def _on_position_query_failed(self, message: str):
        self.status_label.setText(f"未平倉查詢失敗：{message}")

    def _build_query_box(self) -> QGroupBox:
        box = QGroupBox("選擇權合約查詢")
        layout = QHBoxLayout(box)

        self.symbol_edit = QLineEdit()
        self.symbol_edit.setPlaceholderText("股票代碼，例如 SPY")
        # 股票代碼最多幾個字母，預設的 Expanding 政策會把這個輸入框撐到
        # 佔滿大半個查詢列，反而把後面到期日下拉選單擠到很窄——限制寬度
        # 讓版面按實際需要分配空間。
        self.symbol_edit.setMaximumWidth(90)
        self.symbol_edit.returnPressed.connect(self._on_query_symbol)

        query_btn = QPushButton("查詢")
        query_btn.clicked.connect(self._on_query_symbol)

        self.expiry_combo = QComboBox()
        self.expiry_combo.setMinimumWidth(150)  # 裝得下「YYYY-MM-DD」這種到期日標籤
        self.expiry_combo.currentIndexChanged.connect(self._on_query_params_changed)

        self.center_label = QLabel("(尚未查詢)")

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 40)
        self.rows_spin.setValue(DEFAULT_ROWS)
        self.rows_spin.valueChanged.connect(self._on_query_params_changed)

        form = QFormLayout()
        form.addRow("到期日", self.expiry_combo)

        layout.addWidget(QLabel("標的"))
        layout.addWidget(self.symbol_edit)
        layout.addWidget(query_btn)
        layout.addLayout(form)
        layout.addWidget(QLabel("標的現價"))
        layout.addWidget(self.center_label)
        layout.addWidget(QLabel("上下各幾檔"))
        layout.addWidget(self.rows_spin)
        return box

    def _build_side_header_box(self) -> QWidget:
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
        expiry_date = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        days = (expiry_date - datetime.date.today()).days
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

    # ----------------------------------------------------------- 查詢邏輯
    #
    # *** 這支 app 用 qasync 讓 Qt 跟 asyncio 共用同一個事件迴圈(見
    # main.py)，取代 ib_async 自帶的 util.useQt()——後者是巢狀塞 Qt
    # QEventLoop 的 hack，實測發現一旦開始跑就回不了「沒在跑」的狀態，
    # 導致任何同步 IB 呼叫(ib.qualifyContracts()這種內部是
    # loop.run_until_complete() 的呼叫)之後都保證撞上 asyncio 的「這個
    # 事件迴圈已經在跑了」。改用 qasync 之後，Qt 的事件分派本身就是這
    # 個 asyncio 迴圈在跑，所以這裡全部改用 await xxxAsync()，Qt 訊號的
    # handler 用 @asyncSlot() 直接寫成 async def，不用再手動
    # asyncio.ensure_future()、也不需要任何「鎖住同步呼叫」的旗標。
    # self._busy 純粹是應用層級的防呆(避免使用者手速太快讓兩個查詢動作
    # 重疊、互相覆蓋表格內容)，跟 asyncio 的重入問題無關。
    #
    # *** 「查標的」跟「顯示報價」是兩個分開的動作 ***：_query_symbol_core
    # 只做「查合約、列出到期日清單」，不直接畫表格/訂閱——這樣使用者換
    # 標的的當下表格會先清空，不會看到上一個標的殘留的舊報價；真正「畫
    # 表格＋訂閱報價」統一由 _show_expiry_quotes_core() 負責，不管是使
    # 用者自己選到期日觸發(_on_query_params_changed)，還是查完標的後預
    # 設帶出第一個到期日(_query_symbol_core 最後一段)，都是同一套邏輯。
    @asyncSlot()
    async def _on_query_symbol(self):
        symbol = self.symbol_edit.text().strip().upper()
        if not symbol:
            return
        await self._run_query_symbol(symbol)

    async def _run_query_symbol(self, symbol: str):
        if self._busy:
            self.status_label.setText("上一個動作還在處理中，請稍候")
            return
        self.status_label.setText(f"查詢 {symbol} 的選擇權鏈中...")
        self._busy = True
        try:
            await self._query_symbol_core(symbol)
        finally:
            self._busy = False

    async def _query_symbol_core(self, symbol: str):
        stock = build_stock(symbol)
        try:
            await self.ib_client.ib.qualifyContractsAsync(stock)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"查詢 {symbol} 失敗：{exc}")
            return
        if not stock.conId:
            self.status_label.setText(f"查不到標的 {symbol}，確認代碼是否正確")
            return

        try:
            chains = await self.ib_client.ib.reqSecDefOptParamsAsync(symbol, "", "STK", stock.conId)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"查詢選擇權鏈失敗：{exc}")
            return
        chain = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == symbol), None)
        if chain is None:
            self.status_label.setText(f"{symbol} 查不到標準選擇權鏈(SMART)")
            return

        # 換標的：舊的訂閱/選擇權合約快取、表格內容全部作廢，先清乾淨，
        # 不要讓上一個標的的舊報價繼續留在畫面上。
        self._clear_subscriptions()
        self.table.setRowCount(0)
        self._current_symbol = symbol
        self._underlying_contract = stock
        self._underlying_symbol_key = str(stock.conId)
        self._underlying_price = None
        self._expirations = sorted(chain.expirations)
        self._strikes = sorted(chain.strikes)
        if len(self._strikes) >= 2:
            diffs = [b - a for a, b in zip(self._strikes, self._strikes[1:])]
            self._strike_step = min(diffs) if diffs else 1.0
        else:
            self._strike_step = 1.0

        self.expiry_combo.blockSignals(True)
        self.expiry_combo.clear()
        for expiry in self._expirations:
            label = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
            self.expiry_combo.addItem(label, expiry)
        self.expiry_combo.blockSignals(False)
        if self.expiry_combo.count() > 0:
            self.expiry_combo.setCurrentIndex(0)

        self.quote_client.subscribe([stock])
        # 標的股票只有這一檔，訂閱後主動排一次退回查詢，不要傻等
        # pendingTickersEvent 先來一次 tick 才觸發 fallback——見
        # IBQuoteClient.prime_fallback() 的說明，這是「現價完全不會查」
        # 的根本原因。
        self.quote_client.prime_fallback(stock)
        # 到期日清單列出來之後，預設帶出第一個到期日的報價——跟使用者
        # 自己選到期日走的是同一套 _show_expiry_quotes_core()，只是這
        # 裡已經鎖過 self._busy 了，直接呼叫，不要再經過會因為忙碌中而
        # 跳過的 _on_query_params_changed()。
        if self.expiry_combo.count() > 0:
            await self._show_expiry_quotes_core()

    @asyncSlot(str, str)
    async def _on_screener_symbol_selected(self, symbol: str, expiry: str):
        """篩選器結果表格雙擊某一列通過復篩的標的，帶去選擇權報價 dock
        查看完整選擇權鏈——重用既有的查詢流程(_run_query_symbol +
        expiry_combo)，不在篩選器那邊另外做一套訂閱邏輯。"""
        self.symbol_edit.setText(symbol)
        await self._run_query_symbol(symbol)
        idx = self.expiry_combo.findData(expiry)
        if idx >= 0:
            self.expiry_combo.setCurrentIndex(idx)
        self.quote_dock.setVisible(True)
        self.quote_dock.raise_()

    def _on_quote_error(self, symbol_or_action: str, message: str):
        self.status_label.setText(f"報價查詢失敗 [{symbol_or_action}]: {message}")

    def _restore_query_params(self):
        symbol, expiry, rows = query_pref.load()
        if rows is not None:
            self.rows_spin.setValue(rows)
        if symbol:
            self.symbol_edit.setText(symbol)
            asyncio.ensure_future(self._restore_query_params_async(symbol, expiry))
        else:
            self._restoring_query_params = False

    async def _restore_query_params_async(self, symbol: str, expiry: str | None):
        await self._run_query_symbol(symbol)
        if expiry:
            idx = self.expiry_combo.findData(expiry)
            if idx >= 0:
                self.expiry_combo.setCurrentIndex(idx)
        self._restoring_query_params = False

    def _save_query_params(self):
        expiry = self.expiry_combo.currentData()
        if self._current_symbol and expiry:
            query_pref.save(self._current_symbol, expiry, self.rows_spin.value())

    @asyncSlot()
    async def _on_query_params_changed(self):
        """到期日下拉/顯示筆數 spin box 變更的 Qt slot。"""
        if self._busy:
            return
        self._busy = True
        try:
            await self._show_expiry_quotes_core()
        finally:
            self._busy = False

    async def _show_expiry_quotes_core(self):
        """到期日已經確定(使用者自己選的，或查完標的後預設帶出第一
        個)，畫表格＋訂閱報價，並視情況記住這次的查詢參數。呼叫端要嘛
        自己鎖過 self._busy(_query_symbol_core)，要嘛透過上面
        _on_query_params_changed()。"""
        self._update_days_label()
        await self._do_subscribe_core()
        if self._restoring_query_params:
            return
        self._save_query_params()

    def _do_subscribe(self):
        """給「拿到標的現價、第一次重新置中履約價範圍」這種不方便直接
        await 的呼叫點(Qt 訊號 handler 本身不是 async)用，排成背景工作
        即可——如果剛好有另一個查詢/訂閱動作正在進行中，直接跳過這次，
        等它自然做完、畫面本來就會是最新的。"""
        if self._busy:
            return
        asyncio.ensure_future(self._do_subscribe_guarded())

    async def _do_subscribe_guarded(self):
        self._busy = True
        try:
            await self._do_subscribe_core()
        finally:
            self._busy = False

    async def _do_subscribe_core(self):
        """真正的訂閱邏輯。呼叫端要嘛已經自己鎖了 self._busy，要嘛是上
        面的 _do_subscribe_guarded() 已經鎖過。"""
        expiry = self.expiry_combo.currentData()
        if expiry is None or not self._strikes:
            return

        center = self._underlying_price
        if center is None:
            # 還沒收到標的現價，先用履約價清單正中間的值當中心點，收到
            # 現價後 _on_quote_updated 會自動重新置中一次。
            center = self._strikes[len(self._strikes) // 2]
        nearest = min(self._strikes, key=lambda s: abs(s - center))
        idx = self._strikes.index(nearest)
        rows_n = self.rows_spin.value()

        # *** self._strikes 是「所有到期日的聯集」，不是這個到期日實際
        # 掛牌的履約價 ***：週選/0DTE 常常只掛出聯集裡的一部分。如果只
        # 抓剛好 2*rows_n+1 檔候選，被這個到期日濾掉幾檔之後，實際顯示
        # 的上下檔數就會比使用者在「上下各幾檔」設定的少——這就是「切
        # 換到期日有時候不會根據現價抓滿指定檔數」的原因。這裡故意在上
        # 下各多抓 rows_n 檔當緩衝，qualify 完之後再分別從有效的裡面各
        # 取最靠近現價的 rows_n 檔，儘量湊滿使用者要求的檔數。
        margin = rows_n
        lo = max(0, idx - rows_n - margin)
        hi = min(len(self._strikes), idx + rows_n + margin + 1)
        candidate_strikes = self._strikes[lo:hi]

        self._clear_option_contracts()

        call_contracts = [build_option(self._current_symbol, expiry, strike, "C") for strike in candidate_strikes]
        put_contracts = [build_option(self._current_symbol, expiry, strike, "P") for strike in candidate_strikes]
        try:
            await self.ib_client.ib.qualifyContractsAsync(*call_contracts, *put_contracts)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"合約查詢失敗：{exc}")
            return

        # *** qualifyContracts 對「這個履約價在這個到期日根本不存在」不
        # 會丟例外，只會讓那個 Contract 物件的 conId 停在 0 ***：濾掉沒
        # 查到 conId 的履約價，不要把它拿去訂閱報價(會收到 "Unknown
        # contract" 這種更難懂的錯誤)。candidate_strikes 本來就照升冪排
        # 序，過濾後 below/at_or_above 兩段也還是升冪，接起來不用再排序。
        valid_all = [
            (strike, call_contract, put_contract)
            for strike, call_contract, put_contract in zip(candidate_strikes, call_contracts, put_contracts)
            if call_contract.conId and put_contract.conId
        ]
        below = [v for v in valid_all if v[0] < nearest][-rows_n:]
        at_or_above = [v for v in valid_all if v[0] >= nearest][: rows_n + 1]
        valid = below + at_or_above
        # 「跳過幾檔」是跟使用者實際要求的檔數(2*rows_n+1)比，不是跟多
        # 抓的候選數比——candidate_strikes 本來就刻意多抓一些當緩衝，
        # 多出來沒用到的候選不算「跳過」，只有湊不滿使用者要求的檔數時
        # 才算。
        skipped = max(0, (2 * rows_n + 1) - len(valid))

        self.table.setRowCount(len(valid))
        symbols = []
        for row, (strike, call_contract, put_contract) in enumerate(valid):
            self.strike_to_row[strike] = row
            self.row_meta[row] = {"strike": strike, "call_contract": call_contract, "put_contract": put_contract}
            call_key = str(call_contract.conId)
            put_key = str(put_contract.conId)
            self.symbol_row_side[call_key] = (row, "call")
            self.symbol_row_side[put_key] = (row, "put")
            symbols.append(call_contract)
            symbols.append(put_contract)

            palette = self._palette()
            self._set_cell(row, STRIKE_COL, f"{strike:g}", QBrush(palette["strike_bg"]), bold=True)
            self._init_side(row, CALL_COLS, QBrush(palette["call_bg"]))
            self._init_side(row, PUT_COLS, QBrush(palette["put_bg"]))

        self.quote_client.subscribe(symbols)
        if self._underlying_price is not None:
            self._highlight_atm_strike(nearest)
            for row in self.row_meta:
                self._recompute_delta(row, "call")
                self._recompute_delta(row, "put")

        skip_note = f"，{skipped} 檔這個到期日沒有掛牌已跳過" if skipped else ""
        self.status_label.setText(f"已訂閱 {len(valid)} 檔履約價 (共 {len(symbols)} 個合約){skip_note}")

    def _get_contract(self, strike: float, is_call: bool):
        """給 order_entry_widget.py 的價差單第二腳用——只查目前已經顯示
        在表格上、已經 qualify 過的合約，查不到(不在目前顯示範圍內)回傳
        None，呼叫端會提示使用者調整範圍，不會用猜的重新組一個。"""
        row = self.strike_to_row.get(strike)
        if row is None:
            return None
        meta = self.row_meta.get(row)
        if meta is None:
            return None
        return meta["call_contract"] if is_call else meta["put_contract"]

    def _clear_option_contracts(self):
        old_keys = list(self.symbol_row_side.keys())
        if old_keys:
            old_contracts = []
            for row in self.row_meta.values():
                old_contracts.append(row["call_contract"])
                old_contracts.append(row["put_contract"])
            self.quote_client.unsubscribe(old_contracts)
        self.row_meta.clear()
        self.symbol_row_side.clear()
        self.price_last_value.clear()
        self.strike_atm_row = None
        self.strike_to_row.clear()
        if self._active_price_cells:
            self._active_price_cells = set()
            self.table.viewport().update()

    def _clear_subscriptions(self):
        self._clear_option_contracts()
        if self._underlying_contract is not None:
            self.quote_client.unsubscribe([self._underlying_contract])

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
    def _on_quote_updated(self, symbol_key: str, data: dict):
        if symbol_key == self._underlying_symbol_key:
            self._on_underlying_quote(data)
            return

        entry = self.symbol_row_side.get(symbol_key)
        if entry is None:
            return
        row, side = entry
        cols = CALL_COLS if side == "call" else PUT_COLS

        for key, col in cols.items():
            value = data.get(key)
            if value is None:
                continue
            self._update_cell(row, col, value)

        self._recompute_delta(row, side)

    def _on_underlying_quote(self, data: dict):
        price = data.get("last")
        if price is None:
            return
        first_time = self._underlying_price is None
        self._underlying_price = price
        self.center_label.setText(f"{price:g}")
        self.payoff_chart_widget.set_underlying_price(price)
        if first_time:
            self._do_subscribe()  # 第一次拿到現價，重新置中一次
            return
        nearest = min(self._strikes, key=lambda s: abs(s - price)) if self._strikes else None
        if nearest is not None:
            self._highlight_atm_strike(nearest)
        for row in self.row_meta:
            self._recompute_delta(row, "call")
            self._recompute_delta(row, "put")

    def _recompute_delta(self, row: int, side: str) -> None:
        """拿買賣中價反推隱含波動率，算出 Delta 填進表格——IB 基礎報價
        不一定含 Greeks(取決於帳戶的市場資料權限)，這裡自己用
        Black-Scholes 算近似值，不是交易所/券商提供的即時資料。"""
        delta_col = CALL_DELTA_COL if side == "call" else PUT_DELTA_COL
        item = self.table.item(row, delta_col)
        if item is None:
            return
        meta = self.row_meta.get(row)
        if meta is None or self._underlying_price is None:
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

        expiry = self.expiry_combo.currentData()
        if expiry is None:
            item.setText("")
            return
        expiry_date = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        days = (expiry_date - datetime.date.today()).days
        if days <= 0:
            item.setText("")
            return
        time_to_expiry = days / 365.0
        is_call = side == "call"

        iv = black_scholes.implied_vol(is_call, self._underlying_price, meta["strike"], RISK_FREE_RATE, time_to_expiry, mid)
        if iv is None:
            item.setText("")
            return
        d = black_scholes.delta(is_call, self._underlying_price, meta["strike"], RISK_FREE_RATE, time_to_expiry, iv)
        item.setText(f"{d:.2f}" if d is not None else "")

    def _update_cell(self, row: int, col: int, value):
        item = self.table.item(row, col)
        if item is None:
            return
        item.setText(self._fmt(value))

        side = self.col_side.get(col)
        if side is not None:
            self.price_last_value[(row, col)] = value
            self._recolor_cell(row, col, value)

    def _recolor_cell(self, row: int, col: int, value):
        palette = self._palette()
        # 美股選擇權沒有台股那種即時可比對的「昨收參考價」漲跌顏色需
        # 求這麼強——這裡先用預設色顯示，之後有需要再接昨收比較。
        self._paint_cell(row, col, palette["price_bg"], palette["default_text"])
        try:
            float(value)
        except (TypeError, ValueError):
            return

    def _paint_cell(self, row: int, col: int, bg: QColor, fg: QColor):
        item = self.table.item(row, col)
        if item is None:
            return
        item.setBackground(QBrush(bg))
        item.setForeground(QBrush(fg))

    def _apply_table_theme(self):
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
                            self._recolor_cell(row, col, value)
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

    def _highlight_atm_strike(self, strike_value: float):
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
        new_cells = set()
        for symbol_key, side in legs:
            entry = self.symbol_row_side.get(symbol_key)
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
            meta["call_contract"], meta["put_contract"], is_call,
            float(call_bid), float(call_ask), float(put_bid), float(put_ask),
            self._strike_step,
        )
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
        self._clear_subscriptions()
        self.position_manager.shutdown()
        super().closeEvent(event)
