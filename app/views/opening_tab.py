import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QListWidget, QListWidgetItem, QMessageBox, QGroupBox, QSplitter,
)

from app.models.capital_client import CapitalClient
from app.models.capital_kline_client import (
    CapitalKLineClient, KLINE_TYPE_MINUTE, KLINE_TYPE_DAY, KLINE_TYPE_WEEK,
    KLINE_TYPE_MONTH, SESSION_FULL, SESSION_AM,
)
from app.models.capital_quote_client import CapitalQuoteClient
from app.services.intraday_bar_builder import IntradayBarBuilder, DAY_PERIOD_MINUTES
from app.services.opening_analysis import ChartDataService, OpeningAnalysisService, delete_history_entry, load_history
from app.views.analysis_detail_dialog import AnalysisDetailDialog
from app.views.candlestick_chart import PriceChartWidget
from app.views.opening_format import format_history_item, format_summary

NO_SELECTION_TEXT = "尚未選擇分析"

# 顯示文字 -> (sKLineType, sMinuteNumber, 即時組棒週期分鐘數或None)。
# 週/月線的 sMinuteNumber 沒有意義(API文件：這個參數只有 sKLineType=0
# 才有意義)，這裡固定填1。即時組棒週期是None代表週/月線不即時跳動——
# 這週/這月還沒走完的那一根，對短線選擇權交易沒有即時盯盤的意義，一律
# 顯示伺服器查回來最後一根「已經走完」的歷史棒就好。
PERIOD_OPTIONS = [
    ("1分", KLINE_TYPE_MINUTE, 1, 1),
    ("5分", KLINE_TYPE_MINUTE, 5, 5),
    ("30分", KLINE_TYPE_MINUTE, 30, 30),
    ("日線", KLINE_TYPE_DAY, 1, DAY_PERIOD_MINUTES),
    ("週線", KLINE_TYPE_WEEK, 1, None),
    ("月線", KLINE_TYPE_MONTH, 1, None),
]
DEFAULT_PERIOD_INDEX = 3  # 日線，跟原本改版前的預設圖表一致

# 查詢區間預設抓幾天，依週期給不同的量級：分線資料量大，預設抓近期就好；
# 日/週/月線資料量小，可以抓比較長的區間 (伺服器實測回抓得到，見
# capital_kline_client.py 的說明)。使用者滾輪/拖曳靠近邊界時會再往前加倍
# 補抓 (PriceChartWidget.request_more_history)。
DEFAULT_DAYS_BY_PERIOD = {
    "1分": 3,
    "5分": 10,
    "30分": 45,
    "日線": 60,
    "週線": 400,
    "月線": 1500,
}

SESSION_OPTIONS = [("全盤", SESSION_FULL), ("日盤", SESSION_AM)]

# 下午3點後才開始重試「今天這根日K有沒有出現」，沒查到假日曆(使用者決定
# 不查，見開發討論——反正沒資料就重試，不佔多少資源，狀態寫在下面就好)。
RECONCILE_AFTER_HOUR = 15
RECONCILE_RETRY_MS = 60_000


