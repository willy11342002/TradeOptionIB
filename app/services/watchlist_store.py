"""
使用者自訂的股票/ETF自選清單——取代原本的「掃描紀錄」(scan_history_store.py，
已移除)。舊設計是每次市場掃描完都強迫使用者跳出命名對話框存檔，一次掃描
綁一份紀錄；新設計改成使用者自己決定要不要把候選標的加進自選清單，清單
由使用者自己命名、自己決定要不要繼續增減成分股，不綁定任何一次特定的
掃描條件——一個清單可以來自好幾次不同的掃描、也可以完全手動輸入代碼湊
出來。

跟 scan_history_store.py 是同一套慣例：本機小 json 檔，放 pref/ 根目錄
(這是查詢/UI 狀態，不是代表真實交易狀態的資料，見 app/paths.py 的說
明)，讀寫失敗一律靜默吞掉——自選清單存取失敗不該讓篩選器其他功能一起壞
掉。
"""
import json
import uuid
from datetime import datetime
from typing import Optional

from app.paths import PREF_DIR

_FILE = PREF_DIR / "watchlists.json"
_DEFAULT = {"watchlists": []}


def _load_all() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        data.setdefault("watchlists", list(_DEFAULT["watchlists"]))
        return data
    except Exception:
        return {"watchlists": list(_DEFAULT["watchlists"])}


def _save_all(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def list_all() -> list[dict]:
    """回傳所有自選清單，順序就是使用者看到、可以用 `move()` 調整的顯示
    順序(串列本身的順序即顯示順序，不用另外存一個 order 欄位)——呼叫端
    不用再自己依 created_at 排。"""
    return _load_all()["watchlists"]


def get(watchlist_id: str) -> Optional[dict]:
    return next((w for w in list_all() if w["id"] == watchlist_id), None)


def create(name: str, symbols: Optional[list[str]] = None) -> str:
    """建立一個新的自選清單，回傳新清單的 id。symbols 可以在建立當下就
    帶入(例如「加入自選」選了『新增清單』的情境)，也可以留空之後再慢慢
    加。*** 插到最前面，不是 append 到最後 ***：`list_all()` 的順序就是
    顯示順序(見該函式說明)，插最前面才能維持原本「新清單顯示在最上
    面」的習慣，之後使用者可以再用 `move()` 調整。"""
    data = _load_all()
    watchlist_id = uuid.uuid4().hex
    data["watchlists"].insert(0, {
        "id": watchlist_id,
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": list(dict.fromkeys(symbols or [])),  # 去重，保留原本順序
    })
    _save_all(data)
    return watchlist_id


def move(watchlist_id: str, direction: str) -> None:
    """把一個自選清單在顯示順序裡往上("up")或往下("down")移一格(跟相
    鄰那個交換位置)——已經在最上/最下面就什麼都不做，呼叫端(UI)自己
    負責在那種情況下把按鈕停用，這裡只是防呆。"""
    data = _load_all()
    watchlists = data["watchlists"]
    idx = next((i for i, w in enumerate(watchlists) if w["id"] == watchlist_id), None)
    if idx is None:
        return
    target = idx - 1 if direction == "up" else idx + 1
    if target < 0 or target >= len(watchlists):
        return
    watchlists[idx], watchlists[target] = watchlists[target], watchlists[idx]
    _save_all(data)


def rename(watchlist_id: str, new_name: str) -> None:
    data = _load_all()
    for w in data["watchlists"]:
        if w["id"] == watchlist_id:
            w["name"] = new_name
            break
    _save_all(data)


def add_symbols(watchlist_id: str, symbols: list[str]) -> None:
    """加入的代碼已經存在清單裡就跳過，不重複——跟舊版候選清單
    _add_candidates() 的去重邏輯一致。"""
    if not symbols:
        return
    data = _load_all()
    for w in data["watchlists"]:
        if w["id"] == watchlist_id:
            existing = list(w["symbols"])
            for s in symbols:
                if s not in existing:
                    existing.append(s)
            w["symbols"] = existing
            break
    _save_all(data)


def remove_symbol(watchlist_id: str, symbol: str) -> None:
    data = _load_all()
    for w in data["watchlists"]:
        if w["id"] == watchlist_id:
            w["symbols"] = [s for s in w["symbols"] if s != symbol]
            break
    _save_all(data)


def delete(watchlist_id: str) -> None:
    data = _load_all()
    data["watchlists"] = [w for w in data["watchlists"] if w["id"] != watchlist_id]
    _save_all(data)
