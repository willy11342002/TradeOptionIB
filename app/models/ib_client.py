"""
IB TWS/Gateway 連線封裝。

跟群益不一樣，IB 不需要帳密登入——TWS 或 IB Gateway 本身要先手動開著、
登入好，這裡只是用 socket 連上那個已經登入的 process (host/port/
clientId)，沒有 SKCenterLib_Login 那種三段式流程，也沒有帳號選擇這一
步(帳號是 TWS/Gateway 登入時就決定好的)。

*** 這支 app 用 qasync 讓 Qt 跟 asyncio 共用同一個事件迴圈，不是
ib_async 自帶的 util.useQt() ***：util.useQt() 是在 asyncio callback
裡面巢狀塞一個 Qt QEventLoop 的 hack，實測發現這個合併出來的迴圈只要
開始跑(第一次 connect() 之後)就不會再回到「沒在跑」的狀態，導致之後
任何一個同步 IB 呼叫(ib.qualifyContracts()/ib.sleep()/...這種內部是
loop.run_until_complete() 的呼叫)保證撞上 asyncio 的「這個事件迴圈已
經在跑了」——不是偶發的重入問題，是架構上不相容，用旗標/pump timer怎
麼補都補不出正確的行為。qasync.QEventLoop 讓 Qt 的事件分派本身就是這
個 asyncio 迴圈在跑，往後所有 IB API 一律用 xxxAsync() + await，不再
有同步/非同步兩種呼叫方式互相打架的問題(這一步在 main.py 做，不是這個
類別的責任，這裡的 connect_async() 也因此是 async def)。
"""
from ib_async import IB
from PyQt5.QtCore import QObject, pyqtSignal

# 這幾個代碼是市場資料/連線狀態的資訊性通知(connecting/OK/broken)，不是
# 真正的錯誤——跟 scripts/ib_test_*.py 系列腳本實測驗證過的分類一致，
# 照抄過來，不要讓這些洗版式地觸發 error 訊號。
#
# *** 354/10168/10186 這幾個「市場資料未訂閱」代碼也要歸進這類，不能
# 當成連線層級的錯誤 ***：這是這個帳戶已知的市場資料訂閱權限問題(整個
# 系列的實測記錄，包含帳戶端要去 Client Portal 才能解決)，一次查詢一
# 批履約價(main_window.py 一次可能訂閱三五十檔)就會對「每一檔」各噴一
# 次——IBQuoteClient 已經有 fallback(退回 reqHistoricalData)處理這個情
# 況，UI 顯示空白就夠了，不該再讓這個訊號往上傳去跳 modal 對話框(之前
# 就是這樣，一次查詢跳出幾十個「連線失敗」視窗，關都關不完)。
_INFO_ERROR_CODES = {
    1100, 1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158,
    354, 10090, 10168, 10186,
}


class IBClient(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(int, int, str)  # reqId, errorCode, errorString

    def __init__(self):
        super().__init__()
        self.ib = IB()
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnected

    async def connect_async(self, host: str, port: int, client_id: int, timeout: float = 10.0) -> bool:
        """回傳連線是否成功。呼叫端(connect_dialog.py)要在 qasync 的事
        件迴圈裡用 await 呼叫，不能再同步呼叫。"""
        try:
            await self.ib.connectAsync(host, port, clientId=client_id, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            self.error.emit(-1, -1, str(e))
            return False
        if self.ib.isConnected():
            # *** 一定要在任何 reqMktData 之前呼叫，不是可有可無 ***：這個帳
            # 戶股票有即時報價、選擇權沒有——不主動切換模式的話 reqMktData
            # 預設走 Live(1)，選擇權直接收到 code=10168 拒絕，不會自動退成
            # 延遲。4=Delayed-Frozen，跟使用者自己在 notebook 驗證過能動的
            # 設定一致(比純 Delayed(3) 多了「非交易時段回傳最後收盤」)。這
            # 是全域設定，股票查詢前(main_window.py) 沒有另外切回 Live，因
            # 為 IB 的說明文件本身就講「有即時權限的話，就算設定成延遲，也
            # 是繼續給即時，不會反而降級」，不用兩邊切來切去。這是
            # fire-and-forget 呼叫(內部直接送 socket，不經過
            # run_until_complete)，不用 await。
            self.ib.reqMarketDataType(4)
            self.connected.emit()
            return True
        return False

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        if errorCode in _INFO_ERROR_CODES:
            return
        self.error.emit(reqId, errorCode, errorString)

    def _on_disconnected(self) -> None:
        self.disconnected.emit()
