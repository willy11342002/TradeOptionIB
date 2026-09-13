"""
掃描紀錄檢視對話框——雙擊「③ 掃描紀錄」頁籤裡的一筆紀錄時開啟，唯讀顯示
當時的掃描代碼／篩選條件(用 scanner_catalog 轉回中文標籤)，候選股票清
單則可以勾選/新增/移除，按「儲存」才會真的寫回
app/services/scan_history_store.py，不自動存檔(跟這個 app 其他地方「按
鈕才存」的慣例一致)。

跟 screener_widget.py 自己的候選清單表格是分開的兩份實作(這裡的候選清
單是「某一次歷史紀錄」的內容，不是「目前這次操作」的候選清單)，故意不
共用同一個 QTableWidget 實例，但欄位/操作方式刻意做成一樣的，使用起來
才不會有兩套不同邏輯的違和感。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app.models.scanner_catalog import scan_type_by_code
from app.services import scan_history_store

_CANDIDATE_COLUMNS = ["納入", "代碼", "來源", "排名"]


class ScanRunDetailDialog(QDialog):
    def __init__(self, run: dict, parent=None):
        super().__init__(parent)
        self._run = run
        self.setWindowTitle(f"掃描紀錄：{run['name']}")
        self.resize(480, 480)

        layout = QVBoxLayout(self)

        scan_type = scan_type_by_code(run["scan_code"])
        scan_label = scan_type.name_zh if scan_type else run["scan_code"]
        layout.addWidget(QLabel(f"掃描代碼：{scan_label}"))
        layout.addWidget(QLabel(f"建立時間：{run['created_at']}"))

        filters = run.get("filters", [])
        if filters:
            layout.addWidget(QLabel("篩選條件："))
            for f in filters:
                layout.addWidget(QLabel(f"　{self._describe_filter(f['code'])}：{f['value']:g}"))

        layout.addWidget(QLabel("候選標的清單（可勾選/新增/移除，按「儲存」才會寫回紀錄）"))

        self.candidate_table = QTableWidget(0, len(_CANDIDATE_COLUMNS))
        self.candidate_table.setHorizontalHeaderLabels(_CANDIDATE_COLUMNS)
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.candidate_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.candidate_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.candidate_table, 1)
        for cand in run.get("candidates", []):
            self._append_candidate_row(cand)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("全選")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("全不選")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        remove_btn = QPushButton("移除選取列")
        remove_btn.clicked.connect(self._on_remove_selected)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(select_none_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        manual_row = QHBoxLayout()
        self.manual_symbol_edit = QLineEdit()
        self.manual_symbol_edit.setPlaceholderText("手動加入代碼，可用空白或逗號分隔多檔")
        self.manual_symbol_edit.returnPressed.connect(self._on_add_manual_symbols)
        add_btn = QPushButton("新增")
        add_btn.clicked.connect(self._on_add_manual_symbols)
        manual_row.addWidget(self.manual_symbol_edit)
        manual_row.addWidget(add_btn)
        layout.addLayout(manual_row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        save_btn = QPushButton("儲存")
        save_btn.clicked.connect(self._on_save)
        self.buttons.addButton(save_btn, QDialogButtonBox.ActionRole)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def _describe_filter(code: str) -> str:
        for filter_def in _all_filter_defs_cached():
            for field in filter_def.fields:
                if field.code == code:
                    return f"{filter_def.label_zh}（{field.name_en}）"
        return code

    def _append_candidate_row(self, cand: dict) -> None:
        row = self.candidate_table.rowCount()
        self.candidate_table.insertRow(row)
        check_item = QTableWidgetItem()
        check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check_item.setCheckState(Qt.Checked)
        self.candidate_table.setItem(row, 0, check_item)
        self.candidate_table.setItem(row, 1, QTableWidgetItem(cand["symbol"]))
        source_label = "掃描" if cand.get("source") == "scanner" else "手動"
        self.candidate_table.setItem(row, 2, QTableWidgetItem(source_label))
        rank = cand.get("rank")
        self.candidate_table.setItem(row, 3, QTableWidgetItem(str(rank) if rank is not None else ""))

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.candidate_table.rowCount()):
            self.candidate_table.item(row, 0).setCheckState(state)

    def _on_remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.candidate_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.candidate_table.removeRow(row)

    def _on_add_manual_symbols(self) -> None:
        text = self.manual_symbol_edit.text().strip().upper()
        if not text:
            return
        existing = {
            self.candidate_table.item(row, 1).text()
            for row in range(self.candidate_table.rowCount())
        }
        for symbol in text.replace(",", " ").split():
            if symbol and symbol not in existing:
                self._append_candidate_row({"symbol": symbol, "source": "manual", "rank": None})
                existing.add(symbol)
        self.manual_symbol_edit.clear()

    def _on_save(self) -> None:
        candidates = []
        for row in range(self.candidate_table.rowCount()):
            item = self.candidate_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            symbol = self.candidate_table.item(row, 1).text()
            source = "scanner" if self.candidate_table.item(row, 2).text() == "掃描" else "manual"
            rank_text = self.candidate_table.item(row, 3).text()
            candidates.append({
                "symbol": symbol, "source": source,
                "rank": int(rank_text) if rank_text else None,
            })
        scan_history_store.update_run_candidates(self._run["id"], candidates)
        self.accept()


def _all_filter_defs_cached():
    from app.models.scanner_catalog import load_filter_catalog
    return load_filter_catalog()
