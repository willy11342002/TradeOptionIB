"""
Microsoft Excel RTD (RealTimeData) 協定的兩個標準 COM 介面定義。

這兩組 GUID 跟 dispid 是微軟 RTD 規格裡固定的通用值，任何 RTD Server
(不管是 ThinkOrSwim、XQ全球贏家、或這裡的華南 xqrtd.rtdserverhns) 都是
同一組，不是特定廠商專屬的。

參考 (架構思路，非逐字抄錄)：
https://github.com/tifoji/pyrtdc — 一個現代化、用 comtypes 做原生 COM
callback 的 Python RTD client 專案 (MIT License)，這裡採用同樣的手法。

    IRtdServer      — 資料提供端 (RTD Server) 實作的介面
    IRTDUpdateEvent — 客戶端(我們)實作的介面，Server 有新資料時會呼叫
                      UpdateNotify 通知我們

comtypes 用 ctypes 在執行期真的建出一份 C 等級的 vtable，所以
IRTDUpdateEvent.UpdateNotify 才能被 Server 用原生 COM callback 直接呼叫到，
不需要額外開一條 polling 迴圈去等。
"""
from ctypes import HRESULT, POINTER, c_int
from ctypes.wintypes import VARIANT_BOOL

from comtypes import COMMETHOD, GUID, IUnknown, dispid
from comtypes.automation import VARIANT, IDispatch, _midlSAFEARRAY

GUID_IRtdServer = GUID("{EC0E6191-DB51-11D3-8F3E-00C04F3651B8}")
GUID_IRTDUpdateEvent = GUID("{A43788C1-D91B-11D3-8F39-00C04F3651B8}")


class IRTDUpdateEvent(IDispatch):
    _case_insensitive_ = True
    _iid_ = GUID_IRTDUpdateEvent
    _idlflags_ = ["dual", "oleautomation"]
    _methods_ = [
        COMMETHOD([dispid(10)], HRESULT, "UpdateNotify"),
        COMMETHOD(
            [dispid(11), "propget"], HRESULT, "HeartbeatInterval",
            (["out", "retval"], POINTER(c_int), "plRetVal"),
        ),
        COMMETHOD(
            [dispid(11), "propput"], HRESULT, "HeartbeatInterval",
            (["in"], c_int, "plRetVal"),
        ),
        COMMETHOD([dispid(12)], HRESULT, "Disconnect"),
    ]


class IRtdServer(IDispatch):
    _case_insensitive_ = True
    _iid_ = GUID_IRtdServer
    _idlflags_ = ["dual", "oleautomation"]
    _methods_ = [
        COMMETHOD(
            [dispid(10)], HRESULT, "ServerStart",
            (["in"], POINTER(IRTDUpdateEvent), "CallbackObject"),
            (["out", "retval"], POINTER(c_int), "pfRes"),
        ),
        COMMETHOD(
            [dispid(11)], HRESULT, "ConnectData",
            (["in"], c_int, "TopicID"),
            (["in"], POINTER(_midlSAFEARRAY(VARIANT)), "Strings"),
            (["in", "out"], POINTER(VARIANT_BOOL), "GetNewValues"),
            (["out", "retval"], POINTER(VARIANT), "pvarOut"),
        ),
        COMMETHOD(
            [dispid(12)], HRESULT, "RefreshData",
            (["in", "out"], POINTER(c_int), "TopicCount"),
            (["out", "retval"], POINTER(_midlSAFEARRAY(VARIANT)), "parrayOut"),
        ),
        COMMETHOD(
            [dispid(13)], HRESULT, "DisconnectData",
            (["in"], c_int, "TopicID"),
        ),
        COMMETHOD(
            [dispid(14)], HRESULT, "Heartbeat",
            (["out", "retval"], POINTER(c_int), "pfRes"),
        ),
        COMMETHOD([dispid(15)], HRESULT, "ServerTerminate"),
    ]


__all__ = ["IRTDUpdateEvent", "IRtdServer", "GUID_IRtdServer", "GUID_IRTDUpdateEvent"]
