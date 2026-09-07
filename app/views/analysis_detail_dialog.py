from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QDoubleSpinBox, QMessageBox, QWidget,
)

from app.services import theme
from app.services.opening_analysis import update_history_record
from app.views.markdown_text_edit import MarkdownTextEdit, apply_markdown_style
from app.views.opening_format import format_summary
from app.views.raw_data_dialog import RawDataDialog


class LevelListEditor(QWidget):
    """一排可以新增/移除的價位輸入框，給壓力/支撐價位手動調整用。"""

    def __init__(self, levels: list[float]):
        super().__init__()
        self._rows: list[tuple[QWidget, QDoubleSpinBox]] = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self._add_button = QPushButton("+ 新增價位")
        self._add_button.clicked.connect(lambda: self._add_row(0.0))

        for level in levels:
            self._add_row(level)
        self._layout.addWidget(self._add_button)

    def _add_row(self, value: float):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        spin = QDoubleSpinBox()
        spin.setRange(0, 999999)
        spin.setDecimals(0)
        spin.setSingleStep(50)
        spin.setValue(value)
        row_layout.addWidget(spin)

        remove_button = QPushButton("移除")
        remove_button.setFixedWidth(50)
        row_layout.addWidget(remove_button)

        insert_at = self._layout.indexOf(self._add_button)
        if insert_at < 0:
            insert_at = self._layout.count()
        self._layout.insertWidget(insert_at, row)
        self._rows.append((row, spin))
        remove_button.clicked.connect(lambda: self._remove_row(row))

    def _remove_row(self, row: QWidget):
        self._rows = [(r, s) for r, s in self._rows if r is not row]
        row.setParent(None)
        row.deleteLater()

    def values(self) -> list[float]:
        return [spin.value() for _, spin in self._rows]


class AnalysisDetailDialog(QDialog):
    """雙擊歷史紀錄跳出的獨立視窗：完整 Markdown 理由 + 可手動調整的
    壓力/支撐價位，改完按儲存會寫回歷史檔。"""

    record_saved = pyqtSignal(dict)

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self._record = record
        self.setWindowTitle(f"分析詳細 - {record.get('timestamp', '')}")
        self.resize(750, 650)

        layout = QVBoxLayout(self)

        self.header_label = QLabel(format_summary(record))
        layout.addWidget(self.header_label)

        self.text_edit = MarkdownTextEdit()
        apply_markdown_style(self.text_edit, dark=theme.load_theme() == "dark")
        self.text_edit.setMarkdown(record.get("reasoning", ""))
        layout.addWidget(self.text_edit)

        levels_row = QHBoxLayout()
        resistance_box = QGroupBox("壓力價位")
        resistance_layout = QVBoxLayout(resistance_box)
        self.resistance_editor = LevelListEditor(record.get("resistance_levels") or [])
        resistance_layout.addWidget(self.resistance_editor)
        levels_row.addWidget(resistance_box)

        support_box = QGroupBox("支撐價位")
        support_layout = QVBoxLayout(support_box)
        self.support_editor = LevelListEditor(record.get("support_levels") or [])
        support_layout.addWidget(self.support_editor)
        levels_row.addWidget(support_box)
        layout.addLayout(levels_row)

        button_row = QHBoxLayout()
        save_button = QPushButton("儲存修改")
        save_button.clicked.connect(self._on_save)
        button_row.addWidget(save_button)
        view_raw_button = QPushButton("查看原始資料")
        view_raw_button.clicked.connect(self._on_view_raw)
        button_row.addWidget(view_raw_button)
        button_row.addStretch()
        layout.addLayout(button_row)

    def _on_save(self):
        updates = {
            "reasoning": self.text_edit.toMarkdown(),
            "resistance_levels": self.resistance_editor.values(),
            "support_levels": self.support_editor.values(),
        }
        update_history_record(self._record["timestamp"], updates)
        self._record.update(updates)
        self.header_label.setText(format_summary(self._record))
        self.record_saved.emit(self._record)
        QMessageBox.information(self, "已儲存", "修改已儲存")

    def _on_view_raw(self):
        RawDataDialog(self._record, parent=self).exec_()
