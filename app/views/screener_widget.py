"""
選擇權篩選器 dock 內容，對應 test.ipynb 最後「選擇權篩選」章節、以及使
用者提供的目標畫面截圖(tastytrade 風格的兩階段篩選器)。

分成上下兩個階段，各自獨立按鈕觸發(不會掃描完自動接著跑復篩)——復篩要
對每一檔候選標的各查一次選擇權鏈+報價，候選一多會很花時間，讓使用者先
看過初篩清單、自己勾選/增刪要復篩的標的，再手動觸發第二階段，藉此控制
單次要打多少 IB 請求，也是這次跟使用者確認過的流程。
"""
import asyncio
from datetime import datetime

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from app.models.ib_client import IBClient
from app.models.scanner_catalog import load_filter_catalog, load_scan_type_catalog, scan_type_by_code
from app.models.screener import (
    CandidateStock, ScannerParams, ScreenFilters, run_scanner, screen_one,
)
from app.services import scan_history_store
from app.services.background_tasks import spawn
from app.views.filter_assistant_dialog import FilterAssistantDialog
from app.views.scan_code_picker import ScanCodePickerDialog
from app.views.scan_history_widgets import ScanRunDetailDialog
from app.views.scanner_filter_picker import AddFilterDialog, FilterRowWidget
from app.views.widget_helpers import FlowLayout, field_card, range_widget

# 篩選條件面板初次開啟時方便使用者的預設值——對應
# app/resources/scan_filters.json 裡的 id，跟舊版寫死的股價/市值/選擇權
# 成交量 3 個欄位一致，避免介面一開始是空的、體驗倒退。
DEFAULT_FILTER_IDS = ["PRICE", "MKTCAP", "OPTVOLUME"]

CANDIDATE_COLUMNS = ["納入", "代碼", "來源", "排名"]

RESULT_COLUMNS = [
    "代碼", "標的現價", "到期日", "距到期天數", "履約價",
    "C買價", "C賣價", "C價差%", "C近似IV",
    "P買價", "P賣價", "P價差%", "P近似IV", "狀態",
]


