"""
NiceGUI 版委託簿，取代 `app/views/order_book_widgets.py`
(`OrderBookWidget`/`FillReportWidget`)。

*** 用一筆委託一張卡片(`ui.row` + `ui.button`)，不是 `ui.aggrid`/
`ui.table` ***：委託筆數少、狀態變化是人類操作等級的頻率(不像報價那種
高頻 tick)，不需要 AG Grid 的效能/虛擬捲動能力；`records_changed` 觸發
時整個容器清空重畫，完全沒有報價盤那種閃爍疑慮，也不用碰 Quasar table
body-slot 樣板語法，複雜度低很多。

*** 已成交的委託留在委託簿裡，不再另外開一個「成交回報」視窗 ***(使用
者要求「這樣更簡潔」，原本兩個視窗顯示的其實是同一份 `OrderBookManager.
records`，只是用 `status` 篩過)：`_box_row()` 對 `STATUS_FILLED` 的委託
顯示 `fill_price`/`fill_qty`(實際成交價/量，可能跟原本掛的價格/數量不
同)，不是 `record.price`/`record.qty`；`build()` 現在只回傳一個
`open_box_dialog`，呼叫端(`main.py`)也把「成交回報」那顆按鈕整個拿掉。

*** dialog 本身的 UI 延後到第一次真的要開啟才建 ***：跟
`web_quote_board_page.py` 同一個理由(見該檔開頭/`app/services/
lazy_ui.py` 的完整說明)——這裡面嵌了一份 `web_payoff_chart_widget.py`
的 Plotly 到期損益圖，建構本身有實際成本，不需要在 `main.py::index()`
連線成功的當下就先建完。
"""
from typing import Callable

from nicegui import ui

from app.models.order_book import (
    OrderBookManager, STATUS_STAGED, STATUS_LIVE, STATUS_FILLED, STATUS_REJECTED, STATUS_CANCELLED,
)
from app.models.positions import PositionManager
from app.services.lazy_ui import lazy_open
from app.views import web_payoff_chart_widget

_STATUS_LABELS = {
    STATUS_STAGED: "待送出",
    STATUS_LIVE: "掛單中",
    STATUS_FILLED: "已成交",
    STATUS_REJECTED: "失敗",
    "cancelled": "已取消",
}
_RIGHT_LABELS = {"C": "買權", "P": "賣權"}

# *** 標題列跟每筆資料列都要套同一組固定欄寬的 grid-cols，不能用
# flex+gap 各自依內容自然寬度排 ***(使用者實測回報：flex 版本每欄寬度是
# 各自那一列自己的內容決定的，商品欄位在標題列只有兩個字「商品」、在資
# 料列卻是一整串履約價/買賣描述，兩列的第二欄從此對不上)。grid-cols 用
# 絕對寬度(不是內容自動撐開)，同一組模板套在標題列跟每筆資料列上，欄位
# 邊界才會是絕對位置、不受那一列自己內容長短影響。商品欄位只顯示「代碼+
# 到期日」(見 _symbol_text())，不是完整 OCC 編碼字串，寬度不用留太大。
#
# *** 動作/狀態故意排在最後兩欄、且動作在狀態前面 ***：狀態文字被拒單時
# 會接上 IB 原始錯誤訊息(可能一大段)，寬度沒辦法像其他欄位那樣預估——放
# 在真正的最後一欄，內容多長都只是把這一列拉長，不會擠到後面任何欄位；
# 動作欄(按鈕組數量隨狀態變化，已成交的委託沒有任何按鈕)給固定寬度而不
# 是最後一欄，這樣不管哪一列有幾顆按鈕，後面的狀態欄永遠從同一個 x 位置
# 開始，不會因為這一列按鈕比較少/比較多而跟其他列的狀態欄對不齊。
_BOX_GRID_COLS = "grid-cols-[140px_90px_64px_64px_80px_50px_70px_200px_max-content]"


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


def _format_expiry(expiry: str) -> str:
    if len(expiry) == 8 and expiry.isdigit():  # IB 原始格式 "YYYYMMDD"
        return f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    return expiry


def _symbol_text(record) -> str:
    """委託簿的「商品」欄位：只顯示底層代碼+到期日(使用者反饋原始
    local_symbol 那種 OCC 編碼字串「260921C00312500 買」看不懂)——履約
    價/類型/方向已經拆成獨立欄位，不需要在這裡重複塞。複式單兩腳到期日
    理論上一定相同(vertical_spread_legs() 只換履約價、不換到期日)，取
    第一腳就好，不用兩腳都顯示。"""
    leg = record.legs[0]
    return f"{leg.symbol} {_format_expiry(leg.expiry)}".strip()


def _strike_text(record) -> str:
    """複式單兩腳履約價不同，用 "/" 併在一起顯示(例如 "335/337.5")，裸
    買賣只有一腳就是單一數字——跟 record.label() 裡已經內嵌的履約價是同
    一個數字，這裡只是把它拆成獨立欄位方便對照，不是另一套算法。"""
    strikes = [leg.strike for leg in record.legs if leg.strike is not None]
    return "/".join(f"{s:g}" for s in strikes)


