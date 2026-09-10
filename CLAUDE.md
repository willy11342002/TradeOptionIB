# 溝通語言

**一律用中文回覆，不要用英文。** 這是使用者明確要求的，不管任何情境都適用。

# 群益 (Capital) SKCOM API — 一定要查文件，不准用猜的

官方文件在 repo 裡：

```
CapitalAPI_2.13.59_PythonExample\策略王COM元件使用說明_V2.13.59.htm
```

**任何跟群益 API 有關的問題（函式簽名、參數順序、參數意義、回傳格式、事件
欄位定義等），一律先查這份文件，不准用網路搜尋、不准憑印象/訓練資料猜。**
這份文件是使用者從官方 `策略王COM元件使用說明_V2.13.59.htm`（隨
`CapitalAPI_2.13.59_PythonExample` 這個官方 SDK 資料夾附的）另存成 htm
格式的，內容跟 docx 完全一致，只是格式方便查詢。

同資料夾下還有其他細分文件，需要更完整脈絡時也可以查：
- `策略王COM元件使用說明_V2.13.45以上登入代碼定義.docx`
- `策略王COM元件使用說明_期貨新制商品報價元件.docx`
- `策略王COM元件使用說明_ProxyServer下單元件.docx`
- `PythonExampleV2\策略王COM元件使用說明_PythonExampleV2\*.docx`（依主題拆
  成 1.環境設置/3.登入/7.下單-國內期選/12.回報/13.國內報價 等單篇文件）
- `PythonExample*/` 底下的官方範例程式碼（`.py`），用來核對「文件寫的」跟
  「範例實際怎麼呼叫」是否一致

## 怎麼讀這份 htm（每次需要查就直接查，不要轉檔另存）

這個 htm 是 Word 匯出的，**編碼是 Big5**，而且是單一大檔（16 萬行以上），
直接用一般讀檔工具打開會亂碼或太大。正確做法：

1. 先用 Grep 在 htm 裡找函式/事件名稱的錨點或標題行，定位行號：
   ```
   grep -n "FunctionName" 策略王COM元件使用說明_V2.13.59.htm
   ```
   優先找 `<h3>` 開頭那行（真正的標題，前面可能有多個過期/重複的
   `<a name=...>` 書籤，書籤位置不一定準，要以 `<h3>` 那行看到的編號＋函
   式名稱為準）。
2. 用 `sed -n '起始行,結束行p'` 抓出那個範圍，接 `iconv -f big5 -t
   utf-8//IGNORE` 轉碼，再用 `sed -e 's/<[^>]*>//g'` 去掉 HTML 標籤、
   `grep -v '^\s*$'` 過濾空行，直接印出來看，**不要用 `-o` 存成檔案，不要
   轉檔另存**——每次要看內容就重新查一次原檔，不要在專案裡留下轉檔副本。
   ```bash
   sed -n '38700,38900p' "策略王COM元件使用說明_V2.13.59.htm" \
     | iconv -f big5 -t utf-8//IGNORE \
     | sed -e 's/<[^>]*>//g' -e 's/&nbsp;/ /g' \
     | grep -v '^\s*$'
   ```
3. 一個函式的「宣告」通常在標題後方 30~80 行內就有完整參數列表；「回傳
   格式/欄位定義」這種大表格（例如 `OnNewData`）可能長達數千行，這時候用
   `grep -n "^<h3>"` 找下一個標題的行號當作結束邊界，再視需要分段抓。
4. `pandoc`／Word／LibreOffice 這台機器沒裝，讀 docx 只能靠這個 htm。

## 已知的重要細節（2025-09 對照過，非猜測）

- `GetFutureRights(bstrLogInID, bstrAccount, sCoinType)`：第三參數是
  **幣別**（0:全幣別含基幣 1:基幣TWD 2:人民幣RMB），不是查詢格式碼。
  `sCoinType=0` 時每個幣別各回傳一筆 `OnFutureRights`，最後多回傳一筆以
  `##` 開頭的內容代表查詢結束。
- `OnFutureRights(bstrData)` 掛在 **SKOrderLib**（不是 SKReplyLib），跟
  `OnAsyncOrder`/`OnAccount` 同一個物件。欄位定義（0-40）已核對過，見
  `app/models/capital_order_client.py`。
