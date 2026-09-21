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

## 回測模組（合成價格回測：Iron Condor/裸雙賣/Jade Lizard/Twisted Sister）

標題列「回測」按鈕開一個大 dialog，三個分頁：新策略(參數設定)/策略清單/報表。純歷史資料合成回測，
**不依賴 IB 連線**。

- 位置：model 層 `app/models/backtest/`(`spec.py` 策略/規則的純資料定義+驗證+中文描述、`engine.py`
  引擎、`market_data.py` yfinance 抓價+快取、`stats.py` 摘要、`trade.py` 逐筆交易)；存檔
  `app/services/backtest_store.py`(`pref/backtest/runs/`，寫入失敗會丟例外，不像其他 store 靜默
  吞掉)；UI `app/views/web_backtest_dialog.py`；報表的 HTML/CSS/JS 在
  `app/resources/backtest_report/`(從舊 `scripts/options_backtest/backtest_dashboard.html` 複製並
  隔離樣式，不是 iframe)。`scripts/options_backtest/` 是舊的命令列版，保留但 app 不 import、不呼叫它。
- **lazy import 是硬規定**：`pandas`/`polars`/`yfinance`/`engine`/`market_data`/`option_chain` 只能在使用者按下「執行回測」之後
  才 import(見 `web_backtest_dialog._execute()`)；`spec.py`/`stats.py`/`trade.py`/`backtest_store.py`
  必須維持純標準庫，不要在 `main.py` 或任何 view 模組頂層 import 它們，不然首頁又會撞
  `response_timeout`(見 `app/services/lazy_ui.py`)。
- **報表 JS 必須用 `ui.run_javascript()` 注入，不能用 `ui.add_head_html("<script>…")`**：頁面載入後
  NiceGUI 是用 `insertAdjacentHTML` 補插入 head，瀏覽器不會執行這種方式插進去的 `<script>`(只有
  CSS 會生效)。所有回測的逐筆資料一次送進瀏覽器記憶體(`window.BtReport`)，切換回測完全在前端。
- 策略 = 進場規則(策略類型 + 短腳用「條件清單 + and/or」找履約價、長腳=距離短腳固定寬度、進場條件) + 出場
  規則清單(範圍整組/單邊 × 類型停利/停損/到期天數，每個組合最多一條，至少要有一條到期天數)。
  完整語意見 `spec.py`/`engine.py` 開頭說明；**不要加使用者沒要求的自動行為**(例如安全腳順便滾動)。
- **四種策略只差「哪一邊有買保護腳」**(`spec.PROTECTED_SIDES`)：Iron Condor 兩邊都有、裸雙賣都沒有、
  Jade Lizard 只有 call 邊有(put 裸賣)、Twisted Sister 只有 put 邊有(call 裸賣)。UI 用策略名稱下拉切換，
  不拆 put/call 兩邊的設定；短腳條件和出場規則四種共用。裸賣的那一邊 `Trade.K_long` 是 `None`，手續費依實
  際腳數算(價差 2 腳、裸賣 1 腳)。
- **進場條件(`spec.EntryConditions`)跟短腳履約價條件是兩回事**：前者是「部位湊齊後要不要進場」的門檻，
  目前只有「無單邊風險」(總權利金 >= 保護價差寬度，Jade Lizard/Twisted Sister 預設開)。
- **保證金是整組同時持有的部位算的**(`engine.position_margin`，簡化 Reg-T)，每一列存開倉當下的整組
  數字 `Trade.margin`(每股)；報表的「初始保證金建議」就是它的平均。舊存檔不相容 `max_loss`/沒有
  `entry.kind`：`strategy_from_dict` 刻意不給預設值(缺欄位會直接丟 KeyError)，舊存檔已經一次性遷移過。
- **價格是真實的 ThetaData 報價，不是 Black-Scholes 合成價**：`app/models/backtest/option_chain.py::OptionChain`
  讀 `scripts/backfill_thetadata.py` 回補到 `pref/backtest/thetadata/<商品>/YYYY-MM.parquet` 的 EOD 買賣報
  價(用 polars 讀寫，專案沒有 pyarrow)，進場信用、收盤出場的成交價由 `RunConfig.fill_price` 決定：`mid` 買賣中價，或 `worst` 極端成交價(賣出的腳收
  bid、買進的腳付 ask；進場賣短腳收 bid/買長腳付 ask，平倉買回短腳付 ask/賣出長腳收 bid)。挑履約價的權利金/
  Delta 條件仍看中價，盤中觸價用當天實際成交價，都不受 `fill_price` 影響。舊的 `skew`/
  `atm_ratio`/VIX 合成定價整套已經刪除，不要加回來。到期日/履約價只能挑當天真的有掛牌的：`entry.dte` 是目標，
  實際到期日是當天掛牌到期日裡剩餘天數最接近的(`nearest_expiration`)，長腳寬度也是目標，實際長腳是離
  「短腳 ± 目標寬度」最接近的真實履約價(`engine.open_spread`)。
- **只有本機已回補過的標的能回測**：`spec.available_tickers()` 掃 `pref/backtest/thetadata/` 底下有 parquet
  的資料夾，UI 標的下拉選單(每次開對話框才重新掃)和 `validate_config` 都靠它，沒資料的標的直接擋掉，
  **不要做「沒真實資料就退回合成價」的 fallback**(同一份報表會混雜真資料/合成價)。要支援新標的先跑回補腳本。
  `market_data.py` 只剩抓標的每日開高低收(yfinance)，選距現價 % 條件和保證金估算要用到現價。
- **`close` 不是報價，只有 `bid`/`ask` 才是**(「收盤」出場永遠讀報價，不能讀 `close` 欄)：ThetaData 的 `close` 是當天最後一筆成交價，流動性差的合約
  可能是很早以前的成交(`backfill_thetadata.py` 開頭就警告過)；`volume == 0` 時 open/high/low/close 全是 0。
  所以公平價值一律用 `mid`；開高低收(成交價)只給「盤中觸價」判斷有沒有觸價用，`Quote.traded` 是 False(當天
  沒成交)時四個取樣點全退回 `mid`。**免費帳號只到 2023-06-01 之後、只有 EOD、沒有現成的 IV/Delta**
  (`greeks_eod` 要 Standard)：|Delta| 條件用中價經 `black_scholes.implied_vol` 反推 IV 再算，是估算值。
- **停利/停損看的是「整張組合單的淨價」，不是各腳自己**(`engine._evaluate`)：整組就是所有持倉價差價值加總，
  收 1 掛 0.5 停利、掛 2 停損，碰到掛價就成交(使用者定案的邏輯)。盤中觸價：開盤淨價已越過門檻(跳空)用開盤價
  成交，否則收盤淨價越過門檻(價格連續 → 當天必然走過掛價)以掛價成交，整組損益剛好等於門檻，再線性內插拆回各邊
  (`_values_at_level`)。**不要用各腳自己當天的最高/最低成交價去湊「最有利/最不利」的組合價**：不同合約的高低點
  不是同一時刻，湊出來的價格不存在——實測讓整組停利天天觸發(420 筆全勝)、買回價差還倒賺 $0.91(平倉價值為負)。
  EOD 資料沒有同一時刻的盤中組合報價，所以只能用開盤/收盤兩個時間點判斷(盤中碰到又拉回的日子會漏掉：停利保
  守、停損樂觀)。價差價值夾在 [0, 寬度]，各腳成交時間對不上時可能湊出範圍外的值。
- **真實選擇權資料回補(`scripts/backfill_thetadata.py --symbol SPY [QQQ ...]`，商品必填)**：從 ThetaData 抓
  每日收盤的整條選擇權鏈(OHLC/成交量/收盤 bid/ask)，一個月一檔，可中斷重跑、已抓齊的月份自動跳過。API key 在
  `.env` 的 `THETADATA_API_KEY`。目前只回補了 SPY。`polars` 是 `thetadata` 的相依套件，沒有單獨列在
  pyproject。
- 已知取捨：整組平倉後當天收盤價立刻重新進場(舊 v1 是隔天，同一組規則兩邊淨利會差很多)；盤中觸價
  只用開盤/收盤兩個時間點的整組淨價判斷(見下一條)；同一天先判斷停利再判斷停損，對策略偏樂觀。

## Agent skills

### Issue tracker

Issues tracked locally as markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role labels (needs-triage/needs-info/ready-for-agent/ready-for-human/wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (root `CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
