"""
自動平倉/停利停損的設定對話框。三個：單一價差的停利(TakeProfitDialog)、
單一價差的停損(StopLossDialog，規則3/4/5三選一)、群組整組停利
(GroupTakeProfitDialog，規則1)。

規則細節(門檻用點數、履約價填法、口數怎麼算)是使用者拍板定案的，見
C:\\Users\\tingw\\.claude\\plans\\lazy-riding-hearth.md 的規則總表。

存檔一律把 status 設回 STATUS_PAUSED(不管原本是不是武裝中)——改過參數的
規則要求使用者自己重新按「啟用」確認過一次才會恢復監控，不要讓改動後的
規則悄悄繼續用舊的武裝狀態送單。"""
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QDoubleSpinBox, QLabel,
    QCheckBox, QRadioButton, QButtonGroup, QStackedWidget, QWidget,
    QPushButton, QDialogButtonBox,
)

from app.models.auto_close import (
    ReopenSpec, StopLossRule, TakeProfitRule,
    SL_MODE_ADD_LEG, SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE, STATUS_PAUSED,
)
from app.models.positions import Position

_MODE_TO_INDEX = {SL_MODE_REOPEN_DOUBLE: 0, SL_MODE_NEW_GROUP: 1, SL_MODE_ADD_LEG: 2}


def _strike_spin(value: Optional[float] = None) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0, 999999)
    spin.setDecimals(0)
    spin.setSingleStep(50)
    if value is not None:
        spin.setValue(value)
    return spin


def _price_spin(value: Optional[float] = None) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0.1, 99999)
    spin.setDecimals(1)
    spin.setSingleStep(0.5)
    spin.setValue(value if value is not None else 1.0)
    return spin


def _button_row(clear_btn: QPushButton, buttons: QDialogButtonBox) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(clear_btn)
    row.addStretch(1)
    row.addWidget(buttons)
    return row


def _rich_tooltip(text: str) -> str:
    """純文字的 tooltip 在 Qt 裡不會自動換行，一長串字會變成一整條很難看
    的橫向長條——包成簡單的 HTML(Qt 偵測到有 tag 就會走 QTextDocument
    排版)才會自動換行、限制寬度。系統預設的 tooltip 字體很小，這裡順便
    把字放大到看得清楚的大小。"""
    return f"<div style='max-width:320px; font-size:13pt;'>{text}</div>"


def _add_row(form: QFormLayout, label_text: str, widget: QWidget, tooltip: str) -> None:
    """加一列表單，說明文字用滑鼠移過去(tooltip)才顯示，不要整段字硬印
    在畫面上佔空間——label跟輸入框都掛，滑到哪裡都看得到。"""
    html = _rich_tooltip(tooltip)
    label = QLabel(label_text)
    label.setToolTip(html)
    widget.setToolTip(html)
    form.addRow(label, widget)


_TP_THRESHOLD_TOOLTIP = (
    "獲利達此點數時觸發。比較的是「現價跟均價」的點數差本身，不會乘口數——這個"
    "價差不管幾口，點數差都一樣，達標判斷不會因為口數多寡而改變(口數只影響畫"
    "面上損益欄位的金額)。"
)
_SL_THRESHOLD_TOOLTIP = (
    "虧損達此點數時觸發。比較的是「現價跟均價」的點數差本身，不會乘口數——這個"
    "價差不管幾口，點數差都一樣，達標判斷不會因為口數多寡而改變(口數只影響畫"
    "面上損益欄位的金額)。"
)
_REOPEN_STRIKE_TOOLTIP = (
    "重開倉的履約價，只需要填一個，另一腳的履約價會依照原本價差的寬度自動往"
    "價外推算(跟下單面板的價差單分頁同一套規則)。"
)
_REOPEN_PRICE_TOOLTIP = "重開倉這組價差的委託淨價(限價)，會用連續IOC監看送出，沒成交會持續嘗試。"