class OpeningTab(QWidget):
    def __init__(self, capital_client: CapitalClient, quote_client: CapitalQuoteClient):
        super().__init__()
        self._current_record = None
        self._quote_client = quote_client

        self.service = OpeningAnalysisService()
        self.service.progress.connect(self._on_progress)
        self.service.analysis_ready.connect(self._on_analysis_ready)
        self.service.analysis_failed.connect(self._on_analysis_failed)

        # 互動圖表(切換週期/日盤全盤/滾輪補歷史)跟背景的「今天日K有沒有
        # 出現」重試檢查共用同一個 CapitalKLineClient，用不同的 tag 區分
        # ——RequestKLineAMByDate 的 OnNotifyKLineData 事件不帶「這是哪一次
        # 呼叫觸發的」資訊，client 內部用 (tag, symbol) 擋掉同一代碼的重疊
        # 查詢(見 capital_kline_client.py)，兩邊都要各自檢查 is_pending()
        # 才能發新查詢，不能同時對同一代碼各發一次。
        self._kline_client = CapitalKLineClient(capital_client)
        self.chart_service = ChartDataService(self._kline_client)
        self.chart_service.chart_ready.connect(self._on_chart_ready)
        self.chart_service.chart_failed.connect(self._on_chart_failed)
        self.chart_service.chart_waiting.connect(self._on_chart_waiting)

        self._period_index = DEFAULT_PERIOD_INDEX
        self._session = SESSION_FULL
        self._chart_days = 0
        self._chart_loaded = False
        self._chart_busy = False
        self._primary_bars: list[dict] = []
        self._overlay_bars: list[dict] = []

        self._primary_bar_builder = IntradayBarBuilder()
        self._overlay_bar_builder = IntradayBarBuilder()
        self._primary_bar_builder.bar_updated.connect(
            lambda bar, is_new: self._on_live_bar(self._primary_bars, bar, is_new)
        )
        self._overlay_bar_builder.bar_updated.connect(
            lambda bar, is_new: self._on_live_bar(self._overlay_bars, bar, is_new)
        )
        self._quote_client.quote_updated.connect(self._on_quote_updated)
        self._quote_client.subscribe([ChartDataService.PRIMARY_SYMBOL, ChartDataService.OVERLAY_SYMBOL])

        # 即時跳動每一個tick都重畫K線圖太浪費(K棒是整包重新產生QPicture)，
        # 用一個短計時器把同一批tick coalesce成一次重畫。
        self._redraw_pending = False
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(500)
        self._redraw_timer.timeout.connect(self._flush_redraw)
        self._redraw_timer.start()

        self._build_ui()
        self.chart.request_more_history.connect(self._on_request_more_history)
        self._load_history()
        self._apply_period(DEFAULT_PERIOD_INDEX)

        # 「今天這根日K有沒有出現」背景重試：開啟APP立刻查一次，之後每分鐘
        # 檢查一次，下午3點後才會真的觸發重試 (開啟當下那一次不受這個時間
        # 限制)。
        self._daily_reconciled_date = None
        self._did_startup_reconcile = False
        self._kline_client.kline_ready.connect(self._on_reconcile_ready)
        self._kline_client.kline_failed.connect(self._on_reconcile_failed)
        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setInterval(RECONCILE_RETRY_MS)
        self._reconcile_timer.timeout.connect(self._check_daily_reconcile)
        self._reconcile_timer.start()
        QTimer.singleShot(0, self._check_daily_reconcile)

    def _build_ui(self):
        root = QVBoxLayout(self)

        control_row = QHBoxLayout()
        self.analyze_button = QPushButton("分析")
        self.analyze_button.clicked.connect(self._on_analyze_clicked)
        self.status_label = QLabel("尚未分析")
        control_row.addWidget(self.analyze_button)
        control_row.addWidget(self.status_label)
        control_row.addStretch()
        root.addLayout(control_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_history_box())
        splitter.addWidget(self._build_chart_box())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([350, 700])
        root.addWidget(splitter, 1)

        self.chart_status_label = QLabel("")
        root.addWidget(self.chart_status_label)

    def _build_chart_box(self) -> QGroupBox:
        box = QGroupBox("加權指數 K 線 + 台指期")
        layout = QVBoxLayout(box)

        chart_control_row = QHBoxLayout()
        chart_control_row.addWidget(QLabel("週期"))
        self.period_combo = QComboBox()
        for label, *_ in PERIOD_OPTIONS:
            self.period_combo.addItem(label)
        self.period_combo.setCurrentIndex(DEFAULT_PERIOD_INDEX)
        self.period_combo.currentIndexChanged.connect(self._on_period_combo_changed)
        chart_control_row.addWidget(self.period_combo)

        chart_control_row.addWidget(QLabel("盤別"))
        self.session_combo = QComboBox()
        for label, _value in SESSION_OPTIONS:
            self.session_combo.addItem(label)
        self.session_combo.currentIndexChanged.connect(self._on_session_combo_changed)
        chart_control_row.addWidget(self.session_combo)
        chart_control_row.addStretch()
        layout.addLayout(chart_control_row)

        self.selection_label = QLabel(NO_SELECTION_TEXT)
        layout.addWidget(self.selection_label)
        self.chart = PriceChartWidget()
        layout.addWidget(self.chart)
        return box

    def _build_history_box(self) -> QGroupBox:
        history_box = QGroupBox("歷史紀錄（單擊選取、雙擊查看詳細）")
        history_layout = QVBoxLayout(history_box)
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        self.history_list.itemDoubleClicked.connect(self._on_history_item_double_clicked)
        self.history_list.itemSelectionChanged.connect(self._on_history_selection_changed)
        history_layout.addWidget(self.history_list)
        delete_row = QHBoxLayout()
        self.delete_button = QPushButton("刪除選取的紀錄")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        delete_row.addWidget(self.delete_button)
        delete_row.addStretch()
        history_layout.addLayout(delete_row)
        return history_box

    def _load_history(self):
        for record in reversed(load_history()):
            self._add_history_item(record)

    def _add_history_item(self, record: dict, at_top: bool = False):
        item = QListWidgetItem(format_history_item(record))
        item.setData(Qt.UserRole, record)
        if at_top:
            self.history_list.insertItem(0, item)
        else:
            self.history_list.addItem(item)

    def _select_record(self, record: dict):
        self._current_record = record
        self.selection_label.setText(format_summary(record))
        self.chart.set_levels(record.get("resistance_levels") or [], record.get("support_levels") or [])

    def _on_analyze_clicked(self):
        self.analyze_button.setEnabled(False)
        self.status_label.setText("分析中…")
        self.service.run_async()

    def _on_progress(self, message: str):
        self.status_label.setText(message)

    def _on_analysis_ready(self, record: dict):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("分析完成")
        self._add_history_item(record, at_top=True)
        self.history_list.setCurrentRow(0)
        self._select_record(record)

    def _on_analysis_failed(self, message: str):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("分析失敗")
        QMessageBox.warning(self, "分析失敗", message)

    # ------------------------------------------------------------- 圖表週期
    def _current_period(self):
        return PERIOD_OPTIONS[self._period_index]

    def _apply_period(self, index: int):
        self._period_index = index
        label, kline_type, minute_number, live_period = self._current_period()
        self._current_kline_type = kline_type
        self._current_minute_number = minute_number
        self._chart_days = DEFAULT_DAYS_BY_PERIOD[label]

        self._chart_loaded = False
        self._primary_bars = []
        self._overlay_bars = []
        self._primary_bar_builder.set_period(live_period or 1)
        self._overlay_bar_builder.set_period(live_period or 1)
        self._live_period_minutes = live_period
        # 有「正在即時組的那根K棒」的週期(1/5/30分/日線)，圖表開盤中要讓
        # 最新那根K棒待在畫面正中間看；週/月線沒有即時組棒，維持原本「盡
        # 量保留使用者上次看的範圍」那一套。
        self.chart.set_live_follow(live_period is not None)

        self._request_chart()

    def _on_period_combo_changed(self, index: int):
        self._apply_period(index)

    def _on_session_combo_changed(self, index: int):
        self._session = SESSION_OPTIONS[index][1]
        self._chart_loaded = False
        self._primary_bars = []
        self._overlay_bars = []
        self._primary_bar_builder.reset()
        self._overlay_bar_builder.reset()
        self.chart.set_live_follow(self._live_period_minutes is not None)
        self._request_chart()

    def _request_chart(self):
        """RequestKLineAMByDate 文件裡沒有對應的取消查詢函式(有
        CancelRequestStocks/CancelRequestTicks，那是即時報價訂閱用的，跟
        歷史K線查詢是兩回事)——查詢一旦送出去就沒辦法中途喊停，只能等它
        自己收完。所以「查詢還沒收完時使用者又切了別的週期/盤別」這件事
        不能只是「盡量處理」，要在UI層面直接讓它不可能發生：查詢期間鎖住
        週期/盤別下拉選單跟捲軸補歷史，逼使用者等這次收完才能再選，不是
        猜使用者手速會不會比伺服器快。"""
        self._chart_busy = True
        self.period_combo.setEnabled(False)
        self.session_combo.setEnabled(False)
        self.chart_status_label.setText("K線圖讀取中…")
        self.chart_service.run_async(self._chart_days, self._current_kline_type, self._session, self._current_minute_number)

    def _finish_chart_request(self):
        self._chart_busy = False
        self.period_combo.setEnabled(True)
        self.session_combo.setEnabled(True)

    def _on_chart_ready(self, primary_history: list, overlay_history: list):
        self._finish_chart_request()
        self._primary_bars = primary_history
        self._overlay_bars = overlay_history
        self._chart_loaded = True
        self.chart_status_label.setText("")
        self._redraw_now()
        if self._current_record:
            self.chart.set_levels(
                self._current_record.get("resistance_levels") or [],
                self._current_record.get("support_levels") or [],
            )

    def _on_chart_failed(self, message: str):
        self._finish_chart_request()
        self.chart_status_label.setText(f"K線圖讀取失敗：{message}")

    def _on_chart_waiting(self):
        # 這個狀態只會在跟背景的「今天日K重試確認」(不同tag、使用者無法從
        # UI阻止它)撞在一起時才會發生——使用者自己觸發的查詢已經被下拉選單
        # 鎖住擋掉了，不會走到這裡。
        self.chart_status_label.setText("背景資料確認中，圖表稍候更新…")

    def _on_request_more_history(self, days: int):
        if self._chart_busy or days <= self._chart_days:
            return  # 查詢還沒收完就不接受新的捲動補歷史請求，理由同 _request_chart
        self._chart_days = days
        self._request_chart()

    # --------------------------------------------------------- 即時跳動
    def _on_quote_updated(self, symbol: str, data: dict):
        if not self._chart_loaded or self._live_period_minutes is None:
            return  # 還沒載入歷史資料打底，或目前選的是週/月線(不即時組棒)
        price = data.get("last")
        tick_qty = data.get("tick_qty")
        if symbol == ChartDataService.PRIMARY_SYMBOL:
            self._primary_bar_builder.on_tick(price, tick_qty)
        elif symbol == ChartDataService.OVERLAY_SYMBOL:
            self._overlay_bar_builder.on_tick(price, tick_qty)

    def _on_live_bar(self, bars: list, bar: dict, is_new_bar: bool):
        if not bars or is_new_bar:
            bars.append(bar)
        else:
            bars[-1] = bar
        self._redraw_pending = True

    def _flush_redraw(self):
        if self._redraw_pending:
            self._redraw_now()

    def _redraw_now(self):
        self._redraw_pending = False
        self.chart.set_price_data(self._primary_bars, self._overlay_bars, self._chart_days)

    # --------------------------------------------------- 今天日K的重試確認
    RECONCILE_TAG = "reconcile"

    def _check_daily_reconcile(self):
        today = datetime.date.today()
        if self._daily_reconciled_date == today:
            return
        if self._did_startup_reconcile and datetime.datetime.now().time() < datetime.time(RECONCILE_AFTER_HOUR, 0):
            return  # 還沒到下午3點，且不是開啟APP那一次，先不用重試
        if self._kline_client.is_pending(ChartDataService.PRIMARY_SYMBOL):
            return  # 互動圖表正在查同一個代碼，避免撞在一起，下一分鐘再試
        self._did_startup_reconcile = True

        end = today
        start = today - datetime.timedelta(days=5)
        self._kline_client.request_range(
            self.RECONCILE_TAG, ChartDataService.PRIMARY_SYMBOL, KLINE_TYPE_DAY, SESSION_FULL, start, end, 1,
        )

    def _on_reconcile_ready(self, tag: str, symbol: str, bars: list):
        if tag != self.RECONCILE_TAG or symbol != ChartDataService.PRIMARY_SYMBOL:
            return
        today = datetime.date.today()
        has_today = any(bar.get("calendar_date") == today.isoformat() for bar in bars)
        if has_today:
            self._daily_reconciled_date = today
            self.chart_status_label.setText("")
            # 使用者現在剛好在看日線的話，拿伺服器確認過的正式日K重新整理
            # 一次，取代原本用tick自己組的暫定值 (收盤價等細節可能跟正式
            # 結算資料有些微差異)。
            if self._current_period()[0] == "日線" and not self._kline_client.is_pending(ChartDataService.PRIMARY_SYMBOL):
                self._request_chart()
        else:
            self.chart_status_label.setText("今天的日K還沒出現，1分鐘後自動重試…")

    def _on_reconcile_failed(self, tag: str, symbol: str, message: str):
        if tag != self.RECONCILE_TAG or symbol != ChartDataService.PRIMARY_SYMBOL:
            return
        self.chart_status_label.setText(f"今日資料確認失敗，1分鐘後自動重試：{message}")

    def _on_history_item_clicked(self, item: QListWidgetItem):
        self._select_record(item.data(Qt.UserRole))

    def _on_history_item_double_clicked(self, item: QListWidgetItem):
        record = item.data(Qt.UserRole)
        self._select_record(record)
        dialog = AnalysisDetailDialog(record, parent=self)
        dialog.record_saved.connect(self._on_record_saved)
        dialog.exec_()

    def _on_record_saved(self, record: dict):
        if self._current_record and self._current_record.get("timestamp") == record.get("timestamp"):
            self._select_record(record)

    def _on_history_selection_changed(self):
        self.delete_button.setEnabled(bool(self.history_list.selectedItems()))

    def _on_delete_clicked(self):
        item = self.history_list.currentItem()
        if not item:
            return
        confirm = QMessageBox.question(
            self, "刪除紀錄", "確定要刪除這筆歷史紀錄嗎？此動作無法復原。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        record = item.data(Qt.UserRole)
        delete_history_entry(record.get("timestamp"))
        self.history_list.takeItem(self.history_list.row(item))

        if self._current_record and self._current_record.get("timestamp") == record.get("timestamp"):
            self._current_record = None
            self.selection_label.setText(NO_SELECTION_TEXT)
            self.chart.clear_levels()
