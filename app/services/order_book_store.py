"""
下單匣/成交回報本地保存。跟 layout_store.py/position_groups_store.py 同一
套作法：本機小 json 檔，讀寫失敗靜默吞掉(存不了不該讓下單功能整個壞掉，
頂多下次開回空的)。

這裡只負責「讀寫一份 dict 清單」，dataclass <-> dict 的轉換跟「重開機後
非終態委託要怎麼處理(凍結、不自動重送)」是 order_book.py 的責任，不是這
支檔案的——這支檔案不知道 OrderRecord 長什麼樣子，故意保持這樣，跟其他
store 模組一致(純 IO，不懂業務邏輯)。
"""
import json

from app.paths import PROJECT_ROOT

ORDER_BOOK_FILE = PROJECT_ROOT / ".order_book_pref.json"


def load() -> list:
    try:
        data = json.loads(ORDER_BOOK_FILE.read_text(encoding="utf-8"))
        return data.get("records", [])
    except Exception:
        return []


def save(records: list) -> None:
    try:
        ORDER_BOOK_FILE.write_text(
            json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass
