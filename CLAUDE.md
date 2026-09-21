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
  收 1 掛 0.5 停利、掛 2 停損，碰到掛價就成交(使用者定案的邏輯)，整組損益剛好等於門檻，再線性內插拆回各邊
  (`_values_at_level`)。「收盤價」模式只看收盤淨價。**「盤中觸價」用當天各合約的開/高/低成交價，只算兩個情境**
  (`_values_extreme`)：現價在日內低點(put 邊的腳取最高價、call 邊取最低價)、現價在日內高點(相反)，同一邊價差
  的兩腳取**同方向**的極端價。整組淨價對現價是 U 型，日內最大值一定在這兩個端點之一，所以停損判斷是精確的；
  停利的最小值可能在兩端點之間，會漏掉一部分(保守)。開盤淨價已越過門檻(跳空)用開盤價成交，否則取兩情境跟收盤
  淨價裡最不利(停損)/最有利(停利)的比門檻，越過就以掛價成交。**絕對不要「各腳各取自己最有利/最不利的價」去
  湊組合價**(舊版教訓：不同合約的高低點不是同一時刻，湊出的價格不存在——整組停利天天觸發、420 筆全勝、買回價差
  倒賺 $0.91)；現在的兩情境配對每個情境內部都是同一個現價位置，不是這個問題。價差價值夾在 [0, 寬度]。
  **開/高/低成交價會有異常單**(實測 2026-04-08 K=655 put 開盤 0.10、收盤中價 5.76，相鄰履約價都在 $1~3，舊版
  拿它當開盤價停利，虛增一筆 ~$1,800)：`_PrintFilter` 用標的當天高低點 + 收盤中價反推 IV 的 ×0.5~×2 算出每個成交價
  合理的範圍，超出就退回收盤中價；深價內反推不出 IV 時改用「跟收盤中價差距不超過標的當天全幅」。同一天停損跟停利
  都被碰到時先後不明，`KIND_EVAL_ORDER` 是 到期天數 → 停損 → 停利，停損優先(保守)；收盤價模式兩者不可能同時成立，
  順序沒有影響。**這個改動讓所有舊的盤中觸價存檔重跑後數字都會變**(例：Jade Lizard Δ<0.5 觸價冷卻 30 天，
  $17.4k / 含未平倉回撤 −$3.3k → $14.9k / −$6.4k)，收盤價模式的結果完全不變。
- **真實選擇權資料回補(`scripts/backfill_thetadata.py --symbol SPY [QQQ ...]`，商品必填)**：從 ThetaData 抓
  每日收盤的整條選擇權鏈(OHLC/成交量/收盤 bid/ask)，一個月一檔，可中斷重跑、已抓齊的月份自動跳過。API key 在
  `.env` 的 `THETADATA_API_KEY`。目前只回補了 SPY。`polars` 是 `thetadata` 的相依套件，沒有單獨列在
  pyproject。
- **蝶式(`STRATEGIES_CENTERED`：`iron_butterfly`、`reverse_iron_butterfly`)**：中心 = 該到期日 put/call 都有報價的
  履約價裡離「現價 + `EntrySpec.center_offset`」最近的一個(偏移單位美元或現價 %，0 = ATM，正 = 中心在現價上方；舊存檔沒
  有這欄位讀成 0)，不用短腳條件。**偏向跟策略有關**：鐵蝶式中心在上方偏多，反向鐵蝶式中心在上方偏空(相反)；兩翼 = 離「中心 ± 目標寬度」最接近的真實履約價。**用「鐵」的版本，
  是因為它剛好是 put 價差 + call 價差共用中心，沿用既有「兩邊各一個 `_Spread`」的結構，不需要 3 腳/雙口結構**。
  Iron Butterfly = 賣出 ATM 跨式 + 買翼、收權利金(報酬形狀等同買進蝶式，中間獲利)；Reverse Iron Butterfly =
  買進 ATM 跨式 + 賣翼、付權利金(報酬形狀等同賣出蝶式，兩端獲利)。兩者在同一日程下損益**逐筆互為相反數**
  (回測時驗證過，毛損益相加 = 0)，所以掃參數只需要跑一邊。買方版是 `_Spread.debit=True`：`entry_credit` 為負、
  `K_short` 是賣出的翼、`K_long` 是買進的中心、價差價值夾在 [−寬度, 0]；保證金 = 付出的權利金(最大虧損)；
  停利/停損百分比以 `abs(權利金合計)` 為基準，買方停利可超過 100%。**蝶式只能用整組範圍的出場規則**(兩邊共用中心，
  不能單邊出場，`validate_strategy` 會擋)。進場建議起手值在 `spec.CENTERED_ENTRY_DEFAULTS`(天期 30、翼 2%、偏移 0)，UI 從其他策略切到蝶式時帶入，只設進場、不動出場規則；偏移超過約 ±3% 部位會退化(一邊價差價值≈翼寬、另一邊≈0，只剩手續費)。沒有模擬美式提前履約，賣出中心的 ATM 選擇權實際上有被提前指派的風險。
- **停損後冷卻(`ExitRule.cooldown_days`，只有停損規則能設，日曆天)**：停損觸發那天起 N 天內不開任何新部位
  (整組重新進場、單邊「平倉後重開」都算)，第 N 天起恢復；0 = 維持「平倉後當天收盤立刻重進」。原因：停損後
  立刻用同樣 Δ 重賣一個一樣的 put，曝險沒減少、只是兌現虧損，連續下跌段會一路被停損。舊存檔沒有這欄位，
  讀進來是 0(`strategy_from_dict` 用 `.get`)。**效果會隨路徑劇烈變動、不是單調的**(SPY 2023-06～2024-12
  Δ<0.2 停損 100%：冷卻 0/10/20/30 天淨損益 $534/$1,281/$1,455/$334)，一改冷卻天數整段後續進場日程都
  跟著換，不要當成「越久越好」或拿單一數字調參。
- **逐日權益(含未平倉浮動損益)**：`engine.run_backtest_with_equity()` 每個交易日收盤，用持倉的收盤報價
  (跟出場同一套 `_values_at`，`fill_price=worst` 就用極端價)算「已平倉 + 未平倉」的每股毛損益，存在
  `<id>.trades.json` 的 `equity` 欄位(舊存檔沒有，報表會提示重跑；`run_backtest()` 只回逐筆交易，給不需要
  的呼叫端)。存每股毛損益而不是美元，是因為報表切換口數時要即時重算：`report.js::computeMtm` 換算成美元並
  在出場日扣手續費，跟已實現曲線同一套規則，兩條線在出場日重合，落差就是浮動損益。**「淨最大回撤」只看已平
  倉，會嚴重低估過程中的痛苦**(實測 Jade Lizard 不設停損：已平倉 −$287、含未平倉 −$1,151)，評估風險看含
  未平倉那個。
- 已知取捨：整組平倉後當天收盤價立刻重新進場(舊 v1 是隔天，同一組規則兩邊淨利會差很多)；盤中觸價
  只有 EOD 開高低收成交價、沒有真正的日內路徑(見上一條)。

## Agent skills

### Issue tracker

Issues tracked locally as markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role labels (needs-triage/needs-info/ready-for-agent/ready-for-human/wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (root `CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
