"""
群益 SKOrderLib 下單封裝：裸買賣 (SendOptionOrder)、價差複式單
(SendDuplexOrder)、改價/減量/刪單 (CorrectPriceBySeqNo/
DecreaseOrderBySeqNo/CancelOrderBySeqNo)、既有委託查詢 (GetOrderReport)、
自訂委託頻率保護 (SetMaxQty/SetMaxCount/UnlockOrder)。

這支檔案只做「跟 SKOrderLib 講話」這一層薄封裝：送一次委託、解析一次回
報、查一次既有委託——不管委託的生命週期/狀態機、不管「多次(連續)IOC」
要不要重送。那些邏輯搬到 app/models/order_book.py 的 OrderBookManager
(因為要支援同時多組獨立的連續IOC，狀態機式的東西應該一個地方管，不要
散在下單的薄封裝裡)。

FUTUREORDER 結構只有一組 bstrStockNo/bstrStockNo2，沒有第三、四腳位
置——SendDuplexOrder 就是固定二腳的複式單，鐵鷹這種四腳策略要兩個獨立
的 SendDuplexOrder 呼叫。

委託/成交回報走 SKReplyLib 的 OnNewData(userID, bstrData) 事件，逗號分
隔字串。完整欄位定義來自群益文件《12.回報.docx》4-3-g OnNewData（使用
者提供的實際文件截圖核對過，不是猜的），針對期貨/選擇權市場 (TF/TO)：
    [0] KeyNo          原始委託序號
    [1] MarketType      TS/TA/TL/TP/TC/TF/TO/OF/OO/OS
    [2] Type            N:委託 C:取消 U:改量 P:改價 D:成交 B:改價改量
                         S:動態退單(交易所主動退單)
    [3] OrderErr        Y:失敗 T:逾時 N:正常
    [4] Broker          TF/TO 為 IB 代號
    [5] CustNo          交易帳號
    [6] BuySell         B:買 S:賣
    [7] NewClose        Y:當沖 N:新倉 O:平倉 7:代沖銷
    [8] TIF              I:IOC R:ROD F:FOK
    [9] PriceType        1:市價 2:限價 3:範圍市價 4:停損限價 5:收市
    [10] (N/A，期貨/選擇權不使用)
    [11] ExchangeID
    [12] ComId          商品代碼 (單式單)
    [13] StrikePrice    舊欄位，忽略，改看 StrikePrice1/2
    [14] OrderNo        委託書號
    [15] Price          委託價或成交價 (依 Type)
    [16]-[23]           Numerator/Denominator/Price1/Numerator1/
                         Denominator1/Price2/Numerator2/Denominator2
                         (國內期選成交時 Price1/Price2 是兩腳成交價)
    [24] Qty            委託量/成交量/減量數/原委託剩量 (依 Type)
    [25] BeforeQty       (僅證券/複委託)
    [26] AfterQty        (僅證券/複委託)
    [27] Date  [28] Time
    [29] OkSeq  [30] SubID  [31] SaleNo  [32] Agent  [33] TradeDate
    [34] MsgNo  [35] PreOrder
    [36] ComId1  [37] YearMonth1  [38] StrikePrice1   (第一腳)
    [39] ComId2  [40] YearMonth2  [41] StrikePrice2   (第二腳)
    [42] ExecutionNo    成交序號
    [43] PriceSymbol  [44] Reserved  [45] OrderEffective
    [46] CallPut        C:Call P:Put
    [47] OrderSeq  [48] ErrorMsg (OrderErr=='Y' 時的錯誤訊息)
    [49] CancelOrderMarkByExchange  [50] ExchangeTandemMsg
    [51] SeqNo          13碼序號 (改價/減量/刪單都用這個)
    [52] OFSTPFlag  [53] Timefff

*** comtypes 回傳值形狀是實測過的，不是照 IDL 猜的 (整支專案通用規則，
見 capital_quote_client.py 開頭的說明) ***
"""
from typing import Dict, Optional

