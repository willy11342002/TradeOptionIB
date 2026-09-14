"""
NiceGUI 版的選擇權報價盤——NiceGUI 遷移的第一個真正功能頁面，對照舊版
`app/views/main_window.py`(只搬「查詢標的→選到期日→訂閱報價」這條核心
流程，欄位/功能故意精簡，其餘的下單/部位/損益圖/自動平倉等留給後續頁面
接續，見 CLAUDE.md 的路線圖說明)。

兩段式操作，對照舊版 main_window.py 的兩個獨立觸發點(symbol_edit 按
Enter 觸發 `_on_query_symbol()`；expiry_combo 改變觸發
`_on_query_params_changed()`)，這裡故意不做成「選了到期日就自動訂閱」，
使用者要自己按「查詢」才訂閱，行為比較好預期：
    1. 標的代碼輸入框按 Enter → 查現價(訂閱標的股票)＋到期日清單
       (`_query_symbol()`，對照 `_query_symbol_core()`)。
    2. 選到期日之後按「查詢」→ 用現價(還沒查到就用履約價清單正中間值)
       置中，抓上下幾檔訂閱選擇權報價，T 字報價表格才會有內容
       (`_subscribe_current_expiry()`，對照 `_do_subscribe_core()`)。

流程細節對照 `main_window.py::_query_symbol_core()`/`_do_subscribe_core()`：
qualify 標的股票 → `reqSecDefOptParamsAsync` 拿到期日/履約價清單 → 選到
期日後，以現價為中心各抓「上下幾檔＋緩衝」的候選履約價 → qualify 選擇權
合約，過濾掉查無 conId 的(這個到期日沒掛牌這個履約價，qualify 不會丟例
外，只會讓 conId 停在 0，見 `app/models/ib_client.py` 開頭的說明) → 用
`IBQuoteClient` 訂閱。

*** 這是本機單人工具，`IBQuoteClient` 在這個頁面裡是每次進頁面就新建一
個實例(不是 process 內單例)***：跟舊版 Qt app 的「一個視窗一個
IBQuoteClient」概念一致，`IBClient`(IB 連線本身)才是真正跨頁面共用的單
例，由 `main.py`(NiceGUI 進入點)建立一次、傳進來。

整個報價盤是一個置中 `ui.dialog()` modal(跟下單面板/委託簿/成交回報同
一套互動模式)，不是常駐主畫面——股票篩選器改成主畫面之後(見
`web_screener_widget.py` 開頭的說明)，這裡改成標題列按鈕「選擇權報價」
跟候選清單每一列「期權報價」按鈕共用的彈出視窗。

`build()` 回傳 `(quote_client, get_contract, open_dialog)`：
- `quote_client` 自己建立的實例，下單面板要用它讀即時買賣價。
- `get_contract(strike, is_call) -> Optional[Contract]`——對照舊版
  `main_window.py::_get_contract()`，讓下單面板可以查「目前報價盤顯示
  中的其他履約價」的合約物件(組價差單需要「這個履約價±寬度」那一腳，
  不是只有使用者雙擊的那個履約價)，查不到(這個到期日沒有這個履約價/
  根本沒訂閱顯示)回傳 `None`。
- `open_dialog(symbol=None)`——打開這個 dialog；有帶 `symbol` 的話(股
  票篩選器候選清單的「期權報價」按鈕會帶)順便填入標的代碼欄位、直接
  觸發一次查詢，等同使用者自己輸入代碼按 Enter，不用開了 dialog 還要
  再手動打一次代碼。是 `async def`，NiceGUI 的事件派發偵測到 handler
  回傳 awaitable 會自動建立 Task，直接指定給 `on_click` 就好，不需要
  額外用 `spawn()` 包一層。

`build()` 也接受一個可選的 `on_leg_selected` callback——雙擊某一列的
call/put 價格欄位時呼叫，把「這個履約價的 call/put 合約＋目前 bid/
ask＋strike_step」交給呼叫端(`main.py`)接到下單面板上，對照舊版
`main_window.py::_on_cell_double_clicked()` 呼叫
`order_entry_widget.set_context(...)` 那一段，參數順序/意義一比一對
應。
"""
import datetime
from typing import Callable, Optional

