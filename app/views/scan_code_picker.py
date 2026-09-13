"""
掃描代碼(scanCode)輸入元件——IB 有 208 種 STK 掃描代碼可選，不做成傳統
下拉選單(選項太多滑鼠滾半天)，改成文字輸入框：打字當下先用本地字串比
對(difflib)立刻給幾個候選，打完停頓一下(debounce)再讓 AI 從完整 208 種
裡挑出語意上最接近的幾個，取代/補齊本地比對的結果。

*** AI 呼叫一定要 debounce，不能每個按鍵都打一次 ***：OpenRouter 每次
呼叫都有實際延遲(通常一秒以上)跟費用，使用者打字的當下如果每個字元變動
都送一次請求，畫面會塞爆一堆來不及取消的呼叫、費用也不必要地暴增。改
成打完停頓 500ms 才觸發，這段等待期間先顯示本地比對結果，讓輸入框不會
「打字時沒反應」。

*** AI 失敗/沒設定 API key 時要安靜降級，不能彈窗 ***：使用者可能根本
沒設定 OPENROUTER_API_KEY(這是選配功能，本地比對本來就夠用)，這種情況
每次打完字停頓都會觸發一次失敗，不能每次都跳出錯誤視窗——只在下拉清單
上方留一行淡灰提示文字，且只顯示一次(不重複洗畫面)。
"""
from __future__ import annotations

import difflib
import logging

from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget,
)

from app.models.openrouter_client import suggest_scan_codes
from app.models.scanner_catalog import ScanTypeDef
from app.services.background_tasks import spawn

_DEBOUNCE_MS = 500
_MAX_SUGGESTIONS = 15

# *** 追蹤用的細粒度 log，先不要拔掉 ***：使用者實測回報這個元件在「改
# 文字的瞬間」讓整個 app 被 Windows「已停止回應」偵測強制關閉——不是
# Python 例外/asyncio 例外/Qt 訊息/faulthandler 能攔到的那種(那些都試過
# 了，全部沒有任何輸出)，代表主執行緒的訊息迴圈整個被卡住，不是乾淨地
# crash。faulthandler 攔不到「卡住不動」，只能靠這種「每個步驟都留一行
# log」的土法煉鋼，下次卡住時 logs/app.log 最後一行印出來的位置，就是卡
# 住的地方。等定位到問題後再拔掉，不要一直留著洗版面。
_trace_logger = logging.getLogger("scan_code_picker.trace")


