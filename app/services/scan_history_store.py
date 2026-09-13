"""
市場掃描(初篩)紀錄永久保存——跟 layout_store.py/query_pref.py 同一套作
法：本機小 json 檔，讀寫失敗靜默吞掉(記不住歷史紀錄不該讓篩選器整個壞
掉)。放在共用的 pref/ 根目錄，不是 trading_pref_dir()——這是查詢/UI 歷
史，不是代表真實交易狀態的資料，跟 layout_pref.json 是同一類，見
app/paths.py 的說明。

只記「市場掃描(初篩)」這一步驟：掃描代碼＋篩選條件＋候選清單，不含後
續「選擇權復篩」的結果(這次沒有要做)。使用者可以事後改候選清單內容
(update_run_candidates)，這裡只負責存/取一份 dict，資料模型
(ScanFilterValue/CandidateStock <-> dict)轉換是呼叫端
(app/views/screener_widget.py)的責任，這支檔案不 import 那兩個
dataclass，跟其他 store 模組「純 IO，不懂業務邏輯」的分工一致。
"""
import json
import uuid
from datetime import datetime
from typing import Optional

from app.paths import PREF_DIR

_FILE = PREF_DIR / "scan_history.json"
_DEFAULT = {"runs": []}


def _load_all() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        data.setdefault("runs", list(_DEFAULT["runs"]))
        return data
    except Exception:
        return {"runs": list(_DEFAULT["runs"])}


def _save_all(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_all() -> list[dict]:
    """回傳所有紀錄，新到舊沒有特別排序(呼叫端要自己依 created_at 排)。"""
    return _load_all()["runs"]


def save_run(name: str, scan_code: str, filters: list, candidates: list, instrument: str = "STK") -> str:
    """filters: list[ScanFilterValue]，candidates: list[CandidateStock]——
    只用到 .code/.value 跟 .symbol/.source/.rank 這幾個屬性，不 import
    真正的 dataclass 型別，避免 store 模組反過來依賴業務邏輯模組。
    instrument：這次掃描用的商品類型("STK"/"ETF.EQ.US")，「檢視」把紀
    錄帶回①初篩選股時要用來還原商品類型選擇器，不然掃描代碼可能對不上
    (掃描代碼挑選器會依商品類型篩選候選清單，見
    app/views/web_screener_widget.py::_current_scan_type_catalog())。舊
    紀錄(加 ETF 支援之前存的)沒有這個欄位，讀取端用預設值 "STK" 補上，
    見 scan_history.json 讀取處的說明。回傳新記錄的 id，給呼叫端之後要
    更新候選清單時用。"""
    data = _load_all()
    run_id = uuid.uuid4().hex
    data["runs"].append({
        "id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": name,
        "scan_code": scan_code,
        "instrument": instrument,
        "filters": [{"code": f.code, "value": f.value} for f in filters],
        "candidates": [{"symbol": c.symbol, "source": c.source, "rank": c.rank} for c in candidates],
    })
    _save_all(data)
    return run_id


def rename_run(run_id: str, new_name: str) -> None:
    data = _load_all()
    for run in data["runs"]:
        if run["id"] == run_id:
            run["name"] = new_name
            break
    _save_all(data)


def update_run_candidates(run_id: str, candidates: list[dict]) -> None:
    """candidates 直接存扁平 dict(跟 save_run 存的格式一樣)，呼叫端(歷史
    紀錄檢視對話框)負責組好格式，這裡不做轉換。"""
    data = _load_all()
    for run in data["runs"]:
        if run["id"] == run_id:
            run["candidates"] = candidates
            break
    _save_all(data)


def get_run(run_id: str) -> Optional[dict]:
    for run in load_all():
        if run["id"] == run_id:
            return run
    return None


def delete_run(run_id: str) -> None:
    data = _load_all()
    data["runs"] = [r for r in data["runs"] if r["id"] != run_id]
    _save_all(data)
