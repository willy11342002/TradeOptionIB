# 溝通語言

**一律用中文回覆，不要用英文。** 這是使用者明確要求的，不管任何情境都適用。

# 交易對象：Interactive Brokers（美股/ETF 選擇權），透過 ib_async

這支 app 原本是接群益 (Capital) SKCOM API 交易台指選擇權，因為造市商點差
太差已經整個換掉，改成透過 IB Gateway 交易任何美股/ETF 的選擇權。群益的
程式碼、SKCOM 文件查詢流程都已經整個刪除，不要再假設專案裡還有這些東
西——`vendor/capital_api/`、`CapitalAPI_2.13.59_PythonExample/` 這兩個目
錄本來就是 gitignore 的廠商參考資料，這台機器上也已經不存在了。

## 事件迴圈：一定要用 qasync，不要用 `ib_async.util.useQt()`

`main.py` 用 `qasync.QEventLoop` 讓 Qt 跟 asyncio 共用同一個事件迴圈。
**不要改回 `ib_async.util.useQt()`**——那是在 asyncio callback 裡巢狀塞
一個 Qt `QEventLoop` 的 hack，實測發現只要開始跑（第一次 `connect()` 之
後）就回不去「沒在跑」的狀態，導致之後任何一個同步 IB 呼叫都保證撞上
`RuntimeError: This event loop is already running`。這是踩了好幾輪才確
認的架構性不相容，不是猜測。

改用 qasync 之後的規則：
- **所有 IB API 呼叫一律用 `xxxAsync()` 版本 + `await`**，不要用同步版
  （`ib.qualifyContracts()`/`ib.reqSecDefOptParams()`/`ib.connect()`...
  這些同步版內部都是 `loop.run_until_complete()`，在這個架構下一定會撞
  上「事件迴圈已經在跑了」）。例外：`ib.positions()`/`ib.trades()`/
  `ib.ticker()` 是純本地讀取(沒有 `_run()`)、`reqMktData()`/
  `cancelMktData()`/`placeOrder()`/`cancelOrder()`/`reqMarketDataType()`
  是 fire-and-forget(送出 socket 訊息就回傳，不等回應)，這些可以照舊同
  步呼叫，改之前先去 `.venv/Lib/site-packages/ib_async/ib.py` 確認該方
  法內部有沒有 `self._run(...)` 再判斷。
- **Qt 訊號的 handler 要是 async 的話用 `qasync.asyncSlot()` 裝飾**，不
  要自己手動 `asyncio.ensure_future()` 包一層再接訊號。
- **顯示 modal 對話框（`QDialog.exec_()`）不能包在 asyncio Task 裡面呼
  叫**，也不能在 `loop.run_forever()` 開始跑之前呼叫——前者會撞上
  asyncio「同一執行緒不能同時有兩個 Task 在跑」的重入保護，後者會撞上
  `no running event loop`。正確做法是用 `QTimer.singleShot(0, ...)` 排
  程一個純 Qt callback 在 `run_forever()` 開始後才顯示對話框，細節看
  `main.py::_show_connect_dialog()` 的說明註解。

## IB 特性跟 SKCOM 的差異（設計決策依據，不是文件查詢）

- IB 不需要 API 層級的帳密登入，TWS/Gateway 本身要先手動開好、登入
  好，程式端只是 socket connect 到已經登入的 process
  （`app/models/ib_client.py`）。用的是 **Gateway 不是 TWS**，模擬/正式
  環境 port 分別是 **4002/4001**（不是 TWS 的 7497/7496）。
- `conId` 是任何合約穩定、唯一的識別碼，取代群益整套 TAIFEX 符號字串編
  碼（`app/models/option_utils.py`）。換一個新履約價/到期日一定要先
  `qualifyContractsAsync()` 才能拿到 conId，而且對「這個到期日根本沒有
  這個履約價」不會丟例外，只會讓 `conId` 停在 0——呼叫端要自己過濾，見
  `app/views/main_window.py::_do_subscribe_core()`。
- `ib.positions()`/`ib.positionEvent` 給的部位方向（多/空）永遠是明確
  的正負號，不需要像群益那樣猜買賣別欄位（`app/models/positions.py`）。
- IB 的委託（含 BAG 複式單）整個生命週期都有穩定的整數 `orderId`，改
  價/刪單直接對應同 `orderId` 重新 `placeOrder()`/`cancelOrder()`
  （`app/models/order_book.py`），沒有連續 IOC 自動重送引擎——BAG combo
  可以直接掛 `LMT`+`DAY`/`GTC` 等成交。
- 這個帳戶股票有即時報價、選擇權沒有，`reqMarketDataType(4)` 一定要在
  `connect()` 成功後立刻呼叫，不然選擇權的 `reqMktData` 會直接被拒絕
  （見 `app/models/ib_client.py`）。IB 用 `NaN` 或 `-1` 代表「這個欄位
  沒有值」，兩種都要濾掉（`app/models/ib_quote_client.py::_clean()`）。
