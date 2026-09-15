"""
NiceGUI 版股票篩選器，取代 `app/views/screener_widget.py`(PyQt5 版)及其搭
配的 `scanner_filter_picker.py`/`scan_code_picker.py`/
`filter_assistant_dialog.py`/`scan_history_widgets.py`——功能行為對照舊版
1:1 搬過來，畫面改成 NiceGUI 慣用寫法：

    - 股票篩選器是主畫面(`main.py::index()` 直接把 `build()` 的內容組進
      `placeholder` 裡，不是彈出的 `ui.dialog()`)——跟下單面板/委託簿/
      成交回報/選擇權報價那幾個標題列按鈕彈出的置中 modal 不同一套互動
      模式，`build()` 不回傳開啟函式。
    - 兩個階段(初篩/自選清單)用 `ui.tabs()`/`ui.tab_panels()` 取代舊版
      `QTabWidget`。
    - 「新增篩選條件」「選擇掃描代碼」「AI 條件建議」「查看詳細」「加入
      自選」「管理自選清單」都是巢狀 `ui.dialog()`，取代舊版四支獨立的
      QDialog 檔案——這裡沒有拆成好幾支檔案，NiceGUI 的 `build()` 本來
      就是閉包風格，硬拆檔案只會讓一堆內部狀態要用參數傳來傳去，不會比
      較清楚。
    - 候選清單每一列的「期權報價」按鈕呼叫 `main.py` 傳進來的
      `open_quote_board(symbol)`(`web_quote_board_page.build()` 回傳的
      開啟函式)，彈出置中的選擇權報價 dialog 並自動帶入這檔標的、直接
      查詢一次，不用使用者再手動輸入代碼。
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
from app.services import filter_preset_store, industry_translations, watchlist_store
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


def build(ib_client: IBClient, open_quote_board: Callable) -> None:
    ib = ib_client.ib
    state = {
        "candidates": [],       # list[{"symbol","source","rank","row"}]
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

    with ui.column().classes("w-full max-w-[1320px] mx-auto gap-2"):
        ui.label("股票篩選器").classes("text-lg font-semibold")
        with ui.tabs().classes("w-full") as tabs:
            watchlist_tab = ui.tab("自選清單")
            scan_tab = ui.tab("市場掃描")
        with ui.tab_panels(tabs, value=watchlist_tab).classes("w-full"):
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
                        filter_preset_btn = ui.button("篩選範本").props("outline dense")
                        scan_btn = ui.button("執行市場掃描").props("dense")
                    ui.label("篩選欄位留 0 代表這一側不設限(例如市值只設下限、不設上限)").classes(
                        "text-xs text-grey",
                    )
                    filter_rows_container = ui.row().classes("w-full flex-wrap gap-2")
                    scan_status_label = ui.label("尚未執行")

                with ui.column().classes("w-full gap-2 border rounded p-3 mt-2"):
                    ui.label("候選標的清單").classes("font-semibold")
                    with ui.row().classes("items-center gap-3 w-full text-xs text-grey"):
                        ui.label("代碼").classes("w-16 shrink-0")
                        ui.label("名稱").classes("w-48 shrink-0")
                        ui.label("產業").classes("w-28 shrink-0 text-center")
                        ui.label("類別").classes("w-32 shrink-0 text-center")
                        ui.label("支援商品").classes("w-28 shrink-0 text-center")
                    candidate_container = ui.column().classes("w-full gap-1 max-h-72 overflow-y-auto")

            # -------------------------------------------------------- 自選清單頁籤
            with ui.tab_panel(watchlist_tab):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("點清單名稱展開成分股，點「管理」增減成分股").classes(
                        "text-xs text-grey",
                    )
                    new_watchlist_btn = ui.button("＋ 新增自選清單").props("outline dense")
                watchlist_container = ui.column().classes("w-full gap-1")


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
    # 巢狀 dialog：篩選範本(把目前的商品類型/掃描代碼/篩選欄位範圍存成一
    # 份範本，之後直接套用，不用每次重新設定；儲存/套用邏輯對照
    # `filter_preset_store.py` 開頭的說明)
    # ---------------------------------------------------------------------
    with ui.dialog() as filter_preset_dialog, ui.card().classes("w-[420px] max-w-full max-h-[80vh] overflow-y-auto gap-2"):
        ui.label("篩選範本").classes("text-lg font-semibold")
        filter_preset_status = ui.label("").classes("text-xs text-grey")
        filter_preset_list = ui.column().classes("w-full gap-1")
        ui.label("另存新範本：").classes("text-xs text-grey mt-2")
        with ui.row().classes("items-center gap-2 w-full"):
            new_preset_inline_input = ui.input(placeholder="範本名稱").classes("flex-grow")
            new_preset_inline_btn = ui.button("儲存目前設定").props("dense")
        with ui.row():
            ui.button("關閉", on_click=filter_preset_dialog.close).props("flat")

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
    # 巢狀 dialog：加入自選(候選清單每一列各自的「加入自選」按鈕觸發，把
    # 這一檔標的加進使用者選定的一個自選清單)
    # ---------------------------------------------------------------------
    with ui.dialog() as add_to_watchlist_dialog, ui.card().classes("w-[380px] max-w-full gap-2"):
        add_to_watchlist_title = ui.label("加入自選清單").classes("text-lg font-semibold")
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
        # 說明文字掛在整張卡片(row)上，不是只掛在小小的標籤文字上——原本
        # 只有 label_el 有 tooltip，範圍很小使用者容易滑不到；內容除了
        # scan_filters.json 原本的 tooltip_zh(這個欄位量的是什麼)，再補
        # 上單位(IB 原始 XML 帶的 unit_zh，例如「口」「美元」)跟「留 0
        # 不設限」的操作提示——後者是這裡的 UI 慣例(見
        # `_filter_row_values()`)，不是 IB 文件內容，使用者常常不知道
        # 「0~0」代表兩側都不限，不是「卡在 0」。
        unit = filter_def.fields[0].unit_zh if filter_def.fields else ""
        tooltip_parts = [filter_def.tooltip_zh] if filter_def.tooltip_zh else []
        if unit:
            tooltip_parts.append(f"單位：{unit}")
        tooltip_parts.append("上限或下限留 0 代表這一側不設限")
        tooltip_text = "；".join(tooltip_parts)
        with container:
            with ui.row().classes("items-center gap-2 border rounded p-2") as row:
                row.tooltip(tooltip_text)
                ui.label(label).classes("text-xs text-grey shrink-0")
                if filter_def.kind == "range":
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
                ui.button(
                    icon="close", on_click=lambda: _remove_filter_row(filter_def.id, rows_dict),
                ).props("flat dense round size=sm")
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
    # 篩選範本(儲存/套用目前的商品類型/掃描代碼/篩選欄位範圍，取代每次都
    # 要重新勾選/輸入一次)
    # =======================================================================
    def _current_filter_snapshot() -> list[dict]:
        return [
            {
                "filter_id": filter_id,
                "above": entry["above"].value or 0,
                "below": (entry["below"].value or 0) if entry["below"] is not None else None,
            }
            for filter_id, entry in state["filter_rows"].items()
        ]

    def _apply_filter_preset(preset: dict) -> None:
        # 先切商品類型(會連帶重新整理掃描代碼候選清單、清掉不合法的掃描
        # 代碼，見 _on_instrument_change())，再套用範本存的掃描代碼——
        # 範本存檔當下可能是另一個商品類型專用的代碼，這裡如果套用到不
        # 支援的商品類型就跳過，維持「請選擇」，不硬套一個會被 IB 拒絕
        # 的代碼。
        instrument_select.value = preset.get("instrument", "STK")
        scan_code = preset.get("scan_code")
        if scan_code and scan_code in {st.code for st in _current_scan_type_catalog()}:
            _set_scan_code(scan_code)
        for filter_id in list(state["filter_rows"]):
            _remove_filter_row(filter_id, state["filter_rows"])
        for item in preset.get("filters", []):
            filter_def = filter_by_id(item["filter_id"])
            if filter_def is None:
                continue  # 篩選欄位目錄改版、範本存的 id 已經找不到，跳過這一項
            _add_filter_row(filter_def, item.get("above"), item.get("below"))

    def _render_filter_preset_list() -> None:
        filter_preset_list.clear()
        presets = sorted(filter_preset_store.list_all(), key=lambda p: p["created_at"], reverse=True)
        with filter_preset_list:
            if not presets:
                ui.label("尚無篩選範本，用下面「另存新範本」建立一個").classes("text-xs text-grey")
            for p in presets:
                with ui.row().classes("items-center gap-2 border-b py-1 w-full flex-wrap"):
                    ui.label(p["name"]).classes("flex-grow truncate")

                    def _apply(preset=p) -> None:
                        _apply_filter_preset(preset)
                        filter_preset_status.text = f"已套用「{preset['name']}」"
                        filter_preset_dialog.close()

                    def _overwrite(preset=p) -> None:
                        filter_preset_store.update(
                            preset["id"], state["instrument"], state["selected_scan_code"],
                            _current_filter_snapshot(),
                        )
                        filter_preset_status.text = f"已用目前設定更新「{preset['name']}」"
                        _render_filter_preset_list()

                    async def _rename(preset=p) -> None:
                        name = await _prompt_name("重新命名篩選範本", preset["name"])
                        if not name:
                            return
                        filter_preset_store.rename(preset["id"], name)
                        _render_filter_preset_list()

                    async def _delete(preset=p) -> None:
                        if not await _confirm(f"確定要刪除篩選範本「{preset['name']}」嗎？此動作無法復原。"):
                            return
                        filter_preset_store.delete(preset["id"])
                        _render_filter_preset_list()

                    ui.button("套用", on_click=_apply).props("flat dense")
                    ui.button("更新為目前設定", on_click=_overwrite).props("flat dense")
                    ui.button("重新命名", on_click=_rename).props("flat dense")
                    ui.button("刪除", on_click=_delete).props("flat dense color=negative")

    def _open_filter_preset_dialog() -> None:
        filter_preset_status.text = ""
        new_preset_inline_input.value = ""
        _render_filter_preset_list()
        filter_preset_dialog.open()

    def _on_new_preset_inline() -> None:
        name = (new_preset_inline_input.value or "").strip()
        if not name:
            return
        filter_preset_store.create(name, state["instrument"], state["selected_scan_code"], _current_filter_snapshot())
        filter_preset_status.text = f"已儲存「{name}」"
        new_preset_inline_input.value = ""
        _render_filter_preset_list()

    filter_preset_btn.on_click(_open_filter_preset_dialog)
    new_preset_inline_input.on("keydown.enter", lambda e: _on_new_preset_inline())
    new_preset_inline_btn.on_click(_on_new_preset_inline)

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

    def _products_text(products: list[str]) -> str:
        """"支援商品"欄的顯示文字——`cand.products` 固定是
        `["期貨","月選","週選"]` 的子集(順序已經照這個固定順序排好，見
        `app/models/screener.py::_fetch_contract_meta()`)，用頓號連接；
        查不到/三種都沒有顯示 "-"，跟其他欄位缺值的顯示方式一致。"""
        return "、".join(products) if products else "-"

    def _append_candidate_row(cand: CandidateStock) -> None:
        # *** 顯示名稱/產業/類別/支援商品，不顯示現價/漲跌幅 ***：使用者
        # 明確不要即時報價這種會變動的市場資料，改顯示由
        # app/models/screener.py::enrich_candidates() 查
        # reqContractDetailsAsync()/reqSecDefOptParamsAsync() 補上的靜態
        # 分類資訊，一眼看出「這是哪個產業/類別的標的、支援哪些衍生商
        # 品」。查不到(ETF 通常沒有 industry/category，一般股票偶爾查詢
        # 失敗)的候選這幾欄留空，不是失敗，見該函式的說明。產業/類別優
        # 先顯示中文翻譯(見 _translate_industry_category())，沒翻譯到才
        # 退回英文原文；名稱(公司/ETF 全名)不翻譯，維持 IB 原文。
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
                ui.label(_products_text(cand.products)).classes("w-28 shrink-0 text-xs text-grey text-center truncate")
                ui.space()
                ui.button(
                    "期權報價", on_click=lambda s=cand.symbol: open_quote_board(s),
                ).props("flat dense")
                ui.button(
                    "詳細", on_click=lambda s=cand.symbol: _open_fundamentals_dialog(s),
                ).props("flat dense")
                ui.button(
                    "加入自選", on_click=lambda s=cand.symbol: _open_add_to_watchlist_dialog(s),
                ).props("flat dense")
                ui.button(
                    icon="close", on_click=lambda s=cand.symbol: _remove_candidate(s),
                ).props("flat dense round size=sm")
        state["candidates"].append({"symbol": cand.symbol, "source": cand.source, "rank": cand.rank, "row": row})

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
    # 加入自選清單(候選清單每一列各自的「加入自選」按鈕，一次只加一檔)
    # =======================================================================
    add_to_watchlist_state = {"symbol": None}

    def _render_add_to_watchlist_list() -> None:
        add_to_watchlist_list.clear()
        watchlists = sorted(watchlist_store.list_all(), key=lambda w: w["created_at"], reverse=True)
        with add_to_watchlist_list:
            if not watchlists:
                ui.label("尚無自選清單，用下面新增一個").classes("text-xs text-grey")
            for w in watchlists:
                def _pick(watchlist_id=w["id"], name=w["name"]) -> None:
                    watchlist_store.add_symbols(watchlist_id, [add_to_watchlist_state["symbol"]])
                    add_to_watchlist_status.text = f"已將 {add_to_watchlist_state['symbol']} 加入「{name}」"
                    add_to_watchlist_dialog.close()

                ui.button(
                    f"{w['name']}({len(w['symbols'])} 檔)", on_click=_pick,
                ).props("flat dense align=left").classes("w-full justify-start")

    def _open_add_to_watchlist_dialog(symbol: str) -> None:
        add_to_watchlist_state["symbol"] = symbol
        add_to_watchlist_title.text = f"加入自選清單：{symbol}"
        add_to_watchlist_status.text = ""
        new_watchlist_inline_input.value = ""
        _render_add_to_watchlist_list()
        add_to_watchlist_dialog.open()

    def _on_new_watchlist_inline() -> None:
        name = (new_watchlist_inline_input.value or "").strip()
        if not name:
            return
        symbol = add_to_watchlist_state["symbol"]
        watchlist_store.create(name, [symbol])
        add_to_watchlist_status.text = f"已將 {symbol} 加入「{name}」"
        add_to_watchlist_dialog.close()

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
    # 點清單名稱展開/收合成分股表格——欄位/按鈕排版直接照抄「候選標的清
    # 單」那一份(`_append_candidate_row()`)，讓使用者不用切頁籤就能用同
    # 一套介面查期權報價/看詳細資料。watchlist_expand_state 記的是「這個
    # 清單 id 有沒有已經查過、填過內容」，同一個清單收合再展開不用重查一
    # 次 IB，但 `_refresh_watchlist_table()` 整批重建時會清空(舊的
    # container 物件都被 clear() 砍掉了)，重新整理清單列表後全部從收合
    # 狀態開始——這只是操作方便的暫存，不是需要跨重整保留的狀態。
    watchlist_expand_state: dict[str, dict] = {}  # watchlist_id -> {"loaded": bool}

    def _append_watchlist_header_row(container) -> None:
        """展開清單的欄位表頭——跟「候選標的清單」那份用同一組固定寬度
        (見 _append_candidate_row() 開頭的說明)，這樣才能對齊；沒有這排
        表頭使用者看不出「代碼旁邊那一長串空白到底是產業還是類別」(使用
        者原始回報)。"""
        with container:
            with ui.row().classes("items-center gap-3 w-full text-xs text-grey"):
                ui.label("代碼").classes("w-16 shrink-0")
                ui.label("名稱").classes("w-48 shrink-0")
                ui.label("產業").classes("w-28 shrink-0 text-center")
                ui.label("類別").classes("w-32 shrink-0 text-center")
                ui.label("支援商品").classes("w-28 shrink-0 text-center")

    def _append_watchlist_symbol_row(container, watchlist_id: str, symbol: str) -> dict:
        """先只用 symbol 把一列的骨架畫出來(名稱欄顯示「查詢中…」，產業/
        類別/支援商品留空)——期權報價/詳細/移除這三個按鈕只需要 symbol
        就能動作，不用等查完公司名稱/產業分類才能用。實際資料由
        `_update_watchlist_symbol_row()` 補上，兩支函式故意切開，理由見
        `_toggle_watchlist_expand()` 的說明。"""
        with container:
            with ui.row().classes("items-center gap-3 border-b py-1 w-full") as row:
                ui.label(symbol).classes("w-16 shrink-0 font-medium")
                name_label = ui.label("查詢中…").classes("w-48 shrink-0 text-xs truncate text-grey")
                industry_label = ui.label("").classes("w-28 shrink-0 text-xs text-grey text-center truncate")
                category_label = ui.label("").classes("w-32 shrink-0 text-xs text-grey text-center truncate")
                products_label = ui.label("").classes("w-28 shrink-0 text-xs text-grey text-center truncate")
                ui.space()
                ui.button(
                    "期權報價", on_click=lambda s=symbol: open_quote_board(s),
                ).props("flat dense")
                ui.button(
                    "詳細", on_click=lambda s=symbol: _open_fundamentals_dialog(s),
                ).props("flat dense")
                ui.button(
                    icon="close",
                    on_click=lambda s=symbol, r=row: _remove_watchlist_symbol(watchlist_id, s, r),
                ).props("flat dense round size=sm")
        return {
            "name_label": name_label, "industry_label": industry_label,
            "category_label": category_label, "products_label": products_label,
        }

    def _update_watchlist_symbol_row(refs: dict, cand: CandidateStock) -> None:
        refs["name_label"].text = cand.long_name or "-"
        refs["name_label"].classes(remove="text-grey")
        if cand.long_name:
            refs["name_label"].tooltip(cand.long_name)
        refs["industry_label"].text = cand.industry_zh or cand.industry or "-"
        if cand.industry:
            refs["industry_label"].tooltip(cand.industry)
        refs["category_label"].text = cand.category_zh or cand.category or "-"
        if cand.category:
            refs["category_label"].tooltip(cand.category)
        refs["products_label"].text = _products_text(cand.products)

    def _remove_watchlist_symbol(watchlist_id: str, symbol: str, row) -> None:
        watchlist_store.remove_symbol(watchlist_id, symbol)
        row.delete()

    async def _toggle_watchlist_expand(watchlist: dict, container, chevron) -> None:
        entry = watchlist_expand_state.setdefault(watchlist["id"], {"loaded": False})
        container.visible = not container.visible
        chevron.set_name("expand_less" if container.visible else "expand_more")
        if not container.visible or entry["loaded"]:
            return
        entry["loaded"] = True
        symbols = watchlist.get("symbols", [])
        if not symbols:
            with container:
                ui.label("這個清單還沒有成分股，按「管理」加入").classes("text-xs text-grey py-1")
            return
        # *** 先把整份清單的列都畫出來，再各自查詢，不要等
        # enrich_candidates() 整批查完才顯示 ***：`enrich_candidates()`
        # 內部用 asyncio.gather() 平行送出所有請求，總耗時取決於最慢的
        # 那一檔，20 檔的清單體感上就是「展開後卡住不動好幾秒才整批跳
        # 出來」。這裡改成逐檔各自呼叫 `enrich_candidates(ib, [cand])`
        # (單檔清單，效果等同直接查那一檔)，一樣全部平行送出去(用
        # asyncio.gather() 包住每一檔的 _fetch_one())，維持「總耗時不隨
        # 檔數線性增加」的效能特性，但改成哪一檔先查完就先更新哪一列，
        # 使用者看到的是清單先展開、資料逐筆跳出來，不是整批一起卡住。
        _append_watchlist_header_row(container)
        refs_by_symbol = {s: _append_watchlist_symbol_row(container, watchlist["id"], s) for s in symbols}

        async def _fetch_one(symbol: str) -> None:
            cand = CandidateStock(symbol=symbol, source="manual")
            await enrich_candidates(ib, [cand])
            await _translate_industry_category([cand])
            _update_watchlist_symbol_row(refs_by_symbol[symbol], cand)

        await asyncio.gather(*(_fetch_one(s) for s in symbols), return_exceptions=True)

    async def _auto_expand_first(watchlist: dict, container, chevron) -> None:
        """`_refresh_watchlist_table()` 預設展開第一個清單用——一定要先
        `await client.connected()` 卡住，等這個頁面的 websocket 真的握手
        完成才能開始查/寫回畫面。

        *** 根本原因(查了 nicegui/element.py 原始碼才確認，不是猜測)
        ***：`build()` 剛執行完的當下，瀏覽器可能還在走 NiceGUI 的連線
        程序(HTTP 先拿到頁面 HTML，JS 再另外開 websocket 建立真正的
        client，中間這段空檔 `ui.context.client` 對應的 client 物件可能
        還沒 `has_socket_connection`，甚至因為一次握手重試被整個換掉)。
        `_toggle_watchlist_expand()` 裡 `_update_watchlist_symbol_row()`
        呼叫的 `label.text = ...` 底層(`nicegui/binding.py`
        `BindableProperty.__set__` → `TextElement._handle_text_change()`
        → `Element.update()`)在送出更新前會呼叫
        `Element._is_safe_to_interact()`，這個檢查只要目前的 client 是
        `None`/`is_deleted`(不是元素本身被刪除，是「這個瀏覽器連線」被
        判定作廢)就整個靜默略過、不送出任何東西，也不丟例外——這正是
        使用者實測回報的「產業/類別/支援商品都是空的」的真正成因：
        `enrich_candidates()`/`_translate_industry_category()` 查到的資
        料本身完全正確(用暫時的 debug log 直接證實過)，只是 fetch 跑到
        一半(平行查 20 檔，總共要一兩秒)這段期間，client 剛好處於「還
        沒真正連上/被換掉」的狀態，寫回動作被吞掉，畫面就停在建立當下
        的空白骨架。點按鈕手動展開不會踩到這個問題，因為使用者點下去的
        當下 client 早就穩定連線好了。"""
        await ui.context.client.connected()
        await _toggle_watchlist_expand(watchlist, container, chevron)

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
        watchlist_expand_state.clear()  # 舊的 container 物件都被 clear() 砍掉了，展開狀態一起歸零
        first_expand = None  # (watchlist, expand_container, chevron)——畫完整份清單才知道哪個是第一個
        with watchlist_container:
            if not watchlists:
                ui.label("尚無自選清單，按上面「＋ 新增自選清單」建立一個").classes("text-xs text-grey")
            for w in watchlists:
                with ui.row().classes("items-center gap-3 border-b py-1 w-full flex-nowrap"):
                    chevron = ui.icon("expand_more").classes("cursor-pointer text-grey")
                    with ui.row().classes("items-center gap-3 cursor-pointer flex-nowrap") as name_area:
                        ui.label(w["name"]).classes("w-48 shrink-0 font-medium truncate")
                        ui.label(w["created_at"]).classes("w-36 shrink-0 text-xs text-grey")
                        ui.label(f"{len(w['symbols'])} 檔").classes("w-16 shrink-0 text-xs text-grey")
                    ui.space()
                    ui.button("管理", on_click=lambda w=w: _open_manage_watchlist_dialog(w)).props("flat dense")
                    ui.button(
                        "重新命名", on_click=lambda w=w: _on_rename_watchlist(w),
                    ).props("flat dense")
                    ui.button(
                        "刪除", on_click=lambda w=w: _on_delete_watchlist(w),
                    ).props("flat dense color=negative")
                expand_container = ui.column().classes("w-full gap-1 pl-8")
                expand_container.visible = False
                toggle = lambda w=w, c=expand_container, chev=chevron: _toggle_watchlist_expand(w, c, chev)
                chevron.on("click", toggle)
                name_area.on("click", toggle)
                if first_expand is None:
                    first_expand = (w, expand_container, chevron)
        if first_expand is not None:
            # 預設展開第一個清單(使用者要求)——這裡不是掛在 NiceGUI 事件
            # 上觸發的(`build()` 執行完直接呼叫、新增/改名/刪除清單後也
            # 是直接呼叫，不是事件 handler)，跟 AI 建議 debounce 那個計
            # 時器同一個理由，要用 `spawn()` 保留 Task 的強參照，不能單
            # 純呼叫一個 async 函式沒人 await 就不管它。
            spawn(_auto_expand_first(*first_expand))

    new_watchlist_btn.on_click(_on_new_watchlist)

    # 記錄目前(已知)的頁籤值，初始值設成建構時給的預設頁籤
    # (`ui.tab_panels(tabs, value=watchlist_tab)`)——`_on_tab_change()`
    # 靠這個判斷「這次事件是不是真的換了頁籤」，見下面的說明。
    _last_tab_name = {"value": watchlist_tab.props["name"]}

    def _on_tab_change() -> None:
        # tabs.value 在使用者實際點頁籤切換之後，存的是頁籤的 name(字
        # 串)，不是 ui.tab() 物件本身(NiceGUI 的 ValueElement 預設
        # `_event_args_to_value()` 直接回傳 client 送來的原始字串，
        # Tabs/TabPanels 沒有覆寫這個方法去轉回物件)，拿 watchlist_tab
        # 這個物件直接比對永遠是 False——這是這個「切頁籤沒有觸發重畫」
        # 症狀的真正原因，不是下面註解原本猜測的「事件沒被呼叫到」。
        #
        # *** 一定要先過濾掉「值沒有真的改變」的事件 ***：自從預設頁籤
        # 改成 watchlist_tab 之後，Quasar 的 q-tabs 元件在前端掛載完成
        # 時會回報一次「目前值」給後端，觸發一次跟真正點擊切換一模一樣
        # 的 value_change 事件——這次事件的 tabs.value 剛好也是
        # watchlist_tab，會被下面的判斷式接住，跟著 build() 結尾那個無
        # 條件呼叫的 `_refresh_watchlist_table()` 疊在一起連續觸發兩
        # 次。兩次都會重新展開第一個清單、各自平行查一輪 IB，第一輪查
        # 完要寫回的 label 物件已經被第二輪的 `watchlist_container.
        # clear()` 砍掉、變成寫進畫面上看不到的孤兒元件——這是使用者實
        # 測回報「產業/類別/支援商品都是空的」的真正原因(名稱看起來有
        # 值是因為兩輪本來就查到一樣的資料，只是被清空的那輪運氣好比較
        # 晚才寫回去，順序每次不保證)，不是查詢或欄位對齊的問題。
        if tabs.value == _last_tab_name["value"]:
            return
        _last_tab_name["value"] = tabs.value
        if tabs.value == watchlist_tab.props["name"]:
            _refresh_watchlist_table()

    tabs.on_value_change(_on_tab_change)
    # *** 一定要在這裡主動呼叫一次，不能只靠上面的 on_value_change ***：
    # 股票篩選器改成主畫面之前，這裡是彈出視窗，`_open_dialog()` 每次開
    # 啟都會呼叫 `_refresh_watchlist_table()`，靠這個「每次打開都重畫」
    # 順便蓋掉了「切分頁本身」到底有沒有正常觸發 `_on_tab_change()` 這件
    # 事——改成主畫面、`_open_dialog()` 整支砍掉之後，才發現切到②自選
    # 清單分頁時 `_on_tab_change()` 其實沒有被呼叫到(watchlist_container
    # 停在建立當下的空白狀態，連「尚無自選清單」都不會顯示，不是資料真
    # 的是空的)。這裡不用等分頁事件，`build()` 一執行完就直接把
    # watchlist_container 填好，之後任何一次新增/改名/刪除/加入自選都已
    # 經各自呼叫 `_refresh_watchlist_table()` 保持同步，不需要依賴分頁
    # 切換這個不可靠的觸發點。
    _refresh_watchlist_table()
