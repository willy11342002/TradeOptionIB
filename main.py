"""
NiceGUI 版進入點——「連線→股票篩選器(主畫面)→選擇權報價盤/下單面板(含
帳戶權益)/委託簿/部位(標題列按鈕彈出的置中 modal)」這條路徑，成交的委
託留在委託簿裡看，沒有另外一個「成交回報」視窗(使用者要求簡化，見
web_order_book_widgets.py 開頭的說明)。下單面板/委託簿裡各自嵌了一份
到期損益圖(`web_payoff_chart_widget.py`，共用 `app/services/payoff.py`
數學)。部位(`web_position_widgets.py`)含分組顯示/現價浮動損益/分組管
理，以及自動平倉(停利/停損規則引擎，`web_auto_close_dialog.py`)。股票
篩選器(`web_screener_widget.py`)含 AI 條件建議/AI 選掃描代碼/自選清
單。回測(`web_backtest_dialog.py`)是標題列獨立按鈕，不依賴 IB 連線。原本的 PyQt5+qasync 桌面版(`pyqt.py`)已經整支刪除，這裡是唯一的進
入點。

架在 FastAPI/uvicorn 上，本來就是純 asyncio，不需要「Qt 事件迴圈兼
asyncio 迴圈」這種特殊處理，`app.services.background_tasks.py` 的
`spawn()`/`run_blocking()` 直接沿用。
"""
import asyncio
import os

from dotenv import load_dotenv

from app.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

# *** 一定要在最開頭呼叫 ***：這支 app 混了好幾種第三方非同步 SDK
# (ib_async/pydantic-ai/httpx)，出問題時可能完全不會印出任何東西，見
# app/services/app_logging.py 開頭的完整說明。
from app.services.app_logging import (
    install_asyncio_exception_handler, install_hang_watchdog_asyncio, setup_logging,
)

setup_logging()

from nicegui import app, core, ui  # noqa: E402  (要在 setup_logging() 之後才 import，跟 main.py 的順序理由一致)

# *** python-socketio 預設 max_http_buffer_size 只有 1MB，這個 app 跑久
# 了一定會爆 ***：實測踩過的真實案例——選擇權報價盤查一檔標的後，「技術
# 分析」頁籤(`web_technical_analysis_panel.py`)會自動抓一段K線塞進
# Plotly 圖表，這個更新加上頁面原本就有的股票篩選器(候選清單、篩選欄位
# 目錄、AG Grid 欄位定義)這些長期累積的狀態，單一次 websocket 訊息很容
# 易就超過 1MB，client 端(engine.io JS)會直接拒收、跳出「Message too
# long / The message is large for WebSocket transmission」再觸發整頁重
# 新整理——使用者感受到的「T字報價查完現價又跳掉」、「自選清單分頁切過
# 去看不到資料」都是這個整頁被迫重新整理的下游症狀，不是個別欄位邏輯錯
# 誤。`core.sio.eio` 是 python-socketio 底層真正處理 handshake/收送訊息
# 的 engineio server，`max_http_buffer_size` 是它建構子唯一控制訊息上限
# 的參數，NiceGUI 的 `ui.run()` 沒有開放這個旗標，只能在 `core.sio`(在
# `from nicegui import core` 這行 import 當下就已經建構好的模組級單例)
# 建好之後直接改它的屬性——調大到 20MB，這台機器是本機單人工具，不會被
# 拿來源源不絕塞資料攻擊，不需要嚴格卡在預設值。
core.sio.eio.max_http_buffer_size = 20_000_000

from app import paths  # noqa: E402
from app.models.auto_close_manager import AutoCloseManager  # noqa: E402
from app.models.ib_client import IBClient  # noqa: E402
from app.models.ib_order_client import IBOrderClient  # noqa: E402
from app.models.order_book import OrderBookManager  # noqa: E402
from app.models.positions import PositionManager  # noqa: E402
from app.services import ib_prefs, web_theme  # noqa: E402
from app.views import (  # noqa: E402
    web_backtest_dialog, web_order_book_widgets, web_order_entry_widget, web_position_widgets,
    web_quote_board_page, web_screener_widget,
)

