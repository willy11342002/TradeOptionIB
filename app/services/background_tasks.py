"""
asyncio 的 Task 生命週期陷阱：`asyncio.ensure_future(coro)` 建立的 Task
物件，事件迴圈只對它保留弱參照，沒有其他地方持有強參照的話，執行到一半
就可能被 GC 回收——回收當下印的是「Task was destroyed but it is
pending!」，不是 Python 例外，try/except 攔不到，這是 Python 官方文件
明講的 asyncio 已知陷阱，不是這支程式獨有的偶發問題。

qasync 的 `@asyncSlot()` 裝飾器本身就踩在這個陷阱上：它建立的 task 只是
wrapper() 函式裡的區域變數(見 .venv/Lib/site-packages/qasync/__init__.py
的 asyncSlot 實作)，wrapper 一返回(Qt 呼叫完 slot 就會這樣)就沒有任何
地方再持有這個 Task 物件。已經在 logs/app.log 實測抓到真實案例：
ScanCodePicker._fire_ai_lookup() 的 OpenRouter 呼叫明明成功拿到 200 OK
回應，Task 物件卻在那之後 1.3 秒被 GC 回收掉，整個呼叫沒有正常結束，app
隨之無聲消失——不是臆測，是 log 裡的第一手證據。

任何預期會執行超過一瞬間(等網路回應、迴圈呼叫多次 IB/AI API)的 async
handler，都不能只靠 @asyncSlot() 自動建立的 Task，要改用這裡的
spawn()：用一個模組級的集合保留強參照，直到那個 Task 真正完成(不管成功
/失敗/取消)才移除，確保 GC 絕對不會在跑到一半時把它回收掉。
"""
import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Coroutine

_background_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    """建立一個 asyncio Task 並保留強參照直到完成——取代直接把
    @asyncSlot() 接在長時間執行的 async 方法上。"""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).error("背景任務發生未攔截的例外", exc_info=exc)


def _thread_initializer() -> None:
    """*** Windows 專用，這是實測抓到真實案例後才加的，不是預防性寫法
    ***：這個 app 在 Windows 上執行時，中文輸入法(IME)是靠 COM 元件
    (Text Services Framework)運作，綁在主執行緒的 COM apartment。
    `concurrent.futures.ThreadPoolExecutor` 預設的工作執行緒完全沒有初
    始化 COM——實測用 faulthandler 抓到真實崩潰案例：使用者在對話框裡用
    中文輸入法打字，AI 呼叫(丟到這個執行緒池背景執行)完成的當下，
    Windows 丟出 fatal exception code 0x80010108(COM 的
    RPC_E_DISCONNECTED，「呼叫的物件已經跟用戶端斷線」)，懷疑是背景執
    行緒沒初始化 COM，某個環節間接干擾到主執行緒 IME 正在用的 COM 物
    件。讓池子裡每個工作執行緒一開始就用多執行緒公寓模式(MTA)初始化
    COM——這幾個執行緒純粹做背景運算，不會建立/持有任何 COM UI 物件，
    不需要單執行緒公寓(STA)那種跟 UI 訊息迴圈綁定的模式。非 Windows 平
    台沒有 pythoncom，直接跳過。"""
    if sys.platform != "win32":
        return
    try:
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001
        # 這段本身是防禦性修法(懷疑，不是 100% 證實的機制)，初始化失敗
        # 也不該讓整個工作執行緒直接壞掉(ThreadPoolExecutor 的
        # initializer 拋例外會讓那個執行緒整個報廢)——記下來，讓後續呼
        # 叫還是有機會正常執行。
        logging.getLogger(__name__).warning("背景執行緒 COM 初始化失敗", exc_info=True)


# 給任何「同步/阻塞、可能間接碰到 Windows COM API 的呼叫」(目前是
# app/models/openrouter_client.py 的 OpenRouter 呼叫)專用的背景執行緒
# 池——不能用 loop.run_in_executor(None, ...) 的預設執行緒池，那個池子
# 的執行緒沒有跑過上面這段 COM 初始化。
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-blocking", initializer=_thread_initializer)


def run_blocking(fn: Callable, *args):
    """把一個同步/阻塞的函式丟到這個 app 專用的背景執行緒池執行，回傳
    可以 await 的 Future。呼叫端(openrouter_client.py)要自己確保 fn 完
    全不碰任何 Qt widget——這裡只負責「在哪個執行緒跑」，不負責執行緒安
    全性，那是 fn 本身的責任。"""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_executor, fn, *args)
