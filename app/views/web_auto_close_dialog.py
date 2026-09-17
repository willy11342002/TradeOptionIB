"""
NiceGUI 版自動平倉/停利停損設定對話框，取代
`app/views/auto_close_dialog.py` 的三個 `QDialog`(`TakeProfitDialog`/
`StopLossDialog`/`GroupTakeProfitDialog`)。規則本身(觸發條件/動作/口數/
履約價填法)完全不變，見 `app/models/auto_close.py`/
`app/models/auto_close_manager.py` 開頭的說明——這支檔案純粹是換一層
UI，不重新設計規則。

*** 三個對話框都只在 build() 建一次，不是每次點「設定」就現建一個
***：跟 `web_position_widgets.py` 裡 `name_dialog`/`color_dialog`/
`confirm_dialog` 同一個慣例，`prompt_*()` 每次呼叫重新把欄位灌上目前的
值再 `await dialog`，NiceGUI 的 `ui.dialog()` 本來就支援重複 await。

*** 每個 prompt_*() 回傳三選一，呼叫端(web_position_widgets.py)照這個
協定分派 ***：
    None            使用者按取消，什麼都不做。
    ("clear", None) 使用者按「清除設定」，呼叫端要把對應規則設成 None。
    ("save", rule)  存新規則，rule 的 status 固定是 STATUS_PAUSED(照抄
                    Qt 版的規定：改過參數一律要求使用者自己重新按「啟
                    用」，不讓改動悄悄沿用舊的武裝狀態)。

*** Tooltip 文案修正過一處 ***：Qt 版 `_REOPEN_PRICE_TOOLTIP`跟規則5價
格欄位還寫著「會用連續IOC監看送出」，那是群益 IOC 引擎移除前留下的舊文
案，跟 `auto_close_manager.py` 現在一律掛 IB LMT+DAY 限價單、成交才觸發
下一步的行為不符，這裡改寫成符合實際行為的說法。
"""
from typing import Optional, Tuple

from nicegui import ui

from app.models.auto_close import (
    ReopenSpec, StopLossRule, TakeProfitRule,
    SL_MODE_ADD_LEG, SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE, STATUS_PAUSED,
)

_RIGHT_LABELS = {"C": "買權", "P": "賣權"}

_MODE_LABELS = {
    SL_MODE_REOPEN_DOUBLE: "規則3：預期回頭 — 平倉後原地重開，口數×2",
    SL_MODE_NEW_GROUP: "規則4：預期停住 — 平倉這邊，另開一整組新價差",
    SL_MODE_ADD_LEG: "規則5：外在價值不足 — 這邊不平倉，裸賣加開對側一支腳",
}

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
_REOPEN_PRICE_TOOLTIP = "重開倉這組價差的委託淨價(限價)，掛限價單(LMT+DAY)在委託簿上等成交。"
_ADD_LEG_STRIKE_TOOLTIP = "手動填要加開的履約價，不套用寬度公式(跟規則2/3/4的重開不同)。"
_ADD_LEG_PRICE_TOOLTIP = "手動填加開這口的委託限價，掛限價單(LMT+DAY)在委託簿上等成交。"
_GROUP_THRESHOLD_TOOLTIP = (
    "群組內全部部位平掉時的加權合計門檻——這裡跟單一價差的停利/停損不一樣：因"
    "為群組內每個價差的口數可能不對稱，無法直接加總點數比較，改用「各價差自"
    "己的點數差 × 自己的口數」加總後的合計數字去跟門檻比較，不是純點數。"
)

RuleResult = Optional[Tuple[str, object]]


def _strike_number(value: float = 0.0):
    return ui.number("履約價", value=value, format="%.0f", step=50, min=0).classes("w-32")


def _price_number(value: float = 1.0):
    return ui.number("委託價", value=value, format="%.2f", step=0.5, min=0.1).classes("w-32")


def _width_hint(position, sibling, call_put: str) -> str:
    """規則4新開的每一組價差，寬度沿用「同群組裡跟這個買賣權類型相同的
    既有部位」——純顯示用提示文字，不影響實際送單時的計算(那個在
    auto_close_manager.py::_build_new_group_action())，找不到範本就顯示
    問號，不擋使用者繼續設定。"""
    template = position if position.legs[0].right == call_put else sibling
    if template is None or not template.is_combo:
        return "?"
    return f"{abs(template.legs[0].strike - template.legs[1].strike):g}"


