"""
Phase 0 煙霧測試：驗證 `ib_async.util.useQt(qtLib='PyQt5')` 真的能把 IB
的 asyncio 事件迴圈跟 PyQt5 的事件迴圈合併——不用像
scripts/capital_oo_quote_test.py 那樣手動寫 `QCoreApplication.processEvents()`
輪詢迴圈，而是讓 `app.exec_()` 正常跑起來，事件(報價/委託狀態)自己就會
送進來。

這是在正式接進 main.py/main_window.py 之前，先在一個最小 QApplication
迴圈裡驗證這個整合方式本身沒問題，不然接近整個 app 才發現事件迴圈打架
會很難排查。

跑法：
    uv run python scripts/ib_qt_pump_test.py
"""
import sys

from ib_async import Stock, util
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app.models.ib_client import IBClient  # noqa: E402


def main():
    app = QApplication(sys.argv)

    # *** 關鍵一步：QApplication 建好之後、IB().connect() 之前呼叫，把
    # ib_async 內部的 asyncio 事件迴圈跟 Qt 的事件迴圈合併。這一步順序
    # 錯了(例如在 connect() 之後才呼叫)有沒有問題，也是這支腳本要驗證
    # 的一部分，所以特意按照 main.py 未來會用的順序寫。***
    util.useQt(qtLib="PyQt5")

    client = IBClient()
    client.connected.connect(lambda: print("[connected] IBClient.connected 訊號正常"))
    client.disconnected.connect(lambda: print("[disconnected]"))
    client.error.connect(lambda reqId, code, msg: print(f"[error] reqId={reqId} code={code} msg={msg}"))

    print("[connect] 連線到 127.0.0.1:4002 (IB Gateway 模擬帳戶) ...")
    ok = client.connect("127.0.0.1", 4002, client_id=40, timeout=10)
    if not ok:
        print("[FAIL] 連線失敗，檢查 IB Gateway 是否開著、port 對不對。")
        return

    # 訂閱一檔報價，完全不手動 sleep/processEvents，靠 Qt 的事件迴圈把
    # ib_async 的 tick 更新事件送進來——這就是要驗證的核心行為。
    contract = Stock("SPY", "SMART", "USD")
    client.ib.qualifyContracts(contract)
    print(f"[qualifyContracts] conId={contract.conId}")

    ticker = client.ib.reqMktData(contract, "", False, False)
    received = {"count": 0}

    def on_pending_tickers(tickers):
        for t in tickers:
            received["count"] += 1
            print(f"[pendingTickersEvent] #{received['count']} symbol={t.contract.symbol} "
                  f"bid={t.bid} ask={t.ask} last={t.last}")

    client.ib.pendingTickersEvent += on_pending_tickers

    def finish():
        print(f"\n[done] 10 秒內共收到 {received['count']} 次 pendingTickersEvent 更新。")
        if received["count"] == 0:
            print("[note] 0 次很可能是市場資料訂閱權限問題(已知帳戶端問題)，不是這支腳本要驗證的"
                  "事件迴圈整合本身失敗——重點是上面有沒有看到 Python 例外或當掉。")
        client.disconnect()
        app.quit()

    QTimer.singleShot(10_000, finish)
    print("[running] 進入 app.exec_()，10 秒後自動結束 ...")
    app.exec_()
    print("[exit] app.exec_() 正常返回，沒有卡住/當掉。")


if __name__ == "__main__":
    main()
