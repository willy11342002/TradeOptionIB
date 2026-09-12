"""
報價格「標的/到期日/上下幾檔」查詢參數記憶，重開視窗自動套用。跟
layout_store.py 存法一致：本機小 json 檔。

到期日存的是 IB 的 lastTradeDateOrContractMonth 字串(YYYYMMDD)本身——跟
舊版(TAIFEX週三選/月選的相對位置label)不一樣，IB 這邊到期日清單每次查
都是即時的絕對日期，沒有「同一個相對位置換了新一輪合約」這種需要備援邏
輯的問題；如果存的到期日已經不在最新清單裡(到期下架了)，呼叫端自己選清
單裡最近的一個即可，不需要額外的分類備援規則。
"""
import json

from app.paths import PREF_DIR

QUERY_PREF_FILE = PREF_DIR / "quote_query_pref.json"  # 檔名歷史上叫 quote_，模組名叫 query_，兩者其實是同一件事


def save(symbol: str, expiry: str, rows: int) -> None:
    try:
        QUERY_PREF_FILE.write_text(
            json.dumps({"symbol": symbol, "expiry": expiry, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # 記不住下次的查詢條件不影響畫面本身，跟其他 pref store 一致靜默吞掉


def load():
    """回傳 (symbol, expiry, rows)，任何一個沒存過/讀失敗就是 None。"""
    try:
        data = json.loads(QUERY_PREF_FILE.read_text(encoding="utf-8"))
        return data.get("symbol"), data.get("expiry"), data.get("rows")
    except Exception:
        return None, None, None