class ScreenerWidget(QWidget):
    # 使用者雙擊一列已通過復篩的結果，帶去選擇權報價 dock 查看完整選擇
    # 權鏈——只丟 (symbol, expiry)，實際查詢/訂閱交給 MainWindow 既有的
    # _run_query_symbol()/expiry_combo 邏輯，這裡不重複做一套。
    symbol_selected = pyqtSignal(str, str)

    def __init__(self, ib_client: IBClient, parent=None):
        super().__init__(parent)
        self._ib = ib_client.ib
        self._candidates: list[CandidateStock] = []
        self._busy = False
        self._filter_rows: dict[str, FilterRowWidget] = {}
        self._selected_scan_code: str | None = None

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_scan_tab(), "① 初篩選股")
        self.tabs.addTab(self._build_screen_tab(), "② 選擇權復篩")
        self.tabs.addTab(self._build_history_tab(), "③ 掃描紀錄")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        self.status_label = QLabel("尚未執行")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------ 初篩頁籤
    def _build_scan_tab(self) -> QWidget:
        """兩步驟拆成獨立頁籤——復篩要花時間跑一整批標的，讓使用者能把
        候選清單頁跟結果頁分開看，各自都有夠大的表格空間，不用在一個畫
        面裡跟一堆條件欄位擠。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self._build_scan_conditions_box())
        layout.addWidget(self._build_candidate_table_box(), 1)
        return tab

    def _build_scan_conditions_box(self) -> QGroupBox:
        box = QGroupBox("市場掃描條件")
        outer = QVBoxLayout(box)
        outer.setSpacing(12)

        # *** 主畫面只留一個按鈕，不長駐輸入框+下拉清單 ***：原本這裡是
        # 一個一直掛著的 ScanCodePicker(QLineEdit+debounce+AI)，改成跟
        # scanner_filter_picker.py::AddFilterDialog 同一套「按鈕→彈窗
        # 選」互動——按下去才開 ScanCodePickerDialog(裡面還是同一套搜尋
        # +AI 建議的邏輯)，選完就收起來，平常不佔用主畫面版面。
        self.scan_code_btn = QPushButton("請選擇")
        self.scan_code_btn.clicked.connect(self._on_pick_scan_code)

        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(1, 50)
        self.max_results_spin.setValue(20)

        # 每個條件用「標題在上、輸入框在下」的卡片排版(仿照使用者提供的
        # 目標截圖那種風格)，比 QFormLayout 那種「標籤+輸入框同一列左右
        # 排」更不容易在窄一點的 dock 寬度下擠成一整排看不懂的文字。
        row1 = QHBoxLayout()
        row1.addLayout(field_card("掃描代碼", self.scan_code_btn), 3)
        row1.addLayout(field_card("最多取幾檔", self.max_results_spin), 1)
        outer.addLayout(row1)

        # *** 篩選條件不再寫死幾個欄位，改成可動態增減的清單 ***：IB 實
        # 際開放 300 多個數值型篩選欄位(見 app/models/scanner_catalog.py)，
        # 這裡仿 IB 自己 TWS 桌面版 MultiSort 篩選器的做法——「＋ 新增篩
        # 選條件」開搜尋對話框(scanner_filter_picker.py::AddFilterDialog)
        # 挑一個要加的欄位，變成一列可移除的 FilterRowWidget。
        # *** 用 FlowLayout 不用 QHBoxLayout ***：條件加多了 QHBoxLayout
        # 只會無限往右延伸、超出 dock 寬度看不到——FlowLayout 會依可用寬
        # 度自動換行。Qt 的 height-for-width layout 巢狀在另一個 layout
        # 裡容易有高度算不對的問題，穩妥的做法是包一層 QWidget 再
        # addWidget()，不要直接 outer.addLayout(flow_layout)，見
        # widget_helpers.py::FlowLayout 的說明。
        self.filter_rows_layout = FlowLayout(hspacing=8, vspacing=8)
        filter_rows_container = QWidget()
        filter_rows_container.setLayout(self.filter_rows_layout)
        outer.addWidget(filter_rows_container)
        for filter_id in DEFAULT_FILTER_IDS:
            filter_def = next((f for f in load_filter_catalog() if f.id == filter_id), None)
            if filter_def is not None:
                self._add_filter_row(filter_def)

        scan_btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("執行市場掃描")
        self.scan_btn.clicked.connect(lambda: spawn(self._on_run_scanner()))
        scan_btn_row.addWidget(self.scan_btn)
        add_filter_btn = QPushButton("＋ 新增篩選條件")
        add_filter_btn.clicked.connect(self._on_add_filter_clicked)
        scan_btn_row.addWidget(add_filter_btn)
        ai_assist_btn = QPushButton("AI 條件建議")
        ai_assist_btn.clicked.connect(self._on_ai_filter_assist_clicked)
        scan_btn_row.addWidget(ai_assist_btn)
        scan_btn_row.addStretch(1)
        outer.addLayout(scan_btn_row)

        manual_row = QHBoxLayout()
        self.manual_symbol_edit = QLineEdit()
        self.manual_symbol_edit.setPlaceholderText("手動加入代碼，可用空白或逗號分隔多檔，例如 AAPL, SPY")
        self.manual_symbol_edit.returnPressed.connect(self._on_add_manual_symbols)
        add_btn = QPushButton("新增到候選清單")
        add_btn.clicked.connect(self._on_add_manual_symbols)
        manual_row.addWidget(self.manual_symbol_edit)
        manual_row.addWidget(add_btn)
        outer.addLayout(manual_row)

        return box

    # ------------------------------------------------------- 篩選條件列
    def _on_add_filter_clicked(self):
        dialog = AddFilterDialog(load_filter_catalog(), set(self._filter_rows), self)
        if dialog.exec_() != AddFilterDialog.Accepted:
            return
        filter_id = dialog.selected_filter_id()
        if filter_id is None:
            return
        filter_def = next((f for f in load_filter_catalog() if f.id == filter_id), None)
        if filter_def is not None:
            self._add_filter_row(filter_def)

    def _add_filter_row(self, filter_def):
        if filter_def.id in self._filter_rows:
            return
        row_widget = FilterRowWidget(filter_def)
        row_widget.removed.connect(self._on_filter_row_removed)
        self.filter_rows_layout.addWidget(row_widget)
        self._filter_rows[filter_def.id] = row_widget

    def _on_filter_row_removed(self, filter_id: str):
        row_widget = self._filter_rows.pop(filter_id, None)
        if row_widget is not None:
            self.filter_rows_layout.removeWidget(row_widget)
            row_widget.deleteLater()

    def _on_pick_scan_code(self):
        dialog = ScanCodePickerDialog(load_scan_type_catalog(), self._selected_scan_code, self)
        if dialog.exec_() != ScanCodePickerDialog.Accepted:
            return
        code = dialog.selected_code()
        if code is not None:
            self._set_scan_code(code)

    def _set_scan_code(self, code: str) -> None:
        scan_type = scan_type_by_code(code)
        self._selected_scan_code = code
        self.scan_code_btn.setText(scan_type.name_zh if scan_type else code)

    def _on_ai_filter_assist_clicked(self):
        """AI 條件建議——彈對話框讓使用者用自然語言描述需求，AI 提案的
        掃描代碼/篩選條件在對話框裡先預覽，使用者按「套用」才會真的推
        進這裡的正式表單(見 filter_assistant_dialog.py 開頭的說明)。"""
        dialog = FilterAssistantDialog(self)
        if dialog.exec_() != FilterAssistantDialog.Accepted:
            return
        scan_code = dialog.result_scan_code()
        if scan_code:
            self._set_scan_code(scan_code)
        for preview_row in dialog.result_rows():
            filter_def = preview_row.filter_def
            above, below = preview_row.get_values_raw()
            # 已經有這個條件(常見於預設就先加好的 PRICE/MKTCAP/OPTVOLUME)
            # 就直接更新既有那一列的值，不要略過——使用者是在按下「套
            # 用」的當下明確要套用這份 AI 建議，略過等於悄悄丟掉他剛剛
            # 看過、同意的數字，比蓋掉舊值更容易造成誤解。
            if filter_def.id not in self._filter_rows:
                self._add_filter_row(filter_def)
            self._filter_rows[filter_def.id].set_values(above, below)

    def _build_candidate_table_box(self) -> QGroupBox:
        box = QGroupBox("候選標的清單")
        outer = QVBoxLayout(box)
        hint = QLabel("勾選「納入」的標的才會送進復篩")
        hint.setStyleSheet("color: palette(mid);")
        outer.addWidget(hint)

        self.candidate_table = QTableWidget(0, len(CANDIDATE_COLUMNS))
        self.candidate_table.setHorizontalHeaderLabels(CANDIDATE_COLUMNS)
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.candidate_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.candidate_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        # *** 不設 setMaximumHeight ***：之前限制 180px 高，整個 dock 明明
        # 有空間卻硬把表格擠扁，只看得到一兩列——現在讓它跟頁籤分頁本身
        # 一樣高，用 outer.addWidget(..., 1) 撐滿剩餘空間。
        outer.addWidget(self.candidate_table, 1)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("全選")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("全不選")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        remove_btn = QPushButton("移除選取列")
        remove_btn.clicked.connect(self._on_remove_selected)
        clear_btn = QPushButton("清空清單")
        clear_btn.clicked.connect(self._on_clear_candidates)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(select_none_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        return box

    # ------------------------------------------------------------ 復篩頁籤
    def _build_screen_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self._build_screen_conditions_box())
        layout.addWidget(self._build_result_table(), 1)
        return tab

    def _build_screen_conditions_box(self) -> QGroupBox:
        box = QGroupBox("選擇權天期/流動性條件")
        outer = QVBoxLayout(box)

        self.min_dte_spin = QSpinBox()
        self.min_dte_spin.setRange(0, 720)
        self.min_dte_spin.setValue(20)
        self.min_dte_spin.setSuffix(" 天")
        self.max_dte_spin = QSpinBox()
        self.max_dte_spin.setRange(0, 720)
        self.max_dte_spin.setValue(45)
        self.max_dte_spin.setSuffix(" 天")

        self.max_spread_spin = QDoubleSpinBox()
        self.max_spread_spin.setRange(0.1, 500.0)
        self.max_spread_spin.setValue(15.0)
        self.max_spread_spin.setSuffix(" %")

        self.min_iv_spin = QDoubleSpinBox()
        self.min_iv_spin.setRange(0, 500.0)
        self.min_iv_spin.setValue(0.0)
        self.min_iv_spin.setSuffix(" %")
        self.min_iv_spin.setSpecialValueText("不限")

        # 跟初篩條件用同一套「標題在上、控制項在下」卡片排版，取代原本
        # 一堆 QLabel 串成一長條的寫法。
        cond_row = QHBoxLayout()
        cond_row.addLayout(field_card("距到期天數(DTE)", range_widget(self.min_dte_spin, self.max_dte_spin)))
        cond_row.addLayout(field_card("最大買賣價差(佔中價%)", self.max_spread_spin))
        cond_row.addLayout(field_card("最小近似IV", self.min_iv_spin))
        cond_row.addStretch(1)
        outer.addLayout(cond_row)

        btn_row = QHBoxLayout()
        self.screen_btn = QPushButton("執行選擇權復篩")
        self.screen_btn.clicked.connect(lambda: spawn(self._on_run_screen()))
        btn_row.addWidget(self.screen_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        return box

    def _build_result_table(self) -> QTableWidget:
        self.result_table = QTableWidget(0, len(RESULT_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLUMNS)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.result_table.cellDoubleClicked.connect(self._on_result_double_clicked)
        return self.result_table

    # ---------------------------------------------------------- 候選清單
    def _on_add_manual_symbols(self):
        text = self.manual_symbol_edit.text().strip().upper()
        if not text:
            return
        symbols = [s.strip() for s in text.replace(",", " ").split() if s.strip()]
        self._add_candidates([CandidateStock(symbol=s, source="manual") for s in symbols])
        self.manual_symbol_edit.clear()

    def _add_candidates(self, candidates: list[CandidateStock]):
        existing = {c.symbol for c in self._candidates}
        added = 0
        for cand in candidates:
            if cand.symbol in existing:
                continue
            existing.add(cand.symbol)
            self._candidates.append(cand)
            self._append_candidate_row(cand)
            added += 1
        if added:
            self.status_label.setText(f"候選清單新增 {added} 檔，目前共 {len(self._candidates)} 檔")

    def _append_candidate_row(self, cand: CandidateStock):
        row = self.candidate_table.rowCount()
        self.candidate_table.insertRow(row)

        check_item = QTableWidgetItem()
        check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check_item.setCheckState(Qt.Checked)
        self.candidate_table.setItem(row, 0, check_item)

        self.candidate_table.setItem(row, 1, QTableWidgetItem(cand.symbol))
        source_label = "掃描" if cand.source == "scanner" else "手動"
        self.candidate_table.setItem(row, 2, QTableWidgetItem(source_label))
        rank_text = str(cand.rank) if cand.rank is not None else ""
        self.candidate_table.setItem(row, 3, QTableWidgetItem(rank_text))

    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.candidate_table.rowCount()):
            item = self.candidate_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _on_remove_selected(self):
        rows = sorted({idx.row() for idx in self.candidate_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.candidate_table.removeRow(row)
            del self._candidates[row]

    def _on_clear_candidates(self):
        self.candidate_table.setRowCount(0)
        self._candidates.clear()

    def _checked_symbols(self) -> list[str]:
        symbols = []
        for row in range(self.candidate_table.rowCount()):
            item = self.candidate_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                symbols.append(self.candidate_table.item(row, 1).text())
        return symbols

    # -------------------------------------------------------------- 初篩
    async def _on_run_scanner(self):
        # *** 一定要透過 spawn() 呼叫(見按鈕接線處)，不能用 @asyncSlot()
        # 直接接訊號 ***：qasync 的 @asyncSlot() 建立的 Task 只是函式內
        # 的區域變數，slot 一返回就沒人持有，事件迴圈對執行中的 Task 只
        # 有弱參照——已經在 scan_code_picker.py 的 AI 呼叫實測抓到真實案
        # 例(logs/app.log 記錄到「Task was destroyed but it is
        # pending!」，AI 明明已經成功回應)，市場掃描這種同樣要跑好幾秒
        # 的操作有一樣的風險，見 app/services/background_tasks.py 的完
        # 整說明。
        if self._busy:
            return
        self._busy = True
        self.scan_btn.setEnabled(False)
        self.status_label.setText("市場掃描中...")
        try:
            scan_code = self._selected_scan_code
            if scan_code is None:
                self.status_label.setText("請先按「掃描代碼」選一個")
                return
            params = ScannerParams(
                scan_code=scan_code,
                max_results=self.max_results_spin.value(),
                filters=[v for row in self._filter_rows.values() for v in row.values()],
            )
            try:
                candidates = await run_scanner(self._ib, params)
            except Exception as exc:  # noqa: BLE001
                self.status_label.setText(f"市場掃描失敗：{exc}")
                return
            if not candidates:
                self.status_label.setText("市場掃描沒有回傳任何標的")
                return
            self._add_candidates(candidates)
            self._save_scan_history(scan_code, params.filters, candidates)
        finally:
            self.scan_btn.setEnabled(True)
            self._busy = False

    def _save_scan_history(self, scan_code: str, filters, candidates: list[CandidateStock]) -> None:
        """每次市場掃描成功後都存一筆永久紀錄——命名時機仿照
        main_window.py::_on_save_layout() 的既有寫法，掃描完立刻跳
        QInputDialog 讓使用者確認/修改名稱，比「先存預設名稱、之後才能
        改」更符合這個 app 既有的命名互動習慣。使用者按取消就不存(不用
        每次掃描都強迫存檔)。"""
        scan_type = scan_type_by_code(scan_code)
        default_name = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {scan_type.name_zh if scan_type else scan_code}"
        name, ok = QInputDialog.getText(self, "為這次掃描命名", "名稱", text=default_name)
        name = name.strip() if ok else ""
        if not name:
            return
        scan_history_store.save_run(name, scan_code, filters, candidates)
        self._refresh_history_table()

    # -------------------------------------------------------------- 復篩
    async def _on_run_screen(self):
        # 一定要透過 spawn() 呼叫，理由跟 _on_run_scanner() 開頭的說明一
        # 樣——這個迴圈要對每檔候選標的各打好幾次 IB API，執行時間更長。
        if self._busy:
            return
        symbols = self._checked_symbols()
        if not symbols:
            self.status_label.setText("候選清單裡沒有勾選任何標的")
            return

        self._busy = True
        self.screen_btn.setEnabled(False)
        self.result_table.setRowCount(0)
        filters = ScreenFilters(
            min_dte=self.min_dte_spin.value(),
            max_dte=self.max_dte_spin.value(),
            max_spread_pct=self.max_spread_spin.value(),
            min_iv=self.min_iv_spin.value() / 100.0 if self.min_iv_spin.value() > 0 else 0.0,
        )
        passed = 0
        try:
            for i, symbol in enumerate(symbols, start=1):
                self.status_label.setText(f"復篩中 {i}/{len(symbols)}：{symbol}")
                try:
                    result = await screen_one(self._ib, symbol, filters)
                except Exception as exc:  # noqa: BLE001
                    self._append_result_error_row(symbol, str(exc))
                    continue
                self._append_result_row(result)
                if result.passed:
                    passed += 1
            self.status_label.setText(f"復篩完成，{len(symbols)} 檔中有 {passed} 檔通過")
        finally:
            self.screen_btn.setEnabled(True)
            self._busy = False

    def _append_result_error_row(self, symbol: str, message: str):
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        self.result_table.setItem(row, 0, QTableWidgetItem(symbol))
        for col in range(1, len(RESULT_COLUMNS) - 1):
            self.result_table.setItem(row, col, QTableWidgetItem(""))
        status_text = f"查詢失敗：{message}"
        status_item = QTableWidgetItem(status_text)
        status_item.setToolTip(status_text)
        self.result_table.setItem(row, len(RESULT_COLUMNS) - 1, status_item)

    def _append_result_row(self, result):
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)

        def cell(col: int, value):
            text = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
            self.result_table.setItem(row, col, QTableWidgetItem(text))

        cell(0, result.symbol)
        cell(1, result.underlying_price)
        expiry_label = f"{result.expiry[:4]}-{result.expiry[4:6]}-{result.expiry[6:]}" if result.expiry else None
        cell(2, expiry_label)
        cell(3, result.dte)
        cell(4, result.strike)
        cell(5, result.call_bid)
        cell(6, result.call_ask)
        cell(7, result.call_spread_pct)
        cell(8, f"{result.call_iv:.1%}" if result.call_iv is not None else None)
        cell(9, result.put_bid)
        cell(10, result.put_ask)
        cell(11, result.put_spread_pct)
        cell(12, f"{result.put_iv:.1%}" if result.put_iv is not None else None)
        status = "通過" if result.passed else f"未通過：{result.reason}" if result.reason else "未通過"
        cell(13, status)
        # 「狀態」欄可能塞不下完整原因(表格欄位平均分寬)，加 tooltip 讓
        # 使用者滑鼠移過去看完整文字，不用特地拉寬 dock。
        self.result_table.item(row, 13).setToolTip(status)

        # 通過的列存一份 (symbol, expiry)，給雙擊帶去報價查詢用；沒通過
        # 的列(可能連 expiry 都沒查到)不給雙擊動作。
        symbol_item = self.result_table.item(row, 0)
        if result.passed and result.expiry:
            symbol_item.setData(Qt.UserRole, (result.symbol, result.expiry))

    def _on_result_double_clicked(self, row: int, _col: int):
        item = self.result_table.item(row, 0)
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if not data:
            return
        symbol, expiry = data
        self.symbol_selected.emit(symbol, expiry)

    # ------------------------------------------------------------ 掃描紀錄頁籤
    def _build_history_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("雙擊「名稱」欄重新命名，雙擊其他欄位檢視/編輯這筆紀錄的候選清單"))

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["名稱", "建立時間", "掃描代碼", "候選檔數"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history_table.cellDoubleClicked.connect(self._on_history_cell_double_clicked)
        layout.addWidget(self.history_table, 1)

        return tab

    def _on_tab_changed(self, index: int):
        if self.tabs.tabText(index) == "③ 掃描紀錄":
            self._refresh_history_table()

    def _refresh_history_table(self):
        runs = sorted(scan_history_store.load_all(), key=lambda r: r["created_at"], reverse=True)
        self.history_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            self.history_table.setItem(row, 0, QTableWidgetItem(run["name"]))
            self.history_table.setItem(row, 1, QTableWidgetItem(run["created_at"]))
            scan_type = scan_type_by_code(run["scan_code"])
            scan_label = scan_type.name_zh if scan_type else run["scan_code"]
            self.history_table.setItem(row, 2, QTableWidgetItem(scan_label))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(len(run.get("candidates", [])))))
            self.history_table.item(row, 0).setData(Qt.UserRole, run["id"])

    def _on_history_cell_double_clicked(self, row: int, col: int):
        item = self.history_table.item(row, 0)
        if item is None:
            return
        run_id = item.data(Qt.UserRole)
        if col == 0:
            self._on_rename_history_run(run_id, item.text())
        else:
            self._on_view_history_run(run_id)

    def _on_rename_history_run(self, run_id: str, current_name: str):
        name, ok = QInputDialog.getText(self, "重新命名", "名稱", text=current_name)
        name = name.strip() if ok else ""
        if name:
            scan_history_store.rename_run(run_id, name)
            self._refresh_history_table()

    def _on_view_history_run(self, run_id: str):
        run = scan_history_store.get_run(run_id)
        if run is None:
            return
        dialog = ScanRunDetailDialog(run, self)
        dialog.exec_()
        self._refresh_history_table()