import comtypes.client
from PyQt5.QtCore import QObject, pyqtSignal

from app.models.capital_client import CapitalClient

BUY = 0
SELL = 1

TIF_ROD = 0
TIF_IOC = 1
TIF_FOK = 2

NEW_POSITION = 0
CLOSE_POSITION = 1

MARKET_STOCK = 0        # TS 證券
MARKET_FUTURE = 1       # TF 期貨
MARKET_OPTION = 2       # TO 選擇權
MARKET_FOREIGN_STOCK = 3  # OS 複委託
MARKET_SEA_FUTURE = 4    # OF 海期
MARKET_SEA_OPTION = 5    # OO 海選

# *** 這份索引表是用真實回報字串逐欄位核對出來的，不是文件截圖轉寫的那
# 份 (那份索引全部錯位，是造成「委託一直卡在等待回報」的真正原因) ***
#
# 核對方式：使用者送出一筆賣權賣方價差 IOC，用 print log 拿到「委託確
# 認(type=N)」跟「成交(type=D)」兩筆真實回報字串，逐欄位 diff + 跟已知
# 的委託內容(履約價46700/46800、Put、口數1)比對，加上官方範例
# Reply_Service/Reply.py 裡 OnNewData 註解掉的欄位索引提示 (cutData[8]=
# 商品代碼、[10]=委託書號、[11]=價格、[20]=數量、[23]+[24]=日期+時間)
# 互相印證，兩者完全吻合。原本從文件截圖轉寫的版本從「商品代碼」欄位開
# 始，比真實資料多出 4 個並不存在的欄位，後面全部跟著錯位。
#
# 已確認的欄位 (逐欄位比對過，不是猜的)：
#   [0]key_no [1]market_type [2]type(N/C/U/P/D/B/S) [3]order_err(Y/T/N)
#   [4]broker [5]cust_no
#   [8]這裡是複式單兩腳合併顯示的字串(如 TX246700/46800U6)，不是單一
#       商品代碼，不能拿來跟單一商品代碼比對 (這是 _match_record 之前失
#       敗的根因)
#   [10]order_no [11]price
#   [12]numerator [13]denominator [14]price1 [15]numerator1
#   [16]denominator1 [17]price2 [18]numerator2 [19]denominator2
#   [20]qty [21]before_qty [22]after_qty [23]date [24]time
#   [32]com_id1 [33]year_month1 [34]strike_price1
#   [35]com_id2 [36]year_month2 [37]strike_price2
#   [38]execution_no [42]call_put(C/P) [44]error_msg [47]seq_no(13碼)
#   [48]timefff
#
# 未確認的欄位 (先留白，不亂猜，等有更多不同種類的真實回報字串再補)：
#   [6][7][9][25]-[31][39]-[41][43][45][46] 目前不確定實際意義——特別是
#   [6][7] 這兩格很可能包含 buy_sell/new_close/tif 這些方向性欄位，但這
#   份樣本是同一筆委託的「委託確認」跟「成交」兩個事件，兩個事件買賣方
#   向必然相同，光靠這兩筆資料無法反推出哪個位置真的是 buy_sell，需要
#   再拿一筆「裸買賣(單腳)」的真實回報字串來對照才能確定 (單腳的話 [8]
#   應該會是單一商品代碼而不是複式單那種合併字串，可以驗證這個假設)。
_REPORT_FIELDS = [
    "key_no", "market_type", "type", "order_err", "broker", "cust_no",
    "field6_unconfirmed", "field7_unconfirmed", "combo_desc_unconfirmed",
    "field9_unconfirmed", "order_no", "price", "numerator", "denominator",
    "price1", "numerator1", "denominator1", "price2", "numerator2",
    "denominator2", "qty", "before_qty", "after_qty", "date", "time",
    "field25_unconfirmed", "field26_unconfirmed", "field27_unconfirmed",
    "field28_unconfirmed", "field29_unconfirmed", "field30_unconfirmed",
    "field31_unconfirmed", "com_id1", "year_month1", "strike_price1",
    "com_id2", "year_month2", "strike_price2", "execution_no",
    "field39_unconfirmed", "field40_unconfirmed", "field41_unconfirmed",
    "call_put", "field43_unconfirmed", "error_msg", "field45_unconfirmed",
    "field46_unconfirmed", "seq_no", "timefff",
]

