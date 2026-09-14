"""
使用者自訂的篩選條件範本——把股票篩選器①初篩頁籤目前的「商品類型＋掃描
代碼＋篩選欄位範圍」存成一份範本，之後可以直接套用，不用每次重新勾選/
輸入一次。跟 `watchlist_store.py` 是同一套慣例：本機小 json 檔，放
`PREF_DIR` 根目錄(這是查詢/UI 狀態，不是代表真實交易狀態的資料，見
`app/paths.py` 的說明)，讀寫失敗一律靜默吞掉——範本存取失敗不該讓篩選器
其他功能一起壞掉。

一份範本的 `filters` 欄位是 `[{"filter_id", "above", "below"}, ...]`——
`above`/`below` 直接存 `web_screener_widget.py::_add_filter_row()` 讀到
的原始數字(0 代表「這一側不設限」，對照該檔案
`_filter_row_values()`的說明，兩邊要用同一套慣例，不能各自解讀)，
`filter_id` 對照 `app/models/scanner_catalog.py::FilterDef.id`，套用範本
時如果這個 id 在目前的篩選欄位目錄裡已經找不到(欄位目錄改版)，呼叫端會
自己跳過，不是這支模組的責任。
"""
import json
import uuid
from datetime import datetime
from typing import Optional

from app.paths import PREF_DIR

_FILE = PREF_DIR / "filter_presets.json"
_DEFAULT = {"presets": []}


def _load_all() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        data.setdefault("presets", list(_DEFAULT["presets"]))
        return data
    except Exception:
        return {"presets": list(_DEFAULT["presets"])}


def _save_all(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def list_all() -> list[dict]:
    """回傳所有範本，沒有特別排序(呼叫端要自己依 created_at 排)。"""
    return _load_all()["presets"]


def get(preset_id: str) -> Optional[dict]:
    return next((p for p in list_all() if p["id"] == preset_id), None)


def create(name: str, instrument: str, scan_code: Optional[str], filters: list[dict]) -> str:
    """建立一個新的篩選範本，回傳新範本的 id。"""
    data = _load_all()
    preset_id = uuid.uuid4().hex
    data["presets"].append({
        "id": preset_id,
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "instrument": instrument,
        "scan_code": scan_code,
        "filters": filters,
    })
    _save_all(data)
    return preset_id


def update(preset_id: str, instrument: str, scan_code: Optional[str], filters: list[dict]) -> None:
    """用目前編輯器的設定覆蓋既有範本(「更新為目前設定」)，名稱/建立時間不變。"""
    data = _load_all()
    for p in data["presets"]:
        if p["id"] == preset_id:
            p["instrument"] = instrument
            p["scan_code"] = scan_code
            p["filters"] = filters
            break
    _save_all(data)


def rename(preset_id: str, new_name: str) -> None:
    data = _load_all()
    for p in data["presets"]:
        if p["id"] == preset_id:
            p["name"] = new_name
            break
    _save_all(data)


def delete(preset_id: str) -> None:
    data = _load_all()
    data["presets"] = [p for p in data["presets"] if p["id"] != preset_id]
    _save_all(data)
