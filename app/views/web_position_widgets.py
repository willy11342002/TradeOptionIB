"""
NiceGUI 版未平倉部位，取代 `app/views/position_widgets.py`
(`PositionTreeWidget`)，現在含自動平倉(停利/停損)規則的顯示/編輯——原
本開頭寫著「明確不搬」，那是還沒排到的階段，規則引擎本身
(`app/models/auto_close.py`/`app/models/auto_close_manager.py`/
`app/services/auto_close_store.py`)完全沒改，這支檔案只是接上
`web_auto_close_dialog.py` 的三個對話框跟 `AutoCloseManager` 的公開介
面，UI 層的移植細節見 `web_auto_close_dialog.py` 開頭的說明。

跟 Qt 版一樣用 `PositionManager.groups` 分組(自動分組來自本地已成交複
式單紀錄、手動覆蓋存在 `position_groups_pref.json`)。「移到群組」用下
拉選單取代 Qt 版的滑鼠右鍵選單(網頁版沒有 tree widget 的拖曳/右鍵慣
例，下拉選單是同樣操作在網頁上最直接的對應)。

*** `_refresh()` 一定要用 `with client:` 包住 ***(照抄
`web_order_book_widgets.py` 開頭的說明，同一個坑)：`positions_changed`
/`auto_close_manager.rules_changed` 這些訊號會從 `ib.positionEvent`/報
價 tick/委託成交這些 ib_async 事件回呼觸發，不是使用者在這個分頁上點了
什麼——這種情況下 NiceGUI 沒有「目前是哪個瀏覽器分頁」的環境資訊，
`_refresh()` 又是整批 `clear()` 後重新建立全新元件(不是單純改既有元件
屬性)，沒有 `with client:` 的話新元素只會在伺服器端算出來，不會真的推
到瀏覽器上。`_on_auto_close_error()` 用 `ui.notify()` 而不是像其他地方
一樣寫進行內文字標籤，理由同一個：自動平倉的錯誤是背景訊號觸發的，使
用者不一定正好開著「部位」這個對話框，全域 toast 才看得到。
"""
import asyncio
import datetime
from typing import Callable, Optional

from nicegui import ui

from app.models.auto_close import (
    SL_MODE_ADD_LEG, SL_MODE_NEW_GROUP, SL_MODE_REOPEN_DOUBLE,
    STATUS_ARMED, STATUS_FAILED, STATUS_PAUSED, STATUS_TRIGGERED, STATUS_UNSET,
)
from app.models.auto_close_manager import AutoCloseManager
from app.models.positions import (
    PositionGroup, PositionManager, UNGROUPED_ID, current_price, position_pnl,
)
from app.services import position_groups_store
from app.views import web_auto_close_dialog

_DEFAULT_GROUP_COLOR = "#4a90d9"
_NEW_GROUP_SENTINEL = "__new_group__"
_RIGHT_LABELS = {"C": "買權", "P": "賣權"}

_STATUS_ICONS = {
    STATUS_UNSET: "⚪", STATUS_PAUSED: "⏸", STATUS_ARMED: "▶",
    STATUS_TRIGGERED: "✅", STATUS_FAILED: "❌",
}


def _status_icon(status: str) -> str:
    return _STATUS_ICONS.get(status, "⚪")


def _triggered_suffix(rule) -> str:
    if rule is not None and rule.status == STATUS_TRIGGERED and rule.triggered_at:
        return f"(觸發於 {datetime.datetime.fromtimestamp(rule.triggered_at).strftime('%H:%M:%S')})"
    return ""


def _tp_summary(rule) -> str:
    if rule is None:
        return "未設定"
    if rule.reopen is not None:
        text = f"{rule.threshold_points:g}pt，平倉+原地重開({rule.reopen.strike:g} @{rule.reopen.price:g})"
    else:
        text = f"{rule.threshold_points:g}pt，只平倉不重開"
    return text + _triggered_suffix(rule)


