"""
NiceGUI 版股票篩選器，取代 `app/views/screener_widget.py`(PyQt5 版)及其搭
配的 `scanner_filter_picker.py`/`scan_code_picker.py`/
`filter_assistant_dialog.py`/`scan_history_widgets.py`——功能行為對照舊版
1:1 搬過來，畫面改成 NiceGUI 慣用寫法：

    - 整個篩選器是一個置中 `ui.dialog()` modal(跟下單面板/委託簿同一套
      互動模式)，`build()` 回傳 `open_dialog` 給 `main.py` 接到標題列自
      己的按鈕上。
    - 兩個階段(初篩/自選清單)用 `ui.tabs()`/`ui.tab_panels()` 取代舊版
      `QTabWidget`。
    - 「新增篩選條件」「選擇掃描代碼」「AI 條件建議」「查看詳細」「加入
      自選」「管理自選清單」都是巢狀 `ui.dialog()`，取代舊版四支獨立的
      QDialog 檔案——這裡沒有拆成好幾支檔案，NiceGUI 的 `build()` 本來
      就是閉包風格，硬拆檔案只會讓一堆內部狀態要用參數傳來傳去，不會比
      較清楚。
    - 候選清單/自選清單成分股用手刻的 `ui.row()`(勾選框+移除鈕)，不是
      `ui.aggrid`——跟 `web_order_book_widgets.py` 同樣的理由：筆數少、
      需要每列各自的勾選框/按鈕，AG Grid 的效能優勢用不到。
    - 條件列用 `ui.row().classes("flex-wrap")` 取代舊版
      `widget_helpers.py::FlowLayout`——CSS flexbox 本來就有自動換行，
      不需要另外刻一套版面演算法。

*** 沒有「選擇權天期/流動性復篩」這個階段(舊版曾經有過②選擇權復篩頁
籤，對每檔候選標的查選定天期/ATM履約價的買賣價差)***：初篩(IB 市場掃
描器)已經有 OPTVOLUME(選擇權成交量，預設篩選條件之一)、也可以自己加
IMPVOLAT(隱含波動率)當標的層級的粗篩，精確到「某個到期日、某個履約
價」買賣價差多寬這個層級的複篩被判定用不上，只留初篩這一關。
`app/models/screener.py::screen_one()`/`ScreenFilters` 本身沒有跟著
刪——`app/views/screener_widget.py`(PyQt5 舊版)還在用，那支檔案是漸進
式遷移過程中還在跑的舊路徑(見 CLAUDE.md 的路線圖說明)，不能因為這裡不
用了就整支刪掉。

*** 所有巢狀 dialog 都只在 `build()` 最外層建立一次(結構固定，不會被任
何容器的 `clear()` 波及)，每次要開啟前才清空/重新填內容 ***：對照
`web_order_book_widgets.py::dialog_anchor` 的說明——事件 handler 觸發時
NiceGUI 會把「目前 slot」還原成「觸發這個事件的元件所在的 slot」，如果
在按鈕的 on_click 裡才臨時 `with ui.dialog():` 建一個新的，這個 dialog
的內容可能被種進一個之後會被 `clear()` 掉的容器裡，稍後那個容器一
clear()，dialog 就跟著被刪除，這裡索性統一都用「建一次、開之前重新填內
容」，不個別判斷哪個安全哪個不安全。

*** async 事件 handler 直接指定給 `on_click`/`on("keydown.enter", ...)`
就好，不需要 `spawn()` 包一層 ***：這裡是 NiceGUI(純 asyncio)，不是
`pyqt.py` 那套 qasync+`@asyncSlot()`——NiceGUI 自己的事件派發
(`nicegui/events.py::handle_event()`)偵測到 handler 回傳值是 awaitable
時，會用 `background_tasks.create_or_defer()` 建立 Task 並正確保留參
照，不會有 `app/services/background_tasks.py` 開頭說明的那個「Task 被
GC 回收」問題(那是 qasync `@asyncSlot()` 專屬的陷阱)。`spawn()` 只留給
不是直接掛在 UI 事件上的背景工作用(這裡是 AI 建議的 debounce 計時器，
需要自己取消前一次還沒觸發的排程，不是單純的 on_click)。
"""
from __future__ import annotations

import asyncio
import difflib
from typing import Callable, Optional

from nicegui import ui

from app.models.fundamentals_client import fetch_fundamentals
from app.models.ib_client import IBClient
from app.models.openrouter_client import suggest_filters, suggest_scan_codes, translate_terms
from app.models.scanner_catalog import (
    FilterDef, ScanTypeDef, filter_by_id, load_filter_catalog, load_scan_type_catalog, scan_type_by_code,
)
from app.models.screener import CandidateStock, ScanFilterValue, ScannerParams, enrich_candidates, run_scanner
from app.services import industry_translations, watchlist_store
from app.services.background_tasks import spawn

# 篩選條件面板初次開啟時方便使用者的預設值，對照舊版 screener_widget.py 的
# DEFAULT_FILTER_IDS，跟 app/resources/scan_filters.json 裡的 id 一致。
_DEFAULT_FILTER_IDS = ["PRICE", "MKTCAP", "OPTVOLUME"]

_AI_DEBOUNCE_SEC = 0.5
_MAX_SCAN_CODE_SUGGESTIONS = 15

# 沒有從 IB 的參數 XML 拿到每個欄位實際合理的數值上限，統一給一個夠寬鬆
# 的上限，只是擋輸入框不要打出離譜的天文數字，跟舊版 scanner_filter_picker
# 的 _SPIN_MAX 同一個數字。
_FILTER_VALUE_MAX = 1_000_000_000.0


def _pickable_filters(catalog: list[FilterDef], exclude_ids: set[str]) -> list[FilterDef]:
    """只收 value_type 是 double/int 的欄位——IB 少數(7個)是布林值，跟這
    裡 ScanFilterValue.value 統一用 float 的設計不相容，對照舊版
    scanner_filter_picker.py::_pickable() 的說明。"""
    return [f for f in catalog if f.value_type in ("double", "int") and f.id not in exclude_ids]


def _scan_code_label(code: str) -> str:
    """掃描代碼的顯示名稱：優先中文，沒有中文翻譯(這次加 ETF 支援後新
    收進來的 192 筆 ETF.EQ.US 專用代碼大多還沒人工翻譯，見
    scripts/gen_scanner_catalog.py 開頭的說明)才退回英文，都沒有才顯示
    原始代碼——*** 一定要用 `or`，不能只判斷 scan_type 是否存在 ***：
    scan_type_by_code() 找得到這個 code 但 name_zh 是空字串的情況很常
    見，用「scan_type 存在就顯示 name_zh」的寫法會讓按鈕/歷史紀錄顯示
    空白，這是實際踩到的案例，不是預防性寫法。"""
    scan_type = scan_type_by_code(code)
    if scan_type is None:
        return code
    return scan_type.name_zh or scan_type.name_en or code


# 商品類型選擇器的選項——對照 scripts/gen_scanner_catalog.py::
# _SUPPORTED_INSTRUMENTS，IB 掃描器的 instrument 參數值同時也是
# locationCode(這兩個 API 商品類型都是這樣，locationCode 沒有另外的
# ".MAJOR"/".US" 子分類，實測(reqScannerDataAsync)確認過，不是猜的)。
_INSTRUMENT_OPTIONS = {"STK": "股票", "ETF.EQ.US": "ETF"}
_INSTRUMENT_LOCATION = {"STK": "STK.US.MAJOR", "ETF.EQ.US": "ETF.EQ.US"}