class ScanCodePicker(QWidget):
    code_selected = pyqtSignal(str)

    def __init__(self, catalog: list[ScanTypeDef], parent=None):
        super().__init__(parent)
        self._catalog = catalog
        self._by_code = {c.code: c for c in catalog}
        self._selected_code: str | None = None
        self._ai_busy = False
        self._ai_warned = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("輸入關鍵字搜尋掃描代碼，例如「高波動率」...")
        self.edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.edit)

        self.ai_status_label = QLabel("")
        self.ai_status_label.setStyleSheet("color: palette(mid);")
        self.ai_status_label.hide()
        layout.addWidget(self.ai_status_label)

        self.dropdown = QListWidget()
        self.dropdown.itemClicked.connect(self._on_item_clicked)
        # stretch=1：讓下拉清單吃掉這個元件拿到的所有剩餘高度，不要卡死
        # 一個固定的 setMaximumHeight——以前這裡是內嵌在主畫面小小一格的
        # 篩選條件框裡，160px 上限剛好；現在同一顆元件也被包進
        # ScanCodePickerDialog(彈窗給了整個對話框的高度)，還卡在 160px
        # 會留一大塊空白。實際佔多高由外層(ScanCodePickerDialog 給
        # stretch=1／FilterAssistantDialog 沒給 stretch)決定。
        layout.addWidget(self.dropdown, 1)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(_DEBOUNCE_MS)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)

        self._show_suggestions(catalog[:_MAX_SUGGESTIONS])

    def selected_code(self) -> str | None:
        return self._selected_code

    def set_code(self, code: str) -> None:
        """給歷史紀錄檢視/AI 條件助手套用時預填用。"""
        scan_type = self._by_code.get(code)
        if scan_type is None:
            return
        self._selected_code = code
        self.edit.blockSignals(True)
        self.edit.setText(scan_type.name_zh or scan_type.name_en)
        self.edit.blockSignals(False)

    # ---------------------------------------------------------------- 輸入
    def _on_text_changed(self, text: str) -> None:
        _trace_logger.info("_on_text_changed 開始: text=%r", text)
        self._selected_code = None
        text = text.strip()
        if not text:
            _trace_logger.info("_on_text_changed: 空字串，show_suggestions(全部) 前")
            self._show_suggestions(self._catalog[:_MAX_SUGGESTIONS])
        else:
            _trace_logger.info("_on_text_changed: local_match 前")
            matched = self._local_match(text)
            _trace_logger.info("_on_text_changed: local_match 後(%d 筆)，show_suggestions 前", len(matched))
            self._show_suggestions(matched)
        _trace_logger.info("_on_text_changed: show_suggestions 後，debounce_timer.start() 前")
        self._debounce_timer.start()
        _trace_logger.info("_on_text_changed 結束")

    def _local_match(self, text: str) -> list[ScanTypeDef]:
        lowered = text.lower()
        substr = [
            c for c in self._catalog
            if lowered in c.code.lower() or lowered in (c.name_zh or "").lower()
            or lowered in c.name_en.lower()
        ]
        _trace_logger.info("_local_match: substr 比對完(%d 筆)，difflib 前", len(substr))
        names = [c.name_zh or c.name_en for c in self._catalog]
        close = difflib.get_close_matches(text, names, n=_MAX_SUGGESTIONS, cutoff=0.3)
        _trace_logger.info("_local_match: difflib 完(%d 筆)", len(close))
        by_name = {(c.name_zh or c.name_en): c for c in self._catalog}
        fuzzy = [by_name[n] for n in close if n in by_name]

        merged: list[ScanTypeDef] = []
        seen = set()
        for c in substr + fuzzy:
            if c.code not in seen:
                seen.add(c.code)
                merged.append(c)
        return merged[:_MAX_SUGGESTIONS]

    def _show_suggestions(self, items: list[ScanTypeDef]) -> None:
        _trace_logger.info("_show_suggestions 開始(%d 筆)，dropdown.clear() 前", len(items))
        self.dropdown.clear()
        _trace_logger.info("_show_suggestions: dropdown.clear() 後，開始逐筆 addItem")
        for i, c in enumerate(items):
            label = c.name_zh or c.name_en
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.UserRole, c.code)
            tooltip = f"{c.code}"
            if c.tooltip_zh:
                tooltip += f"\n{c.tooltip_zh}"
            list_item.setToolTip(tooltip)
            self.dropdown.addItem(list_item)
        _trace_logger.info("_show_suggestions 結束")

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        code = item.data(Qt.UserRole)
        self._selected_code = code
        self.edit.blockSignals(True)
        self.edit.setText(item.text())
        self.edit.blockSignals(False)
        self.code_selected.emit(code)

    # -------------------------------------------------------------- AI 建議
    def _on_debounce_timeout(self) -> None:
        # *** 不能用 @asyncSlot() 直接接 timeout ***：qasync 的
        # @asyncSlot() 建立的 Task 只有函式內的區域變數在撐著，slot 一
        # 返回就沒人持有，事件迴圈對執行中的 Task 只有弱參照——實測抓到
        # 真實案例：這裡的 AI 呼叫明明成功拿到回應，Task 物件卻在那之後
        # 被 GC 回收掉，整個呼叫沒有正常結束(logs/app.log 記錄到「Task
        # was destroyed but it is pending!」)。改用 spawn()，用一個模組
        # 級集合保留強參照到真正完成為止，見
        # app/services/background_tasks.py 的完整說明。
        _trace_logger.info("_on_debounce_timeout 觸發，spawn(_fire_ai_lookup) 前")
        spawn(self._fire_ai_lookup())
        _trace_logger.info("_on_debounce_timeout: spawn() 後(已排程，不代表已執行完)")

    async def _fire_ai_lookup(self) -> None:
        _trace_logger.info("_fire_ai_lookup 開始")
        text = self.edit.text().strip()
        if not text or self._ai_busy:
            _trace_logger.info("_fire_ai_lookup: 提早返回(text 空或 ai_busy 中)")
            return
        self._ai_busy = True
        try:
            _trace_logger.info("_fire_ai_lookup: await suggest_scan_codes 前")
            codes = await suggest_scan_codes(text, self._catalog)
            _trace_logger.info("_fire_ai_lookup: await suggest_scan_codes 後(%d 筆)", len(codes))
        except Exception:  # noqa: BLE001
            _trace_logger.info("_fire_ai_lookup: suggest_scan_codes 拋例外", exc_info=True)
            # 安靜降級：維持本地比對結果，只提示一次，不彈窗、不重複洗畫面。
            if not self._ai_warned:
                self._ai_warned = True
                self.ai_status_label.setText("AI 建議未啟用(可能未設定 OPENROUTER_API_KEY)，僅顯示本地比對結果")
                self.ai_status_label.show()
            return
        finally:
            self._ai_busy = False

        if codes:
            self.ai_status_label.hide()
            _trace_logger.info("_fire_ai_lookup: show_suggestions(AI結果) 前")
            self._show_suggestions([self._by_code[c] for c in codes if c in self._by_code])
        _trace_logger.info("_fire_ai_lookup 結束")


class ScanCodePickerDialog(QDialog):
    """掃描代碼選擇對話框——把 ScanCodePicker 包進一個彈出視窗，取代主畫
    面上長駐的輸入框+下拉清單。主畫面只留一個顯示目前選擇的按鈕，按下去
    才開這個對話框搜尋/用 AI 選，跟 scanner_filter_picker.py::
    AddFilterDialog 的「按鈕→彈窗選」是同一套互動模式，選完就收起來，不
    會一直佔著主畫面的版面。"""

    def __init__(self, catalog: list[ScanTypeDef], current_code: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("選擇掃描代碼")
        self.resize(480, 480)

        layout = QVBoxLayout(self)
        self.picker = ScanCodePicker(catalog)
        if current_code:
            self.picker.set_code(current_code)
        layout.addWidget(self.picker, 1)
        # 雙擊下拉清單裡的項目直接選定並關閉對話框，跟 AddFilterDialog
        # 的雙擊行為一致，不用每次都多點一次「確定」。
        self.picker.dropdown.itemDoubleClicked.connect(lambda _item: self.accept())

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def selected_code(self) -> str | None:
        return self.picker.selected_code()
