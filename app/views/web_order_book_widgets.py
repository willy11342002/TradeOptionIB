"""
NiceGUI 版委託簿/成交回報，取代 `app/views/order_book_widgets.py`
(`OrderBookWidget`/`FillReportWidget`)。

*** 用一筆委託一張卡片(`ui.row` + `ui.button`)，不是 `ui.aggrid`/
`ui.table` ***：委託筆數少、狀態變化是人類操作等級的頻率(不像報價那種
高頻 tick)，不需要 AG Grid 的效能/虛擬捲動能力；`records_changed` 觸發
時整個容器清空重畫，完全沒有報價盤那種閃爍疑慮，也不用碰 Quasar table
body-slot 樣板語法，複雜度低很多。

跟舊版一樣兩塊內容共用同一個 `OrderBookManager`：委託簿排除已成交
(`STATUS_FILLED`)，成交回報只顯示已成交、唯讀無動作按鈕。狀態文字/欄
位對照舊版 `_STATUS_LABELS`/`_BOX_COLUMNS`/`_FILL_COLUMNS`。

委託簿／成交回報現在是兩個各自獨立、置中顯示的 `ui.dialog()` modal(不
是同一個視窗裡的分頁)，對照使用者的要求「每個視窗單獨一個按鈕」——
`build()` 回傳 `(open_box_dialog, open_fill_dialog)`，呼叫端(`main.py`)
各自接一個按鈕。
"""
import datetime

from nicegui import ui

from app.models.order_book import (
    OrderBookManager, STATUS_STAGED, STATUS_LIVE, STATUS_FILLED, STATUS_REJECTED,
)

_STATUS_LABELS = {
    STATUS_STAGED: "待送出",
    STATUS_LIVE: "掛單中",
    STATUS_FILLED: "已成交",
    STATUS_REJECTED: "失敗",
    "cancelled": "已取消",
}
_RIGHT_LABELS = {"C": "買權", "P": "賣權"}


def _direction_text(record) -> str:
    # 複式單一定要看 net_buyer，不能看 legs[0].buy——兩者不是同一件事，
    # 見 app/models/order_book.py::OrderRecord.net_buyer 的說明。
    if record.kind == "duplex":
        return "買方" if record.net_buyer else "賣方"
    return "買進" if record.legs[0].buy else "賣出"


def _right_text(record) -> str:
    for leg in record.legs:
        if leg.right:
            return _RIGHT_LABELS.get(leg.right, leg.right)
    return ""


