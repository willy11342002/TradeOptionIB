from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QGroupBox, QSplitter,
)

from app.services.opening_analysis import ChartDataService, OpeningAnalysisService, delete_history_entry, load_history
from app.views.analysis_detail_dialog import AnalysisDetailDialog
from app.views.candlestick_chart import PriceChartWidget
from app.views.opening_format import format_history_item, format_summary

NO_SELECTION_TEXT = "尚未選擇分析"
CHART_DAYS = 60


class OpeningTab(QWidget):
    def __init__(self):
        super().__init__()
        self._current_record = None

        self.service = OpeningAnalysisService()
        self.service.progress.connect(self._on_progress)
        self.service.analysis_ready.connect(self._on_analysis_ready)
        self.service.analysis_failed.connect(self._on_analysis_failed)

        self.chart_service = ChartDataService()
        self.chart_service.chart_ready.connect(self._on_chart_ready)
        self.chart_service.chart_failed.connect(self._on_chart_failed)
        self._chart_days = CHART_DAYS

        self._build_ui()
        self.chart.request_more_history.connect(self._on_request_more_history)
        self._load_history()
        self.chart_service.run_async(days=self._chart_days)

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

    def _build_chart_box(self) -> QGroupBox:
        box = QGroupBox("加權指數 K 線 + 台指期")
        layout = QVBoxLayout(box)
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

    def _on_chart_ready(self, taiex_history: list, futures_history: list):
        self.chart.set_price_data(taiex_history, futures_history, self._chart_days)
        if self._current_record:
            self.chart.set_levels(
                self._current_record.get("resistance_levels") or [],
                self._current_record.get("support_levels") or [],
            )

    def _on_chart_failed(self, message: str):
        QMessageBox.warning(self, "圖表載入失敗", message)

    def _on_request_more_history(self, days: int):
        if days <= self._chart_days:
            return
        self._chart_days = days
        self.chart_service.run_async(days=days)

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
