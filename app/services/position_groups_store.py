"""
未平倉部位手動分組/標籤存檔。跟 layout_store.py/theme.py 同一套作法：本機
小 json 檔，讀寫失敗靜默吞掉(存不了分組不該讓部位視窗整個壞掉，頂多下次
開回未分組狀態)。

用 symbol(群益商品代碼字串) 當 key，不是部位物件本身——這是唯一在「broker
查回來的部位」跟「本地委託紀錄」兩邊都存在、穩定不變的識別碼，才能讓手動
分組在重新查詢未平倉之後還留得住(見 app/models/positions.py 的 reconcile
邏輯)。
"""
import json
import uuid

from app.paths import PROJECT_ROOT

POSITION_GROUPS_FILE = PROJECT_ROOT / ".position_groups_pref.json"


def _load_all() -> dict:
    try:
        data = json.loads(POSITION_GROUPS_FILE.read_text(encoding="utf-8"))
        data.setdefault("groups", {})
        data.setdefault("manual_overrides", {})
        return data
    except Exception:
        return {"groups": {}, "manual_overrides": {}}


def _save_all(data: dict) -> None:
    try:
        POSITION_GROUPS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 存分組設定失敗不影響部位查詢本身，純粹記不住這次的手動分組


def list_groups() -> dict:
    """group_id -> {"name":, "color":}"""
    return _load_all()["groups"]


def create_group(name: str, color: str) -> str:
    data = _load_all()
    group_id = uuid.uuid4().hex
    data["groups"][group_id] = {"name": name, "color": color}
    _save_all(data)
    return group_id


def rename_group(group_id: str, name: str) -> None:
    data = _load_all()
    if group_id in data["groups"]:
        data["groups"][group_id]["name"] = name
        _save_all(data)


def set_group_color(group_id: str, color: str) -> None:
    data = _load_all()
    if group_id in data["groups"]:
        data["groups"][group_id]["color"] = color
        _save_all(data)


def delete_group(group_id: str) -> None:
    """刪除群組本身，並把所有指到這個群組的手動覆蓋清空(改回自動分組/未
    分組)，不留孤兒覆蓋。"""
    data = _load_all()
    data["groups"].pop(group_id, None)
    for symbol, gid in list(data["manual_overrides"].items()):
        if gid == group_id:
            data["manual_overrides"].pop(symbol, None)
    _save_all(data)


def get_manual_overrides() -> dict:
    """symbol -> group_id (or None 代表「使用者手動取消分組」，這個狀態
    要跟「symbol 完全沒被設定過」區分開來，所以歸零/取消分組時仍然要寫入
    一筆 None，不是直接刪掉這個 key)。"""
    return _load_all()["manual_overrides"]


def set_manual_override(symbol: str, group_id) -> None:
    data = _load_all()
    data["manual_overrides"][symbol] = group_id
    _save_all(data)


def clear_manual_override(symbol: str) -> None:
    """部位已經歸零、symbol 不再持有，清掉孤兒覆蓋(不是使用者主動取消分
    組，所以直接刪 key，不留 None)。"""
    data = _load_all()
    if symbol in data["manual_overrides"]:
        del data["manual_overrides"][symbol]
        _save_all(data)
