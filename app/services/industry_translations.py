"""
產業(industry)/類別(category) 中文翻譯快取——app/models/screener.py::
enrich_candidates() 查 reqContractDetailsAsync() 拿到的這兩個欄位是 IB
內部固定的英文分類字串(不是自由文字，同一個產業/類別永遠對應到同一個
英文字串，例如所有生技股都是 "Biotechnology")，翻譯一次就能重複用在
所有候選清單上，不用每次顯示候選清單都重新呼叫 AI。

跟 scan_history_store.py 同一套慣例：本機小 json 檔，放在 pref/(不是
app/resources/，這不是隨 app 一起發布的靜態資料，是執行期間累積出來的
快取，見 app/paths.py 的說明)，讀寫失敗靜默吞掉——翻譯快取壞掉不該讓候
選清單整個顯示不出來，退回顯示英文原文就好。
"""
import json

from app.paths import PREF_DIR

_FILE = PREF_DIR / "industry_translations.json"


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def get_cached(terms: set[str]) -> dict[str, str]:
    """回傳 terms 裡已經翻譯過的部分，查不到的字串不會出現在回傳的
    dict 裡——要不要為了這些缺漏的字串去問 AI，是呼叫端的決定，這裡只
    負責讀快取。"""
    data = _load()
    return {t: data[t] for t in terms if t in data}


def save_translations(mapping: dict[str, str]) -> None:
    if not mapping:
        return
    data = _load()
    data.update(mapping)
    _save(data)
