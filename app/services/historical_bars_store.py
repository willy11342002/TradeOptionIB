"""
技術分析頁籤K線的本機快取——用 conId+週期 當 key，把 `reqHistoricalDataAsync`
查到的K棒存一份在本機，之後同一個標的/週期只需要抓「快取之後新增的這一
小段」做增量更新，不用每次切換週期/重新整理都整段重查(分K的完整回補很
花時間、也容易撞 IB 的 pacing 限制)，見
`app/views/web_technical_analysis_panel.py` 的 `_TIMEFRAMES` 說明。

跟 `chart_drawing_store.py`/`watchlist_store.py` 同一套慣例：本機小 json
檔案放 `PREF_DIR`，讀寫失敗一律靜默吞掉——存取失敗不該讓技術分析圖表整
個壞掉，退回重查就好。key 用 conId 不用 symbol，理由跟 CLAUDE.md 提過的
一樣：conId 才是合約穩定、唯一的識別碼。K棒資料是純市場資料，不代表真實
交易狀態，不受 `app/paths.py` 那套 pref/paper、pref/live 環境隔離規則約
束(跟 chart_drawings.json 一樣直接放 PREF_DIR 底下的子目錄)。
"""
import json

from app.paths import PREF_DIR

_DIR = PREF_DIR / "historical_bars"
_DIR.mkdir(exist_ok=True)


def _file(con_id: int, bar_size_key: str):
    return _DIR / f"{con_id}_{bar_size_key}.json"


def load(con_id: int, bar_size_key: str) -> list[dict]:
    try:
        return json.loads(_file(con_id, bar_size_key).read_text(encoding="utf-8"))
    except Exception:
        return []


def save(con_id: int, bar_size_key: str, bars: list[dict]) -> None:
    try:
        _file(con_id, bar_size_key).write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def merge(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """用 `x`(ISO 日期字串)當 key 合併，新資料覆蓋舊的同一根——IB 最後一
    根K棒在收盤/收週/收月之前查到的都是「還在跳動」的未完成K棒，增量更新
    抓到同一根時要用新值蓋掉舊值，不是單純附加。回傳依日期排序後的完整清
    單。"""
    by_x = {b["x"]: b for b in existing}
    for b in fresh:
        by_x[b["x"]] = b
    return [by_x[k] for k in sorted(by_x)]
