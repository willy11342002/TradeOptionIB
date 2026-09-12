import asyncio
import sys

from dotenv import load_dotenv

from app.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

import qasync
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QDialog

from app.views.connect_dialog import ConnectDialog
from app.views.main_window import MainWindow


def _show_connect_dialog(loop) -> None:
    # *** ConnectDialog().exec_() 一定要在 loop.run_forever() 已經開始
    # 跑了之後才呼叫，而且不能包在 asyncio Task 裡面呼叫，兩個條件都要
    # 滿足 ***：
    #   1. qasync 只有在 run_forever()(內部呼叫
    #      asyncio.events._set_running_loop(self))期間，
    #      asyncio.get_running_loop() 才找得到這個迴圈——@asyncSlot()
    #      內部就是靠這個排程；放在 run_forever() 之前呼叫 exec_()，一
    #      按下「連線」就會直接噴 RuntimeError: no running event loop。
    #   2. 但如果為了解決(1)把 exec_() 包進一個用 loop.create_task()
    #      排程的 async function 裡面呼叫，反而會撞上另一個問題：
    #      dialog.exec_() 這個巢狀 Qt 迴圈在處理「連線」按鈕的點擊事件
    #      時，會在同一個 Python 呼叫堆疊裡同步觸發 @asyncSlot() 想建立
    #      的新 Task——這時候外層那個包住 exec_() 的 Task 都還沒把自己
    #      交還給事件迴圈(卡在同步的 exec_() 呼叫裡)，asyncio 會判定
    #      「同一個執行緒不能同時有兩個 Task 在跑」，丟出 RuntimeError:
    #      Cannot enter into task ... while another task is being
    #      executed。
    #   兩者都要避免，答案是用 QTimer.singleShot(0, ...) 排程一個「純
    #   Qt callback」(不是 asyncio Task)，在 run_forever() 已經跑起來
    #   之後才被 Qt 自己的事件分派呼叫——這樣 dialog.exec_() 不在任何
    #   Task 的呼叫堆疊裡面，按鈕點擊要建立的新 Task 不會跟誰衝突，
    #   同時 get_running_loop() 也找得到這個從 run_forever() 開始就一
    #   直設定著的迴圈。
    connect_dialog = ConnectDialog()
    if connect_dialog.exec_() != QDialog.Accepted:
        loop.stop()
        return

    window = MainWindow(ib_client=connect_dialog.ib_client)
    window.showMaximized()


def main():
    app = QApplication(sys.argv)

    # *** 用 qasync 讓 Qt 跟 asyncio 共用同一個事件迴圈，取代 ib_async
    # 自帶的 util.useQt() ***：util.useQt() 是在 asyncio callback 裡巢
    # 狀塞一個 Qt QEventLoop 的 hack，實測發現只要開始跑(第一次
    # connect() 之後)就不會再回到「沒在跑」的狀態，導致之後任何一個同
    # 步 IB 呼叫都保證撞上 asyncio 的「這個事件迴圈已經在跑了」。改用
    # qasync.QEventLoop 之後，Qt 的事件分派本身就是這個 asyncio 迴圈在
    # 跑，往後 IB API 一律用 xxxAsync() + await，見 ib_client.py。
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        QTimer.singleShot(0, lambda: _show_connect_dialog(loop))
        loop.run_forever()
    # 舊版群益(SKCOM comtypes COM 物件)在直譯器自己收尾時容易 segfault，
    # 所以用 os._exit(0) 跳過正常收尾——ib_async 不持有 COM 物件，這個
    # workaround 不再需要，讓程式正常結束。


if __name__ == "__main__":
    main()