def build() -> tuple:
    # ------------------------------------------------------------ 規則2：單邊停利
    with ui.dialog() as tp_dialog, ui.card().classes("w-[420px] gap-3"):
        ui.label("設定停利").classes("text-lg font-semibold")
        tp_threshold = ui.number("門檻點數", value=10.0, format="%.1f", step=0.5, min=0.1).classes("w-40")
        tp_threshold.tooltip(_TP_THRESHOLD_TOOLTIP)
        tp_reopen_checkbox = ui.checkbox("平倉後原地重開同類型價差(口數不變)")
        with ui.row().classes("gap-4"):
            tp_strike = _strike_number()
            tp_price = _price_number()
        tp_strike.tooltip(_REOPEN_STRIKE_TOOLTIP)
        tp_price.tooltip(_REOPEN_PRICE_TOOLTIP)

        def _update_tp_reopen_enabled(_e=None) -> None:
            enabled = tp_reopen_checkbox.value
            tp_strike.set_enabled(enabled)
            tp_price.set_enabled(enabled)

        tp_reopen_checkbox.on_value_change(_update_tp_reopen_enabled)

        with ui.row().classes("w-full items-center"):
            ui.button("清除設定", on_click=lambda: tp_dialog.submit(("clear", None))).props("flat")
            ui.space()
            ui.button("取消", on_click=lambda: tp_dialog.submit(None)).props("flat")
            ui.button("儲存", on_click=lambda: tp_dialog.submit(("save", TakeProfitRule(
                threshold_points=tp_threshold.value,
                reopen=ReopenSpec(tp_strike.value, tp_price.value) if tp_reopen_checkbox.value else None,
                status=STATUS_PAUSED,
            ))))

    async def prompt_take_profit(existing: Optional[TakeProfitRule]) -> RuleResult:
        has_reopen = existing is not None and existing.reopen is not None
        tp_threshold.value = existing.threshold_points if existing else 10.0
        tp_reopen_checkbox.value = has_reopen
        tp_strike.value = existing.reopen.strike if has_reopen else 0.0
        tp_price.value = existing.reopen.price if has_reopen else 1.0
        _update_tp_reopen_enabled()
        return await tp_dialog

    # ------------------------------------------------------------ 規則3/4/5：單邊停損
    with ui.dialog() as sl_dialog, ui.card().classes("w-[480px] gap-3"):
        ui.label("設定停損").classes("text-lg font-semibold")
        sl_threshold = ui.number("門檻點數", value=10.0, format="%.1f", step=0.5, min=0.1).classes("w-40")
        sl_threshold.tooltip(_SL_THRESHOLD_TOOLTIP)
        sl_mode = ui.radio(_MODE_LABELS, value=SL_MODE_REOPEN_DOUBLE).classes("gap-1")

        with ui.column().classes("gap-2 pl-2") as mode3_section:
            with ui.row().classes("gap-4"):
                m3_strike = _strike_number()
                m3_price = _price_number()
            m3_strike.tooltip(_REOPEN_STRIKE_TOOLTIP)
            m3_price.tooltip(_REOPEN_PRICE_TOOLTIP)

        with ui.column().classes("gap-2 pl-2") as mode4_section:
            ui.label("新Put價差").classes("text-sm text-grey")
            m4_put_hint = ui.label("").classes("text-xs text-grey")
            with ui.row().classes("gap-4"):
                m4_put_strike = _strike_number()
                m4_put_price = _price_number()
            m4_put_price.tooltip(_REOPEN_PRICE_TOOLTIP)
            ui.label("新Call價差").classes("text-sm text-grey")
            m4_call_hint = ui.label("").classes("text-xs text-grey")
            with ui.row().classes("gap-4"):
                m4_call_strike = _strike_number()
                m4_call_price = _price_number()
            m4_call_price.tooltip(_REOPEN_PRICE_TOOLTIP)

        with ui.column().classes("gap-2 pl-2") as mode5_section:
            m5_note = ui.label("")
            with ui.row().classes("gap-4"):
                m5_strike = _strike_number()
                m5_price = _price_number()
            m5_strike.tooltip(_ADD_LEG_STRIKE_TOOLTIP)
            m5_price.tooltip(_ADD_LEG_PRICE_TOOLTIP)

        def _update_sl_mode_visibility(_e=None) -> None:
            mode = sl_mode.value
            mode3_section.set_visibility(mode == SL_MODE_REOPEN_DOUBLE)
            mode4_section.set_visibility(mode == SL_MODE_NEW_GROUP)
            mode5_section.set_visibility(mode == SL_MODE_ADD_LEG)

        sl_mode.on_value_change(_update_sl_mode_visibility)

        def _build_sl_rule() -> StopLossRule:
            threshold = sl_threshold.value
            mode = sl_mode.value
            if mode == SL_MODE_NEW_GROUP:
                return StopLossRule(
                    threshold_points=threshold, mode=mode,
                    new_put=ReopenSpec(m4_put_strike.value, m4_put_price.value),
                    new_call=ReopenSpec(m4_call_strike.value, m4_call_price.value),
                    status=STATUS_PAUSED,
                )
            if mode == SL_MODE_ADD_LEG:
                return StopLossRule(
                    threshold_points=threshold, mode=mode,
                    add_leg=ReopenSpec(m5_strike.value, m5_price.value),
                    status=STATUS_PAUSED,
                )
            return StopLossRule(
                threshold_points=threshold, mode=SL_MODE_REOPEN_DOUBLE,
                reopen=ReopenSpec(m3_strike.value, m3_price.value),
                status=STATUS_PAUSED,
            )

        with ui.row().classes("w-full items-center"):
            ui.button("清除設定", on_click=lambda: sl_dialog.submit(("clear", None))).props("flat")
            ui.space()
            ui.button("取消", on_click=lambda: sl_dialog.submit(None)).props("flat")
            ui.button("儲存", on_click=lambda: sl_dialog.submit(("save", _build_sl_rule())))

    async def prompt_stop_loss(position, sibling, existing: Optional[StopLossRule]) -> RuleResult:
        mode = existing.mode if existing else SL_MODE_REOPEN_DOUBLE
        sl_threshold.value = existing.threshold_points if existing else 10.0
        sl_mode.value = mode

        m3 = existing.reopen if existing and existing.mode == SL_MODE_REOPEN_DOUBLE else None
        m3_strike.value = m3.strike if m3 else 0.0
        m3_price.value = m3.price if m3 else 1.0

        new_put = existing.new_put if existing and existing.mode == SL_MODE_NEW_GROUP else None
        new_call = existing.new_call if existing and existing.mode == SL_MODE_NEW_GROUP else None
        m4_put_strike.value = new_put.strike if new_put else 0.0
        m4_put_price.value = new_put.price if new_put else 1.0
        m4_call_strike.value = new_call.strike if new_call else 0.0
        m4_call_price.value = new_call.price if new_call else 1.0
        m4_put_hint.text = f"寬度沿用原Put價差的寬度({_width_hint(position, sibling, 'P')})，另一腳自動往價外推算。"
        m4_call_hint.text = f"寬度沿用原Call價差的寬度({_width_hint(position, sibling, 'C')})，另一腳自動往價外推算。"

        add_leg = existing.add_leg if existing and existing.mode == SL_MODE_ADD_LEG else None
        m5_strike.value = add_leg.strike if add_leg else 0.0
        m5_price.value = add_leg.price if add_leg else 1.0
        opposite = _RIGHT_LABELS["P"] if position.legs[0].right == "C" else _RIGHT_LABELS["C"]
        m5_note.text = f"這邊不平倉，裸賣加開一口{opposite}"

        _update_sl_mode_visibility()
        return await sl_dialog

    # ------------------------------------------------------------ 規則1：整組停利
    with ui.dialog() as group_dialog, ui.card().classes("w-[420px] gap-3"):
        group_title = ui.label("").classes("text-lg font-semibold")
        group_threshold = ui.number("門檻點數", value=20.0, format="%.1f", step=0.5, min=0.1).classes("w-40")
        group_threshold.tooltip(_GROUP_THRESHOLD_TOOLTIP)
        group_paused_note = ui.label("").classes("text-xs text-grey")

        with ui.row().classes("w-full items-center"):
            ui.button("清除設定", on_click=lambda: group_dialog.submit(("clear", None))).props("flat")
            ui.space()
            ui.button("取消", on_click=lambda: group_dialog.submit(None)).props("flat")
            ui.button("儲存", on_click=lambda: group_dialog.submit(("save", TakeProfitRule(
                threshold_points=group_threshold.value, reopen=None, status=STATUS_PAUSED,
            ))))

    async def prompt_group_take_profit(group_name: str, existing: Optional[TakeProfitRule]) -> RuleResult:
        group_title.text = f"設定「{group_name}」整組停利"
        group_threshold.value = existing.threshold_points if existing else 20.0
        has_reason = bool(existing and existing.paused_reason)
        group_paused_note.text = f"目前暫停原因：{existing.paused_reason}" if has_reason else ""
        group_paused_note.set_visibility(has_reason)
        return await group_dialog

    return prompt_take_profit, prompt_stop_loss, prompt_group_take_profit
