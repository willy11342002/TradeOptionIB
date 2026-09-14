"""
選擇權報價「技術分析」頁籤裡使用者自己畫的直線/斜線持久化——用標的代碼
當 key(不分時間週期，同一組線在切換 1分K/日線/週線...時都會帶出來，見
`app/views/web_technical_analysis_panel.py` 開頭的說明)，跟
`watchlist_store.py`/`filter_preset_store.py` 同一套慣例：本機小 json
檔，放 `PREF_DIR` 根目錄(這是 UI 狀態，不是代表真實交易狀態的資料，見
`app/paths.py` 的說明)，讀寫失敗一律靜默吞掉——存取失敗不該讓報價盤其
他功能一起壞掉。

存的是 Plotly shape 物件原始 dict(`{"type": "line", "x0":..., "y0":...,
"x1":..., "y1":..., "line": {...}, ...}`)，直接來自前端
`plotly_relayout` 事件裡的 `shapes` 欄位，不額外轉換格式——畫線工具是
Plotly 內建的 `drawline`/`eraseshape`，讀回來原封不動塞回
`layout.shapes` 就能還原，不需要自己另外設計一套線段格式。
"""
import json

from app.paths import PREF_DIR

_FILE = PREF_DIR / "chart_drawings.json"


def _load_all() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load(symbol: str) -> list[dict]:
    return _load_all().get(symbol, [])


def save(symbol: str, shapes: list[dict]) -> None:
    data = _load_all()
    if shapes:
        data[symbol] = shapes
    else:
        data.pop(symbol, None)
    _save_all(data)
