"""
自然語言篩選助手——使用者用一句話描述想找的股票特徵，AI
(app/models/openrouter_client.py::suggest_filters())從實際存在的掃描代
碼/篩選條件清單裡挑出建議，在這個對話框裡先預覽、可自行調整/刪除，按
「套用」才會真的推進 ScreenerWidget 的正式表單——不會不經確認就直接覆
蓋使用者當下已經設好的條件。
"""
from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from app.models.openrouter_client import suggest_filters
from app.models.scanner_catalog import load_filter_catalog, load_scan_type_catalog
from app.services.background_tasks import spawn
from app.views.scan_code_picker import ScanCodePicker
from app.views.scanner_filter_picker import FilterRowWidget


class FilterAssistantDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 條件建議")
        self.resize(520, 560)
        self._busy = False
        self._scan_type_catalog = load_scan_type_catalog()
        self._filter_catalog = load_filter_catalog()
        self._preview_rows: dict[str, FilterRowWidget] = {}

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("用一句話描述你想篩選的股票特徵："))
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("例如：高波動率、股價10到100美元之間、市值大於10億美元")
        self.query_edit.returnPressed.connect(lambda: spawn(self._on_ask_ai()))
        layout.addWidget(self.query_edit)

        self.ask_btn = QPushButton("AI 建議條件")
        self.ask_btn.clicked.connect(lambda: spawn(self._on_ask_ai()))
        layout.addWidget(self.ask_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("預覽（套用前可自行調整或刪除）"))
        self.scan_code_picker = ScanCodePicker(self._scan_type_catalog)
        # *** 這裡要限制高度，不能讓它自由吃滿版面 ***：ScanCodePicker
        # 內部的下拉清單本身沒有上限(交給外層決定，見
        # scan_code_picker.py 的說明)，在 ScanCodePickerDialog 那種整個
        # 對話框都是它的情境下應該吃滿；但這裡下面還有篩選條件預覽/
        # rationale 要顯示空間，不能被它擠掉，鎖一個跟舊版視覺效果一致
        # 的高度。
        self.scan_code_picker.setMaximumHeight(160)
        layout.addWidget(self.scan_code_picker)

        self.preview_rows_layout = QVBoxLayout()
        layout.addLayout(self.preview_rows_layout, 1)

        self.rationale_label = QLabel("")
        self.rationale_label.setWordWrap(True)
        self.rationale_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.rationale_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("套用")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        layout.addWidget(self.buttons)

    async def _on_ask_ai(self) -> None:
        # *** 一定要透過 spawn() 呼叫，不能用 @asyncSlot() 直接接訊號
        # ***：qasync 的 @asyncSlot() 建立的 Task 只有函式內的區域變數在
        # 撐著，slot 一返回就沒人持有——已經在 scan_code_picker.py 的
        # AI 呼叫實測抓到真實案例(logs/app.log 記錄到「Task was
        # destroyed but it is pending!」，明明 AI 已經成功回應)，見
        # app/services/background_tasks.py 的完整說明。
        text = self.query_edit.text().strip()
        if not text or self._busy:
            return
        self._busy = True
        self.ask_btn.setEnabled(False)
        self.status_label.setText("AI 分析中...")
        try:
            proposal = await suggest_filters(text, self._filter_catalog, self._scan_type_catalog)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"AI 建議失敗：{exc}")
            return
        finally:
            self.ask_btn.setEnabled(True)
            self._busy = False

        self._clear_preview_rows()
        if proposal.scan_code:
            self.scan_code_picker.set_code(proposal.scan_code)
        for proposed in proposal.filters:
            filter_def = next((f for f in self._filter_catalog if f.id == proposed.filter_id), None)
            if filter_def is None:
                continue
            self._add_preview_row(filter_def, proposed.above, proposed.below)

        self.rationale_label.setText(proposal.rationale)
        self.status_label.setText(f"AI 建議了 {len(self._preview_rows)} 個篩選條件，可自行調整後再按「套用」")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(self._preview_rows) or bool(proposal.scan_code))

    def _add_preview_row(self, filter_def, above: float | None, below: float | None) -> None:
        row = FilterRowWidget(filter_def)
        row.set_values(above, below)
        row.removed.connect(self._on_preview_row_removed)
        self.preview_rows_layout.addWidget(row)
        self._preview_rows[filter_def.id] = row

    def _on_preview_row_removed(self, filter_id: str) -> None:
        row = self._preview_rows.pop(filter_id, None)
        if row is not None:
            self.preview_rows_layout.removeWidget(row)
            row.deleteLater()

    def _clear_preview_rows(self) -> None:
        for row in list(self._preview_rows.values()):
            self.preview_rows_layout.removeWidget(row)
            row.deleteLater()
        self._preview_rows.clear()

    def result_scan_code(self) -> str | None:
        return self.scan_code_picker.selected_code()

    def result_rows(self) -> list[FilterRowWidget]:
        return list(self._preview_rows.values())
