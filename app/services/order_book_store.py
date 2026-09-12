"""
下單匣/成交回報本地保存。跟 layout_store.py/position_groups_store.py 同一
套作法：本機小 json 檔，讀寫失敗靜默吞掉(存不了不該讓下單功能整個壞掉，
頂多下次開回空的)。

*** 存在 pref/paper/ 或 pref/live/ 底下(看目前連線的環境)，不是共用的
pref/ 根目錄 ***：委託紀錄代表真實交易狀態，paper 帳戶測試出來的委託不
能跟 live 帳戶的混在一起，見 app/paths.py::trading_pref_dir() 的說明。

這裡只負責「讀寫一份 dict 清單」，dataclass <-> dict 的轉換跟「重開機後
非終態委託要怎麼處理」是 order_book.py 的責任，不是這支檔案的——這支檔
案不知道 OrderRecord 長什麼樣子，故意保持這樣，跟其他 store 模組一致
(純 IO，不懂業務邏輯)。
"""
import json

from app.paths import trading_pref_dir


def _file():
    return trading_pref_dir() / "order_book_pref.json"


def load() -> list:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data.get("records", [])
    except Exception:
        return []


def save(records: list) -> None:
    try:
        _file().write_text(
            json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass
