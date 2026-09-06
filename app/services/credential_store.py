"""
用 Windows DPAPI (CryptProtectData/CryptUnprotectData) 把登入帳密加密成
二進位檔存在本機，只有同一台電腦、同一個 Windows 帳號能解密。
讀取/解密任何一步失敗都直接回傳 None，呼叫端讓使用者重新輸入即可，不丟例外。
"""
import ctypes
import json
from ctypes import wintypes
from typing import Optional, Tuple

from app.paths import PROJECT_ROOT

CRED_FILE = PROJECT_ROOT / ".kgi_credentials.bin"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes, buf_holder: list) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    buf_holder.append(buf)  # 避免 buf 被 GC 回收
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _protect(data: bytes) -> bytes:
    buf_holder: list = []
    blob_in = _to_blob(data, buf_holder)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    buf_holder: list = []
    blob_in = _to_blob(data, buf_holder)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_credentials(person_id: str, person_pwd: str, simulation: bool) -> None:
    try:
        payload = json.dumps({
            "person_id": person_id,
            "person_pwd": person_pwd,
            "simulation": simulation,
        }).encode("utf-8")
        CRED_FILE.write_bytes(_protect(payload))
    except Exception:
        pass  # 記憶功能是加分項，存檔失敗不影響登入流程


def load_credentials() -> Optional[Tuple[str, str, bool]]:
    try:
        payload = _unprotect(CRED_FILE.read_bytes())
        data = json.loads(payload.decode("utf-8"))
        return data["person_id"], data["person_pwd"], bool(data["simulation"])
    except Exception:
        return None


def clear_credentials() -> None:
    try:
        CRED_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
