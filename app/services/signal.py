"""
`Signal`：`QObject`+`pyqtSignal` 的極簡替代品，讓 model 層(IBClient/
IBQuoteClient/...)不用依賴 PyQt5，NiceGUI 版跟舊版 Qt app 才能共用同一份
model 程式碼(NiceGUI 進程沒有 Qt，不能 import PyQt5 的東西)。

不需要 Qt signal 那套「跨執行緒 queued connection」機制——這支 app 從頭到
尾只在單一 asyncio 事件迴圈執行緒上跑(不管是舊版 qasync 的迴圈還是新版
NiceGUI/uvicorn 的迴圈)，一個同步呼叫的 callback list 就是行為完全對等
的替代品：呼叫端原本 `some_signal.connect(handler)`/`some_signal.emit(x)`
的寫法完全不用改。

*** 一定要在 `__init__` 裡建立實例，不能當成類別屬性 ***：類別屬性的話
所有實例會共用同一個 `Signal`(同一份訂閱清單)，這是 pyqtSignal 用
Qt 的 meta-object 系統在背後幫每個實例分開處理的地方，這裡沒有那套機制，
要自己在 `__init__` 裡各自建立一份。
"""
from typing import Callable


class Signal:
    def __init__(self) -> None:
        self._callbacks: list[Callable] = []

    def connect(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    def disconnect(self, callback: Callable) -> None:
        self._callbacks.remove(callback)

    def emit(self, *args) -> None:
        for callback in list(self._callbacks):
            callback(*args)
