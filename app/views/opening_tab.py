from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTextEdit, QListWidget, QListWidgetItem, QMessageBox, QGroupBox, QSplitter,
)

from app.services import theme
from app.services.opening_analysis import (
    OpeningAnalysisService, load_history, delete_history_entry, update_history_reasoning,
)
from app.views.raw_data_dialog import RawDataDialog

AMP_LABEL = {"high": "高", "low": "低"}
VOL_LABEL = {"high": "高", "low": "低"}
NO_RECORD_TEXT = "尚無分析結果"

DEFAULT_FONT_POINT_SIZE = 13

# setMarkdown() 轉出來的內建樣式沒什麼顏色層次，這裡疊一層 QTextDocument
# 的 default stylesheet 讓標題/粗體/程式碼有顏色，深色模式跟淺色模式各一套。
LIGHT_MARKDOWN_CSS = """
h1, h2, h3 { color: #1a5fb4; }
strong { color: #c01c28; }
code { background-color: #eeeeee; padding: 1px 4px; border-radius: 3px; }
"""
DARK_MARKDOWN_CSS = """
h1, h2, h3 { color: #62a0ea; }
strong { color: #ff7b63; }
code { background-color: #3c3f41; padding: 1px 4px; border-radius: 3px; }
"""


class MarkdownTextEdit(QTextEdit):
    """會把內容當 Markdown 渲染的 QTextEdit，並支援 Ctrl+滾輪縮放字體
    (跟瀏覽器/編輯器一樣的慣例，滑鼠單純滾動還是正常捲動內容，不會跟縮放
    衝突)。"""

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoomIn(1)
            elif event.angleDelta().y() < 0:
                self.zoomOut(1)
            event.accept()
        else:
            super().wheelEvent(event)


def _format_history_item(record: dict) -> str:
    return f"{record.get('timestamp', '')}　{record.get('strategy', '')}"


def _format_levels(levels: list) -> str:
    return "、".join(str(level) for level in levels) if levels else "—"


def _format_header(record: dict) -> str:
    amp = AMP_LABEL.get(record.get("amplitude"), record.get("amplitude"))
    vol = VOL_LABEL.get(record.get("volatility"), record.get("volatility"))
    resistance = _format_levels(record.get("resistance_levels") or [])
    support = _format_levels(record.get("support_levels") or [])
    return (
        f"時間：{record.get('timestamp', '')}　"
        f"振幅：{amp}　波動率：{vol}　建議策略：{record.get('strategy', '')}\n"
        f"壓力：{resistance}　支撐：{support}"
    )


class OpeningTab(QWidget):
    def __init__(self):
        super().__init__()
        self._current_record = None

        self.service = OpeningAnalysisService()
        self.service.progress.connect(self._on_progress)
        self.service.analysis_ready.connect(self._on_analysis_ready)
        self.service.analysis_failed.connect(self._on_analysis_failed)

        self._build_ui()
        self._load_history()

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
        splitter.addWidget(self._build_result_box())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([350, 700])
        root.addWidget(splitter, 1)

    def _build_result_box(self) -> QGroupBox:
        result_box = QGroupBox("本次分析結果")
        result_layout = QVBoxLayout(result_box)
        self.result_header_label = QLabel(NO_RECORD_TEXT)
        result_layout.addWidget(self.result_header_label)
        self.result_text = MarkdownTextEdit()
        font = self.result_text.font()
        font.setPointSize(DEFAULT_FONT_POINT_SIZE)
        self.result_text.setFont(font)
        css = DARK_MARKDOWN_CSS if theme.load_theme() == "dark" else LIGHT_MARKDOWN_CSS
        self.result_text.document().setDefaultStyleSheet(css)
        result_layout.addWidget(self.result_text)
        save_row = QHBoxLayout()
        self.save_button = QPushButton("儲存修改")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)
        save_row.addWidget(self.save_button)
        self.view_raw_data_button = QPushButton("查看原始資料")
        self.view_raw_data_button.setEnabled(False)
        self.view_raw_data_button.clicked.connect(self._on_view_raw_data_clicked)
        save_row.addWidget(self.view_raw_data_button)
        save_row.addStretch()
        result_layout.addLayout(save_row)
        return result_box

    def _build_history_box(self) -> QGroupBox:
        history_box = QGroupBox("歷史紀錄（點選查看詳細）")
        history_layout = QVBoxLayout(history_box)
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
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
        item = QListWidgetItem(_format_history_item(record))
        item.setData(Qt.UserRole, record)
        if at_top:
            self.history_list.insertItem(0, item)
        else:
            self.history_list.addItem(item)

    def _show_record(self, record: dict):
        self._current_record = record
        self.result_header_label.setText(_format_header(record))
        self.result_text.setMarkdown(record.get("reasoning", ""))
        self.save_button.setEnabled(True)
        self.view_raw_data_button.setEnabled(True)

    def _clear_record(self):
        self._current_record = None
        self.result_header_label.setText(NO_RECORD_TEXT)
        self.result_text.clear()
        self.save_button.setEnabled(False)
        self.view_raw_data_button.setEnabled(False)

    def _on_analyze_clicked(self):
        self.analyze_button.setEnabled(False)
        self._clear_record()
        self.status_label.setText("分析中…")
        self.service.run_async()

    def _on_progress(self, message: str):
        self.status_label.setText(message)

    def _on_analysis_ready(self, record: dict):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("分析完成")
        self._show_record(record)
        self._add_history_item(record, at_top=True)

    def _on_analysis_failed(self, message: str):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("分析失敗")
        QMessageBox.warning(self, "分析失敗", message)

    def _on_history_item_clicked(self, item: QListWidgetItem):
        self._show_record(item.data(Qt.UserRole))

    def _on_history_selection_changed(self):
        self.delete_button.setEnabled(bool(self.history_list.selectedItems()))

    def _on_save_clicked(self):
        if not self._current_record:
            return
        new_reasoning = self.result_text.toMarkdown()
        self._current_record["reasoning"] = new_reasoning
        update_history_reasoning(self._current_record["timestamp"], new_reasoning)
        QMessageBox.information(self, "已儲存", "修改已儲存")

    def _on_view_raw_data_clicked(self):
        if not self._current_record:
            return
        RawDataDialog(self._current_record, parent=self).exec_()

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
            self._clear_record()
