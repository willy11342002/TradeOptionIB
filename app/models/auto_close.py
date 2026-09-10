"""
未平倉部位自動平倉／停利停損規則的純資料模型。不依賴 Qt，方便單獨測試
(跟 app/services/payoff.py 同風格)。

整套規則(觸發條件、動作、口數、履約價填法)是使用者在對話中逐項拍板定案
的，這裡只是把定案的結構轉成 dataclass，細節緣由見
app/models/auto_close_manager.py 開頭的說明。
"""
from dataclasses import dataclass
from typing import Optional

STATUS_UNSET = "unset"       # 從沒設定過
STATUS_PAUSED = "paused"     # 設定過但目前暫停中(手動暫停/單邊觸發後自動暫停/App重啟凍結)
STATUS_ARMED = "armed"       # 武裝中，持續監控現價
STATUS_TRIGGERED = "triggered"  # 已觸發，平倉/重開單處理中或已完成
STATUS_FAILED = "failed"     # 觸發後平倉單被交易所真的拒絕(非未成交)，鏈結中止待人工排查

# 單邊停損三選一動作
SL_MODE_REOPEN_DOUBLE = "reopen_double"  # 規則3：預期回頭，原地重開、口數×2
SL_MODE_NEW_GROUP = "new_group"          # 規則4：預期停住，虧損邊平倉+另開一整組新價差
SL_MODE_ADD_LEG = "add_leg"              # 規則5：外在價值不足，虧損邊不動、加開對側裸賣一支腳


@dataclass
class ReopenSpec:
    """新倉一支腳需要的最少資訊：履約價(1個，另一腳寬度沿用原寬度、方向
    照下單面板 _leg2_strike 同一套規則推算，見 auto_close_manager.py)、
    委託價(使用者設定時手動填的絕對限價)。"""
    strike: float
    price: float


@dataclass
class TakeProfitRule:
    """規則1(整組)/規則2(單邊)共用同一個結構——整組停利的 reopen 固定是
    None(不重開)，單邊停利的 reopen 是 None 代表「只平倉不重開」、有值才
    是規則2「平倉+原地重開」。"""
    threshold_points: float
    reopen: Optional[ReopenSpec] = None
    status: str = STATUS_UNSET
    paused_reason: Optional[str] = None
    triggered_at: Optional[float] = None


@dataclass
class StopLossRule:
    threshold_points: float
    mode: str = SL_MODE_REOPEN_DOUBLE
    reopen: Optional[ReopenSpec] = None    # mode=SL_MODE_REOPEN_DOUBLE
    new_put: Optional[ReopenSpec] = None   # mode=SL_MODE_NEW_GROUP
    new_call: Optional[ReopenSpec] = None  # mode=SL_MODE_NEW_GROUP
    add_leg: Optional[ReopenSpec] = None   # mode=SL_MODE_ADD_LEG
    status: str = STATUS_UNSET
    triggered_at: Optional[float] = None


@dataclass
class PositionRules:
    """一個部位(symbol_key)的停利/停損設定，任一個可以是 None(未設
    定)。停利/停損同時武裝、互斥觸發——任一個觸發後另一個要改成
    STATUS_PAUSED，見 auto_close_manager.py。"""
    take_profit: Optional[TakeProfitRule] = None
    stop_loss: Optional[StopLossRule] = None