def build(ib_client: IBClient) -> Callable:
    ib = ib_client.ib
    state = {
        "candidates": [],       # list[{"symbol","source","rank","checkbox","row"}]
        "filter_rows": {},      # filter_id -> {"filter_def","above","below","row"}
        "selected_scan_code": None,
        "instrument": "STK",    # "STK" 或 "ETF.EQ.US"，決定掃描代碼挑選器顯示哪些候選、真正掃描時的 instrument/locationCode
        "scan_busy": False,
    }

    # *** Quasar 的 QTabPanels 元件會自己量測「目前這個 tab 的內容高
    # 度」，用 JS inline style 把 .q-tab-panels 卡死在那個高度、
    # overflow:hidden——這個高度是切換分頁當下量的，之後這個分頁裡動態
    # 增加的內容(篩選條件列、候選清單列)超出這個高度就被裁切掉，卡死的
    # 高度不會跟著長高，外層 card 的 max-h-[90vh]+overflow-y-auto 也就
    # 量不到真正的內容高度，完全沒有卷軸可捲——實測(瀏覽器
    # getComputedStyle)證實：.q-tab-panels 是 height:480px 固定值 +
    # overflow:hidden，不是卡片本身沒有捲動空間。用 !important 蓋掉這個
    # inline style(CSS `!important` 蓋得過 inline style 缺 !important 的
    # 情況)，讓 panels 恢復成跟內容一樣高、不裁切，捲動交回外層 card 處
    # 理。
    ui.add_head_html(
        "<style>"
        ".q-tab-panels.q-panel-parent { height: auto !important; overflow: visible !important; }"
        ".q-tab-panel { overflow: visible !important; height: auto !important; }"
        "</style>"
    )

    with ui.dialog() as dialog, ui.card().classes("w-[1320px] max-w-full max-h-[90vh] overflow-y-auto gap-2"):
        ui.label("股票篩選器").classes("text-lg font-semibold")
        with ui.tabs().classes("w-full") as tabs:
            scan_tab = ui.tab("① 初篩選股")
            watchlist_tab = ui.tab("② 自選清單")
        with ui.tab_panels(tabs, value=scan_tab).classes("w-full"):
            # -------------------------------------------------------- 初篩頁籤
            with ui.tab_panel(scan_tab):
                with ui.column().classes("w-full gap-2 border rounded p-3"):
                    ui.label("市場掃描條件").classes("font-semibold")
                    with ui.row().classes("items-end gap-2"):
                        instrument_select = ui.select(
                            _INSTRUMENT_OPTIONS, value="STK", label="商品類型",
                        ).classes("w-24")
                        with ui.column().classes("gap-0"):
                            ui.label("掃描代碼").classes("text-xs text-grey")
                            scan_code_btn = ui.button("請選擇").props("outline")
                        max_results_input = ui.number(
                            "最多取幾檔", value=20, format="%d", min=1, max=50,
                        ).classes("w-24 shrink-0")
                        add_filter_btn = ui.button("＋ 新增篩選條件").props("outline dense")
                        ai_filter_btn = ui.button("AI 條件建議").props("outline dense")
                        scan_btn = ui.button("執行市場掃描").props("dense")
                    filter_rows_container = ui.row().classes("w-full flex-wrap gap-2")
                    scan_status_label = ui.label("尚未執行")

                with ui.column().classes("w-full gap-2 border rounded p-3 mt-2"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label("候選標的清單").classes("font-semibold")
                        ui.label("勾選「納入」的標的才會被「加入自選」納入").classes("text-xs text-grey")
                    with ui.row().classes("items-center gap-3 w-full text-xs text-grey"):
                        ui.label("").classes("w-8 shrink-0")  # 對齊勾選框寬度
                        ui.label("代碼").classes("w-16 shrink-0")
                        ui.label("名稱").classes("w-48 shrink-0")
                        ui.label("產業").classes("w-28 shrink-0 text-center")
                        ui.label("類別").classes("w-32 shrink-0 text-center")
                    candidate_container = ui.column().classes("w-full gap-1 max-h-72 overflow-y-auto")
                    with ui.row().classes("items-center gap-2"):
                        manual_symbol_input = ui.input(
                            placeholder="手動加入代碼(空白/逗號分隔)",
                        ).classes("w-48 shrink")
                        add_manual_btn = ui.button("新增到候選清單").props("dense")
                        select_all_btn = ui.button("全選").props("outline dense")
                        select_none_btn = ui.button("全不選").props("outline dense")
                        clear_candidates_btn = ui.button("清空清單").props("outline dense")
                        add_to_watchlist_btn = ui.button("加入自選").props("outline dense")

            # -------------------------------------------------------- 自選清單頁籤
            with ui.tab_panel(watchlist_tab):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("點「帶入①初篩選股」把成分股帶回候選清單，點「管理」增減成分股").classes(
                        "text-xs text-grey",
                    )
                    new_watchlist_btn = ui.button("＋ 新增自選清單").props("outline dense")
                watchlist_container = ui.column().classes("w-full gap-1")

    # *** 不放「關閉」按鈕 ***：ui.dialog() 預設點旁邊背景/按 ESC 就會關
    # 閉(persistent 才會關掉這個行為，這裡沒設)，跟下單面板/委託簿/成交
    # 回報那幾個 modal 一致，不需要另外佔一顆按鈕。

    # ---------------------------------------------------------------------
    # 巢狀 dialog：新增篩選條件
    # ---------------------------------------------------------------------
    with ui.dialog() as add_filter_dialog, ui.card().classes("w-[420px] max-w-full h-[520px] gap-2"):
        ui.label("新增篩選條件").classes("text-lg font-semibold")
        add_filter_search = ui.input(placeholder="搜尋篩選條件名稱或分類...").classes("w-full")
        add_filter_list = ui.column().classes("w-full gap-0 flex-grow overflow-y-auto")
        with ui.row():
            ui.button("取消", on_click=add_filter_dialog.close).props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：選擇掃描代碼
    # ---------------------------------------------------------------------
    with ui.dialog() as scan_code_dialog, ui.card().classes("w-[440px] max-w-full gap-2"):
        ui.label("選擇掃描代碼").classes("text-lg font-semibold")
        scan_code_picker_container = ui.column().classes("w-full gap-1")
        with ui.row():
            ui.button("取消", on_click=scan_code_dialog.close).props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：AI 條件建議
    # ---------------------------------------------------------------------
    with ui.dialog() as ai_filter_dialog, ui.card().classes("w-[560px] max-w-full max-h-[85vh] overflow-y-auto gap-2"):
        ui.label("AI 條件建議").classes("text-lg font-semibold")
        ui.label("用一句話描述你想篩選的股票特徵：")
        ai_query_input = ui.input(
            placeholder="例如：高波動率、股價10到100美元之間、市值大於10億美元",
        ).classes("w-full")
        ai_ask_btn = ui.button("AI 建議條件")
        ai_status_label = ui.label("").classes("text-xs text-grey")
        ui.label("掃描代碼建議").classes("text-xs text-grey mt-2")
        ai_scan_code_container = ui.column().classes("w-full gap-1")
        ui.label("預覽（套用前可自行調整或刪除）").classes("text-xs text-grey mt-2")
        ai_preview_container = ui.row().classes("w-full flex-wrap gap-2")
        ai_rationale_label = ui.label("").classes("text-xs text-grey")
        with ui.row():
            ai_apply_btn = ui.button("套用")
            ui.button("取消", on_click=ai_filter_dialog.close).props("flat")
        ai_apply_btn.disable()

    # ---------------------------------------------------------------------
    # 巢狀 dialog：命名/重新命名(新增自選清單、改名稱共用同一個)
    # ---------------------------------------------------------------------
    with ui.dialog() as name_prompt_dialog, ui.card():
        name_prompt_title = ui.label("")
        name_prompt_input = ui.input().classes("w-72")
        with ui.row():
            ui.button("儲存", on_click=lambda: name_prompt_dialog.submit(name_prompt_input.value))
            ui.button("取消", on_click=lambda: name_prompt_dialog.submit(None)).props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：查看詳細(yfinance 基本資料/財報/財報發布日/分析師評等)
    # ---------------------------------------------------------------------
    # *** 頁籤結構在這裡建一次，內容(fundamentals_*_body)每次開對話框才
    # 清空重填 ***：跟外層股票篩選器本身的頁籤同一個道理，`build()` 最
    # 外層那段全域 CSS(.q-tab-panels/.q-tab-panel 的 height:auto+
    # overflow:visible !important)本來就是掛在整個頁面上，這裡的巢狀頁
    # 籤不用再另外處理一次「Quasar QTabPanels 卡死高度」的問題。
    with ui.dialog() as fundamentals_dialog, ui.card().classes("w-[920px] max-w-full max-h-[85vh] overflow-y-auto gap-2"):
        fundamentals_title = ui.label("").classes("text-lg font-semibold")
        fundamentals_status = ui.label("").classes("text-xs text-grey")
        with ui.tabs().classes("w-full") as fundamentals_tabs:
            fundamentals_basic_tab = ui.tab("基本資料")
            fundamentals_income_tab = ui.tab("損益表")
            fundamentals_balance_tab = ui.tab("資產負債表")
            fundamentals_cashflow_tab = ui.tab("現金流量表")
            fundamentals_equity_tab = ui.tab("股東權益變動")
            fundamentals_analyst_tab = ui.tab("財報/分析師")
        with ui.tab_panels(fundamentals_tabs, value=fundamentals_basic_tab).classes("w-full"):
            with ui.tab_panel(fundamentals_basic_tab):
                fundamentals_basic_body = ui.column().classes("w-full gap-1")
            with ui.tab_panel(fundamentals_income_tab):
                fundamentals_income_body = ui.column().classes("w-full gap-1")
            with ui.tab_panel(fundamentals_balance_tab):
                fundamentals_balance_body = ui.column().classes("w-full gap-1")
            with ui.tab_panel(fundamentals_cashflow_tab):
                fundamentals_cashflow_body = ui.column().classes("w-full gap-1")
            with ui.tab_panel(fundamentals_equity_tab):
                # 免責說明放在這裡(頁籤結構本身，只建一次)，不是放進下面
                # 每次開對話框都會被 _render_period_table() clear() 掉重
                # 建的 fundamentals_equity_body——這兩個字串不會因為查的
                # 標的不同而改變，不需要每次重畫。
                ui.label(
                    "yfinance 沒有提供正式的股東權益變動表，以下用資產負債表的權益科目"
                    "(當季期末餘額)+現金流量表的籌資活動(當季發生數)拼出的近似版本，"
                    "僅供參考。",
                ).classes("text-xs text-grey mb-1")
                fundamentals_equity_body = ui.column().classes("w-full gap-1")
            with ui.tab_panel(fundamentals_analyst_tab):
                fundamentals_analyst_body = ui.column().classes("w-full gap-1")
        with ui.row():
            ui.button("關閉", on_click=fundamentals_dialog.close).props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：加入自選(把候選清單裡勾選的標的加進一個自選清單)
    # ---------------------------------------------------------------------
    with ui.dialog() as add_to_watchlist_dialog, ui.card().classes("w-[380px] max-w-full gap-2"):
        ui.label("加入自選清單").classes("text-lg font-semibold")
        add_to_watchlist_status = ui.label("").classes("text-xs text-grey")
        add_to_watchlist_list = ui.column().classes("w-full gap-0 max-h-56 overflow-y-auto")
        ui.label("或新增一個清單：").classes("text-xs text-grey mt-2")
        with ui.row().classes("items-center gap-2 w-full"):
            new_watchlist_inline_input = ui.input(placeholder="新清單名稱").classes("flex-grow")
            new_watchlist_inline_btn = ui.button("新增並加入").props("dense")
        with ui.row():
            ui.button("取消", on_click=add_to_watchlist_dialog.close).props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：管理自選清單成分股
    # ---------------------------------------------------------------------
    with ui.dialog() as manage_watchlist_dialog, ui.card().classes("w-[480px] max-w-full max-h-[85vh] overflow-y-auto gap-2"):
        manage_watchlist_title = ui.label("").classes("text-lg font-semibold")
        manage_watchlist_container = ui.column().classes("w-full gap-1 max-h-72 overflow-y-auto")
        with ui.row().classes("items-center gap-2 w-full"):
            manage_watchlist_input = ui.input(placeholder="手動加入代碼(空白/逗號分隔)").classes("flex-grow")
            manage_watchlist_add_btn = ui.button("新增").props("dense")
        with ui.row():
            manage_watchlist_close_btn = ui.button("關閉").props("flat")

    # ---------------------------------------------------------------------
    # 巢狀 dialog：通用確認(目前給刪除自選清單用)
    # ---------------------------------------------------------------------
    with ui.dialog() as confirm_dialog, ui.card():
        confirm_message = ui.label("")
        with ui.row():
            ui.button("確定", on_click=lambda: confirm_dialog.submit(True))
            ui.button("取消", on_click=lambda: confirm_dialog.submit(False)).props("flat")

    async def _confirm(message: str) -> bool:
        confirm_message.text = message
        return bool(await confirm_dialog)

    # =======================================================================
    # 篩選條件列(初篩表單/AI 預覽共用)
    # =======================================================================
    def _add_filter_row(
        filter_def: FilterDef, above: float | None = None, below: float | None = None,
        *, container=None, rows_dict: dict | None = None,
    ) -> None:
        container = container if container is not None else filter_rows_container
        rows_dict = rows_dict if rows_dict is not None else state["filter_rows"]
        if filter_def.id in rows_dict:
            return
        decimals = 2 if filter_def.value_type == "double" else 0
        step = 0.01 if filter_def.value_type == "double" else 1
        label = filter_def.label_zh or filter_def.id
        with container:
            with ui.column().classes("border rounded p-2 gap-1") as row:
                with ui.row().classes("items-center justify-between w-full gap-2"):
                    label_el = ui.label(label).classes("text-xs text-grey")
                    ui.button(
                        icon="close", on_click=lambda: _remove_filter_row(filter_def.id, rows_dict),
                    ).props("flat dense round size=sm")
                if filter_def.tooltip_zh:
                    label_el.tooltip(filter_def.tooltip_zh)
                if filter_def.kind == "range":
                    with ui.row().classes("items-center gap-1"):
                        above_input = ui.number(
                            value=above or 0, format=f"%.{decimals}f", step=step, min=0, max=_FILTER_VALUE_MAX,
                        ).classes("w-20")
                        ui.label("~")
                        below_input = ui.number(
                            value=below or 0, format=f"%.{decimals}f", step=step, min=0, max=_FILTER_VALUE_MAX,
                        ).classes("w-20")
                else:
                    above_input = ui.number(
                        value=above or 0, format=f"%.{decimals}f", step=step, min=0, max=_FILTER_VALUE_MAX,
                    ).classes("w-24")
                    below_input = None
        rows_dict[filter_def.id] = {
            "filter_def": filter_def, "above": above_input, "below": below_input, "row": row,
        }

    def _remove_filter_row(filter_id: str, rows_dict: dict) -> None:
        entry = rows_dict.pop(filter_id, None)
        if entry is not None:
            entry["row"].delete()

    def _filter_row_values(rows_dict: dict) -> list[ScanFilterValue]:
        result = []
        for entry in rows_dict.values():
            filter_def = entry["filter_def"]
            if filter_def.kind == "range":
                pairs = [(filter_def.fields[0], entry["above"]), (filter_def.fields[1], entry["below"])]
            else:
                pairs = [(filter_def.fields[0], entry["above"])]
            for field, inp in pairs:
                value = inp.value or 0
                if value > 0:
                    result.append(ScanFilterValue(code=field.code, value=value))
        return result

    def _set_filter_row_values(entry: dict, above: float | None, below: float | None) -> None:
        if entry["below"] is not None:
            if above is not None:
                entry["above"].value = above
            if below is not None:
                entry["below"].value = below
        else:
            value = above if above is not None else below
            if value is not None:
                entry["above"].value = value

    for filter_id in _DEFAULT_FILTER_IDS:
        filter_def = filter_by_id(filter_id)
        if filter_def is not None:
            _add_filter_row(filter_def)

    # =======================================================================
    # 掃描代碼挑選(彈窗選/AI 條件建議共用同一套搜尋+AI 邏輯)
    # =======================================================================
    def _current_scan_type_catalog() -> list[ScanTypeDef]:
        """只回傳目前選定商品類型(state["instrument"])支援的掃描代碼——
        對照 app/models/scanner_catalog.py::ScanTypeDef.instruments 的說
        明，STK 專用的代碼(例如某些只對股票有意義的財報類指標)套用在
        ETF 掃描上大多會被 IB 直接拒絕，不能讓使用者選到。"""
        instrument = state["instrument"]
        return [st for st in load_scan_type_catalog() if instrument in st.instruments]

    def _build_scan_code_picker(
        container, catalog_provider: Callable[[], list[ScanTypeDef]], *,
        list_classes: str, on_select: Optional[Callable] = None,
    ) -> tuple[Callable, Callable, Callable]:
        """對照舊版 scan_code_picker.py::ScanCodePicker——打字先用本地
        difflib 比對立刻給候選，停頓 500ms 後再讓 AI 從完整清單挑語意上
        最接近的幾個覆蓋過去。`catalog_provider` 不是固定 list，是每次要
        用時才呼叫的函式——商品類型(股票/ETF)切換時候選清單要跟著換，用
        provider 才能每次都拿到當下最新的清單，不用整個 picker 重建。回
        傳 (get_code, set_code, refresh)。"""
        picker_state = {"code": None, "ai_warned": False, "ai_task": None, "suppress_change": False}

        with container:
            search_input = ui.input(placeholder="輸入關鍵字搜尋掃描代碼，例如「高波動率」...").classes("w-full")
            ai_status = ui.label("").classes("text-xs text-grey")
            ai_status.visible = False
            items_container = ui.column().classes(f"w-full gap-0 overflow-y-auto border rounded p-1 {list_classes}")

        def _local_match(text: str) -> list[ScanTypeDef]:
            catalog = catalog_provider()
            lowered = text.lower()
            substr = [
                c for c in catalog
                if lowered in c.code.lower() or lowered in (c.name_zh or "").lower() or lowered in c.name_en.lower()
            ]
            names = [c.name_zh or c.name_en for c in catalog]
            close = difflib.get_close_matches(text, names, n=_MAX_SCAN_CODE_SUGGESTIONS, cutoff=0.3)
            by_name = {(c.name_zh or c.name_en): c for c in catalog}
            fuzzy = [by_name[n] for n in close if n in by_name]
            merged: list[ScanTypeDef] = []
            seen = set()
            for c in substr + fuzzy:
                if c.code not in seen:
                    seen.add(c.code)
                    merged.append(c)
            return merged[:_MAX_SCAN_CODE_SUGGESTIONS]

        def _render_items(items: list[ScanTypeDef]) -> None:
            items_container.clear()
            with items_container:
                for c in items:
                    label = c.name_zh or c.name_en

                    def _pick(code=c.code, label=label) -> None:
                        picker_state["code"] = code
                        # 跟舊版 scan_code_picker.py::_on_item_clicked() 的
                        # blockSignals() 同一個目的——程式碼自己改
                        # search_input.value 不該再觸發下面
                        # _on_search_change() 重新跑一次本地比對+排一次不
                        # 必要的 AI 呼叫(NiceGUI 的 ValueElement 無論是使用
                        # 者輸入還是程式賦值，都會呼叫已註冊的
                        # on_value_change handler，不會自動分辨兩者)。
                        picker_state["suppress_change"] = True
                        search_input.value = label
                        if on_select is not None:
                            on_select(code)

                    btn = ui.button(label, on_click=_pick).props("flat dense align=left").classes("w-full justify-start")
                    if c.tooltip_zh:
                        btn.tooltip(c.tooltip_zh)

        async def _debounced_ai_lookup(text: str) -> None:
            await asyncio.sleep(_AI_DEBOUNCE_SEC)
            if not text:
                return
            catalog = catalog_provider()
            try:
                codes = await suggest_scan_codes(text, catalog)
            except Exception:  # noqa: BLE001
                # 安靜降級：AI 未設定/呼叫失敗時維持本地比對結果，只提示一次。
                if not picker_state["ai_warned"]:
                    picker_state["ai_warned"] = True
                    ai_status.text = "AI 建議未啟用(可能未設定 OPENROUTER_API_KEY)，僅顯示本地比對結果"
                    ai_status.visible = True
                return
            by_code = {c.code: c for c in catalog}
            if codes:
                ai_status.visible = False
                _render_items([by_code[c] for c in codes if c in by_code])

        def _on_search_change(e) -> None:
            if picker_state["suppress_change"]:
                picker_state["suppress_change"] = False
                return
            picker_state["code"] = None
            text = (e.value or "").strip()
            _render_items(_local_match(text) if text else catalog_provider()[:_MAX_SCAN_CODE_SUGGESTIONS])
            if picker_state["ai_task"] is not None:
                picker_state["ai_task"].cancel()
            picker_state["ai_task"] = spawn(_debounced_ai_lookup(text))

        search_input.on_value_change(_on_search_change)
        _render_items(catalog_provider()[:_MAX_SCAN_CODE_SUGGESTIONS])

        def get_code() -> Optional[str]:
            return picker_state["code"]

        def set_code(code: str) -> None:
            scan_type = next((c for c in catalog_provider() if c.code == code), None)
            if scan_type is None:
                return
            picker_state["code"] = code
            picker_state["suppress_change"] = True
            search_input.value = scan_type.name_zh or scan_type.name_en

        def refresh() -> None:
            """商品類型切換時呼叫——用目前搜尋框文字(通常是空字串)重新
            跑一次 _on_search_change 同一套邏輯，讓清單換成新商品類型的
            候選，不用整個 picker 重建。"""
            text = (search_input.value or "").strip()
            _render_items(_local_match(text) if text else catalog_provider()[:_MAX_SCAN_CODE_SUGGESTIONS])

        return get_code, set_code, refresh

    def _set_scan_code(code: str) -> None:
        state["selected_scan_code"] = code
        scan_code_btn.text = _scan_code_label(code)

    def _on_scan_code_picked(code: str) -> None:
        _set_scan_code(code)
        scan_code_dialog.close()

    _scan_code_get, _scan_code_set, _scan_code_refresh = _build_scan_code_picker(
        scan_code_picker_container, _current_scan_type_catalog,
        list_classes="h-64", on_select=_on_scan_code_picked,
    )

    def _open_scan_code_dialog() -> None:
        if state["selected_scan_code"]:
            _scan_code_set(state["selected_scan_code"])
        scan_code_dialog.open()

    def _on_instrument_change(e) -> None:
        state["instrument"] = e.value
        _scan_code_refresh()
        valid_codes = {st.code for st in _current_scan_type_catalog()}
        if state["selected_scan_code"] not in valid_codes:
            state["selected_scan_code"] = None
            scan_code_btn.text = "請選擇"

    scan_code_btn.on_click(_open_scan_code_dialog)
    instrument_select.on_value_change(_on_instrument_change)

    # =======================================================================
    # 新增篩選條件對話框
    # =======================================================================
    def _render_add_filter_list(items: list[FilterDef]) -> None:
        add_filter_list.clear()
        by_category: dict[str, list[FilterDef]] = {}
        for f in items:
            by_category.setdefault(f.category_zh or f.category_en, []).append(f)
        with add_filter_list:
            for category in sorted(by_category):
                ui.label(category).classes("text-xs font-semibold text-grey mt-2")
                for f in sorted(by_category[category], key=lambda x: x.label_zh or x.id):
                    label = f.label_zh or f.id

                    def _pick(filter_def=f) -> None:
                        _add_filter_row(filter_def)
                        add_filter_dialog.close()

                    btn = ui.button(label, on_click=_pick).props("flat dense align=left").classes("w-full justify-start")
                    if f.tooltip_zh:
                        btn.tooltip(f.tooltip_zh)

    def _on_add_filter_search_change(e) -> None:
        text = (e.value or "").strip().lower()
        items = _pickable_filters(load_filter_catalog(), set(state["filter_rows"]))
        if text:
            items = [
                f for f in items
                if text in (f.label_zh or "").lower() or text in (f.category_zh or "").lower()
                or text in f.id.lower() or text in (f.category_en or "").lower()
            ]
        _render_add_filter_list(items)

    add_filter_search.on_value_change(_on_add_filter_search_change)

    def _open_add_filter_dialog() -> None:
        add_filter_search.value = ""
        _render_add_filter_list(_pickable_filters(load_filter_catalog(), set(state["filter_rows"])))
        add_filter_dialog.open()

    add_filter_btn.on_click(_open_add_filter_dialog)

    # =======================================================================
    # AI 條件建議對話框
    # =======================================================================
    ai_preview_rows: dict = {}
    _ai_scan_code_get, _ai_scan_code_set, _ai_scan_code_refresh = _build_scan_code_picker(
        ai_scan_code_container, _current_scan_type_catalog, list_classes="h-32",
    )

    async def _on_ai_ask() -> None:
        text = (ai_query_input.value or "").strip()
        if not text:
            return
        ai_ask_btn.disable()
        ai_status_label.text = "AI 分析中..."
        try:
            proposal = await suggest_filters(text, load_filter_catalog(), _current_scan_type_catalog())
        except Exception as exc:  # noqa: BLE001
            ai_status_label.text = f"AI 建議失敗：{exc}"
            return
        finally:
            ai_ask_btn.enable()

        ai_preview_container.clear()
        ai_preview_rows.clear()
        if proposal.scan_code:
            _ai_scan_code_set(proposal.scan_code)
        for proposed in proposal.filters:
            filter_def = filter_by_id(proposed.filter_id)
            if filter_def is None:
                continue
            _add_filter_row(
                filter_def, proposed.above, proposed.below,
                container=ai_preview_container, rows_dict=ai_preview_rows,
            )
        ai_rationale_label.text = proposal.rationale
        ai_status_label.text = f"AI 建議了 {len(ai_preview_rows)} 個篩選條件，可自行調整後再按「套用」"
        if ai_preview_rows or proposal.scan_code:
            ai_apply_btn.enable()
        else:
            ai_apply_btn.disable()

    def _on_ai_apply() -> None:
        code = _ai_scan_code_get()
        if code:
            _set_scan_code(code)
        for filter_id, entry in ai_preview_rows.items():
            above = entry["above"].value or None
            below = (entry["below"].value or None) if entry["below"] is not None else None
            if filter_id not in state["filter_rows"]:
                _add_filter_row(entry["filter_def"], above, below)
            else:
                _set_filter_row_values(state["filter_rows"][filter_id], above, below)
        ai_filter_dialog.close()

    def _open_ai_filter_dialog() -> None:
        ai_query_input.value = ""
        ai_status_label.text = ""
        ai_rationale_label.text = ""
        ai_preview_container.clear()
        ai_preview_rows.clear()
        ai_apply_btn.disable()
        _ai_scan_code_refresh()  # 商品類型可能在上次開這個對話框之後換過，重新套用目前的篩選清單
        ai_filter_dialog.open()

    ai_query_input.on("keydown.enter", lambda e: _on_ai_ask())
    ai_ask_btn.on_click(_on_ai_ask)
    ai_apply_btn.on_click(_on_ai_apply)
    ai_filter_btn.on_click(_open_ai_filter_dialog)

    # =======================================================================
    # 候選標的清單
    # =======================================================================
    async def _translate_industry_category(candidates: list[CandidateStock]) -> None:
        """幫候選清單裡的 industry/category(IB 回傳的英文分類字串)補上中
        文翻譯，就地填入 cand.industry_zh/category_zh。*** 一定要在
        enrich_candidates()/run_scanner() 之後才呼叫 ***：要先有英文原
        文才知道要翻譯什麼。同一個英文字串(例如所有生技股的
        "Biotechnology")只會真的呼叫一次 AI，之後都從
        app/services/industry_translations.py 的本機快取拿，不會每次顯
        示候選清單都重打 API。AI 沒設定/呼叫失敗就讓這幾筆維持沒有中文
        翻譯，_append_candidate_row() 會自動退回顯示英文原文，不是失
        敗。"""
        terms = {t for c in candidates for t in (c.industry, c.category) if t}
        if not terms:
            return
        cached = industry_translations.get_cached(terms)
        missing = terms - set(cached)
        if missing:
            translated = await translate_terms(sorted(missing))
            industry_translations.save_translations(translated)
            cached.update(translated)
        for cand in candidates:
            if cand.industry:
                cand.industry_zh = cached.get(cand.industry)
            if cand.category:
                cand.category_zh = cached.get(cand.category)

    def _append_candidate_row(cand: CandidateStock) -> None:
        # *** 顯示名稱/產業/類別，不顯示現價/漲跌幅 ***：使用者明確不要
        # 即時報價這種會變動的市場資料，改顯示由 app/models/screener.py
        # ::enrich_candidates() 查 reqContractDetailsAsync() 補上的靜態
        # 分類資訊，一眼看出「這是哪個產業/類別的標的」。查不到(ETF 通
        # 常沒有 industry/category，一般股票偶爾查詢失敗)的候選這幾欄留
        # 空，不是失敗，見該函式的說明。產業/類別優先顯示中文翻譯(見
        # _translate_industry_category())，沒翻譯到才退回英文原文；名
        # 稱(公司/ETF 全名)不翻譯，維持 IB 原文。
        name_text = cand.long_name or "-"
        industry_text = cand.industry_zh or cand.industry or "-"
        category_text = cand.category_zh or cand.category or "-"
        with candidate_container:
            # *** 名稱欄一定要是固定寬度(w-48)，不能用 flex-grow ***：
            # 這一列尾端還有 ui.space()+移除鈕，如果名稱欄也是
            # flex-grow，會跟 ui.space() 一起搶剩餘空間，兩個 flex-grow
            # 元件實際各自分到的寬度依這一列其他內容而變動，導致這裡的
            # 產業/類別欄位跟上面表頭那排固定寬度的label對不齊(表頭那排
            # 沒有 ui.space()+按鈕，flex-grow的名稱標籤會把剩餘空間全部
            # 吃光，跟這裡「flex-grow名稱只分到一半」的寬度不一樣)——
            # 這是實測踩到的對齊 bug，不是預防性寫法，兩邊都要用固定寬
            # 度才會對齊。
            with ui.row().classes("items-center gap-3 border-b py-1 w-full") as row:
                checkbox = ui.checkbox(value=True).classes("w-8 shrink-0")
                ui.label(cand.symbol).classes("w-16 shrink-0 font-medium")
                name_label = ui.label(name_text).classes("w-48 shrink-0 text-xs truncate")
                if cand.long_name:
                    name_label.tooltip(cand.long_name)
                industry_label = ui.label(industry_text).classes("w-28 shrink-0 text-xs text-grey text-center truncate")
                if cand.industry:
                    industry_label.tooltip(cand.industry)
                category_label = ui.label(category_text).classes("w-32 shrink-0 text-xs text-grey text-center truncate")
                if cand.category:
                    category_label.tooltip(cand.category)
                ui.space()
                ui.button(
                    "詳細", on_click=lambda s=cand.symbol: _open_fundamentals_dialog(s),
                ).props("flat dense")
                ui.button(
                    icon="close", on_click=lambda s=cand.symbol: _remove_candidate(s),
                ).props("flat dense round size=sm")
        state["candidates"].append({
            "symbol": cand.symbol, "source": cand.source, "rank": cand.rank, "checkbox": checkbox, "row": row,
        })

    def _add_candidates(new_candidates: list[CandidateStock]) -> None:
        existing = {c["symbol"] for c in state["candidates"]}
        added = 0
        for cand in new_candidates:
            if cand.symbol in existing:
                continue
            existing.add(cand.symbol)
            _append_candidate_row(cand)
            added += 1
        if added:
            scan_status_label.text = f"候選清單新增 {added} 檔，目前共 {len(state['candidates'])} 檔"

    def _remove_candidate(symbol: str) -> None:
        for i, c in enumerate(state["candidates"]):
            if c["symbol"] == symbol:
                c["row"].delete()
                del state["candidates"][i]
                break

    def _on_clear_candidates() -> None:
        candidate_container.clear()
        state["candidates"].clear()

    def _set_all_candidates_checked(value: bool) -> None:
        for c in state["candidates"]:
            c["checkbox"].value = value

    def _checked_symbols() -> list[str]:
        return [c["symbol"] for c in state["candidates"] if c["checkbox"].value]

    async def _on_add_manual_symbols() -> None:
        text = (manual_symbol_input.value or "").strip().upper()
        if not text:
            return
        symbols = [s.strip() for s in text.replace(",", " ").split() if s.strip()]
        new_candidates = [CandidateStock(symbol=s, source="manual") for s in symbols]
        scan_status_label.text = "查詢中..."
        await enrich_candidates(ib, new_candidates)
        await _translate_industry_category(new_candidates)
        _add_candidates(new_candidates)
        manual_symbol_input.value = ""

    manual_symbol_input.on("keydown.enter", lambda e: _on_add_manual_symbols())
    add_manual_btn.on_click(_on_add_manual_symbols)
    select_all_btn.on_click(lambda: _set_all_candidates_checked(True))
    select_none_btn.on_click(lambda: _set_all_candidates_checked(False))
    clear_candidates_btn.on_click(_on_clear_candidates)

    # =======================================================================
    # 查看詳細(yfinance 基本資料/財報/財報發布日/分析師評等)
    # =======================================================================
    def _fmt_money(value: float | None) -> str:
        if value is None:
            return "-"
        return f"${value:,.0f}"

    def _fmt_millions(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value / 1_000_000:,.0f}"

    def _fmt_num(value: float | None, decimals: int = 2) -> str:
        if value is None:
            return "-"
        return f"{value:.{decimals}f}"

    def _fundamentals_field(label: str, value: str) -> None:
        ui.label(label).classes("text-xs text-grey")
        ui.label(value).classes("text-sm")

    def _render_period_table(
        container, columns: list[tuple[str, str, Callable]], periods: list, empty_message: str,
    ) -> None:
        """財報頁籤共用的「期間 x 數字欄」表格——columns 是
        (表頭文字, 屬性名稱, 格式化函式) 的清單，periods 是
        IncomeStatementPeriod/BalanceSheetPeriod/CashFlowPeriod/
        EquityChangePeriod 的實例列表，每個都有 `.period` 屬性當第一欄。
        *** 一定要 flex-nowrap + 外層 overflow-x-auto，不能讓欄位自動換
        行 ***：欄位數多(損益表/資產負債表都到 11 欄)，固定寬度加起來
        很容易比對話框窄，讓 flexbox 自動換行的話同一列會被拆成兩行，
        數字對不到自己的欄位標題——跟 web_order_book_widgets.py::
        box_container 同一個處理方式，寧可讓這塊內容自己橫向捲動。"""
        container.clear()
        with container:
            if not periods:
                ui.label(empty_message).classes("text-sm text-grey")
                return
            with ui.column().classes("w-full overflow-x-auto"):
                with ui.row().classes("items-center gap-3 text-xs text-grey flex-nowrap"):
                    ui.label("期間").classes("w-24 shrink-0 whitespace-nowrap")
                    for label, _attr, _fmt in columns:
                        ui.label(label).classes("w-28 shrink-0 text-right whitespace-nowrap")
                for p in periods:
                    with ui.row().classes("items-center gap-3 text-sm flex-nowrap border-b py-1"):
                        ui.label(p.period).classes("w-24 shrink-0 whitespace-nowrap")
                        for _label, attr, fmt in columns:
                            ui.label(fmt(getattr(p, attr))).classes("w-28 shrink-0 text-right whitespace-nowrap")

    _RECOMMENDATION_LABELS = {
        "strong_buy": "強力買進", "buy": "買進", "hold": "持有",
        "sell": "賣出", "strong_sell": "強力賣出", "underperform": "落後大盤", "none": "無評等",
    }
    _RATING_COUNT_LABELS = [
        ("strongBuy", "強力買進"), ("buy", "買進"), ("hold", "持有"),
        ("sell", "賣出"), ("strongSell", "強力賣出"),
    ]

    async def _open_fundamentals_dialog(symbol: str) -> None:
        fundamentals_title.text = symbol
        fundamentals_status.text = "查詢中..."
        fundamentals_tabs.value = fundamentals_basic_tab
        for body in (
            fundamentals_basic_body, fundamentals_income_body, fundamentals_balance_body,
            fundamentals_cashflow_body, fundamentals_equity_body, fundamentals_analyst_body,
        ):
            body.clear()
        fundamentals_dialog.open()
        snapshot = await fetch_fundamentals(symbol)
        fundamentals_status.text = ""
        if snapshot.error:
            with fundamentals_basic_body:
                ui.label(snapshot.error).classes("text-negative text-sm")
            return

        fundamentals_title.text = f"{symbol} — {snapshot.long_name}" if snapshot.long_name else symbol

        # ---------------------------------------------------------- 基本資料
        with fundamentals_basic_body:
            ui.label("基本資料").classes("font-semibold")
            with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1"):
                _fundamentals_field("產業/類別", snapshot.sector or snapshot.category or "-")
                _fundamentals_field("子類別", snapshot.industry or "-")
                _fundamentals_field("交易所", snapshot.exchange or "-")
                _fundamentals_field("市值/資產規模", _fmt_money(snapshot.market_cap))
                _fundamentals_field("本益比(TTM)", _fmt_num(snapshot.pe_ratio))
                _fundamentals_field("預估本益比", _fmt_num(snapshot.forward_pe))
                _fundamentals_field(
                    "殖利率", f"{snapshot.dividend_yield:.2f}%" if snapshot.dividend_yield is not None else "-",
                )
                _fundamentals_field("每股盈餘(TTM)", _fmt_num(snapshot.eps_ttm))
                _fundamentals_field("52週高", _fmt_num(snapshot.week52_high))
                _fundamentals_field("52週低", _fmt_num(snapshot.week52_low))
                if snapshot.employees is not None:
                    _fundamentals_field("員工數", f"{snapshot.employees:,}")
                if snapshot.website:
                    _fundamentals_field("網站", snapshot.website)
            if snapshot.summary:
                ui.label("公司簡介").classes("font-semibold mt-2")
                ui.label(snapshot.summary).classes("text-xs text-grey")

        # ---------------------------------------------------------- 損益表
        _render_period_table(
            fundamentals_income_body,
            [
                ("營收(百萬)", "revenue", _fmt_millions),
                ("營業成本(百萬)", "cost_of_revenue", _fmt_millions),
                ("毛利(百萬)", "gross_profit", _fmt_millions),
                ("研發費用(百萬)", "rd_expense", _fmt_millions),
                ("管銷費用(百萬)", "sga_expense", _fmt_millions),
                ("營業利益(百萬)", "operating_income", _fmt_millions),
                ("稅前淨利(百萬)", "pretax_income", _fmt_millions),
                ("所得稅費用(百萬)", "tax_provision", _fmt_millions),
                ("淨利(百萬)", "net_income", _fmt_millions),
                ("基本EPS(美元)", "basic_eps", _fmt_num),
                ("稀釋EPS(美元)", "diluted_eps", _fmt_num),
            ],
            snapshot.income_statements,
            "查無損益表資料(可能是 ETF)",
        )

        # ---------------------------------------------------------- 資產負債表
        _render_period_table(
            fundamentals_balance_body,
            [
                ("總資產(百萬)", "total_assets", _fmt_millions),
                ("流動資產(百萬)", "current_assets", _fmt_millions),
                ("現金及約當現金(百萬)", "cash_and_equivalents", _fmt_millions),
                ("總負債(百萬)", "total_liabilities", _fmt_millions),
                ("流動負債(百萬)", "current_liabilities", _fmt_millions),
                ("總負債金額(百萬)", "total_debt", _fmt_millions),
                ("長期負債(百萬)", "long_term_debt", _fmt_millions),
                ("股東權益(百萬)", "stockholders_equity", _fmt_millions),
                ("保留盈餘(百萬)", "retained_earnings", _fmt_millions),
                ("營運資金(百萬)", "working_capital", _fmt_millions),
                ("負債比(%)", "debt_ratio", lambda v: _fmt_num(v, 1)),
            ],
            snapshot.balance_sheets,
            "查無資產負債表資料(可能是 ETF)",
        )

        # ---------------------------------------------------------- 現金流量表
        _render_period_table(
            fundamentals_cashflow_body,
            [
                ("營業現金流(百萬)", "operating_cash_flow", _fmt_millions),
                ("資本支出(百萬)", "capital_expenditure", _fmt_millions),
                ("自由現金流(百萬)", "free_cash_flow", _fmt_millions),
                ("投資現金流(百萬)", "investing_cash_flow", _fmt_millions),
                ("籌資現金流(百萬)", "financing_cash_flow", _fmt_millions),
                ("股利發放(百萬)", "dividends_paid", _fmt_millions),
                ("股票回購(百萬)", "stock_repurchase", _fmt_millions),
                ("現金淨變動(百萬)", "net_change_in_cash", _fmt_millions),
            ],
            snapshot.cash_flows,
            "查無現金流量表資料(可能是 ETF)",
        )

        # ---------------------------------------------------------- 股東權益變動(近似版)
        # 免責說明是頁籤本身的固定內容(見上面 tab_panel 建立處)，這裡只
        # 重畫表格本身。
        _render_period_table(
            fundamentals_equity_body,
            [
                ("股東權益(百萬)", "stockholders_equity", _fmt_millions),
                ("普通股股本(百萬)", "common_stock", _fmt_millions),
                ("保留盈餘(百萬)", "retained_earnings", _fmt_millions),
                ("股票回購(百萬)", "stock_repurchase", _fmt_millions),
                ("股利發放(百萬)", "dividends_paid", _fmt_millions),
                ("普通股發行淨額(百萬)", "stock_issuance", _fmt_millions),
            ],
            snapshot.equity_changes,
            "查無資料(可能是 ETF)",
        )

        # ---------------------------------------------------------- 財報發布日/分析師
        with fundamentals_analyst_body:
            ui.label("財報發布日").classes("font-semibold")
            ui.label(snapshot.next_earnings_date or "查無資料(可能是 ETF，或資料源未提供)").classes("text-sm")

            ui.label("分析師目標價").classes("font-semibold mt-2")
            if snapshot.analyst and (
                snapshot.analyst.target_mean_price is not None or snapshot.analyst.number_of_analysts
            ):
                analyst = snapshot.analyst
                with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1"):
                    _fundamentals_field("平均目標價", _fmt_num(analyst.target_mean_price))
                    _fundamentals_field("中位數目標價", _fmt_num(analyst.target_median_price))
                    _fundamentals_field("最高目標價", _fmt_num(analyst.target_high_price))
                    _fundamentals_field("最低目標價", _fmt_num(analyst.target_low_price))
                    _fundamentals_field(
                        "分析師人數", str(analyst.number_of_analysts) if analyst.number_of_analysts else "-",
                    )
                    _fundamentals_field(
                        "綜合評等",
                        _RECOMMENDATION_LABELS.get(analyst.recommendation_key, analyst.recommendation_key or "-"),
                    )
                if analyst.rating_counts:
                    ui.label("評等分布(最近一期)").classes("font-semibold mt-2")
                    with ui.row().classes("gap-4"):
                        for key, zh in _RATING_COUNT_LABELS:
                            ui.label(f"{zh}：{analyst.rating_counts.get(key, 0)}").classes("text-sm")
            else:
                ui.label("查無分析師覆蓋資料(可能是 ETF 或小型股)").classes("text-sm text-grey")

    # =======================================================================
    # 加入自選清單
    # =======================================================================
    def _render_add_to_watchlist_list() -> None:
        add_to_watchlist_list.clear()
        watchlists = sorted(watchlist_store.list_all(), key=lambda w: w["created_at"], reverse=True)
        with add_to_watchlist_list:
            if not watchlists:
                ui.label("尚無自選清單，用下面新增一個").classes("text-xs text-grey")
            for w in watchlists:
                def _pick(watchlist_id=w["id"], name=w["name"]) -> None:
                    watchlist_store.add_symbols(watchlist_id, _checked_symbols())
                    add_to_watchlist_status.text = f"已加入「{name}」"
                    add_to_watchlist_dialog.close()

                ui.button(
                    f"{w['name']}({len(w['symbols'])} 檔)", on_click=_pick,
                ).props("flat dense align=left").classes("w-full justify-start")

    def _open_add_to_watchlist_dialog() -> None:
        if not _checked_symbols():
            scan_status_label.text = "候選清單裡沒有勾選任何標的"
            return
        add_to_watchlist_status.text = ""
        new_watchlist_inline_input.value = ""
        _render_add_to_watchlist_list()
        add_to_watchlist_dialog.open()

    def _on_new_watchlist_inline() -> None:
        name = (new_watchlist_inline_input.value or "").strip()
        if not name:
            return
        watchlist_store.create(name, _checked_symbols())
        add_to_watchlist_status.text = f"已加入「{name}」"
        add_to_watchlist_dialog.close()

    add_to_watchlist_btn.on_click(_open_add_to_watchlist_dialog)
    new_watchlist_inline_input.on("keydown.enter", lambda e: _on_new_watchlist_inline())
    new_watchlist_inline_btn.on_click(_on_new_watchlist_inline)

    # =======================================================================
    # 命名對話框(新增自選清單/重新命名共用)
    # =======================================================================
    async def _prompt_name(title: str, default: str) -> Optional[str]:
        name_prompt_title.text = title
        name_prompt_input.value = default
        result = await name_prompt_dialog
        result = (result or "").strip()
        return result or None

    # =======================================================================
    # 初篩(市場掃描)
    # =======================================================================
    async def _on_run_scanner() -> None:
        # 一定要對照舊版 screener_widget.py::_on_run_scanner() 的說明
        # ——市場掃描這種要跑好幾秒的操作直接掛在 on_click 上就好，NiceGUI
        # 自己的事件派發會正確保留 Task 參照，不需要 spawn()。
        if state["scan_busy"]:
            return
        state["scan_busy"] = True
        scan_btn.disable()
        scan_status_label.text = "市場掃描中..."
        try:
            scan_code = state["selected_scan_code"]
            if scan_code is None:
                scan_status_label.text = "請先按「請選擇」挑一個掃描代碼"
                return
            instrument = state["instrument"]
            params = ScannerParams(
                scan_code=scan_code,
                instrument=instrument,
                location_code=_INSTRUMENT_LOCATION.get(instrument, instrument),
                max_results=int(max_results_input.value or 20),
                filters=_filter_row_values(state["filter_rows"]),
            )
            try:
                candidates = await run_scanner(ib, params)
            except Exception as exc:  # noqa: BLE001
                scan_status_label.text = f"市場掃描失敗：{exc}"
                return
            if not candidates:
                scan_status_label.text = "市場掃描沒有回傳任何標的"
                return
            await _translate_industry_category(candidates)
            _add_candidates(candidates)
        finally:
            scan_btn.enable()
            state["scan_busy"] = False

    scan_btn.on_click(_on_run_scanner)

    # =======================================================================
    # 自選清單
    # =======================================================================
    async def _load_watchlist_into_scan_tab(watchlist: dict) -> None:
        """點「帶入①初篩選股」——把這個自選清單的成分股加進目前的候選
        清單(用跟手動輸入/市場掃描一樣的 `_add_candidates()` 去重合
        併，不是整批取代)，切到①初篩選股頁籤方便直接接著調整條件、重
        新掃描。自選清單不像舊版「掃描紀錄」綁定某一次的掃描代碼/篩選
        條件，這裡只有成分股代碼可以帶，沒有條件可以還原。"""
        symbols = watchlist.get("symbols", [])
        tabs.value = scan_tab
        if not symbols:
            return
        candidates = [CandidateStock(symbol=s, source="manual") for s in symbols]
        scan_status_label.text = f"載入自選清單「{watchlist['name']}」，查詢中..."
        await enrich_candidates(ib, candidates)
        await _translate_industry_category(candidates)
        _add_candidates(candidates)

    async def _on_new_watchlist() -> None:
        name = await _prompt_name("新增自選清單", "")
        if not name:
            return
        watchlist_store.create(name)
        _refresh_watchlist_table()

    async def _on_rename_watchlist(watchlist: dict) -> None:
        name = await _prompt_name("重新命名自選清單", watchlist["name"])
        if not name:
            return
        watchlist_store.rename(watchlist["id"], name)
        _refresh_watchlist_table()

    async def _on_delete_watchlist(watchlist: dict) -> None:
        if not await _confirm(f"確定要刪除自選清單「{watchlist['name']}」嗎？此動作無法復原。"):
            return
        watchlist_store.delete(watchlist["id"])
        _refresh_watchlist_table()

    # ---------------------------------------------------------- 管理自選清單成分股
    manage_watchlist_state = {"watchlist_id": None, "members": []}  # members: [{"symbol","row"}]

    def _manage_append_row(symbol: str) -> None:
        with manage_watchlist_container:
            with ui.row().classes("items-center gap-3 border-b py-1 w-full") as row:
                ui.label(symbol).classes("font-medium")
                ui.space()
                ui.button(
                    icon="close", on_click=lambda s=symbol: _manage_remove_symbol(s),
                ).props("flat dense round size=sm")
        manage_watchlist_state["members"].append({"symbol": symbol, "row": row})

    def _manage_remove_symbol(symbol: str) -> None:
        watchlist_store.remove_symbol(manage_watchlist_state["watchlist_id"], symbol)
        for i, entry in enumerate(manage_watchlist_state["members"]):
            if entry["symbol"] == symbol:
                entry["row"].delete()
                del manage_watchlist_state["members"][i]
                break

    def _manage_add_manual() -> None:
        text = (manage_watchlist_input.value or "").strip().upper()
        if not text:
            return
        existing = {e["symbol"] for e in manage_watchlist_state["members"]}
        new_symbols = [s for s in text.replace(",", " ").split() if s and s not in existing]
        if new_symbols:
            watchlist_store.add_symbols(manage_watchlist_state["watchlist_id"], new_symbols)
            for s in new_symbols:
                _manage_append_row(s)
        manage_watchlist_input.value = ""

    def _open_manage_watchlist_dialog(watchlist: dict) -> None:
        manage_watchlist_state["watchlist_id"] = watchlist["id"]
        manage_watchlist_state["members"] = []
        manage_watchlist_title.text = f"管理自選清單：{watchlist['name']}"
        manage_watchlist_container.clear()
        manage_watchlist_input.value = ""
        for s in watchlist.get("symbols", []):
            _manage_append_row(s)
        manage_watchlist_dialog.open()

    def _close_manage_watchlist_dialog() -> None:
        manage_watchlist_dialog.close()
        _refresh_watchlist_table()

    manage_watchlist_input.on("keydown.enter", lambda e: _manage_add_manual())
    manage_watchlist_add_btn.on_click(_manage_add_manual)
    manage_watchlist_close_btn.on_click(_close_manage_watchlist_dialog)

    # ---------------------------------------------------------------- 清單列表
    def _refresh_watchlist_table() -> None:
        watchlists = sorted(watchlist_store.list_all(), key=lambda w: w["created_at"], reverse=True)
        watchlist_container.clear()
        with watchlist_container:
            if not watchlists:
                ui.label("尚無自選清單，按上面「＋ 新增自選清單」建立一個").classes("text-xs text-grey")
            for w in watchlists:
                with ui.row().classes("items-center gap-3 border-b py-1 w-full flex-nowrap"):
                    ui.label(w["name"]).classes("w-48 shrink-0 font-medium truncate")
                    ui.label(w["created_at"]).classes("w-36 shrink-0 text-xs text-grey")
                    ui.label(f"{len(w['symbols'])} 檔").classes("w-16 shrink-0 text-xs text-grey")
                    ui.space()
                    ui.button(
                        "帶入①初篩選股", on_click=lambda w=w: _load_watchlist_into_scan_tab(w),
                    ).props("flat dense")
                    ui.button("管理", on_click=lambda w=w: _open_manage_watchlist_dialog(w)).props("flat dense")
                    ui.button(
                        "重新命名", on_click=lambda w=w: _on_rename_watchlist(w),
                    ).props("flat dense")
                    ui.button(
                        "刪除", on_click=lambda w=w: _on_delete_watchlist(w),
                    ).props("flat dense color=negative")

    new_watchlist_btn.on_click(_on_new_watchlist)

    def _on_tab_change() -> None:
        if tabs.value == watchlist_tab:
            _refresh_watchlist_table()

    tabs.on_value_change(_on_tab_change)

    def _open_dialog() -> None:
        _refresh_watchlist_table()
        dialog.open()

    return _open_dialog