def _sl_summary(rule) -> str:
    if rule is None:
        return "未設定"
    if rule.mode == SL_MODE_REOPEN_DOUBLE and rule.reopen is not None:
        text = f"{rule.threshold_points:g}pt，規則3 平倉+原地重開×2口({rule.reopen.strike:g} @{rule.reopen.price:g})"
    elif rule.mode == SL_MODE_NEW_GROUP:
        parts = []
        if rule.new_put is not None:
            parts.append(f"Put {rule.new_put.strike:g}@{rule.new_put.price:g}")
        if rule.new_call is not None:
            parts.append(f"Call {rule.new_call.strike:g}@{rule.new_call.price:g}")
        text = f"{rule.threshold_points:g}pt，規則4 平倉+另開新價差({'、'.join(parts) or '未設定'})"
    elif rule.mode == SL_MODE_ADD_LEG and rule.add_leg is not None:
        text = f"{rule.threshold_points:g}pt，規則5 不平倉+加開對側({rule.add_leg.strike:g} @{rule.add_leg.price:g})"
    else:
        text = f"{rule.threshold_points:g}pt"
    return text + _triggered_suffix(rule)

# *** 部位列一定要套跟委託簿(web_order_book_widgets.py)同一套固定欄寬
# grid-cols，不能用 flex+gap 各自依內容自然寬度排 ***(使用者反饋：只有
# 資料沒有欄位名稱看不懂，加標題列的話又會重演委託簿那次「flex 版本標題
# 列跟資料列對不齊」的問題)。群組欄放最後——`ui.select` 是固定寬度的下
# 拉選單，不像狀態/商品欄那樣長度會隨內容變化，放最後一欄不會有委託簿那
# 種「內容太長擠到後面欄位」的疑慮，純粹沿用委託簿「操作型欄位放最後」
# 的慣例。到期日跟代碼分成兩欄、各自有標題(使用者反饋合併成一欄的話到
# 期日那半段沒有欄位名稱看不懂)。
_POSITION_GRID_COLS = "grid-cols-[70px_90px_80px_56px_56px_56px_70px_70px_80px_170px]"

# *** 欄位分隔線用 inline style 的 border-left，不是 Tailwind 的
# `divide-x` class ***(實測 `divide-x`/`divide-white/10` 在這個
# NiceGUI/Tailwind 組合下完全沒有效果——量過 computed style，
# border-left-width 恆為 0px，猜測是 NiceGUI 內建的 Tailwind 沒有把
# `divide-x` 產生的 `:not([hidden]) ~ :not([hidden])` 兄弟選擇器編譯進最
# 終樣式表；沒有再深入查證，直接換一個確定會生效的寫法)。第一欄(代碼)
# 不用加，不然卡片最左邊會多一條沒意義的線。
_DIVIDER_STYLE = "border-left: 1px solid rgba(255,255,255,.12)"


def _grid_cell(text: str, *, first: bool = False, extra_classes: str = "") -> None:
    label = ui.label(text).classes(f"whitespace-nowrap text-center px-2 {extra_classes}".strip())
    if not first:
        label.style(_DIVIDER_STYLE)


def _direction_text(position) -> str:
    return "買進" if position.buy else "賣出"


def _right_text(position) -> str:
    labels = {_RIGHT_LABELS.get(leg.right, leg.right) for leg in position.legs}
    return "/".join(sorted(labels))


def _format_expiry(expiry: str) -> str:
    if len(expiry) == 8 and expiry.isdigit():  # IB 原始格式 "YYYYMMDD"
        return f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    return expiry


def _symbol_text(position) -> str:
    """只顯示底層代碼，不是完整 OCC 編碼字串(跟委託簿那邊
    web_order_book_widgets.py::_symbol_text() 同樣的理由)——履約價/到期
    日/類型已經拆成獨立欄位。`legs` 目前恆為 1 個(見 positions.py 開頭說
    明，combo 顯示還沒做)，不用處理多腳串接。"""
    return position.legs[0].symbol


def _expiry_text(position) -> str:
    leg = position.legs[0]
    return _format_expiry(getattr(leg, "lastTradeDateOrContractMonth", "") or "")


def _strike_text(position) -> str:
    strikes = [leg.strike for leg in position.legs if getattr(leg, "strike", None) is not None]
    return "/".join(f"{s:g}" for s in strikes)


def _pnl_classes(pnl: Optional[float]) -> str:
    if pnl is None:
        return "text-grey"
    return "text-positive" if pnl >= 0 else "text-negative"


def _pnl_text(pnl: Optional[float]) -> str:
    return "—" if pnl is None else f"{pnl:,.0f}"


