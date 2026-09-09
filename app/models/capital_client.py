"""
群益證券/期貨 SKCOM API (comtypes COM 元件) 的登入封裝。

跟凱基不一樣，登入是「三段式」流程 (參考群益官方範例
CapitalAPI_2.13.59_PythonExample/PythonExampleV2/Login/LoginForm.py 驗證過)：
    1. SKCenterLib_SetAuthority 選正式/測試環境，SKCenterLib_Login(id, pwd)
       同步登入，直接拿回傳碼 (不是像凱基那樣要另外等 signal)
    2. 登入成功後呼叫 SKOrderLib_Initialize() + GetUserAccount()，交易帳號
       是透過 SKOrderLib 的 OnAccount 事件非同步回報的 (可能不只一個帳號)
    3. 呼叫端要從回報的帳號清單選一個 (select_account) 才能下單/查詢

SKCOM.dll 是要先註冊過的 COM 元件 (跑一次
vendor/capital_api/x64/install.bat 內的 regsvr32，一次性環境設置，不是這支
程式做的事)，comtypes.client.GetModule 讀 typelib 這一步不需要註冊就能做，
但 CreateObject 實際產生物件需要。
"""
import comtypes.client
from PyQt5.QtCore import QObject, pyqtSignal

from app.paths import PROJECT_ROOT

SKCOM_DLL = PROJECT_ROOT / "vendor" / "capital_api" / "x64" / "SKCOM.dll"

AUTHORITY_PROD = 0  # 正式環境
AUTHORITY_TEST = 2  # 測試環境


class CapitalClient(QObject):
    """登入後 self.order / self.quote / self.reply 這幾個 COM 物件給
    capital_quote_client / capital_order_client 共用（同一份 SKOrderLib /
    SKQuoteLib 物件在整個 process 只能建立一次）。"""

    login_failed = pyqtSignal(str)
    accounts_ready = pyqtSignal(list)  # 每次收到新帳號都會送出目前完整清單
    report_connect_status = pyqtSignal(bool, str)  # 回報主機連線結果 (OnConnect)
    report_ready = pyqtSignal()                    # 回報回補完成 (OnComplete)，之前沒收到過

    def __init__(self):
        super().__init__()
        comtypes.client.GetModule(str(SKCOM_DLL))
        import comtypes.gen.SKCOMLib as sk
        self.sk = sk

        self.center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        self.order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
        self.quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        self.reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

        self.user_id = None
        self.account = None
        self.accounts = []

        self._order_events = _OrderEvents(self)
        self._order_handler = comtypes.client.GetEvents(self.order, self._order_events)

        # SKCOM 硬性要求 SKReplyLib 的 OnReplyMessage 事件槽要在登入前就掛
        # 好，不然 SKCenterLib_Login 會直接失敗回報
        # SK_WARNING_REGISTER_REPLYLIB_ONREPLYMESSAGE_FIRST。這裡先掛一個最
        # 小的事件槽，實際的委託/成交回報 (OnNewData) 交給
        # capital_order_client.py 另外掛一個獨立的事件槽處理。
        self._reply_events = _ReplyEvents(self)
        self._reply_handler = comtypes.client.GetEvents(self.reply, self._reply_events)

    @property
    def is_logged_in(self) -> bool:
        return self.user_id is not None

    def login(self, user_id: str, password: str, simulation: bool) -> bool:
        """同步呼叫，SDK 本身就是同步回傳登入結果，不用開執行緒。
        回傳 True 代表登入 + 帳號查詢流程都送出成功；實際帳號清單稍後從
        accounts_ready 訊號收，因為那是非同步事件回報的。"""
        authority = AUTHORITY_TEST if simulation else AUTHORITY_PROD
        code = self.center.SKCenterLib_SetAuthority(authority)
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

        code = self.center.SKCenterLib_Login(user_id, password)
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

        self.user_id = user_id
        self.accounts = []

        code = self.order.SKOrderLib_Initialize()
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

        # 下單前一定要讀憑證，不然憑證沒驗證過，送單時會回
        # SK_ERROR_CERT_NOT_VERIFIED (查報價/查帳號不需要憑證，所以這步
        # 漏掉時前面都測得過，只有真的送單才會爆——親身踩過)。順序照官
        # 方文件《群益PythonAPI使用前看我看我.docx》：Initialize →
        # ReadCertByID → GetUserAccount。
        code = self.order.ReadCertByID(user_id)
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

        code = self.order.GetUserAccount()
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

        # *** 這一步之前漏掉，是委託回報 OnNewData 收不到、狀態卡在「掛
        # 單中」出不來的根因 ***：SKReplyLib_ConnectByID 是連線回報主機的
        # 必要呼叫，跟前面掛 OnReplyMessage/OnNewData 事件槽是兩回事——掛
        # 事件槽只是「準備好接收的管道」，沒呼叫這個的話回報主機根本沒把
        # 你接上，事件永遠不會觸發。核對自官方範例
        # PythonExample/Reply_Service/Reply.py 的 btnConnect_Click，文件
        # 《12.回報.docx》也明講「不需先做回報連線」就能登入，所以放在登
        # 入流程最後一步呼叫，不影響登入本身成不成功。連線結果非同步從
        # OnConnect 事件回報 (report_connect_status 訊號)，回報回補完成
        # 從 OnComplete 事件回報 (report_ready 訊號)；文件明講「若未收到
        # OnComplete 通知，代表新建立的回報連線及回傳回報資料異常」，所以
        # 這個事件值得讓 UI 顯示出來，不要默默失敗。
        code = self.reply.SKReplyLib_ConnectByID(user_id)
        print(f"[SKReplyLib_ConnectByID] code={code} ({self._center_msg(code) if code != 0 else 'OK，等待 OnConnect/OnComplete'})")
        if code != 0:
            self.report_connect_status.emit(False, self._center_msg(code))

        return True

    def select_account(self, account: str) -> None:
        self.account = account

    def _center_msg(self, code: int) -> str:
        try:
            return self.center.SKCenterLib_GetReturnCodeMessage(code)
        except Exception:  # noqa: BLE001
            return f"錯誤碼 {code}"


