"""
T 字報價「到期別/價格間距/上下幾檔」查詢參數記憶，重開視窗自動套用。
跟 layout_store.py 存法一致：本機小 json 檔。

到期別存的是 ContractExpiry.label (如 "202609W2")，不是履約價/日期本身，
因為週選每週都會捲動到新的一週，同一個 label 到了下一個週期會指向新的
ContractExpiry 物件 (同一個「相對位置」，不是同一份合約)——如果那個舊
label 已經不在最新清單裡 (代表那份合約已經到期下架)，呼叫端要自己套用
「優先週三選、其次月選」的備援邏輯，這裡只負責存/讀，不管備援規則。
"""
import json

from app.paths import PROJECT_ROOT

QUERY_PREF_FILE = PROJECT_ROOT / ".quote_query_pref.json"


def save(expiry_label: str, step: int, rows: int) -> None:
    try:
        QUERY_PREF_FILE.write_text(
            json.dumps({"expiry_label": expiry_label, "step": step, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"儲存查詢條件失敗: {e}")
        pass  # 記不住下次的查詢條件不影響畫面本身，不要讓存檔失敗炸掉程式


def load():
    """回傳 (expiry_label, step, rows)，任何一個沒存過/讀失敗就是 None。"""
    try:
        data = json.loads(QUERY_PREF_FILE.read_text(encoding="utf-8"))
        return data.get("expiry_label"), data.get("step"), data.get("rows")
    except Exception as e:
        print(f"讀取查詢條件失敗: {e}")
        return None, None, None