- `SendDuplexOrder` 用同一個 `FUTUREORDER` 結構，兩腳分別用
  `bstrStockNo`/`sBuySell`（第一腳）跟 `bstrStockNo2`/`sBuySell2`（第二
  腳），`bstrPrice` 是淨價，不是各自的委託價。
- `CorrectPriceBySeqNo`/`DecreaseOrderBySeqNo`/`CancelOrderBySeqNo` 都是
  `(bstrLogInID, bAsyncOrder, bstrAccount, bstrSeqNo, ...)` 開頭，
  `bstrSeqNo` 用的是 13 碼序號（`SeqNo`），不是原始委託序號（`KeyNo`）。
- `GetOrderReport`/`GetFulfillReport` IDL 上宣告是 `void`，但最後一個參數
  是 `[out, retval] BSTR*`，comtypes 會把它變成一般的函式回傳值（同步阻
  塞式呼叫，文件要求查詢間隔至少 5 秒）。
- `SKCenterLib_SetAuthority` 的參數是位元旗標（不是單純的環境列舉）：
  bit0 是 SGX 專線開關，**bit1 才是環境設定**（0=正式環境，設該位元=測試
  環境）。目前 `capital_client.py` 用的 `AUTHORITY_PROD=0`/`AUTHORITY_TEST
  =2` 剛好對應 bit1，數值正確，但語意跟參數命名容易誤導，改動這段前務必
  重新查文件。
- `SKQuoteLib_RequestKLine`/`RequestKLineAM`/`RequestKLineAMByDate` 三個
  都明確標「僅提供歷史資料」，不是即時推播——盤中查詢「今天還沒結束的那
  個時段」(不管是日盤還是夜盤) 一律查不到，實測過(2026-09-09)`end_date`
  設今天或明天結果完全一樣，都不會多出當時已經進行好幾小時的夜盤資料。
- **`OnNotifyLiveKLineData` 這個事件，文件 4-4-u 有寫，但實際安裝的
  `vendor/capital_api/x64/SKCOM.dll`(2.13.59.0，版本號跟文件完全對得上)
  裡根本不存在**——直接用 `comtypes.client.GetModule` 對這支DLL重新產生
  過COM介面，`_ISKQuoteLibEvents`裡沒有這個方法，不是猜的、不是版本不
  合、也不是comtypes快取沒更新。文件跟實際出貨的DLL對不上，遇到「查得到
  文件但呼叫/接不到事件」時要先懷疑這件事，不要預設文件一定跟DLL同步。
- 「盤中才開程式，怎麼拿到開盤到現在這段」正確做法：`SKQuoteLib_
  RequestTicks([in,out] SHORT* psPageNo, [in] BSTR bstrStockNo)` 訂閱
  (`psPageNo`從0開始、一檔一個獨立頁碼，不能像`RequestStocks`那樣逗號分
  隔多檔)，訂閱當下會先收到`OnNotifyHistoryTicksLONG`回補當天所有成交明
  細，之後才是`OnNotifyTicksLONG`即時tick——這兩個事件都實測過真的存在、
  真的能拿到回補(2026-09-09實測台指期夜盤15:00開盤後的完整逐筆成交，訂
  閱當下一次收到9540筆回補)。`nTimehms`是時分秒打包成整數(例如90025代表
  09:00:25，不補零)，`nSimulate!=0`是試算揭示不是真的成交要濾掉，價格未
  還原小數位數要另外查`GetStockByNoLONG`拿`sDecimal`自己換算。詳見
  `app/models/capital_tick_client.py`。

## 工作流程規定

1. 群益 API 相關問題：先查這份 htm，查到再回答/動手，查不到要老實說查不
   到，不要用網路搜尋結果或訓練資料猜答案。
2. 改動任何跟群益 API 呼叫有關的程式碼前，先對照文件核對函式簽名/參數順
   序/回傳格式，核對結果要讓使用者看得到依據（引用文件內容），不是憑印
   象改。
3. 這份文件是「查閱用」，不要轉檔、不要在 repo 裡留副本、不要把整份轉成
   Markdown 存檔——每次現查現讀。
