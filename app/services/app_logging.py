"""
統一的錯誤/崩潰紀錄機制——目的是讓「主控台完全沒有任何錯誤訊息就直接
消失」這種情況不再發生。這支 app 是 asyncio + 多個第三方非同步 SDK
(ib_async/pydantic-ai/httpx)混在同一個事件迴圈的組合，一個問題可能從
三個完全不同的管道洩漏出來，各自要單獨接管才會真的被記錄下來，缺一條
都可能造成「明明出事了、但看起來什麼訊息都沒有」：

    1. 一般 Python 例外(sys.excepthook)——大部分同步程式碼的錯誤走這條。
    2. asyncio 自己攔到、不是用 raise 往上丟的例外
       (loop.set_exception_handler())——asyncio 偵測到「Task 還沒做完
       就被 GC 回收掉」「callback 裡本身丟例外」這類狀況時，直接呼叫這
       個 handler，預設只會經過 logging 模組(等級 ERROR)，如果沒設定
       logging 輸出目的地，這則訊息可能哪裡都看不到。"Task was
       destroyed but it is pending!" 就是走這一條，不是普通的 Python
       exception，try/except 攔不到。
    3. 原生層級的致命訊號(faulthandler)——真正的 segfault/access
       violation，前兩種完全攔不到，只有 faulthandler 能在收到訊號的當
       下把各執行緒目前的 Python 呼叫堆疊印出來，是唯一能看到「當下卡
       在哪一行」的手段。

三條全部接到同一個檔案(logs/app.log)，用「開啟後每行立刻 flush」的方式
寫，不要等緩衝區滿或 process 正常結束才落盤——如果 process 是被強制終
止/當掉，作業系統緩衝區裡還沒真正寫進硬碟的內容會直接消失，這正是「主
控台明明應該要印東西、卻什麼都沒看到」的常見成因之一(不是真的沒印，是
印了但沒来得及落盤)。
"""
import asyncio
import faulthandler
import logging
import sys
import traceback

from app.paths import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_log_file_handle = None
_hang_watchdog_task = None


def setup_logging() -> None:
    """main.py 最開頭就要呼叫，越早越好——要在任何 asyncio 物件建立之前
    先把管道接上，才不會漏掉啟動過程中的訊息。"""
    global _log_file_handle
    # buffering=1 = line-buffered：每寫一行就實際觸發一次 flush，不要等
    # 緩衝區滿——見檔案開頭的說明，這是這支 log 機制存在的意義所在。
    _log_file_handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(_log_file_handle), logging.StreamHandler(sys.stderr)],
        force=True,  # 蓋掉 pydantic-ai 等第三方套件 import 時可能已經呼叫過的 basicConfig
    )

    sys.excepthook = _log_unhandled_exception
    faulthandler.enable(file=_log_file_handle, all_threads=True)
    _silence_watchfiles_feedback_loop()

    logging.info("=== 應用程式啟動，logging 已就緒：%s ===", LOG_FILE)