# *** IB 連線(IBClient)要存成 process 級的全域狀態，不能只放在
# index() 的區域變數/closure 裡 ***：NiceGUI 的 @ui.page 每次瀏覽器「重新
# 整理」都是重新呼叫一次 index()，區域變數會整個歸零重來，之前連線成功的
# 狀態(哪個 ib_client、已經連上了)就這樣不見了，畫面才會跳回連線表單，
# 即使 IB 這邊的 socket 其實根本沒斷。這是本機單人工具，process 內同一
# 時間本來就只會有一個真正的 IB 連線(見 CLAUDE.md 的說明)，用一個模組級
# 變數記住目前這個連線，重新整理時先檢查「是不是已經連線了」，是的話直
# 接跳過連線表單。
_ib_client: IBClient | None = None

# *** IBOrderClient/OrderBookManager 也要是 process 級單例，但理由跟
# IBClient 不太一樣，且比它更嚴格 ***：IBQuoteClient 每次進頁面可以放心
# 重新建立(靠 ui.context.client.on_disconnect() 清乾淨舊訂閱)，但
# IBOrderClient.__init__ 會掛
# `self._ib.orderStatusEvent += self._on_order_status`(execDetailsEvent/
# errorEvent 同理)，這些訂閱沒有解除機制——如果每次重新整理網頁都重新
# `IBOrderClient(ib_client)`，舊分頁留下的訂閱會一直疊加，同一筆委託狀
# 態變化會被處理 N 次(N = 重新整理過幾次)。這兩個物件代表的是「這個
# process 對這個 IB 連線唯一一份委託簿」，只能在整個連線的生命週期裡建
# 立一次。
_order_client: IBOrderClient | None = None
_order_book_manager: OrderBookManager | None = None
# *** PositionManager 也要是 process 級單例，理由跟 IBOrderClient 一樣
# 更嚴格 ***：建構子會掛 `self._ib.positionEvent += self._on_position_
# event`，這個訂閱沒有解除機制，每次重新整理網頁都重新 `PositionManager
# (...)` 的話，舊分頁留下的訂閱會一直疊加。*** 唯一的已知落差 ***：它
# 建構時吃的 `quote_client` 來自第一次成功連線當下那個 `web_quote_board_
# page.build()`，之後每次重新整理頁面 `web_quote_board_page.build()` 都
# 會建一個新的 IBQuoteClient(那支本來就設計成可以放心重建，見它自己的
# 說明)，但這個已經建好的 PositionManager 單例不會跟著換——目前只有損
# 益圖(web_payoff_chart_widget.py)在用它，損益圖只吃部位的履約價/均
# 價、不吃即時報價，這個落差不影響損益圖；只有在之後真的要做「即時報
# 價驅動的部位浮動損益」時才需要處理，先不在這裡預先解決一個目前沒有呼
# 叫路徑的問題。
_position_manager: PositionManager | None = None
# *** AutoCloseManager 也要是 process 級單例，理由跟 PositionManager 一
# 樣 ***：建構子會掛 `position_manager.positions_changed`/
# `order_book_manager.records_changed` 兩個訂閱，同樣沒有解除機制，重新
# 整理網頁不能重建一份新的，不然舊分頁的訂閱會一直疊加、同一次觸發被處
# 理 N 次。
_auto_close_manager: AutoCloseManager | None = None

# *** 模擬(paper)/正式(live) 環境切換用——預設模擬，比較安全 ***：沒有
# 連線表單了(host/clientId 固定吃環境變數，見 index() 開頭)，port 完全
# 由這個旗標決定(4002/4001，跟 app/services/ib_prefs.py 的
# DEFAULT_PORT_PAPER/DEFAULT_PORT_LIVE 常數一致)。畫面右上角的切換按鈕
# 改這個值之後會整個重新整理頁面，靠 index() 開頭「_ib_client 是否已連
# 線」的判斷式重新走一次連線流程，不在切換按鈕的 handler 裡直接動手改
# 畫面——這是本機單人工具，正式/模擬環境全域只有一份，不需要每個分頁各
# 自記自己的環境。
_simulation: bool = True

# *** 沒有連線表單之後，每次有分頁載入 index() 都會自動嘗試連線，多開幾
# 個分頁/重新整理很快就可能同時觸發兩次 connect_async() ***：實測踩過
# 的真實案例——两次幾乎同時發生的連線請求都用同一個 clientId(環境變數
# 固定值，不像舊版表單每次手動填可能填到不同值)，第一個還沒連完、
# `_ib_client` 還是 None 的當下，第二個判斷式一樣覺得「還沒連線」，也
# 跟著送出一次 connect_async()，IB 直接回 Error 326(clientId 已經被佔
# 用)把第二個踢掉。用一個 asyncio.Lock 讓「檢查有沒有連線→真的送出連線
# 請求」這整段變成互斥區間，晚到的請求會等前一個連線請求做完，做完之後
# 重新檢查一次「是不是已經連好了」，連好了就直接沿用，不會真的送出第二
# 次 connect_async()。
_connect_lock = asyncio.Lock()