# 官方文件《策略王COM元件使用說明_V2.13.59.docx》4-2-i OnFutureRights 逐欄
# 位核對過 (不是猜的)，[0]-[40] 共 41 欄：
#   [21] 又是一個「原始保證金」，文件本身就是重複命名，不是轉寫錯誤——保
#   留 initial_margin_2 這個名字誠實反映這件事，不去猜它跟 [13] 差在哪。
FUTURE_RIGHTS_FIELDS = [
    "account_balance", "floating_pnl", "realized_fee", "transaction_tax",
    "withheld_premium", "premium_settled", "equity", "excess_margin",
    "deposit_withdrawal", "long_option_value", "short_option_value",
    "futures_realized_pnl", "intraday_unrealized_pnl", "initial_margin",
    "maintenance_margin", "position_initial_margin", "position_maintenance_margin",
    "order_margin", "excess_best_margin", "total_premium_value", "withheld_fee",
    "initial_margin_2", "previous_day_balance", "option_combo_margin_flag",
    "maintenance_ratio", "currency", "full_initial_margin", "full_maintenance_margin",
    "full_available", "collateral_amount", "securities_available", "available_balance",
    "full_cash_available", "securities_value", "risk_indicator", "option_expiry_diff",
    "option_expiry_loss", "futures_expiry_pnl", "additional_margin", "login_id",
    "account_no",
]

# 中文標籤給 UI 顯示用，順序跟 FUTURE_RIGHTS_FIELDS 一一對應，文字照抄文件。
FUTURE_RIGHTS_LABELS = [
    "帳戶餘額", "浮動損益", "已實現費用", "交易稅", "預扣權利金", "權利金收付",
    "權益數", "超額保證金", "存提款", "買方市值", "賣方市值", "期貨平倉損益",
    "盤中未實現", "原始保證金", "維持保證金", "部位原始保證金", "部位維持保證金",
    "委託保證金", "超額最佳保證金", "權利總值", "預扣費用", "原始保證金",
    "昨日餘額", "選擇權組合單加不加收保證金", "維持率", "幣別", "足額原始保證金",
    "足額維持保證金", "足額可用", "抵繳金額", "有價可用", "可用餘額", "足額現金可用",
    "有價價值", "風險指標", "選擇權到期差異", "選擇權到期差損", "期貨到期損益",
    "加收保證金", "LOGIN_ID", "ACCOUNT_NO",
]

# 幣別：0:全幣別(含基幣) 1:基幣(台幣TWD) 2:人民幣RMB (4-2-38 GetFutureRights)。
COIN_TYPE_TWD = 1