from nicegui import ui

from app.models.ib_client import IBClient
from app.models.ib_quote_client import IBQuoteClient
from app.models.option_utils import build_option, build_stock
from app.services import black_scholes
from app.views import web_technical_analysis_panel

_DEFAULT_ROWS = 8

# Delta 反推用的無風險利率——跟 main_window.py::RISK_FREE_RATE 同一個數
# 字(美債短天期利率量級)，這裡沒有共用同一個常數是因為那支模組是 Qt 檔
# 案，之後 main_window.py 整支刪除時這裡不用跟著動。
_RISK_FREE_RATE = 0.04


def _format_expiry_label(expiry: str) -> str:
    """"YYYYMMDD" → "YYYY-MM-DD(剩N天)"，天數算到今天(含到期日當天)，跟
    _compute_delta() 算 time_to_expiry 用的是同一個「到期日減今天」公
    式，兩邊數字要對得起來，不要各自重算一套。"""
    date_str = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    expiry_date = datetime.datetime.strptime(expiry, "%Y%m%d").date()
    days = (expiry_date - datetime.date.today()).days
    return f"{date_str}(剩{days}天)" if days >= 0 else f"{date_str}(已過期)"


def build(ib_client: IBClient, on_leg_selected: Optional[Callable] = None):
    quote_client = IBQuoteClient(ib_client)
    # 使用者重新整理/關掉分頁時，這個 quote_client 物件不會自動消失(還有
    # IB 那邊的市場資料訂閱掛著)——main.py 現在會在重新整理時沿用同一個
    # IBClient 直接跳過連線表單，如果不主動清掉舊分頁留下的訂閱，重新整
    # 理幾次之後 IB 那邊的訂閱數會一直往上疊。`on_disconnect` 是這個分頁
    # 真的斷線(關閉/整理/連線中斷)時才觸發，不是每次資料更新都觸發。
    ui.context.client.on_disconnect(quote_client.unsubscribe_all)
    state = {
        "symbol": None,
        "strikes": [],
        "underlying_key": None,       # str(stock.conId)，用來從 quote_updated 認出「這是現價，不是選擇權報價」
        "underlying_contract": None,  # 保留 Contract 物件，換到期日重新訂閱選擇權時要重新訂閱現價用
        "underlying_price": None,
        "strike_step": 1.0,       # 相鄰履約價差的最小值，給下單面板的價差單預設寬度用
        "row_meta": {},           # symbol_key(conId字串) -> {"strike":..., "side": "call"/"put"}
        "rows_by_strike": {},     # strike -> 表格一列的資料
        "contracts_by_strike": {},  # strike -> {"call": Contract, "put": Contract}，下單面板要用真正的合約物件
    }

    with ui.dialog() as dialog, ui.card().classes("w-[1100px] max-w-full max-h-[90vh] overflow-y-auto gap-2"):
        ui.label("期權報價").classes("text-lg font-semibold")

        # 「選擇權報價」/「技術分析」兩個頁籤共用同一個標的(symbol_input 查
        # 到的那一檔)，但查詢列(標的代碼/到期日/上下各幾檔)本身只有「選擇
        # 權報價」頁籤的 T 字報價表格用得到(履約價/到期日這些欄位)，技術
        # 分析頁籤只是被動接收查到的標的(`ta_set_symbol()`，跟這排欄位的
        # DOM 位置無關)，所以查詢列放進「選擇權報價」頁籤內，不是放在頁籤
        # 切換的上層——使用者要換標的還是得切回這個頁籤，但畫面比較乾淨，
        # 技術分析頁籤不會看到一排跟自己無關的欄位。
        with ui.tabs().classes("w-full") as page_tabs:
            quote_tab = ui.tab("選擇權報價")
            ta_tab = ui.tab("技術分析")
        with ui.tab_panels(page_tabs, value=quote_tab).classes("w-full"):
            with ui.tab_panel(quote_tab).classes("gap-2"):
                with ui.row().classes("items-end gap-4"):
                    symbol_input = ui.input("標的代碼(輸入後按 Enter)").classes("w-48")
                    # 用 readonly 的 ui.input 而不是 ui.label 顯示現價——
                    # 這樣才會跟其他欄位一樣「上面標籤＋下面底線框」的樣
                    # 式跟高度，不會在同一排裡看起來對不齊。
                    price_input = ui.input("現價").props("readonly").classes("w-24")
                    expiry_select = ui.select({}, label="到期日").classes("w-56")
                    rows_input = ui.number("上下各幾檔", value=_DEFAULT_ROWS, format="%d", min=1).classes("w-28")
                    subscribe_btn = ui.button("查詢")
                status_label = ui.label("")
                # AG Grid 的表頭是 flex 容器(.ag-header-cell-label)，一
                # 般的 text-align 對它沒作用，要改 justify-content 才能
                # 讓標題文字置中，這裡用
                # defaultColDef.headerClass="center-header" 掛上這個
                # class，實際樣式用一段 <style> 補上去(NiceGUI 沒有對應
                # 這個的現成參數)。
                ui.add_head_html(
                    "<style>.center-header .ag-header-cell-label { justify-content: center; }</style>"
                )
                grid = ui.aggrid({
                    # *** 一定要給 getRowId，不然 applyTransaction() 的
                    # update 沒辦法比對出「這是哪一列」，只能整批 rowData
                    # 換掉重畫(會閃爍)***：":" 開頭的 key 是 NiceGUI 的
                    # 慣例，值是一段 JS expression 字串，client 端
                    # (aggrid.js)會用它 new Function 組成真正的 JS
                    # callback，履約價在同一個表格裡不會重複，拿來當
                    # row id 剛好。
                    ":getRowId": "params => String(params.data.strike)",
                    # *** 欄寬用 sizeColumnsToFit()，不要靠
                    # defaultColDef.flex ***：flex 理論上該有效，但實測
                    # (含 AG Grid 34 這個版本)在這種巢狀欄位分組
                    # (children)+ tab/drawer 容器裡就是量不出正確寬度，
                    # 每欄卡死在 AG Grid 的預設寬 200px，9 欄合計比容器
                    # 寬多了，逼出欄位虛擬捲動，畫面上只看得到兩欄、標題
                    # 也被截斷成「成...」「De...」。改叫
                    # `sizeColumnsToFit()`(AG Grid 從第一版就有的 API，
                    # 不依賴新版 Theming API 的 autoSizeStrategy)最可
                    # 靠：`onGridReady`(grid 第一次建好)跟
                    # `onGridSizeChanged`(容器大小改變，例如切換右側抽
                    # 屜/切換頁籤)都重新呼叫一次，兩邊都是 ":" 開頭的
                    # NiceGUI 慣例(JS expression 字串，client 端會 new
                    # Function 組成真正的 callback)。
                    ":onGridReady": "params => params.api.sizeColumnsToFit()",
                    ":onGridSizeChanged": "params => params.api.sizeColumnsToFit()",
                    # cellStyle 置中儲存格內容；headerClass 只負責掛一個
                    # CSS class，真正讓「標題文字」置中還要靠下面
                    # add_head_html 注入的樣式(AG Grid 的表頭是 flex 容
                    # 器，純 text-align 對它無效，要改 justify-content)。
                    "defaultColDef": {"minWidth": 72, "cellStyle": {"textAlign": "center"}, "headerClass": "center-header"},
                    # 欄位順序/中文名稱對照舊版 main_window.py 的
                    # COLUMNS 排法(由外到內：賣價/買價/成交價/Delta，履
                    # 約價在正中間，Put 側鏡像對稱)，買權/賣權用 AG
                    # Grid 的欄位分組(children)在上面疊一層群組標題，跟
                    # 舊版「買權(Call)／賣權(Put)」左右兩塊標籤是同一個
                    # 概念。
                    "columnDefs": [
                        {
                            "headerName": "買權(Call)",
                            "children": [
                                {"headerName": "賣價", "field": "call_ask"},
                                {"headerName": "買價", "field": "call_bid"},
                                {"headerName": "成交價", "field": "call_last"},
                                {"headerName": "Delta", "field": "call_delta"},
                            ],
                        },
                        {"headerName": "履約價", "field": "strike", "cellClass": "font-bold"},
                        {
                            "headerName": "賣權(Put)",
                            "children": [
                                {"headerName": "Delta", "field": "put_delta"},
                                {"headerName": "成交價", "field": "put_last"},
                                {"headerName": "買價", "field": "put_bid"},
                                {"headerName": "賣價", "field": "put_ask"},
                            ],
                        },
                    ],
                    "rowData": [],
                }).classes("w-full h-96")
            with ui.tab_panel(ta_tab).classes("gap-2"):
                ta_set_symbol, ta_set_visible = web_technical_analysis_panel.build(ib_client)

        # 技術分析頁籤裡的 Plotly 圖表一定要等頁籤真的切過來(容器有實際寬
        # 高)才能第一次塞K線資料，見 `web_technical_analysis_panel.py::
        # build()` 開頭的說明——這裡把頁籤切換事件轉給那支模組的
        # set_visible()，讓它自己決定「使用者切過去時，要不要因為剛好有
        # 一筆還沒畫的新標的資料，補畫一次」。
        async def _on_page_tab_change(e) -> None:
            # NiceGUI 的 Tabs/TabPanels 送出來的 e.value 是 Tab 的
            # name(字串)，不是 Tab 物件本身(見
            # nicegui/elements/tabs.py::Tabs._value_to_event_value())，
            # 拿 e.value 直接比對 ta_tab 這個物件永遠是 False。
            await ta_set_visible(e.value == ta_tab.props["name"])

        page_tabs.on_value_change(_on_page_tab_change)

    def _row_for(strike: float) -> dict:
        return state["rows_by_strike"].setdefault(strike, {
            "strike": strike,
            "call_bid": None, "call_ask": None, "call_last": None, "call_delta": None,
            "put_bid": None, "put_ask": None, "put_last": None, "put_delta": None,
        })

    def _compute_delta(row: dict, side: str) -> Optional[float]:
        """拿買賣中價反推隱含波動率、算出 Delta——對照舊版
        main_window.py::_recompute_delta() 同一套算法(Black-Scholes 反推
        IV，近似值，不是交易所/券商提供的即時 Greeks)。"""
        underlying_price = state["underlying_price"]
        if underlying_price is None:
            return None
        bid, ask, last = row.get(f"{side}_bid"), row.get(f"{side}_ask"), row.get(f"{side}_last")
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif last and last > 0:
            mid = last
        else:
            return None
        expiry = expiry_select.value
        if expiry is None:
            return None
        expiry_date = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        days = (expiry_date - datetime.date.today()).days
        if days <= 0:
            return None
        time_to_expiry = days / 365.0
        is_call = side == "call"
        iv = black_scholes.implied_vol(is_call, underlying_price, row["strike"], _RISK_FREE_RATE, time_to_expiry, mid)
        if iv is None:
            return None
        return black_scholes.delta(is_call, underlying_price, row["strike"], _RISK_FREE_RATE, time_to_expiry, iv)

    def _update_row_delta(row: dict, side: str) -> None:
        d = _compute_delta(row, side)
        row[f"{side}_delta"] = round(d, 2) if d is not None else None

    def _replace_all_rows() -> None:
        """整批換掉 rowData(換標的/換到期日重新訂閱這種「整批重來」的情
        境才用這個，一定會整個表格重畫一次)，跟下面 `_update_rows()` 的
        差別是後者只動被改到的那幾列，平常報價更新走那條，不要接錯。"""
        grid.options["rowData"] = [state["rows_by_strike"][s] for s in sorted(state["rows_by_strike"])]
        grid.update()

    def _update_rows(rows: list) -> None:
        """一或多列報價更新用——呼叫 AG Grid 自己的 `applyTransaction()`
        API 只更新這幾列的 cell，不重畫整個表格。之前每個 tick 都整批換
        rowData + grid.update()，等於每次 tick 都把整個表格砍掉重建，畫
        面會整片閃爍(尤其同時訂閱二三十檔、tick 頻率很高的時候)。不用
        await(fire-and-forget)：這裡是從 Signal 的同步 callback 呼叫，
        沒有事件迴圈可以 await。"""
        if rows:
            grid.run_grid_method("applyTransaction", {"update": rows})

    def _on_quote_updated(symbol_key: str, data: dict) -> None:
        if symbol_key == state["underlying_key"]:
            price = data.get("last")
            if price is not None:
                state["underlying_price"] = price
                price_input.value = f"{price:g}"
                # 現價變了，所有正在顯示的列(call/put 兩側)的 Delta 都要
                # 跟著重算——這裡刻意集中在一次 applyTransaction 裡送出去
                # (而不是每一列各呼叫一次)，避免現價一跳、瞬間送出一大串
                # grid method 呼叫。
                rows = list(state["rows_by_strike"].values())
                for row in rows:
                    _update_row_delta(row, "call")
                    _update_row_delta(row, "put")
                _update_rows(rows)
            return
        meta = state["row_meta"].get(symbol_key)
        if meta is None:
            return  # 已經換過標的/到期日，這是舊訂閱殘留的最後幾筆 tick，不理它
        row = _row_for(meta["strike"])
        side = meta["side"]
        row[f"{side}_bid"] = data.get("bid")
        row[f"{side}_ask"] = data.get("ask")
        row[f"{side}_last"] = data.get("last")
        _update_row_delta(row, side)
        _update_rows([row])

    quote_client.quote_updated.connect(_on_quote_updated)

    async def _query_symbol() -> None:
        symbol = symbol_input.value.strip().upper()
        if not symbol:
            return
        symbol_input.disable()
        status_label.text = f"查詢 {symbol} 的選擇權鏈中..."
        try:
            stock = build_stock(symbol)
            await ib_client.ib.qualifyContractsAsync(stock)
            if not stock.conId:
                status_label.text = f"查不到標的 {symbol}，確認代碼是否正確"
                return
            chains = await ib_client.ib.reqSecDefOptParamsAsync(symbol, "", "STK", stock.conId)
            chain = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == symbol), None)
            if chain is None:
                status_label.text = f"{symbol} 查不到標準選擇權鏈(SMART)"
                return

            quote_client.unsubscribe_all()
            state["rows_by_strike"].clear()
            state["row_meta"].clear()
            _replace_all_rows()

            state["symbol"] = symbol
            state["strikes"] = sorted(chain.strikes)
            # 相鄰履約價差的最小值，跟 main_window.py 同一個公式，價差單
            # 預設寬度用。
            if len(state["strikes"]) >= 2:
                diffs = [b - a for a, b in zip(state["strikes"], state["strikes"][1:])]
                state["strike_step"] = min(diffs) if diffs else 1.0
            else:
                state["strike_step"] = 1.0
            state["underlying_key"] = str(stock.conId)
            state["underlying_contract"] = stock
            state["underlying_price"] = None
            price_input.value = "查詢中..."

            expirations = sorted(chain.expirations)
            options = {e: _format_expiry_label(e) for e in expirations}
            expiry_select.set_options(options, value=expirations[0] if expirations else None)
            status_label.text = f"{symbol} 查詢完成，共 {len(expirations)} 個到期日，選好到期日後按「查詢」訂閱報價"

            # 訂閱標的股票現價——跟選擇權報價共用同一個 quote_client/
            # quote_updated，_on_quote_updated() 靠 underlying_key 分辨這
            # 是現價還是某一腳選擇權的報價。prime_fallback 是主動排一次
            # 退回查詢(reqHistoricalData)，不等 pendingTickersEvent 先來
            # 一次 tick 才觸發，見 IBQuoteClient.prime_fallback() 的說明。
            quote_client.subscribe([stock])
            quote_client.prime_fallback(stock)
            # 技術分析頁籤只看標的走勢，跟到期日/履約價無關，查到新標的
            # 就一起刷新，不用等使用者自己切過去那個頁籤才觸發。
            await ta_set_symbol(symbol, stock)
        finally:
            symbol_input.enable()

    async def _subscribe_current_expiry() -> None:
        expiry = expiry_select.value
        strikes = state["strikes"]
        if expiry is None or not strikes:
            status_label.text = "請先輸入標的代碼並按 Enter 查詢"
            return
        subscribe_btn.disable()
        try:
            rows_n = int(rows_input.value)
            # 用現價置中(還沒查到現價就退回用履約價清單正中間值)，多抓
            # 「上下幾檔＋緩衝」的候選，qualify 完之後過濾掉這個到期日查
            # 無 conId 的履約價，再各取最靠近中心的 rows_n 檔湊滿——跟
            # main_window.py::_do_subscribe_core() 同一套邏輯。
            center = state["underlying_price"]
            if center is None:
                center = strikes[len(strikes) // 2]
            nearest = min(strikes, key=lambda s: abs(s - center))
            idx = strikes.index(nearest)
            margin = rows_n
            lo = max(0, idx - rows_n - margin)
            hi = min(len(strikes), idx + rows_n + margin + 1)
            candidates = strikes[lo:hi]

            status_label.text = "查詢合約中..."
            # unsubscribe_all() 會連標的股票的現價訂閱一起取消掉，所以取
            # 消後要重新訂閱一次現價——不能只取消選擇權合約，
            # IBQuoteClient 沒有提供「只取消部分訂閱」以外的選擇性介面，
            # 全部取消再重新訂閱現價，寫法比較單純。
            quote_client.unsubscribe_all()
            state["rows_by_strike"].clear()
            state["row_meta"].clear()
            state["contracts_by_strike"].clear()
            _replace_all_rows()
            if state["underlying_contract"] is not None:
                quote_client.subscribe([state["underlying_contract"]])

            call_contracts = [build_option(state["symbol"], expiry, s, "C") for s in candidates]
            put_contracts = [build_option(state["symbol"], expiry, s, "P") for s in candidates]
            await ib_client.ib.qualifyContractsAsync(*call_contracts, *put_contracts)

            valid = [
                (strike, call, put)
                for strike, call, put in zip(candidates, call_contracts, put_contracts)
                if call.conId and put.conId
            ]
            below = [v for v in valid if v[0] < nearest][-rows_n:]
            at_or_above = [v for v in valid if v[0] >= nearest][: rows_n + 1]
            chosen = below + at_or_above
            if not chosen:
                status_label.text = f"{expiry} 這個到期日在目前的履約價範圍內查不到任何合約"
                return

            symbols = []
            for strike, call, put in chosen:
                state["row_meta"][str(call.conId)] = {"strike": strike, "side": "call"}
                state["row_meta"][str(put.conId)] = {"strike": strike, "side": "put"}
                state["contracts_by_strike"][strike] = {"call": call, "put": put}
                _row_for(strike)
                symbols.append(call)
                symbols.append(put)
            _replace_all_rows()
            quote_client.subscribe(symbols)
            status_label.text = f"已訂閱 {len(chosen)} 檔履約價"
        finally:
            subscribe_btn.enable()

    def _on_cell_double_clicked(e) -> None:
        if on_leg_selected is None:
            return
        args = e.args
        col_id = args.get("colId", "")
        if not (col_id.startswith("call_") or col_id.startswith("put_")):
            return  # 雙擊履約價欄位本身不做任何事，跟舊版一致
        strike = args.get("data", {}).get("strike")
        contracts = state["contracts_by_strike"].get(strike)
        if contracts is None:
            return
        row = state["rows_by_strike"].get(strike, {})
        on_leg_selected(
            contracts["call"], contracts["put"], col_id.startswith("call_"),
            row.get("call_bid"), row.get("call_ask"), row.get("put_bid"), row.get("put_ask"),
            state["strike_step"],
        )

    def _get_contract(strike: float, is_call: bool):
        contracts = state["contracts_by_strike"].get(strike)
        if contracts is None:
            return None
        return contracts["call"] if is_call else contracts["put"]

    async def open_dialog(symbol: Optional[str] = None) -> None:
        dialog.open()
        if symbol:
            symbol_input.value = symbol.strip().upper()
            await _query_symbol()

    symbol_input.on("keydown.enter", lambda _e: _query_symbol())
    subscribe_btn.on_click(_subscribe_current_expiry)
    grid.on("cellDoubleClicked", _on_cell_double_clicked)

    return quote_client, _get_contract, open_dialog