async def _on_startup() -> None:
    # 只有在事件迴圈已經在跑的時候才能呼叫這兩個(都需要 running loop)，
    # app.on_startup 註冊的 callback 保證跑在這個時機點。
    install_asyncio_exception_handler(asyncio.get_running_loop())
    install_hang_watchdog_asyncio()


app.on_startup(_on_startup)


def _build_trading_screen(ib_client: IBClient, side_buttons: dict) -> None:
    """組出整個交易畫面：股票篩選器放主畫面(`web_screener_widget.build()`
    直接把內容組進目前這個容器，不是彈出的 modal)，下單面板／委託簿／
    部位／選擇權報價都是各自獨立、置中顯示的 modal(`ui.dialog()`)，
    標題列上各有一顆按鈕(`side_buttons`，由 index() 先建立好、初始是
    disabled 的)——按了才彈出來，不是像舊版抽屜那樣常駐展開。這幾個
    build() 依序呼叫，靠 closure 互相接起來：
    - 報價盤雙擊某個履約價的 call/put 價格欄位時(`on_leg_selected`)，把
      資料轉交給下單面板的 `set_context()`(那支函式本身也會自動把下單
      面板的視窗彈出來，不用使用者自己再按按鈕)。
    - 股票篩選器候選清單每一列的「期權報價」按鈕呼叫報價盤的
      `open_quote_board(symbol)`，彈出報價盤並自動帶入這檔標的、直接查
      詢一次。

    *** 這四個 build() 現在都是「秒回」的——真正的 dialog UI(AG
    Grid/Plotly 圖表/技術分析頁籤這些)延後到使用者第一次按按鈕才建
    ***：實測抓到真實案例，`index()` 連線成功後如果在這裡把四個視窗的
    完整 UI 都同步建完，加總很容易超過 NiceGUI `@ui.page` 預設 3 秒的
    `response_timeout`，讓整頁被砍成 500(參見 `app/services/lazy_ui.py`
    開頭的完整說明)。這裡回傳的 `open_quote_board`/`open_box`/
    `open_order_entry`/`open_positions`/`set_context` 都只是輕量的
    façade，第一次呼叫才觸發真正的 UI 建構，之後重複呼叫沿用同一份，
    不會重複訂閱 Signal。"""
    global _order_client, _order_book_manager, _position_manager, _auto_close_manager
    if _order_book_manager is None:
        _order_client = IBOrderClient(ib_client)
        _order_book_manager = OrderBookManager(_order_client)

    set_context = None  # 下單面板還沒建立出來之前的佔位，見下面的說明

    def _on_leg_selected(*args) -> None:
        # 報價盤在建立當下就可能需要這個 callback(雙擊事件的訂閱在
        # web_quote_board_page.build() 內部完成)，但下單面板要等報價盤
        # 建完才能跟著建——用這個轉接函式延後綁定，實際雙擊一定發生在
        # 整個畫面組完之後，這裡取用 set_context 是安全的。
        if set_context is not None:
            set_context(*args)

    quote_client, get_contract, open_quote_board = web_quote_board_page.build(
        ib_client, on_leg_selected=_on_leg_selected,
    )
    if _position_manager is None:
        _position_manager = PositionManager(ib_client, _order_book_manager, quote_client)
    if _auto_close_manager is None:
        _auto_close_manager = AutoCloseManager(ib_client, _position_manager, _order_book_manager)
    # *** 委託簿要先建好，下單面板才能拿到 open_box 傳進去 ***：暫存到
    # 委託簿之後要自動關掉下單面板、跳去委託簿(使用者要求)，下單面板的
    # build() 需要接一個「開委託簿」的 callback——委託簿本身不依賴下單
    # 面板的任何東西，對調兩者的建構順序沒有副作用。
    open_box = web_order_book_widgets.build(_order_book_manager, _position_manager)
    set_context, open_order_entry = web_order_entry_widget.build(
        _order_book_manager, quote_client, get_contract, _position_manager, open_box, ib_client,
    )
    open_positions = web_position_widgets.build(_position_manager, _auto_close_manager)
    web_screener_widget.build(ib_client, open_quote_board)

    for button, opener in (
        (side_buttons["order_entry"], open_order_entry),
        (side_buttons["order_book"], open_box),
        (side_buttons["positions"], open_positions),
        (side_buttons["quote_board"], open_quote_board),
    ):
        button.on_click(opener)
        button.enable()