def build(order_book_manager: OrderBookManager) -> tuple:
    # *** 一定要記住這個分頁的 client，_refresh() 重畫整批列的時候要
    # 用 `with client:` 包起來，不能省 ***：委託狀態變化(送出成交/被交
    # 易所取消)是從 ib_async 的事件回呼觸發的，不是使用者在這個分頁上
    # 點了什麼，這種情況下 NiceGUI 沒有「目前是哪個瀏覽器分頁」的環境資
    # 訊——單純「修改既有元件的屬性」(例如 `label.text = ...`)不受影響
    # 照樣會即時推給瀏覽器，但這裡 `_refresh()` 是整批 `clear()` 後重新
    # 建立全新的元件，建立新元件這個動作沒有 `with client:` 的話只會在
    # 伺服器端算出結果，不會真的推到瀏覽器上——實測踩過的真實案例：委
    # 託被取消(狀態變成「已取消」)之後，畫面停在「掛單中」不會動，要重
    # 新整理頁面才看得到正確狀態，重新整理當下讀到的資料其實早就是對
    # 的，只是沒推過去而已。
    client = ui.context.client

    with ui.dialog() as box_dialog, ui.card().classes("w-[900px] max-w-full gap-2"):
        ui.label("委託簿").classes("text-lg font-semibold")
        box_status = ui.label("").classes("text-sm text-negative")
        # overflow-x-auto：每一列(商品/買權賣權/方向/價格/口數/委託條
        # 件/狀態/動作)固定寬度加起來可能比這個 modal 寬，寧可讓這塊內
        # 容自己橫向捲動，不要把整個視窗擠寬或硬把文字截斷看不全。
        box_container = ui.column().classes("w-full gap-1 overflow-x-auto")

    with ui.dialog() as fill_dialog, ui.card().classes("w-[900px] max-w-full gap-2"):
        ui.label("成交回報").classes("text-lg font-semibold")
        fill_container = ui.column().classes("w-full gap-1 overflow-x-auto")

    # *** 改價/改量的 ui.dialog() 一定要掛在這個穩定、永遠不會被清空的
    # anchor 底下，不能直接在按鈕的 on_click 裡建立 ***：NiceGUI 呼叫事
    # 件 handler 時會把「目前的 slot」還原成「觸發這個事件的元件所在的
    # slot」(見 aggrid 那類元件 handle_event 的說明)，這裡的按鈕是
    # `_box_row()` 動態產生、活在 `box_container` 裡的——如果直接在
    # on_click 裡 `with ui.dialog():`，這個 dialog 會被種進
    # `box_container` 那個會被 `_refresh()` 整個 `clear()` 掉的容器。使
    # 用者按下「確定」時，`on_confirm(...)` 觸發的 `records_changed` 會
    # 先讓 `_refresh()` 把 `box_container`(連同這個 dialog)整個清空重
    # 建，才輪到 `dialog.close()`，這時候 dialog 早就被刪除了，
    # NiceGUI 會印出「An element has been deleted but is still being
    # used」的警告，是實測踩到的真實案例，不是預防性寫法。
    dialog_anchor = ui.column().classes("hidden")

    def _amend_dialog(title: str, current_value, on_confirm, *, decimals: int, step: float, minimum: float) -> None:
        with dialog_anchor, ui.dialog() as dialog, ui.card():
            ui.label(title)
            value_input = ui.number(value=current_value, format=f"%.{decimals}f", step=step, min=minimum)
            with ui.row():
                ui.button("確定", on_click=lambda: (on_confirm(value_input.value), dialog.close()))
                ui.button("取消", on_click=dialog.close)
        dialog.open()

    def _box_row(record) -> None:
        # *** 每個欄位都要 shrink-0 whitespace-nowrap，不能只給固定寬度
        # (w-48 這種)***：固定寬度不夠放的話，文字會在那個寬度裡自動換
        # 行，變成一列擠成三行、其他欄位反而看起來對不齊——這裡故意讓每
        # 欄「依內容自然寬度、不換行、不被 flex 擠壓」，商品欄位這種複
        # 式單組合描述本來就長，讓它一行寫完，寬度不夠放的話靠外層
        # box_container 的 overflow-x-auto 整列橫向捲動，比每欄各自換行
        # 好讀。
        with ui.row().classes("flex-nowrap items-center gap-4 border-b py-1"):
            ui.label(record.label()).classes("shrink-0 whitespace-nowrap")
            ui.label(_right_text(record)).classes("shrink-0 whitespace-nowrap w-16")
            ui.label(_direction_text(record)).classes("shrink-0 whitespace-nowrap w-16")
            ui.label(f"{record.price:g}").classes("shrink-0 whitespace-nowrap w-20")
            ui.label(str(record.qty)).classes("shrink-0 whitespace-nowrap w-12")
            ui.label(record.tif).classes("shrink-0 whitespace-nowrap w-16")
            status_text = _STATUS_LABELS.get(record.status, record.status)
            if record.status == STATUS_REJECTED and record.error_msg:
                status_text += f"({record.error_msg})"
            ui.label(status_text).classes("shrink-0 whitespace-nowrap")
            with ui.row().classes("shrink-0 flex-nowrap gap-1"):
                if record.status == STATUS_STAGED:
                    ui.button("送出", on_click=lambda r=record: order_book_manager.confirm_send(r.id)).props("dense")
                    ui.button(
                        "刪除", on_click=lambda r=record: order_book_manager.discard_staged(r.id),
                    ).props("dense outline")
                elif record.status == STATUS_LIVE:
                    ui.button("改價", on_click=lambda r=record: _amend_dialog(
                        "新委託價格", r.price,
                        lambda v, rid=r.id: order_book_manager.amend_price(rid, v),
                        decimals=2, step=0.05, minimum=0.01,
                    )).props("dense")
                    ui.button("改量", on_click=lambda r=record: _amend_dialog(
                        "新口數", r.qty,
                        lambda v, rid=r.id: order_book_manager.amend_qty(rid, int(v)),
                        decimals=0, step=1, minimum=1,
                    )).props("dense")
                    ui.button(
                        "刪單", on_click=lambda r=record: order_book_manager.cancel(r.id),
                    ).props("dense outline")
                # rejected/cancelled：不給操作，純顯示，跟舊版一致

    def _fill_row(record) -> None:
        with ui.row().classes("flex-nowrap items-center gap-4 border-b py-1"):
            ui.label(record.label()).classes("shrink-0 whitespace-nowrap")
            ui.label(_right_text(record)).classes("shrink-0 whitespace-nowrap w-16")
            ui.label(_direction_text(record)).classes("shrink-0 whitespace-nowrap w-16")
            ui.label(str(record.fill_price or "")).classes("shrink-0 whitespace-nowrap w-20")
            ui.label(str(record.fill_qty or "")).classes("shrink-0 whitespace-nowrap w-12")
            filled_at = ""
            if record.filled_at:
                filled_at = datetime.datetime.fromtimestamp(record.filled_at).strftime("%Y-%m-%d %H:%M:%S")
            ui.label(filled_at).classes("shrink-0 whitespace-nowrap")

    def _refresh() -> None:
        with client:
            box_container.clear()
            with box_container:
                records = [r for r in order_book_manager.records if r.status != STATUS_FILLED]
                if not records:
                    ui.label("目前沒有委託").classes("text-sm text-grey")
                for record in records:
                    _box_row(record)

            fill_container.clear()
            with fill_container:
                fills = [r for r in order_book_manager.records if r.status == STATUS_FILLED]
                if not fills:
                    ui.label("目前沒有成交").classes("text-sm text-grey")
                for record in fills:
                    _fill_row(record)

    def _on_error(message: str) -> None:
        box_status.text = f"操作失敗：{message}"

    order_book_manager.records_changed.connect(_refresh)
    order_book_manager.order_book_error.connect(_on_error)
    _refresh()  # OrderBookManager 建構時已經從本機檔案載入過既有委託，這裡立刻畫出來，不用等第一次變動

    return box_dialog.open, fill_dialog.open
