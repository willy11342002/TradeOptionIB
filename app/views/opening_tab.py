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
from app.models.capital_tick_client import CapitalTickClient
from app.services.intraday_bar_builder import MinuteBarAggregator, DAY_PERIOD_MINUTES
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
    def __init__(self, capital_client: CapitalClient):
        super().__init__()
        self._current_record = None
        # dock 沒打開/沒被看到就不要打 K 線查詢、不要訂閱即時 tick——見
        # activate()/deactivate()，由 main_window.py 接 QDockWidget 的
        # visibilityChanged 呼叫。
        self._active = False

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
        # 日/週/月線走這組：history是ChartDataService查來的(日/週/月線
        # 沒有對齊問題，直接用伺服器給的)，today是MinuteBarAggregator用
        # tick組的「今天」day-bucket，兩者事後合併去重(_merge_history_and_today)。
        self._primary_history: list[dict] = []
        self._overlay_history: list[dict] = []
        self._primary_today: list[dict] = []
        self._overlay_today: list[dict] = []
        # 1/5/30分這種「分鐘週期」改走這組：歷史(1分鐘K)跟今天(tick組的
        # 1分鐘K)灌進同一個MinuteBarAggregator，統一用整點/半點對齊二次
        # 聚合，aggregator吐出來的就是最終結果，不用再另外合併——見
        # intraday_bar_builder.py 開頭說明「為什麼歷史也要拆成1分鐘K自己
        # 重疊」(伺服器給的多分鐘K是照各商品自己的開盤時間對齊，不是整點/
        # 半點，TX00跟TSEA疊在一起會對不齊)。
        self._is_minute_period = False
        self._primary_final: list[dict] = []
        self._overlay_final: list[dict] = []

        # 「今天」的資料改用 CapitalTickClient(SKQuoteLib_RequestTicks) 拿，
        # 不再靠即時報價(RequestStocks)的tick_qty欄位組——那個機制只有訂閱
        # 之後才會有資料，沒辦法回補「開盤到訂閱那一刻」這段，RequestTicks
        # 訂閱時會先回補當天的逐筆成交(OnNotifyHistoryTicksLONG)，之後才是
        # 即時tick(OnNotifyTicksLONG)，兩者都餵給MinuteBarAggregator，用tick
        # 自己的時間戳記分K棒，回補的歷史tick才不會被誤判成「現在」。
        #
        # *** 訂閱本身延後到 activate() 才做(dock 真的打開才訂閱)，不是這
        # 裡就訂 ***——RequestTicks 訂閱當下會回補當天全部逐筆成交(不是
        # 小動作)，dock 關著的話沒必要打這個查詢。
        self._tick_client = CapitalTickClient(capital_client)
        self._tick_client.tick_received.connect(self._on_tick_received)
        self._tick_client.subscribe_failed.connect(self._on_tick_subscribe_failed)

        self._primary_aggregator = MinuteBarAggregator()
        self._overlay_aggregator = MinuteBarAggregator()
        self._primary_aggregator.bars_changed.connect(self._on_primary_today_changed)
        self._overlay_aggregator.bars_changed.connect(self._on_overlay_today_changed)

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
        # 只設定週期狀態/UI，不觸發真的查詢(_apply_period 內部呼叫的
        # _request_chart 在 self._active=False 時是no-op，見下面)。
        self._apply_period(DEFAULT_PERIOD_INDEX)

        # 「今天這根日K有沒有出現」背景重試：dock 打開才開始查(activate()
        # 觸發)，之後每分鐘檢查一次，下午3點後才會真的觸發重試(dock剛打
        # 開那一次不受這個時間限制)。計時器本身在這裡建立，但要等
        # activate() 才 start()，不要 dock 關著也一直在背景打。
        self._daily_reconciled_date = None
        self._did_startup_reconcile = False
        self._kline_client.kline_ready.connect(self._on_reconcile_ready)
        self._kline_client.kline_failed.connect(self._on_reconcile_failed)
        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setInterval(RECONCILE_RETRY_MS)
        self._reconcile_timer.timeout.connect(self._check_daily_reconcile)

    # --------------------------------------------------------- 開/關 dock
    def activate(self) -> None:
        """dock 變成看得到才呼叫(main_window.py 接 QDockWidget.
        visibilityChanged)。K線查詢/tick訂閱都在這裡才第一次真的打出去，
        不是建構子當下——dock 沒打開就完全不消耗這些查詢額度/連線資源。

        *** tick 訂閱(RequestTicks)沒有辦法中途取消 ***：這個專案目前查
        過的群益文件只有 RequestStocks 有對應的
        SKQuoteLib_CancelRequestStocks，RequestTicks 沒有查到對應的取消
        函式(不是沒查、是真的沒有)，所以 deactivate() 沒辦法真的停止已
        經訂閱過的 tick 串流——只能保證「還沒開過 dock 之前絕對不會訂
        閱」，訂閱過一次之後，就算之後關掉 dock，tick 還是會繼續在背景
        收(不會拿去做任何事，因為 K 線查詢/重繪不會再被觸發)。"""
        if self._active:
            return
        self._active = True
        self._tick_client.subscribe(ChartDataService.PRIMARY_SYMBOL)
        self._tick_client.subscribe(ChartDataService.OVERLAY_SYMBOL)
        if not self._chart_loaded and not self._chart_busy:
            self._request_chart()
        self._reconcile_timer.start()
        QTimer.singleShot(0, self._check_daily_reconcile)

    def deactivate(self) -> None:
        """dock 關掉/切到別的分頁看不到了才呼叫。K線背景重試計時器可以
        真的停掉；tick 訂閱的限制見 activate() 的說明，這裡停不了。"""
        if not self._active:
            return
        self._active = False
        self._reconcile_timer.stop()

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
        self._is_minute_period = (kline_type == KLINE_TYPE_MINUTE)

        self._chart_loaded = False
        self._primary_history = []
        self._overlay_history = []
        self._primary_today = []
        self._overlay_today = []
        self._primary_final = []
        self._overlay_final = []
        # set_period不清掉已經收到的1分鐘K(不管是回補還是即時)，只是換一
        # 種粒度重新疊一次——不然切個週期，剛回補到的「開盤到現在」就白費
        # 了(見 intraday_bar_builder.py 的說明)。
        self._primary_aggregator.set_period(minute_number if self._is_minute_period else (live_period or 1))
        self._overlay_aggregator.set_period(minute_number if self._is_minute_period else (live_period or 1))
        self._live_period_minutes = live_period
        # 有「正在即時組的那根K棒」的週期(1/5/30分/日線)，圖表開盤中要讓
        # 最新那根K棒待在畫面正中間看；週/月線沒有即時組棒，維持原本「盡
        # 量保留使用者上次看的範圍」那一套。
        self.chart.set_live_follow(live_period is not None)

        self._request_chart()

    def _on_period_combo_changed(self, index: int):
        self._apply_period(index)

    def _on_session_combo_changed(self, index: int):
        # RequestTicks沒有盤別的概念，tick不分日盤/全盤——切換盤別時「今天」
        # 這部分沒辦法只挑合乎新盤別的部分保留，只能整批清掉重來(reset)，
        # 等新的歷史查詢+新tick重新填，這段期間「今天」會暫時是空的。
        self._session = SESSION_OPTIONS[index][1]
        self._chart_loaded = False
        self._primary_history = []
        self._overlay_history = []
        self._primary_today = []
        self._overlay_today = []
        self._primary_final = []
        self._overlay_final = []
        self._primary_aggregator.reset()
        self._overlay_aggregator.reset()
        self._request_chart()

    def _request_chart(self):
        """RequestKLineAMByDate 文件裡沒有對應的取消查詢函式(有
        CancelRequestStocks/CancelRequestTicks，那是即時報價訂閱用的，跟
        歷史K線查詢是兩回事)——查詢一旦送出去就沒辦法中途喊停，只能等它
        自己收完。所以「查詢還沒收完時使用者又切了別的週期/盤別」這件事
        不能只是「盡量處理」，要在UI層面直接讓它不可能發生：查詢期間鎖住
        週期/盤別下拉選單跟捲軸補歷史，逼使用者等這次收完才能再選，不是
        猜使用者手速會不會比伺服器快。

        dock 還沒打開(self._active=False)時直接跳過，不要在背景默默打
        API——呼叫端(_apply_period/_on_session_combo_changed/
        _on_request_more_history)已經把「要查什麼」的狀態記好了
        (self._chart_loaded 維持 False)，等 activate() 才會真的補查。"""
        if not self._active:
            return
        self._chart_busy = True
        self.period_combo.setEnabled(False)
        self.session_combo.setEnabled(False)
        self.chart_status_label.setText("K線圖讀取中…")
        # 分鐘週期一律跟伺服器要1分鐘K，5分/30分完全自己在本地對齊聚合——
        # 伺服器給的多分鐘K是照各商品自己的開盤時間對齊，不是整點/半點，
        # TX00(08:45開盤)跟TSEA(09:00開盤)疊在一起會對不齊(實測過，見
        # intraday_bar_builder.py)。
        server_minute_number = 1 if self._is_minute_period else self._current_minute_number
        self.chart_service.run_async(self._chart_days, self._current_kline_type, self._session, server_minute_number)

    def _finish_chart_request(self):
        self._chart_busy = False
        self.period_combo.setEnabled(True)
        self.session_combo.setEnabled(True)

    def _on_chart_ready(self, primary_history: list, overlay_history: list):
        self._finish_chart_request()
        self._chart_loaded = True
        self.chart_status_label.setText("")
        if self._is_minute_period:
            # 這裡收到的其實是1分鐘K(不管使用者選5分還是30分)，跟今天
            # tick組的1分鐘K灌進同一個池子，由aggregator統一對齊聚合成
            # 使用者真正選的週期，結果會從bars_changed訊號回來觸發重畫。
            self._primary_aggregator.load_minute_bars(primary_history)
            self._overlay_aggregator.load_minute_bars(overlay_history)
        else:
            self._primary_history = primary_history
            self._overlay_history = overlay_history
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
    def _on_tick_received(self, symbol: str, dt, price: float, qty: float):
        if self._live_period_minutes is None:
            return  # 目前選的是週/月線，不即時組棒
        if symbol == ChartDataService.PRIMARY_SYMBOL:
            self._primary_aggregator.on_tick(dt, price, qty)
        elif symbol == ChartDataService.OVERLAY_SYMBOL:
            self._overlay_aggregator.on_tick(dt, price, qty)

    def _on_tick_subscribe_failed(self, symbol: str, message: str):
        self.chart_status_label.setText(f"{symbol} 即時報價訂閱失敗：{message}")

    def _on_primary_today_changed(self, bars: list):
        # 分鐘週期下，aggregator吐出來的已經是「歷史1分鐘K+今天1分鐘K」
        # 統一對齊聚合完的最終結果，不用再合併；日/週/月線下，這裡只是
        # 「今天」，還要跟歷史事後合併去重(_merge_history_and_today)。
        if self._is_minute_period:
            self._primary_final = bars
        else:
            self._primary_today = bars
        self._redraw_pending = True

    def _on_overlay_today_changed(self, bars: list):
        if self._is_minute_period:
            self._overlay_final = bars
        else:
            self._overlay_today = bars
        self._redraw_pending = True

    def _flush_redraw(self):
        if self._redraw_pending:
            self._redraw_now()

    def _redraw_now(self):
        self._redraw_pending = False
        if self._is_minute_period:
            primary = self._primary_final
            overlay = self._overlay_final
        else:
            primary = self._merge_history_and_today(self._primary_history, self._primary_today)
            overlay = self._merge_history_and_today(self._overlay_history, self._overlay_today)
        self.chart.set_price_data(primary, overlay, self._chart_days)

    @staticmethod
    def _merge_history_and_today(history: list, today: list) -> list:
        """history(ChartDataService查來的)跟today(CapitalTickClient tick組
        的)可能重疊——history查詢範圍是到「今天」，今天已經結束的盤(例如
        現在是夜盤時間，今天的日盤早就收盤了)會出現在history裡；today是
        自己用tick組的，只要今天訂閱過就有資料，不管那個盤結束了沒。同一
        個時間點(同一個date標籤)兩邊都有的話，以history(伺服器正式資料)
        為準，today只補history沒有的部分(通常就是還在進行中的那個盤)。"""
        history_dates = {bar["date"] for bar in history}
        return history + [bar for bar in today if bar["date"] not in history_dates]

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
        today_bar = next((b for b in bars if b.get("calendar_date") == today.isoformat()), None)
        if today_bar is not None:
            self._daily_reconciled_date = today
            self.chart_status_label.setText("")
            # 使用者現在剛好在看日線的話，重新查一次歷史——這次會員含伺服
            # 器正式確認過的今天日K，跟tick組的today合併時(_merge_history_
            # and_today)以歷史為準，自然取代掉tick自己組的暫定值，不用手動
            # 拼接。
            if self._current_period()[0] == "日線" and not self._chart_busy:
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