def build(position_manager: PositionManager, auto_close_manager: AutoCloseManager) -> Callable:
    client = ui.context.client  # 見檔案開頭 *** _refresh() 一定要用 with client: *** 的說明
    prompt_take_profit, prompt_stop_loss, prompt_group_take_profit = web_auto_close_dialog.build()

    # *** 卡片寬度用 w-fit，不要固定 w-[820px] ***(使用者反饋：固定寬度比
    # 欄位實際需要的寬度窄一點點，逼出一條難看的橫向捲軸)——跟委託簿
    # (web_order_book_widgets.py)同一個解法，讓卡片直接長到「剛好放得
    # 下 _POSITION_GRID_COLS 這幾欄」的寬度，max-w-[95vw] 只在真的超過
    # 螢幕寬度時才讓 groups_container 的 overflow-x-auto 出來擋。
    with ui.dialog() as dialog, ui.card().classes("w-fit max-w-[95vw] max-h-[85vh] overflow-y-auto gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("未平倉部位").classes("text-lg font-semibold")
            ui.button("重新查詢", on_click=lambda: position_manager.refresh()).props("flat dense")
            ui.button(
                "全部啟動", on_click=lambda: auto_close_manager.arm_all_paused(),
            ).props("flat dense").tooltip("把所有已設定但暫停中的停利/停損/整組停利規則一次全部啟用")
        status_label = ui.label("").classes("text-xs text-grey")
        # 欄位名稱列：跟 _build_position_row() 用同一組 _POSITION_GRID_COLS
        # 固定欄寬(見該常數定義處的說明)，只在對話框頂端放一次，不是每個
        # 群組底下都重複一次。
        # *** 不要用 justify-items-center ***：那是把每個儲存格「內容本
        # 身」縮到多窄就置中多窄，分隔線會跟著內容寬度飄動，標題列跟資料
        # 列的分隔線因此對不齊(兩者文字長度不同)。維持 grid 預設的
        # stretch(每欄撐滿整個欄寬)，靠 text-center 把「文字」在撐滿的欄
        # 位裡置中，分隔線才會落在欄位真正的邊界上、每一列都一樣。
        with ui.element("div").classes(f"grid {_POSITION_GRID_COLS} gap-x-0 items-center text-xs text-grey"):
            _grid_cell("代碼", first=True)
            _grid_cell("到期日")
            _grid_cell("履約價")
            _grid_cell("類型")
            _grid_cell("方向")
            _grid_cell("口數")
            _grid_cell("均價")
            _grid_cell("現價")
            _grid_cell("損益")
            _grid_cell("群組")
        groups_container = ui.column().classes("w-full gap-3 overflow-x-auto")

    # 巢狀 dialog：命名(新群組／重新命名共用)，跟 web_screener_widget.py
    # 的 name_prompt_dialog 同一個慣例——這幾個巢狀 dialog 都建在
    # groups_container 外面、只建一次，不會被 _refresh() 的 clear() 波及。
    with ui.dialog() as name_dialog, ui.card():
        name_dialog_title = ui.label("")
        name_dialog_input = ui.input().classes("w-72")
        with ui.row():
            ui.button("儲存", on_click=lambda: name_dialog.submit(name_dialog_input.value))
            ui.button("取消", on_click=lambda: name_dialog.submit(None)).props("flat")

    async def _prompt_name(title: str, default: str) -> Optional[str]:
        name_dialog_title.text = title
        name_dialog_input.value = default
        result = await name_dialog
        result = (result or "").strip()
        return result or None

    with ui.dialog() as color_dialog, ui.card():
        ui.label("設定群組顏色")
        color_dialog_input = ui.color_input(value=_DEFAULT_GROUP_COLOR)
        with ui.row():
            ui.button("儲存", on_click=lambda: color_dialog.submit(color_dialog_input.value))
            ui.button("取消", on_click=lambda: color_dialog.submit(None)).props("flat")

    async def _prompt_color(default: str) -> Optional[str]:
        color_dialog_input.value = default
        return await color_dialog

    with ui.dialog() as confirm_dialog, ui.card():
        confirm_message = ui.label("")
        with ui.row():
            ui.button("確定", on_click=lambda: confirm_dialog.submit(True))
            ui.button("取消", on_click=lambda: confirm_dialog.submit(False)).props("flat")

    async def _confirm(message: str) -> bool:
        confirm_message.text = message
        return bool(await confirm_dialog)

    def _group_options() -> dict:
        options = {gid: info["name"] for gid, info in position_groups_store.list_groups().items()}
        options[UNGROUPED_ID] = "未分組"
        options[_NEW_GROUP_SENTINEL] = "＋ 新群組..."
        return options

    async def _on_move_change(symbol_key: str, group_id: str) -> None:
        if group_id == _NEW_GROUP_SENTINEL:
            name = await _prompt_name("新群組", "")
            if name:
                new_id = position_manager.create_group(name, _DEFAULT_GROUP_COLOR)
                position_manager.move_to_group(symbol_key, new_id)
        else:
            # *** UNGROUPED_ID 要轉成 None 才呼叫 move_to_group ***：跟 Qt
            # 版「取消分組」是同一個動作，None 才會讓
            # position_groups_store 存一筆「使用者手動取消分組」(見
            # positions.py::effective_group_for 的說明)，不是把
            # "__ungrouped__" 這個字串存進去。
            position_manager.move_to_group(symbol_key, None if group_id == UNGROUPED_ID else group_id)
        _refresh()

    async def _on_rename_group(group: PositionGroup) -> None:
        name = await _prompt_name("重新命名群組", group.name)
        if name:
            position_manager.rename_group(group.group_id, name)
            _refresh()

    async def _on_set_group_color(group: PositionGroup) -> None:
        color = await _prompt_color(group.color)
        if color:
            position_manager.set_group_color(group.group_id, color)
            _refresh()

    async def _on_delete_group(group: PositionGroup) -> None:
        if await _confirm(f"刪除群組「{group.name}」？裡面的部位會變回未分組(部位本身不受影響)。"):
            position_manager.delete_group(group.group_id)
            _refresh()

    async def _on_edit_group_take_profit(group: PositionGroup) -> None:
        rule = auto_close_manager.get_group_rule(group.group_id)
        result = await prompt_group_take_profit(group.name, rule)
        if result is None:
            return
        action, new_rule = result
        auto_close_manager.set_group_rule(group.group_id, None if action == "clear" else new_rule)

    def _on_toggle_group_rule(group: PositionGroup, rule) -> None:
        new_status = STATUS_PAUSED if rule.status == STATUS_ARMED else STATUS_ARMED
        auto_close_manager.set_group_rule_status(group.group_id, new_status)

    def _build_group_header(group: PositionGroup) -> None:
        total_qty = sum(p.qty for p in group.positions)
        pnls = [position_pnl(p, current_price(position_manager, p)) for p in group.positions]
        pnls = [v for v in pnls if v is not None]
        group_pnl = sum(pnls) if pnls else None
        group_rule = auto_close_manager.get_group_rule(group.group_id)
        with ui.row().classes("items-center gap-2 w-full"):
            ui.element("div").classes("w-3 h-3 rounded-full shrink-0").style(f"background-color:{group.color}")
            ui.label(group.name).classes("font-semibold")
            ui.label(f"{total_qty:g} 口").classes("text-xs text-grey")
            ui.label(_pnl_text(group_pnl)).classes(f"text-sm {_pnl_classes(group_pnl)}")
            ui.label(f"{_status_icon(group_rule.status if group_rule else STATUS_UNSET)} 整組停利").classes(
                "text-xs text-grey",
            ).tooltip(_tp_summary(group_rule) if group_rule else "未設定整組停利")
            if group_rule is not None and group_rule.paused_reason:
                ui.icon("info").classes("text-xs text-grey").tooltip(f"暫停原因：{group_rule.paused_reason}")
            ui.space()
            ui.button(
                "整組停利", on_click=lambda g=group: asyncio.ensure_future(_on_edit_group_take_profit(g)),
            ).props("flat dense size=sm")
            if group_rule is not None and group_rule.status in (STATUS_ARMED, STATUS_PAUSED):
                toggle_label = "暫停" if group_rule.status == STATUS_ARMED else "啟用"
                ui.button(
                    toggle_label, on_click=lambda g=group, r=group_rule: _on_toggle_group_rule(g, r),
                ).props("flat dense size=sm")
            if group.group_id != UNGROUPED_ID:  # 「未分組」是固定虛擬群組，不能改名/改色/刪除，跟 Qt 版一致
                ui.button(
                    icon="edit", on_click=lambda g=group: asyncio.ensure_future(_on_rename_group(g)),
                ).props("flat dense round size=sm").tooltip("重新命名")
                ui.button(
                    icon="palette", on_click=lambda g=group: asyncio.ensure_future(_on_set_group_color(g)),
                ).props("flat dense round size=sm").tooltip("設定顏色")
                ui.button(
                    icon="delete", on_click=lambda g=group: asyncio.ensure_future(_on_delete_group(g)),
                ).props("flat dense round size=sm").tooltip("刪除群組")

    def _build_position_row(position, group: PositionGroup) -> None:
        # 跟標題列用同一組 _POSITION_GRID_COLS/分隔線/置中設定(_grid_cell)，
        # 欄位順序/寬度/對齊方式不能各自改，不然標題列跟資料列會對不上。
        price = current_price(position_manager, position)
        pnl = position_pnl(position, price)
        with ui.element("div").classes(f"grid {_POSITION_GRID_COLS} gap-x-0 items-center border-b py-1 text-sm"):
            _grid_cell(_symbol_text(position), first=True)
            _grid_cell(_expiry_text(position))
            _grid_cell(_strike_text(position))
            _grid_cell(_right_text(position))
            _grid_cell(_direction_text(position))
            _grid_cell(f"{position.qty:g}")
            _grid_cell(f"{position.avg_cost:g}")
            _grid_cell(f"{price:g}" if price is not None else "—")
            _grid_cell(_pnl_text(pnl), extra_classes=_pnl_classes(pnl))
            with ui.element("div").classes("px-2").style(_DIVIDER_STYLE):
                ui.select(
                    _group_options(), value=group.group_id,
                    on_change=lambda e, key=position.symbol_key: asyncio.ensure_future(_on_move_change(key, e.value)),
                ).props("dense options-dense").classes("w-full")

    def _apply_position_rule_result(symbol_key: str, kind: str, result) -> None:
        if result is None:
            return
        action, rule = result
        new_rule = None if action == "clear" else rule
        if kind == "take_profit":
            auto_close_manager.set_take_profit(symbol_key, new_rule)
        else:
            auto_close_manager.set_stop_loss(symbol_key, new_rule)

    def _on_toggle_position_rule(symbol_key: str, kind: str, rule) -> None:
        new_status = STATUS_PAUSED if rule.status == STATUS_ARMED else STATUS_ARMED
        auto_close_manager.set_position_rule_status(symbol_key, kind, new_status)

    def _build_rule_row(position, kind: str, rule) -> None:
        # kind: "take_profit"/"stop_loss"，對照 AutoCloseManager 的 API。
        label_text = "停利" if kind == "take_profit" else "停損"
        summary = _tp_summary(rule) if kind == "take_profit" else _sl_summary(rule)

        async def _on_edit() -> None:
            if kind == "take_profit":
                result = await prompt_take_profit(rule)
            else:
                sibling = auto_close_manager.find_sibling(position)
                result = await prompt_stop_loss(position, sibling, rule)
            _apply_position_rule_result(position.symbol_key, kind, result)

        with ui.row().classes("items-center gap-2 pl-8 text-xs"):
            ui.label(f"{_status_icon(rule.status if rule else STATUS_UNSET)} {label_text}").classes("w-14 shrink-0")
            ui.label(summary).classes("text-grey")
            ui.space()
            ui.button("設定", on_click=lambda: asyncio.ensure_future(_on_edit())).props("flat dense size=sm")
            if rule is not None and rule.status in (STATUS_ARMED, STATUS_PAUSED):
                toggle_label = "暫停" if rule.status == STATUS_ARMED else "啟用"
                ui.button(
                    toggle_label,
                    on_click=lambda k=kind, r=rule: _on_toggle_position_rule(position.symbol_key, k, r),
                ).props("flat dense size=sm")

    def _build_rule_rows(position) -> None:
        rules = auto_close_manager.get_position_rules(position.symbol_key)
        _build_rule_row(position, "take_profit", rules.take_profit)
        _build_rule_row(position, "stop_loss", rules.stop_loss)

    def _on_auto_close_error(message: str) -> None:
        with client:
            ui.notify(message, type="negative", close_button=True, timeout=0)

    def _refresh() -> None:
        with client:
            groups_container.clear()
            groups = position_manager.groups
            with groups_container:
                if not groups:
                    ui.label("目前沒有未平倉部位").classes("text-sm text-grey")
                for group in groups:
                    with ui.column().classes("w-full gap-0.5 border rounded p-2"):
                        _build_group_header(group)
                        for position in group.positions:
                            _build_position_row(position, group)
                            _build_rule_rows(position)
            total = sum(len(g.positions) for g in groups)
            status_label.text = f"共 {total} 筆部位" if total else ""

    position_manager.positions_changed.connect(_refresh)
    auto_close_manager.rules_changed.connect(_refresh)
    auto_close_manager.auto_close_error.connect(_on_auto_close_error)
    _refresh()  # PositionManager 建構時已經查過一次未平倉，這裡立刻畫出來，不用等第一次變動

    return dialog.open
