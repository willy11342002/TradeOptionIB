"""
NiceGUI 版下單面板，取代 `app/views/order_entry_widget.py`
(`OrderEntryWidget(QWidget)`)。兩個分頁對照舊版：裸買賣(單腳)／價差單
(垂直價差兩腳)。

*** 送出按鈕只是「暫存」到委託簿(STATUS_STAGED)，不是真的送出去 IB
***：跟舊版一致，`app/models/order_book.py::OrderBookManager` 本來就是
兩段式確認設計(暫存→委託簿裡按「送出」才真的 `confirm_send()`)，這裡
不重新發明、也不能跳過這一步。

跟舊版 `main_window.py` 一樣，這支模組完全不獨立呼叫 IB API 查合約/確
認 conId——所有合約物件都是報價盤已經 qualify 過、目前顯示中的(靠呼叫
端傳進來的 `get_contract(strike, is_call)`，對照舊版
`main_window.py::_get_contract()`)。價差單換算出來的另一腳履約價如果
不在報價盤目前顯示範圍內，`get_contract` 會回傳 `None`，這裡直接顯示
錯誤訊息，不會另外發一次 IB 查詢去補——跟舊版限制一致，是刻意的，不是
遺漏。

*** 明確不搬的東西 ***：舊版雙擊儲存格後，報價盤儲存格會畫一個橘色外
框標示「這個價格正在餵給下單面板」(`active_legs_changed` 訊號 +
`_ActivePriceCellDelegate`)。這是純視覺回饋，不影響下單邏輯，這次先不
做，之後真的需要再補。

畫面是一個置中的 `ui.dialog()` modal，不是常駐在畫面上的固定面板——
`build()` 回傳 `(set_context, open_dialog)`：`open_dialog` 給呼叫端
(`main.py`)接到標題列自己的按鈕上；`set_context` 除了原本更新表單內容
之外，也會自動 `dialog.open()`，雙擊報價盤選商品時直接跳出視窗，不用
使用者自己再按一次按鈕。
"""
from typing import Callable, Optional

from nicegui import ui

from app.models.ib_quote_client import IBQuoteClient
from app.models.option_utils import vertical_spread_legs
from app.models.order_book import OrderBookManager

_TIF_CHOICES = ["DAY", "GTC", "IOC", "FOK"]
# 寬度選項對照舊版 order_entry_widget.py 的 spread_points_combo。
_WIDTH_OPTIONS = {1.0: "1", 2.5: "2.5", 5.0: "5", 10.0: "10", 25.0: "25", 50.0: "50"}