class _OrderEvents:
    """只接 OnAccount，其餘 ISKOrderLibEvents 事件 (OnAsyncOrder 等) 交給
    capital_order_client.py 另外掛一個事件槽處理，comtypes 同一個 COM 物件
    可以掛多個獨立的 GetEvents 事件槽。"""

    def __init__(self, client: "CapitalClient"):
        self._client = client

    def OnAccount(self, bstrLogInID, bstrAccountData):
        # bstrAccountData 逗號分隔欄位，依官方文件《4.下單準備介紹.docx》：
        # 市場,分公司代碼,分公司,帳號,身份證字號,姓名。完整帳號 = 分公司
        # 代碼(index 1) + 帳號(index 3)，組法照抄官方範例
        # order_service/Order.py 的 OnAccount。
        #
        # 同一個登入 ID 底下每種市場別(TS證券/TF期貨/OF海期/OS複委託...)
        # 都會各自呼叫一次 OnAccount，不是只有一筆。這支程式只做期貨/
        # 選擇權下單 (SendOptionOrder/SendDuplexOrder 都要求期貨帳號)，
        # 期貨、選擇權文件上是共用同一個 TF 帳號 (Order.py 裡
        # boxFutureAccount 選好之後同時 SetAccount 給 future 跟 option
        # 兩個下單物件)，所以這裡只留 market == 'TF' 的帳號，非期貨帳號
        # 一律忽略，不會混進選單、也不會被誤選成下單帳號。
        values = bstrAccountData.split(',')
        if len(values) < 4:
            return
        market, broker, _branch, account_no = values[0], values[1], values[2], values[3]
        if market != 'TF':
            return
        full_account = broker + account_no
        existing = {a["full_account"] for a in self._client.accounts}
        if full_account not in existing:
            self._client.accounts.append({
                "full_account": full_account,
                "market": market,
                "raw": bstrAccountData,
            })
            self._client.accounts_ready.emit(list(self._client.accounts))


class _ReplyEvents:
    """OnReplyMessage 是公告訊息 (跟委託/成交回報 OnNewData 無關)，SKCOM
    規定這個事件槽要先掛好才能登入。官方範例的回傳值固定給 -1 (代表不處
    理這則公告)，照抄。OnConnect/OnComplete/OnDisconnect 是回報主機連線
    狀態，OnNewData(委託/成交回報本身) 由 capital_order_client.py 另外掛
    一個獨立事件槽處理 (comtypes 同一個 COM 物件可以掛多個事件槽)。"""

    def __init__(self, client: "CapitalClient"):
        self._client = client

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1

    def OnConnect(self, bstrUserID, nErrorCode):
        print(f"[OnConnect] userID={bstrUserID} errorCode={nErrorCode}")
        if nErrorCode == 0:
            self._client.report_connect_status.emit(True, "已連線")
        else:
            self._client.report_connect_status.emit(False, self._client._center_msg(nErrorCode))

    def OnComplete(self, bstrUserID):
        # 文件：「回報連線後會進行回報回補，等收到此事件通知後表示回補
        # 完成」「若未收到此通知，代表新建立的回報連線及回傳回報資料異
        # 常」——這個事件本身就是「回報現在真的能正常收了」的證明。
        print(f"[OnComplete] userID={bstrUserID} 回報回補完成")
        self._client.report_ready.emit()

    def OnDisconnect(self, bstrUserID, nErrorCode):
        self._client.report_connect_status.emit(False, self._client._center_msg(nErrorCode))