def _toggle_environment() -> None:
    """右上角模擬/正式切換按鈕——沒有另外做「原地重連」的邏輯，直接斷線
    +清空五個 process 級單例(_ib_client/_order_client/_order_book_manager/
    _position_manager/_auto_close_manager)+整頁重新整理，讓 index() 開頭
    那段「_ib_client 是否已連線」的判斷式重新走一次全新的連線流程(port
    由新的 _simulation 值決定)。委託簿/委託 client/部位/自動平倉這些物
    件的建構子/初始化邏輯只在「全新連線」這個時機點寫過一次，切環境本來
    就等同於斷線重連，沒有必要為了原地切換另外寫一套「清空重建」的邏
    輯，多一套邏輯多一種可能兜不起來的風險。"""
    global _ib_client, _order_client, _order_book_manager, _position_manager, _auto_close_manager, _simulation
    if _ib_client is not None:
        _ib_client.disconnect()
    _ib_client = None
    _order_client = None
    _order_book_manager = None
    _position_manager = None
    _auto_close_manager = None
    _simulation = not _simulation
    ui.navigate.reload()


@ui.page(
    "/",
    # *** 一定要蓋掉 NiceGUI 預設 3.0 秒的 response_timeout ***：實測用
    # timing log 抓到真實案例——`ib_client.connect_async()` 內部呼叫的
    # `ib.connectAsync()` 會併發打 `reqOpenOrdersAsync()`/
    # `reqCompletedOrdersAsync()` 等好幾個「連線當下順便同步」的請求，
    # 這兩個目前在這個帳戶會timeout(見 app/models/ib_client.py::
    # connect_async() 開頭關於 IB Gateway 唯讀模式的說明)，`connect_
    # async()` 因此固定要吃滿它自己的 `timeout=10.0` 秒才回傳，遠超過
    # NiceGUI 預設值——沒蓋掉的話，每次連線都會被 NiceGUI 自己的機制在
    # 3 秒整砍成 500(`nicegui/page.py` 的 `asyncio.wait(..., timeout=
    # response_timeout)`)，跟畫面組得快不快完全無關。15 秒是
    # `connect_async()` 那個 10 秒上限加緩衝，不是隨便挑的數字。
    response_timeout=15.0,
)
async def index() -> None:
    global _ib_client
    web_theme.apply()

    # *** ui.header() 一定要是頁面的直接子層，不能包在下面 placeholder
    # 那個會被 clear()/重建的 ui.column() 裡面 ***：Quasar 的版面配置
    # (QLayout)要求 QHeader 是固定的結構元件，跟著內容一起被清空重建會
    # 壞掉。下單面板/委託簿/部位/選擇權報價都是標題列按鈕彈出的置中
    # modal，不是常駐面板——股票篩選器才是主畫面(見 CLAUDE.md 路線
    # 圖)。連線成功前這幾個按鈕還沒有東西可以開，先 disable，
    # `_build_trading_screen()` 建好對應的 dialog 之後才各自 enable。
    with ui.header().classes("items-center justify-between"):
        ui.label("Options TBoard").classes("text-lg font-semibold")
        with ui.row().classes("items-center gap-2"):
            side_buttons = {
                "order_entry": ui.button("下單／帳戶").props("flat color=white"),
                "order_book": ui.button("委託簿").props("flat color=white"),
                "positions": ui.button("部位").props("flat color=white"),
                "quote_board": ui.button("選擇權報價").props("flat color=white"),
            }
            # 回測不依賴 IB 連線(純歷史資料合成回測)，不放進上面那組「連線後才 enable」的
            # side_buttons，永遠可以按。dialog 內容第一次按才建(lazy_open)，pandas/yfinance
            # 要等到使用者真的按下「執行回測」才 import，首頁載入不受影響，見
            # app/views/web_backtest_dialog.py 開頭。
            ui.button("回測", on_click=web_backtest_dialog.build()).props("flat color=white")
            # 模擬用綠色(positive)、正式用紅色(negative)特別標出來，不
            # 用跟其他按鈕一樣的 flat 樣式——這顆按鈕代表的是「等一下下
            # 單會真的送到哪個帳戶」，故意讓它比較顯眼，不要跟旁邊幾顆
            # 功能按鈕長得一樣不小心按錯。
            env_btn = ui.button(
                "模擬環境" if _simulation else "正式環境",
                on_click=_toggle_environment,
            ).props(f"color={'positive' if _simulation else 'negative'}")
    for button in side_buttons.values():
        button.disable()

    placeholder = ui.column().classes("w-full")

    with placeholder:
        if _ib_client is not None and _ib_client.is_connected:
            # 重新整理(或開新分頁)時如果 process 裡已經有一個連線好的
            # IBClient，直接接上去用，不用重新連線。
            _build_trading_screen(_ib_client, side_buttons)
            return
        status_label = ui.label("連線中...")

    # *** 沒有連線表單，host/clientId 固定吃環境變數，port 由右上角切換
    # 鈕決定的 _simulation 算出來 ***：跟舊版 web_connect_page.py 的邏輯
    # 比，少了「使用者手動輸入」這一步，但驗證/重試邏輯是一樣的——連線
    # 失敗就顯示錯誤訊息，請使用者確認 IB Gateway 有沒有開著再重新整理
    # 頁面重試(不用另外做重試按鈕，重新整理頁面本來就會重新走一次這段)。
    async with _connect_lock:
        # 排隊等鎖的期間，可能已經有另一個分頁的請求把連線建好了(見
        # _connect_lock 定義處的說明)，拿到鎖之後要重新檢查一次，是的話
        # 直接沿用，不要無條件再送一次 connect_async()。
        if _ib_client is not None and _ib_client.is_connected:
            status_label.text = ""
            placeholder.clear()
            with placeholder:
                _build_trading_screen(_ib_client, side_buttons)
            return

        host = os.environ.get("IB_HOST") or ib_prefs.DEFAULT_HOST
        try:
            client_id = int(os.environ.get("IB_CLIENT_ID") or ib_prefs.DEFAULT_CLIENT_ID)
        except ValueError:
            client_id = ib_prefs.DEFAULT_CLIENT_ID
        port = ib_prefs.DEFAULT_PORT_PAPER if _simulation else ib_prefs.DEFAULT_PORT_LIVE

        ib_client = IBClient()
        ok = await ib_client.connect_async(host, port, client_id)
        if not ok:
            status_label.text = f"連線失敗，檢查 IB Gateway 是否開著、port 對不對(host={host}, port={port})"
            return

    _ib_client = ib_client
    paths.set_environment(_simulation)
    env_btn.text = "模擬環境" if _simulation else "正式環境"

    placeholder.clear()
    with placeholder:
        _build_trading_screen(ib_client, side_buttons)


