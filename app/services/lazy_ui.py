"""按鈕點了才顯示的視窗(下單面板/委託簿/部位/選擇權報價)不需要在頁面剛
載入、IB 剛連線成功的當下就建完——這是 main.py 的 index() 常常撞上
NiceGUI 預設 3 秒 response_timeout(見 app/views/web_quote_board_page.py
開頭的除錯記錄：實測抓到連線本身不到 0.1 秒完成，真正吃掉 2.6 秒的是連
線成功後緊接著同步組裝這幾個視窗的 UI，AG Grid/Plotly 圖表/技術分析頁
籤全部擠在同一段流程裡才回應瀏覽器第一次請求)的主因。這裡提供一個共用
的「延後到第一次真的要開啟才建」包裝器，取代讓每個 view 檔案各自維護一
份「有沒有建過」的旗標。"""
import inspect
from typing import Callable


def lazy_open(build_dialog: Callable[[], Callable]) -> Callable:
    """回傳一個可以直接掛在按鈕 on_click 上的 open 函式：第一次呼叫才真
    的執行 build_dialog()(建立 ui.dialog() 底下的完整內容，回傳真正的
    open 函式)，之後的呼叫直接沿用那個已經建好的 open 函式，不會重複
    build(重複 build 會讓 dialog 裡掛的 Signal 訂閱一直疊加，見
    CLAUDE.md 對 process 級單例訂閱的說明)。build_dialog() 回傳的 open
    函式不管是同步(例如 `ui.dialog().open`)還是 async def 都吃得下。"""
    state: dict = {}

    async def open_(*args, **kwargs):
        if "open" not in state:
            state["open"] = build_dialog()
        result = state["open"](*args, **kwargs)
        if inspect.isawaitable(result):
            await result

    return open_