class CapitalOrderClient(QObject):
    order_sent = pyqtSignal(str)       # 送出當下的訊息 (SendXxxOrder 回傳的 bstrMessage)
    order_failed = pyqtSignal(str)     # 送出失敗 (SendXxxOrder 呼叫本身 retCode != 0)
    order_report = pyqtSignal(dict)    # OnNewData 解析後的欄位 dict (見 _REPORT_FIELDS)，含 "raw" 原始字串
    future_rights = pyqtSignal(dict)   # OnFutureRights 解析後的欄位 dict (見 FUTURE_RIGHTS_FIELDS)，含 "raw" 原始字串
    future_rights_failed = pyqtSignal(str)  # GetFutureRights 呼叫本身失敗 (retCode != 0)

    def __init__(self, client: CapitalClient):
        super().__init__()
        self._client = client
        self._order = client.order
        self._reply = client.reply
        self._sk = client.sk

        self._reply_events = _ReplyEvents(self)
        self._reply_handler = comtypes.client.GetEvents(self._reply, self._reply_events)

        # OnAsyncOrder 掛在 SKOrderLib 上 (不是 SKReplyLib)，核對自官方
        # 範例 order_service/Order.py 的 SKOrderLibEvent.OnAsyncOrder，
        # 只用來印出來給使用者看非同步送單(連續IOC)當下收單/失敗，不做
        # nThreadID 關聯比對(官方範例都沒示範怎麼把它跟哪一筆送單對應起
        # 來，用猜的關聯邏輯風險太高，這裡先不做)。
        self._order_events = _OrderAsyncEvents(self)
        self._order_handler = comtypes.client.GetEvents(self._order, self._order_events)

    def _require_login(self) -> None:
        # 這幾個函式的帳號/使用者ID參數都是 COM BSTR，傳 None 進去不是乾
        # 淨的 Python 例外，是直接 access violation 把整個程式炸掉(實測
        # 過)。還沒登入完成(account 還是 None)就不要讓呼叫真的打到 COM。
        if not self._client.user_id or not self._client.account:
            raise RuntimeError("尚未登入完成 (沒有 user_id/account)，無法呼叫委託相關 API")

    # ------------------------------------------------------------ 裸買賣
    def _build_option_order(
        self, symbol: str, buy: bool, price: float, qty: int, tif: int, new_close: int,
    ):
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = self._client.account
        order.bstrStockNo = symbol
        order.sBuySell = BUY if buy else SELL
        order.sTradeType = tif
        order.bstrPrice = str(price)
        order.nQty = int(qty)
        order.sNewClose = new_close
        return order

    def send_option_order_once(
        self, symbol: str, buy: bool, price: float, qty: int,
        tif: int = TIF_ROD, new_close: int = NEW_POSITION, is_async: bool = False,
    ) -> Optional[str]:
        """回傳成功時的 13 碼委託序號 (SeqNo)，失敗或非同步模式回傳 None。

        *** 同步/非同步的選擇，不是猜的 ***：官方文件《7.下單-國內期選
        .docx》講得很清楚——不管同步/非同步，這個呼叫的回傳值都只代表
        「成功送至交易所」，交易所實際撮合結果(成交/退單)兩種模式都一樣
        要靠 OnNewData 回報確認，用同步不能跳過等回報這件事。
        同步模式下成功時 bstrMessage 直接就是13碼SeqNo，不用等事件；但
        同步呼叫本質上是等交易所閘道處理完才回應，會卡住呼叫端(這裡是
        UI 執行緒，PyQt 沒有安全的方式把這個 COM 呼叫丟到背景執行緒——
        SKOrderLib 是在主執行緒建立的 STA COM 物件，跨執行緒呼叫需要額
        外做 COM marshaling，之前踩過"傳 None 給 BSTR 直接 access
        violation"這種等級的坑，這裡不去冒這個風險)。
        所以只有「一次性送單」(使用者按下送出的單一動作，短暫等待符合
        正常UX直覺) 才用同步換取立即的 SeqNo；連續IOC重送這種會被報價
        更新高頻觸發的呼叫(_maybe_fire)一定要用非同步(is_async=True)，
        不然重送愈頻繁、UI 卡愈久，又會重現先前「暫停/刪除按了沒反應」
        的問題。非同步模式的 bstrMessage 不是 SeqNo(文件寫「非同步：參
        照OnAsyncOrder」)，這裡直接回傳 None，SeqNo 之後如果真的要用要
        靠 OnAsyncOrder 事件(已知官方範例都沒示範怎麼把它跟哪一筆送單
        對應起來，這裡先不做，避免用猜的關聯邏輯)。"""
        self._require_login()
        order = self._build_option_order(symbol, buy, price, qty, tif, new_close)
        message, code = self._order.SendOptionOrder(self._client.user_id, is_async, order)
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
            return None
        self.order_sent.emit(str(message))
        return None if is_async else str(message)

    # ------------------------------------------------------------ 價差複式單
    def _build_duplex_order(
        self, symbol1: str, buy1: bool, symbol2: str, buy2: bool,
        net_price: float, qty: int, tif: int, new_close: int,
    ):
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = self._client.account
        order.bstrStockNo = symbol1
        order.bstrStockNo2 = symbol2
        order.sBuySell = BUY if buy1 else SELL
        order.sBuySell2 = BUY if buy2 else SELL
        order.sTradeType = tif
        order.bstrPrice = str(net_price)
        order.nQty = int(qty)
        order.sNewClose = new_close
        return order

    def send_duplex_order_once(
        self, symbol1: str, buy1: bool, symbol2: str, buy2: bool,
        net_price: float, qty: int, tif: int = TIF_IOC, new_close: int = NEW_POSITION,
        is_async: bool = False,
    ) -> Optional[str]:
        """tif 只接受 TIF_IOC/TIF_FOK，交易所規則不開放複式單用 ROD。
        is_async/回傳值意義同 send_option_order_once。"""
        if tif not in (TIF_IOC, TIF_FOK):
            raise ValueError("價差複式單只能用 IOC 或 FOK")
        self._require_login()
        order = self._build_duplex_order(symbol1, buy1, symbol2, buy2, net_price, qty, tif, new_close)
        message, code = self._order.SendDuplexOrder(self._client.user_id, is_async, order)
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
            return None
        self.order_sent.emit(str(message))
        return None if is_async else str(message)

    # -------------------------------------------------------- 改價/減量/刪單
    def correct_price_by_seq_no(self, seq_no: str, new_price: float, tif: int):
        self._require_login()
        message, code = self._order.CorrectPriceBySeqNo(
            self._client.user_id, True, self._client.account, seq_no, str(new_price), tif,
        )
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
        else:
            self.order_sent.emit(str(message))

    def decrease_order_by_seq_no(self, seq_no: str, decrease_qty: int):
        self._require_login()
        message, code = self._order.DecreaseOrderBySeqNo(
            self._client.user_id, True, self._client.account, seq_no, int(decrease_qty),
        )
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
        else:
            self.order_sent.emit(str(message))

    def cancel_order_by_seq_no(self, seq_no: str):
        self._require_login()
        message, code = self._order.CancelOrderBySeqNo(
            self._client.user_id, True, self._client.account, seq_no,
        )
        if code != 0:
            self.order_failed.emit(self._center_msg(code))
        else:
            self.order_sent.emit(str(message))

    # ------------------------------------------------------------- 既有委託查詢
    def get_order_report(self, n_format: int = 1) -> str:
        """*** 阻塞式呼叫 (官方文件明講)，呼叫端要自己節流 (文件要求每次
        查詢間隔至少 5 秒) 並且不要在會卡住整個 UI 的地方直接呼叫。***
        回傳原始字串，逐欄位格式官方文件沒有查到對照表，先不解析。"""
        self._require_login()
        return self._order.GetOrderReport(self._client.user_id, self._client.account, n_format)

    def get_fulfill_report(self, n_format: int = 1) -> str:
        self._require_login()
        return self._order.GetFulfillReport(self._client.user_id, self._client.account, n_format)

    def query_future_rights(self, s_coin_type: int = COIN_TYPE_TWD) -> None:
        """查詢期貨/選擇權帳戶權益數 (期貨、選擇權共用同一期貨帳戶，權益
        數已經合併選擇權部位，不需要另外查)。非同步查詢，結果透過
        OnFutureRights 事件回傳，解析後從 future_rights 訊號送出。

        s_coin_type 對應官方文件 4-2-38 GetFutureRights 的「幣別」參數
        (不是查詢格式碼)：0:全幣別含基幣 1:基幣(台幣TWD) 2:人民幣RMB。
        這支專案只用台幣帳戶，固定用 COIN_TYPE_TWD，不處理 0 時每個幣別
        各回傳一筆、最後多回傳一筆 "##" 開頭結束標記的情況。

        不要連續呼叫，太頻繁會回 SK_ERROR_QUERY_IN_PROCESSING (1019)。"""
        self._require_login()
        code = self._order.GetFutureRights(self._client.user_id, self._client.account, s_coin_type)
        if code != 0:
            self.future_rights_failed.emit(self._center_msg(code))

    # ------------------------------------------------------------- 頻率保護
    def set_max_qty(self, market_type: int, max_qty: int) -> Optional[str]:
        code = self._order.SetMaxQty(market_type, int(max_qty))
        return None if code == 0 else self._center_msg(code)

    def set_max_count(self, market_type: int, max_count: int) -> Optional[str]:
        code = self._order.SetMaxCount(market_type, int(max_count))
        return None if code == 0 else self._center_msg(code)

    def unlock_order(self, market_type: int) -> Optional[str]:
        code = self._order.UnlockOrder(market_type)
        return None if code == 0 else self._center_msg(code)

    # ------------------------------------------------------------- 回報解析
    def _center_msg(self, code: int) -> str:
        try:
            return self._client.center.SKCenterLib_GetReturnCodeMessage(code)
        except Exception:  # noqa: BLE001
            return f"錯誤碼 {code}"

    def _handle_report(self, bstr_data: str) -> None:
        # 印出原始字串+目前對照表解析結果，是排查「委託一直卡在等待回
        # 報」這個問題唯一的辦法——這裡收不到 OnNewData 完全不會印出任
        # 何東西，收得到但欄位對不上(_match_record 抓不到對應的委託)兩
        # 種情況印出來的內容不一樣，靠這個區分是連線問題還是欄位對照表
        # 錯誤。
        print(f"[OnNewData] raw={bstr_data}")
        parts = bstr_data.split(',')
        report: Dict[str, str] = {"raw": bstr_data}
        for name, value in zip(_REPORT_FIELDS, parts):
            report[name] = value
        print(f"[OnNewData] parsed com_id1={report.get('com_id1')} com_id2={report.get('com_id2')} "
              f"buy_sell={report.get('buy_sell')} type={report.get('type')} order_err={report.get('order_err')} "
              f"seq_no={report.get('seq_no')}")
        self.order_report.emit(report)

    def _handle_future_rights(self, bstr_data: str) -> None:
        # 文件：查詢結束時會多回傳一筆以 "##" 開頭的內容——這支只查
        # COIN_TYPE_TWD 單一幣別，理論上不會像全幣別查詢那樣收到多筆，
        # 但結束標記這筆還是可能出現，濾掉不當成資料解析/送出。
        if bstr_data.startswith("##"):
            return
        parts = bstr_data.split(',')
        rights: Dict[str, str] = {"raw": bstr_data}
        for name, value in zip(FUTURE_RIGHTS_FIELDS, parts):
            rights[name] = value
        self.future_rights.emit(rights)


class _ReplyEvents:
    def __init__(self, owner: CapitalOrderClient):
        self._owner = owner

    def OnNewData(self, bstrUserID, bstrData):
        self._owner._handle_report(bstrData)


class _OrderAsyncEvents:
    def __init__(self, owner: CapitalOrderClient):
        self._owner = owner

    def OnAsyncOrder(self, nThreadID, nCode, bstrMessage):
        # 文件：委託成功時 bstrMessage 是13碼委託序號，失敗時是失敗原因。
        print(f"[OnAsyncOrder] ThreadID={nThreadID} Code={nCode} Message={bstrMessage}")
        if nCode != 0:
            self._owner.order_failed.emit(f"{self._owner._center_msg(nCode)}：{bstrMessage}")

    def OnFutureRights(self, bstrData):
        self._owner._handle_future_rights(bstrData)
