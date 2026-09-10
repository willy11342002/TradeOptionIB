"""
未平倉部位自動平倉／停利停損規則本地保存。跟 order_book_store.py/
position_groups_store.py 同一套作法：本機小 json 檔，讀寫失敗靜默吞掉
(存不了規則設定不該讓部位視窗整個壞掉，頂多下次開回未設定狀態)。

這裡只負責「讀寫一份 dict」，dataclass <-> dict 的轉換是
app/models/auto_close_manager.py 的責任，這支檔案不知道
TakeProfitRule/StopLossRule 長什麼樣子，跟其他 store 模組一致(純 IO，不
懂業務邏輯)。

存三塊：
    position_rules： symbol_key -> {"take_profit": {...}|None, "stop_loss": {...}|None}
    group_rules：    group_id -> {"take_profit": {...}|None}
    pending_fill_actions： order_record_id -> 觸發後排隊等平倉單成交才執行
        的重開參數——這個要跟著存檔，不然 App 重開後「平倉單成交才重開」
        這個鏈結會斷掉(平倉單本身靠 order_book_store.py 已經會凍結成暫停
        繼續留著，但沒有這份資料就不知道它成交之後還要做什麼)。
"""
import json

from app.paths import PREF_DIR

AUTO_CLOSE_FILE = PREF_DIR / "auto_close_pref.json"

_DEFAULT = {"position_rules": {}, "group_rules": {}, "pending_fill_actions": {}}


def load() -> dict:
    try:
        data = json.loads(AUTO_CLOSE_FILE.read_text(encoding="utf-8"))
        for key, default_value in _DEFAULT.items():
            data.setdefault(key, default_value)
        return data
    except Exception:
        return {k: dict(v) for k, v in _DEFAULT.items()}


def save(data: dict) -> None:
    try:
        AUTO_CLOSE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