def build(order_book_manager: OrderBookManager, quote_client: IBQuoteClient, get_contract: Callable) -> tuple:
    state = {
        "call_contract": None,
        "put_contract": None,
        "outright_contract": None,  # 裸買賣分頁用的那一腳(雙擊 call 欄位就是 call_contract，反之亦然)
    }

    def _cached_bid_ask(contract) -> tuple:
        if contract is None:
            return None, None
        data = quote_client.get_cached(str(contract.conId)) or {}
        return data.get("bid"), data.get("ask")

    with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-full gap-2"):
        title_label = ui.label("下單面板(雙擊報價盤的價格欄位選擇商品)").classes("text-lg font-semibold")

        with ui.tabs().classes("w-full") as tabs:
            outright_tab = ui.tab("裸買賣")
            duplex_tab = ui.tab("價差單")

        with ui.tab_panels(tabs, value=outright_tab).classes("w-full"):
            with ui.tab_panel(outright_tab):
                with ui.row().classes("items-end gap-4"):
                    out_side = ui.select({True: "買進", False: "賣出"}, value=True, label="方向").classes("w-24")
                    out_price = ui.number("價格", value=0.0, format="%.2f", step=0.05, min=0.01).classes("w-28")
                    out_qty = ui.number("口數", value=1, format="%d", min=1).classes("w-24")
                    out_tif = ui.select(_TIF_CHOICES, value="DAY", label="委託條件").classes("w-24")
                    out_submit_btn = ui.button("暫存到委託簿")
                out_status = ui.label("").classes("text-sm")

            with ui.tab_panel(duplex_tab):
                with ui.row().classes("items-end gap-4"):
                    spread_right = ui.select({"C": "買權", "P": "賣權"}, value="C", label="買權/賣權").classes("w-24")
                    spread_side = ui.select({True: "買方", False: "賣方"}, value=True, label="方向").classes("w-24")
                    spread_width = ui.select(_WIDTH_OPTIONS, value=1.0, label="寬度").classes("w-24")
                    spread_price = ui.number("淨價格", value=0.0, format="%.2f", step=0.05, min=0.01).classes("w-28")
                    spread_qty = ui.number("口數", value=1, format="%d", min=1).classes("w-24")
                    spread_tif = ui.select(_TIF_CHOICES, value="DAY", label="委託條件").classes("w-24")
                    spread_submit_btn = ui.button("暫存到委託簿")
                spread_status = ui.label("").classes("text-sm")

    def _autofill_outright_price() -> None:
        contract = state["outright_contract"]
        if contract is None:
            return
        bid, ask = _cached_bid_ask(contract)
        if not out_price.value:  # 只在欄位是空的/0 的時候自動帶入，不要蓋掉使用者手動改過的價格
            price = ask if out_side.value else bid
            if price:
                out_price.value = round(price, 2)

    def _leg_cost(contract, buy: bool) -> Optional[float]:
        """單腳的「成本」：買方是付出的 ask(正)，賣方是收到的 bid(負)，
        兩腳加總取絕對值就是淨價差的debit/credit，公式照抄舊版
        order_entry_widget.py::_leg_cost()。"""
        bid, ask = _cached_bid_ask(contract)
        if buy:
            return ask if ask and ask > 0 else None
        return -bid if bid and bid > 0 else None

    def _resolve_duplex_legs():
        """回傳 ((leg1_contract, buy1), (leg2_contract, buy2))，查不到某
        一腳合約(這個到期日的履約價範圍沒涵蓋算出來的另一腳履約價)回傳
        None。"""
        call_contract = state["call_contract"]
        if call_contract is None:
            return None
        anchor_strike = call_contract.strike
        right = spread_right.value
        width = spread_width.value
        buy_spread = spread_side.value
        (low_strike, low_action), (high_strike, high_action) = vertical_spread_legs(
            anchor_strike, width, right, buy_spread,
        )
        is_call = right == "C"
        low_contract = get_contract(low_strike, is_call)
        high_contract = get_contract(high_strike, is_call)
        if low_contract is None or high_contract is None:
            return None
        return (low_contract, low_action == "BUY"), (high_contract, high_action == "BUY")

    def _autofill_duplex_price() -> None:
        legs = _resolve_duplex_legs()
        if legs is None:
            return
        (leg1, buy1), (leg2, buy2) = legs
        cost1 = _leg_cost(leg1, buy1)
        cost2 = _leg_cost(leg2, buy2)
        if cost1 is None or cost2 is None:
            return
        if not spread_price.value:
            spread_price.value = round(max(abs(cost1 + cost2), 0.01), 2)

    def set_context(
        call_contract, put_contract, is_call: bool,
        call_bid, call_ask, put_bid, put_ask, strike_step: float,
    ) -> None:
        """報價盤雙擊某個 call/put 價格欄位時呼叫(見
        web_quote_board_page.py 的 on_leg_selected)，參數對照舊版
        order_entry_widget.py::set_context()。"""
        state["call_contract"] = call_contract
        state["put_contract"] = put_contract
        state["outright_contract"] = call_contract if is_call else put_contract

        outright = state["outright_contract"]
        local_symbol = getattr(outright, "localSymbol", "") or outright.symbol
        title_label.text = f"下單面板——{local_symbol}"

        # 寬度預設值：跟這次雙擊之前記錄的相鄰履約價差最接近的選項。
        spread_width.value = min(_WIDTH_OPTIONS, key=lambda w: abs(w - strike_step))
        spread_right.value = "C" if is_call else "P"

        out_price.value = 0.0
        spread_price.value = 0.0
        out_status.text = ""
        spread_status.text = ""
        _autofill_outright_price()
        _autofill_duplex_price()
        dialog.open()

    def _on_outright_side_change() -> None:
        out_price.value = 0.0
        _autofill_outright_price()

    def _on_duplex_params_change() -> None:
        spread_price.value = 0.0
        _autofill_duplex_price()

    def _on_stage_outright() -> None:
        contract = state["outright_contract"]
        if contract is None:
            out_status.text = "請先雙擊報價盤的價格欄位選擇商品"
            return
        order_book_manager.stage_outright(contract, out_side.value, out_price.value, out_qty.value, out_tif.value)
        out_status.text = "已暫存到委託簿(還沒送出，去委託簿按「送出」)"

    def _on_stage_duplex() -> None:
        legs = _resolve_duplex_legs()
        if legs is None:
            spread_status.text = "這個寬度算出來的另一腳履約價目前不在報價盤顯示範圍內，改小寬度或先訂閱涵蓋範圍更大的履約價"
            return
        (leg1, buy1), (leg2, buy2) = legs
        order_book_manager.stage_duplex(
            leg1, buy1, leg2, buy2, spread_price.value, spread_qty.value,
            tif=spread_tif.value, net_buyer=spread_side.value,
        )
        spread_status.text = "已暫存到委託簿(還沒送出，去委託簿按「送出」)"

    out_side.on_value_change(lambda _e: _on_outright_side_change())
    spread_right.on_value_change(lambda _e: _on_duplex_params_change())
    spread_side.on_value_change(lambda _e: _on_duplex_params_change())
    spread_width.on_value_change(lambda _e: _on_duplex_params_change())
    out_submit_btn.on_click(_on_stage_outright)
    spread_submit_btn.on_click(_on_stage_duplex)

    return set_context, dialog.open