class TakeProfitDialog(QDialog):
    """規則2(單邊停利)：門檻點數 + 要不要重開，勾了才展開新履約價/委託
    價；不勾就是「只平倉不重開」。"""

    def __init__(self, position: Position, existing: Optional[TakeProfitRule], parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定停利")

        self._cleared = False
        has_reopen = existing is not None and existing.reopen is not None

        form = QFormLayout()
        self.threshold_spin = _price_spin(existing.threshold_points if existing else 10.0)
        _add_row(form, "門檻點數", self.threshold_spin, _TP_THRESHOLD_TOOLTIP)

        self.reopen_checkbox = QCheckBox("平倉後原地重開同類型價差(口數不變)")
        self.reopen_checkbox.setChecked(has_reopen)
        form.addRow("", self.reopen_checkbox)

        self.strike_spin = _strike_spin(existing.reopen.strike if has_reopen else None)
        self.price_spin = _price_spin(existing.reopen.price if has_reopen else None)
        _add_row(form, "新履約價", self.strike_spin, _REOPEN_STRIKE_TOOLTIP)
        _add_row(form, "新委託價", self.price_spin, _REOPEN_PRICE_TOOLTIP)

        # 沒勾「平倉後原地重開」代表這個規則只平倉不重開，履約價/委託價
        # 這兩欄位當下沒有意義，鎖起來(不能點)避免使用者誤以為填了就會
        # 生效——要重開就先勾這個框，欄位才會打開讓你填。
        self.reopen_checkbox.toggled.connect(self._update_reopen_enabled)
        self._update_reopen_enabled(has_reopen)

        clear_btn = QPushButton("清除設定")
        clear_btn.clicked.connect(self._on_clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(_button_row(clear_btn, buttons))

    def _update_reopen_enabled(self, checked: bool) -> None:
        self.strike_spin.setEnabled(checked)
        self.price_spin.setEnabled(checked)

    def _on_clear(self) -> None:
        self._cleared = True
        self.accept()

    def result_rule(self) -> Optional[TakeProfitRule]:
        if self._cleared:
            return None
        reopen = None
        if self.reopen_checkbox.isChecked():
            reopen = ReopenSpec(strike=self.strike_spin.value(), price=self.price_spin.value())
        return TakeProfitRule(threshold_points=self.threshold_spin.value(), reopen=reopen, status=STATUS_PAUSED)


class StopLossDialog(QDialog):
    """規則3/4/5三選一，使用者依當下對盤勢的判斷自己選——見規則定案說
    明，這裡不做任何自動判斷/建議。"""

    def __init__(
        self, position: Position, sibling: Optional[Position],
        existing: Optional[StopLossRule], parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("設定停損")
        self._cleared = False
        self._position = position
        self._sibling = sibling

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.threshold_spin = _price_spin(existing.threshold_points if existing else 10.0)
        _add_row(form, "門檻點數", self.threshold_spin, _SL_THRESHOLD_TOOLTIP)
        layout.addLayout(form)

        self.radio3 = QRadioButton("規則3：預期回頭 — 平倉後原地重開，口數×2")
        self.radio4 = QRadioButton("規則4：預期停住 — 平倉這邊，另開一整組新價差")
        self.radio5 = QRadioButton("規則5：外在價值不足 — 這邊不平倉，裸賣加開對側一支腳")
        self.mode_group = QButtonGroup(self)
        for idx, radio in enumerate((self.radio3, self.radio4, self.radio5)):
            self.mode_group.addButton(radio, idx)
            layout.addWidget(radio)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_mode3_page(existing))
        self.stack.addWidget(self._build_mode4_page(existing))
        self.stack.addWidget(self._build_mode5_page(position, existing))
        layout.addWidget(self.stack)
        self.mode_group.idClicked.connect(self.stack.setCurrentIndex)

        mode = existing.mode if existing else SL_MODE_REOPEN_DOUBLE
        (self.radio3, self.radio4, self.radio5)[_MODE_TO_INDEX[mode]].setChecked(True)
        self.stack.setCurrentIndex(_MODE_TO_INDEX[mode])

        clear_btn = QPushButton("清除設定")
        clear_btn.clicked.connect(self._on_clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addLayout(_button_row(clear_btn, buttons))

    def _build_mode3_page(self, existing: Optional[StopLossRule]) -> QWidget:
        reopen = existing.reopen if existing and existing.mode == SL_MODE_REOPEN_DOUBLE else None
        page = QWidget()
        form = QFormLayout(page)
        self.m3_strike = _strike_spin(reopen.strike if reopen else None)
        self.m3_price = _price_spin(reopen.price if reopen else None)
        _add_row(form, "新履約價", self.m3_strike, _REOPEN_STRIKE_TOOLTIP)
        _add_row(form, "新委託價", self.m3_price, _REOPEN_PRICE_TOOLTIP)
        return page

    def _width_hint(self, call_put: str) -> str:
        """規則4新開的每一組價差，寬度自動沿用「同群組裡跟這個買賣權類
        型相同的既有部位」，self._sibling 是呼叫端用
        auto_close_manager.find_sibling() 算出來傳進來的，不影響實際送單時的計算
        (實際計算在 auto_close_manager.py，這裡純顯示，找不到就顯示問
        號，不擋使用者繼續設定)。"""
        template = self._position if self._position.legs[0].right == call_put else self._sibling
        if template is None or not template.is_combo:
            return "?"
        return f"{abs(template.legs[0].strike - template.legs[1].strike):g}"

    def _build_mode4_page(self, existing: Optional[StopLossRule]) -> QWidget:
        new_put = existing.new_put if existing and existing.mode == SL_MODE_NEW_GROUP else None
        new_call = existing.new_call if existing and existing.mode == SL_MODE_NEW_GROUP else None
        page = QWidget()
        form = QFormLayout(page)
        self.m4_put_strike = _strike_spin(new_put.strike if new_put else None)
        self.m4_put_price = _price_spin(new_put.price if new_put else None)
        self.m4_call_strike = _strike_spin(new_call.strike if new_call else None)
        self.m4_call_price = _price_spin(new_call.price if new_call else None)
        _add_row(form, "新Put價差 履約價", self.m4_put_strike,
                 f"寬度沿用原Put價差的寬度({self._width_hint('P')})，另一腳自動往價外推算。")
        _add_row(form, "新Put價差 委託價", self.m4_put_price, _REOPEN_PRICE_TOOLTIP)
        _add_row(form, "新Call價差 履約價", self.m4_call_strike,
                 f"寬度沿用原Call價差的寬度({self._width_hint('C')})，另一腳自動往價外推算。")
        _add_row(form, "新Call價差 委託價", self.m4_call_price, _REOPEN_PRICE_TOOLTIP)
        return page

    def _build_mode5_page(self, position: Position, existing: Optional[StopLossRule]) -> QWidget:
        add_leg = existing.add_leg if existing and existing.mode == SL_MODE_ADD_LEG else None
        opposite = "賣權(Put)" if position.legs[0].right == "C" else "買權(Call)"
        page = QWidget()
        form = QFormLayout(page)
        form.addRow("動作", QLabel(f"這邊不平倉，裸賣加開一口{opposite}"))
        self.m5_strike = _strike_spin(add_leg.strike if add_leg else None)
        self.m5_price = _price_spin(add_leg.price if add_leg else None)
        _add_row(form, "履約價", self.m5_strike, "手動填要加開的履約價，不套用寬度公式(跟規則2/3/4的重開不同)。")
        _add_row(form, "委託價", self.m5_price, "手動填加開這口的委託限價，會用連續IOC監看送出。")
        return page

    def _on_clear(self) -> None:
        self._cleared = True
        self.accept()

    def result_rule(self) -> Optional[StopLossRule]:
        if self._cleared:
            return None
        threshold = self.threshold_spin.value()
        if self.radio4.isChecked():
            return StopLossRule(
                threshold_points=threshold, mode=SL_MODE_NEW_GROUP,
                new_put=ReopenSpec(self.m4_put_strike.value(), self.m4_put_price.value()),
                new_call=ReopenSpec(self.m4_call_strike.value(), self.m4_call_price.value()),
                status=STATUS_PAUSED,
            )
        if self.radio5.isChecked():
            return StopLossRule(
                threshold_points=threshold, mode=SL_MODE_ADD_LEG,
                add_leg=ReopenSpec(self.m5_strike.value(), self.m5_price.value()),
                status=STATUS_PAUSED,
            )
        return StopLossRule(
            threshold_points=threshold, mode=SL_MODE_REOPEN_DOUBLE,
            reopen=ReopenSpec(self.m3_strike.value(), self.m3_price.value()),
            status=STATUS_PAUSED,
        )


class GroupTakeProfitDialog(QDialog):
    """規則1(整組停利)：只有門檻點數，沒有重開(觸發就整組平倉結束)。"""

    def __init__(self, group_name: str, existing: Optional[TakeProfitRule], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"設定「{group_name}」整組停利")
        self._cleared = False

        form = QFormLayout()
        self.threshold_spin = _price_spin(existing.threshold_points if existing else 20.0)
        _add_row(form, "門檻點數", self.threshold_spin, (
            "群組內全部部位平掉時的加權合計門檻——這裡跟單一價差的停利/停損不一"
            "樣：因為群組內每個價差的口數可能不對稱，無法直接加總點數比較，改用"
            "「各價差自己的點數差 × 自己的口數」加總後的合計數字去跟門檻比較，"
            "不是純點數。"
        ))

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        if existing and existing.paused_reason:
            note = QLabel(f"目前暫停原因：{existing.paused_reason}")
            note.setWordWrap(True)
            layout.addWidget(note)

        clear_btn = QPushButton("清除設定")
        clear_btn.clicked.connect(self._on_clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addLayout(_button_row(clear_btn, buttons))

    def _on_clear(self) -> None:
        self._cleared = True
        self.accept()

    def result_rule(self) -> Optional[TakeProfitRule]:
        if self._cleared:
            return None
        return TakeProfitRule(threshold_points=self.threshold_spin.value(), reopen=None, status=STATUS_PAUSED)
