# 溝通語言

**一律用中文回覆，不要用英文。** 這是使用者明確要求的，不管任何情境都適用。

# 交易對象：Interactive Brokers（美股/ETF 選擇權），透過 ib_async

這支 app 原本是接群益 (Capital) SKCOM API 交易台指選擇權，因為造市商點差
太差已經整個換掉，改成透過 IB Gateway 交易任何美股/ETF 的選擇權。群益的
程式碼、SKCOM 文件查詢流程都已經整個刪除，不要再假設專案裡還有這些東
西——`vendor/capital_api/`、`CapitalAPI_2.13.59_PythonExample/` 這兩個目
錄本來就是 gitignore 的廠商參考資料，這台機器上也已經不存在了。

## 只有 NiceGUI 一個版本，PyQt5+qasync 舊版已經整支刪除

這支 app 原本先接 IB、UI layer 用 PyQt5+qasync(`qasync.QEventLoop` 讓
Qt 跟 asyncio 共用同一個事件迴圈)，後來發現這套組合踩過好幾次完全無聲
的 process 崩潰(攔截管道連同 Windows 自己的事件記錄檔都沒留下任何東
西)，改成 NiceGUI(架在 FastAPI/uvicorn 上，純 asyncio，沒有 Qt 事件迴
圈這個不穩定的組合)。遷移完成後 PyQt5 相關的進入點(`pyqt.py`)、views
(`app/views/` 底下不帶 `web_` 前綴的檔案)、`pyqt5`/`pyqt5-qt5`/
`pyqtgraph`/`qasync` 依賴都已經整支刪除，**不要假設專案裡還有 Qt 相關
程式碼**，也不要為了「相容 Qt 版」保留任何分支邏輯。

- `main.py` 是唯一的進入點，跑在一般 asyncio(uvicorn)上。NiceGUI 頁面
  放在 `app/views/`，統一用 `web_` 前綴(例如 `web_quote_board_page.py`)
  ——這個前綴是歷史命名，不用因為 Qt 版沒了就重新命名成不帶前綴。
- model 層(`app/models/ib_client.py`/`ib_quote_client.py`/
  `ib_order_client.py`/`order_book.py`/`positions.py`/
  `auto_close_manager.py`)不依賴任何 UI 框架：`app/services/signal.py`
  的 `Signal` 類別(`.connect()`/`.emit()` 介面)取代了原本 Qt 的
  `QObject`+`pyqtSignal`。新增/修改這幾個檔案時，記得 `Signal()` 一定
  要在 `__init__` 裡建立(實例層級)，不能當類別屬性(不然所有實例會共
  用同一份訂閱清單)。
- 背景工作用 `app/services/background_tasks.py::spawn()`/
  `run_blocking()`；崩潰/卡死診斷用 `app/services/app_logging.py` 的
  `setup_logging()`/`install_asyncio_exception_handler()`/
  `install_hang_watchdog_asyncio()`，細節見那支檔案開頭的完整說明。

## IB 特性跟 SKCOM 的差異（設計決策依據，不是文件查詢）

- IB 不需要 API 層級的帳密登入，TWS/Gateway 本身要先手動開好、登入
  好，程式端只是 socket connect 到已經登入的 process
  （`app/models/ib_client.py`）。用的是 **Gateway 不是 TWS**，模擬/正式
  環境 port 分別是 **4002/4001**（不是 TWS 的 7497/7496）。
- `conId` 是任何合約穩定、唯一的識別碼，取代群益整套 TAIFEX 符號字串編
  碼（`app/models/option_utils.py`）。換一個新履約價/到期日一定要先
  `qualifyContractsAsync()` 才能拿到 conId，而且對「這個到期日根本沒有
  這個履約價」不會丟例外，只會讓 `conId` 停在 0——呼叫端要自己過濾，見
  `app/views/web_quote_board_page.py::_subscribe_current_expiry()`。
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

## Agent skills

### Issue tracker

Issues tracked locally as markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role labels (needs-triage/needs-info/ready-for-agent/ready-for-human/wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (root `CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
