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
        self._reply_events = _ReplyEvents()
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

        code = self.order.GetUserAccount()
        if code != 0:
            self.login_failed.emit(self._center_msg(code))
            return False

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
        # bstrAccountData 逗號分隔欄位，完整帳號 = IB代號(index 1) + 帳號
        # (index 3)，組法照抄群益官方範例 LoginForm.py 的 OnAccount。
        values = bstrAccountData.split(',')
        if len(values) < 4:
            return
        full_account = values[1] + values[3]
        if full_account not in self._client.accounts:
            self._client.accounts.append(full_account)
            self._client.accounts_ready.emit(list(self._client.accounts))


class _ReplyEvents:
    """OnReplyMessage 是公告訊息 (跟委託/成交回報 OnNewData 無關)，SKCOM
    規定這個事件槽要先掛好才能登入。官方範例的回傳值固定給 -1 (代表不處
    理這則公告)，照抄。"""

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1
