"""
華南 XQ RTD (xqrtd.rtdserverhns) 的 Python 客戶端，不透過 Excel。

用 comtypes 讓 Python 直接實作 IRTDUpdateEvent COM 介面 (原生 vtable
callback，不是 polling)，直接呼叫 RTD Server 的 IRtdServer 介面。
架構思路參考 https://github.com/tifoji/pyrtdc (MIT License)，寫法依
xqrtd.rtdserverhns 的實際行為調整。

跟 Excel 裡看到的 =RTD("xqrtd.rtdserverhns",,"TX2N09C40900.TF-Price") 對照：
    ProgID = "xqrtd.rtdserverhns"（固定）
    Server 參數（第二個逗號中間那個）留空 = 本機
    Topic  = "商品代碼.欄位名" 單一字串（不是像某些 RTD 那樣拆多個參數）

已知欄位名 (TF- 開頭): ID, Name, PreClose, Open, High, Low,
PreTotalVolume, Time, Price, Bid, Ask, Delta, Gamma, Theta, Vega,
TheoryPrice, ImplyVolatility
"""
from typing import Callable, Dict, Optional

import pythoncom
from comtypes import COMObject
from comtypes.automation import VARIANT, VARIANT_BOOL
from comtypes.client import CreateObject

from app.models.rtd_interfaces import IRTDUpdateEvent, IRtdServer

PROGID = "xqrtd.rtdserverhns"


def _unwrap_connect_result(raw):
    """ConnectData 的 GetNewValues 參數是 in/out，comtypes 因此把回傳值包成
    [GetNewValues結果, 真正的初始值(pvarOut)] 這種 2 元素結構，不能直接把
    整包當成報價值使用，這裡把真正的值拆出來。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return raw[1]
    return raw


class RTDClient(COMObject):
    _com_interfaces_ = [IRTDUpdateEvent]

    def __init__(self, on_update: Optional[Callable[[], None]] = None):
        super().__init__()
        self.server: Optional[IRtdServer] = None
        self._on_update = on_update
        self._heartbeat_interval = 15000
        self.topics: Dict[int, str] = {}  # topic_id -> "商品代碼.欄位名"
        self._next_topic_id = 1

    # -------------------------------------------------- IRTDUpdateEvent
    # 以下方法名稱要跟 rtd_interfaces.IRTDUpdateEvent 定義的一致，
    # comtypes.COMObject 會自動比對、掛進原生 vtable，讓 Server 端真的能
    # 呼叫到 (不是我們自己在等 polling)。
    def UpdateNotify(self):
        if self._on_update:
            self._on_update()
        return True

    def Disconnect(self):
        pass

    @property
    def heartbeat_interval(self) -> int:
        return self._heartbeat_interval

    @heartbeat_interval.setter
    def heartbeat_interval(self, value: int):
        self._heartbeat_interval = value

    # -------------------------------------------------------- 對外 API
    def start(self) -> None:
        pythoncom.CoInitialize()
        self.server = CreateObject(PROGID, interface=IRtdServer)
        result = self.server.ServerStart(self)
        if result != 1:
            raise RuntimeError(f"ServerStart 失敗，回傳值: {result}")

    def subscribe(self, topic_str: str) -> int:
        """topic_str 例如 'TX2N09C40900.TF-Price'。回傳 (topic_id, 初始值)。"""
        topic_id = self._next_topic_id
        self._next_topic_id += 1
        strings = (VARIANT * 1)()
        strings[0].value = topic_str
        get_new_values = VARIANT_BOOL(True)
        raw = self.server.ConnectData(topic_id, strings, get_new_values)
        self.topics[topic_id] = topic_str
        return topic_id, _unwrap_connect_result(raw)

    def unsubscribe(self, topic_id: int) -> None:
        try:
            self.server.DisconnectData(topic_id)
        finally:
            self.topics.pop(topic_id, None)

    def refresh(self) -> Dict[str, object]:
        """呼叫 RefreshData，回傳 {topic_str: value}（只含這次有更新的項目）。"""
        result = self.server.RefreshData()
        out: Dict[str, object] = {}
        if not result:
            return out
        topic_count, data = result
        if not data or topic_count == 0:
            return out
        topic_ids, raw_values = data
        for tid, val in zip(topic_ids, raw_values):
            topic_str = self.topics.get(tid)
            if topic_str is not None:
                out[topic_str] = val
        return out

    def heartbeat(self) -> int:
        return self.server.Heartbeat()

    def stop(self) -> None:
        try:
            if self.server is not None:
                self.server.ServerTerminate()
        except Exception:  # noqa: BLE001
            pass
        self.server = None