def _silence_watchfiles_feedback_loop() -> None:
    """main.py 用 `ui.run(reload=True)` 時，uvicorn 底層用 `watchfiles`
    監控檔案變動——實測抓到真實案例：`watchfiles` 自己的原始 watch()
    呼叫沒有套用 include/exclude 規則(見
    `.venv/Lib/site-packages/uvicorn/supervisors/watchfilesreload.py`
    `WatchFilesReload.__init__` 裡 `watch(..., watch_filter=None, ...)`)，
    是「先無條件監控整個資料夾樹、偵測到任何變動都先印一行 INFO 等級的
    `watchfiles.main: N change detected`，uvicorn 才在這之後才用
    include/exclude 規則決定要不要真的重啟」。這一行 INFO log 會被上面
    `logging.basicConfig()` 設定的 root logger handler 一起寫進
    `logs/app.log`——問題是 `logs/` 本身也在被監控的資料夾樹裡，這一寫
    又被 watchfiles 偵測成一次新的檔案變動，變成自己餵自己的無窮迴圈
    (logs/app.log 洗版洗出一堆 `watchfiles.main: 1 change detected`，
    不是真的有人一直在存檔，也不是防毒軟體之類的外部干擾)。把
    `watchfiles` 這個 logger 的等級拉到 WARNING 以上，它自己的 INFO 等
    級變動通知就不會再被任何 handler 處理、更不會被寫回 app.log，迴圈
    的源頭直接被掐斷；uvicorn 是否真的重新載入 app 完全不受影響，因為
    那個判斷邏輯(`FileFilter`)是獨立的，不受這個 logger 等級控制。"""
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _log_unhandled_exception(exc_type, exc_value, exc_tb) -> None:
    logging.critical("未攔截的例外", exc_info=(exc_type, exc_value, exc_tb))
    # 保留原本(沒裝自訂 excepthook 時)的行為——印到 stderr，不要讓裝了
    # log 機制之後終端機反而看不到東西。
    traceback.print_exception(exc_type, exc_value, exc_tb)


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """一定要在 loop 物件建立之後才呼叫得到(qasync.QEventLoop(app) 之
    後)。asyncio 自己攔到的例外/警告(包含 "Task was destroyed but it is
    pending!" 這種)預設只會走 logging，這裡明確接手成同一份 log，順便
    在有附帶 exception 物件時印出完整堆疊。"""
    def handler(loop_, context):
        message = context.get("message")
        exc = context.get("exception")
        logging.error("asyncio 內部例外／警告：%s | context=%s", message, context, exc_info=exc)

    loop.set_exception_handler(handler)


def install_hang_watchdog_asyncio(interval: float = 5.0, dump_after: float = 15.0) -> None:
    """主執行緒被同步卡死(不是乾淨的 crash，是完全卡住不動、之後被使用者
    強制關掉)時，前面兩條管道(excepthook/asyncio handler)加上
    faulthandler.enable() 全部攔不到——它們都是「某個東西丟出例外或訊
    號」才會觸發，事件迴圈完全沒機會處理任何東西的話，什麼訊號都不會發
    生。實測踩過的真實案例(AI 建議查詢、同時 IB 選擇權報價大量湧入)就
    是這種：logs/app.log 前面幾條管道全部沒有任何輸出，process 直接卡
    死。

    做法是拿 faulthandler.dump_traceback_later() 當一個「死人開關」
    (dead man's switch)：每次呼叫都是「n 秒後印出所有執行緒的呼叫堆
    疊，除非在那之前被取消/重設」。這裡用一個純 asyncio 背景 task 的
    `asyncio.sleep()` 迴圈當心跳，每 `interval` 秒就重新把這個倒數設成
    `dump_after` 秒後觸發——只要事件迴圈還在正常轉動(還能排到這個
    task)，倒數就會不斷被重設、永遠不會真的觸發。一旦事件迴圈卡住不
    動，心跳停止觸發，上一次設定的倒數會自然到期，把當下所有執行緒(包
    含卡住的那個)的 Python 呼叫堆疊印進 log——下次再發生同樣的「無聲消
    失」，就會留下卡在哪一行的第一手證據，不用再靠零星加 trace log 土
    法煉鋼定位。

    一定要在事件迴圈已經在跑的時候呼叫(`asyncio.ensure_future` 需要一個
    running loop)，NiceGUI 用 `app.on_startup()` 註冊的 callback 就是跑
    在這個時機點。`dump_after` 要大於 `interval` 好幾倍，避免事件迴圈只
    是暫時處理別的事情就被誤判成卡死。"""
    global _hang_watchdog_task

    async def _heartbeat() -> None:
        while True:
            faulthandler.dump_traceback_later(dump_after, repeat=False, file=_log_file_handle)
            await asyncio.sleep(interval)

    _hang_watchdog_task = asyncio.ensure_future(_heartbeat())  # 保留參照，不然沒人持有會被 GC 回收掉