def build(order_book_manager: OrderBookManager, position_manager: PositionManager) -> Callable:
    def _build_dialog() -> Callable:
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

        # *** 卡片寬度用 w-fit，不要再固定 w-[900px] ***(使用者要求)：複式單
        # 那排(商品/履約價/類型/方向/價格/口數/委託條件/動作/狀態)欄位加起來
        # 常常比 900px 窄，固定寬度浪費空間；反過來欄位多、狀態欄的拒單訊息
        # 很長的時候又會超過 900px，還是得靠 box_container 的
        # overflow-x-auto 橫向捲動(這則回報的真正起因)。w-fit 讓卡片直接長
        # 到「剛好放得下目前這批列」的寬度，max-w-[95vw] 只在真的超過螢幕寬
        # 度時才啟動 overflow-x-auto 這個備案，不是常態。
        with ui.dialog() as box_dialog, ui.card().classes("w-fit max-w-[95vw] gap-2"):
            ui.label("委託簿").classes("text-lg font-semibold")
            box_status = ui.label("").classes("text-sm text-negative")
            # 到期損益圖(使用者要求)：目前部位+委託簿裡所有待成交委託一起算
            # 進去，見 web_payoff_chart_widget.py 開頭的說明——跟 Qt 版同一套
            # app/services/payoff.py 數學，這裡只是換成 Plotly 畫。
            web_payoff_chart_widget.build(position_manager, order_book_manager)
            ui.separator()
            # 欄位名稱列：跟 _box_row() 用同一組 _BOX_GRID_COLS 固定欄寬，跟成
            # 交回報那份是同一套道理(見 _FILL_GRID_COLS/_BOX_GRID_COLS 定義處
            # 的說明)。
            with ui.element("div").classes(f"grid {_BOX_GRID_COLS} items-center gap-4 text-xs text-grey"):
                ui.label("商品").classes("whitespace-nowrap")
                ui.label("履約價").classes("whitespace-nowrap")
                ui.label("類型").classes("whitespace-nowrap")
                ui.label("方向").classes("whitespace-nowrap")
                ui.label("價格").classes("whitespace-nowrap")
                ui.label("口數").classes("whitespace-nowrap")
                ui.label("委託條件").classes("whitespace-nowrap")
                ui.label("動作").classes("whitespace-nowrap")
                ui.label("狀態").classes("whitespace-nowrap")
            box_container = ui.column().classes("w-full gap-1 overflow-x-auto")

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
            # 跟 _refresh() 讀到的同一份 records，見 _BOX_GRID_COLS 定義處的
            # 說明——動作欄(按鈕數量隨狀態變化)排在狀態欄前面、給固定寬度，
            # 狀態欄(拒單訊息可能很長)排最後一欄，兩者順序不能對換。
            # 已成交(STATUS_FILLED)顯示的是實際成交價/量(fill_price/
            # fill_qty)，不是原本掛的 price/qty——兩者可能不一樣(限價單成交
            # 價不會比掛的價格差，但不保證剛好等於掛的價格)。
            if record.status == STATUS_FILLED and record.fill_price is not None:
                price_text = f"{record.fill_price:g}"
            else:
                price_text = f"{record.price:g}"
            qty_text = str(record.fill_qty) if record.status == STATUS_FILLED and record.fill_qty is not None else str(record.qty)
            with ui.element("div").classes(f"grid {_BOX_GRID_COLS} items-center gap-4 border-b py-1"):
                ui.label(_symbol_text(record)).classes("whitespace-nowrap")
                ui.label(_strike_text(record)).classes("whitespace-nowrap")
                ui.label(_right_text(record)).classes("whitespace-nowrap")
                ui.label(_direction_text(record)).classes("whitespace-nowrap")
                ui.label(price_text).classes("whitespace-nowrap")
                ui.label(qty_text).classes("whitespace-nowrap")
                ui.label(record.tif).classes("whitespace-nowrap")
                with ui.row().classes("flex-nowrap gap-1"):
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
                    elif record.status in (STATUS_REJECTED, STATUS_CANCELLED):
                        # *** 這裡呼叫的是 OrderBookManager.delete()，不是
                        # discard_staged() ***：discard_staged() 只認
                        # STATUS_STAGED，對這兩種終態什麼都不會做(見它的
                        # 判斷式)。delete() 才是通用版——對已經是終態的紀錄，
                        # 不會再去呼叫 IB cancelOrder()(那段判斷式只在
                        # STATUS_LIVE 時才動作)，單純把這筆從
                        # `_records`/本機保存檔案移掉，不然失敗/取消的委託
                        # 沒有任何管道可以從畫面上清掉，會一直卡在委託簿裡
                        # (使用者原始回報)。
                        ui.button(
                            "刪除", on_click=lambda r=record: order_book_manager.delete(r.id),
                        ).props("dense outline")
                status_text = _STATUS_LABELS.get(record.status, record.status)
                if record.status == STATUS_REJECTED and record.error_msg:
                    status_text += f"({record.error_msg})"
                ui.label(status_text).classes("whitespace-nowrap")

        def _refresh() -> None:
            with client:
                box_container.clear()
                with box_container:
                    records = order_book_manager.records
                    if not records:
                        ui.label("目前沒有委託").classes("text-sm text-grey")
                    for record in records:
                        _box_row(record)

        def _on_error(message: str) -> None:
            box_status.text = f"操作失敗：{message}"

        order_book_manager.records_changed.connect(_refresh)
        order_book_manager.order_book_error.connect(_on_error)
        _refresh()  # OrderBookManager 建構時已經從本機檔案載入過既有委託，這裡立刻畫出來，不用等第一次變動

        return box_dialog.open

    return lazy_open(_build_dialog)