if __name__ in {"__main__", "__mp_main__"}:
    # *** reload=True 曾經卡進無限重啟迴圈，根因跟防毒/OneDrive這類外部
    # 因素完全無關，是我們自己的 logging 設定跟 uvicorn reload 機制互咬
    # ***：讀了 .venv/Lib/site-packages/uvicorn/supervisors/
    # watchfilesreload.py 的原始碼才確認——uvicorn 底層的 watchfiles
    # watch() 呼叫沒有套用任何 include/exclude 規則，是「先無條件監控整
    # 個資料夾樹、偵測到任何變動都先印一行 INFO 等級的 watchfiles.main:
    # N change detected，之後才決定要不要真的重啟」。這一行 log 被
    # app_logging.py 的 root logger handler 一起寫進 logs/app.log，但
    # logs/ 也在被監控的範圍裡，這一寫又被偵測成新的變動，變成自己餵自
    # 己的無窮迴圈——跟這台機器上其他 FastAPI/reload 專案不會發生同樣問
    # 題的原因完全吻合：那些專案沒有像 app_logging.py 這樣把 root
    # logger 全部導向一個受監控資料夾底下的檔案。已經在
    # app_logging.py::_silence_watchfiles_feedback_loop() 把
    # watchfiles 這個 logger 的等級拉到 WARNING，源頭直接掐斷，不影響
    # uvicorn 實際判斷要不要重啟的邏輯，reload 模式可以放心使用。
    #
    # 預設關閉 reload(正常交易用途不需要、也少一個變動來源)，開發時想要
    # autoreload 就加 --reload 參數啟動(對應 .claude/launch.json 的
    # "nicegui-main-reload" configuration)。
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    ui.run(title="Options TBoard (Web)", reload=args.reload)
