"""
回測結果的本機保存。跟其他 store 一樣是純 IO(不懂回測邏輯)，但有一個刻意的差異：**寫入失敗不
靜默吞掉，直接丟例外**。其他 store 存的是可以重來的 UI 偏好，這裡存的是使用者跑了一段時間才產
生的回測結果，存不進去卻假裝成功會讓人以為資料還在，UI 要接住例外並明確告知。

*** 存在 pref/backtest/runs/，不是 pref/paper|live/ ***：回測是純模擬，跟 IB 帳戶環境無關，不需
要像委託簿那樣分 paper/live(見 app/paths.py::trading_pref_dir 的說明)。

每一次回測(run)兩個檔案，列清單時只讀小的 meta，不用把每次幾百筆的逐筆交易都讀進來：
    <id>.json         meta：id、名稱、策略參數、回測設定、摘要、建立/更新時間
    <id>.trades.json  {"trades": [...逐筆交易...], "benchmark": [[日期, 收盤價], ...],
                       "equity": [[日期, 每股毛損益], ...]}
                      benchmark 是標的每日收盤價，報表畫 buy-and-hold 對照線用；equity 是逐日「已平倉 +
                      未平倉浮動損益」(每股、不含手續費，見 engine.run_backtest_with_equity)，報表畫含
                      浮動損益的權益曲線用。舊存檔沒有 equity 欄位，讀出來是空清單(重跑一次才會有)。
寫入一律先寫暫存檔再 os.replace，避免寫到一半當機留下壞檔。
"""
import json
import os
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from app.paths import PREF_DIR

RUNS_DIR = PREF_DIR / "backtest" / "runs"


def _meta_path(run_id: str):
    return RUNS_DIR / f"{run_id}.json"


def _trades_path(run_id: str):
    return RUNS_DIR / f"{run_id}.trades.json"


def _write_json_atomic(path, data) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def save_run(run_id: str, name: str, strategy: dict, config: dict, summary: dict,
             trades: List[dict], benchmark: List[list], equity: List[list]) -> dict:
    """新增或覆蓋(重跑)一次回測，回傳存下去的 meta。覆蓋時保留原本的建立時間。"""
    existing = get_meta(run_id)
    meta = {
        "id": run_id,
        "name": name,
        "strategy": strategy,
        "config": config,
        "summary": summary,
        "created_at": existing["created_at"] if existing else _now(),
        "updated_at": _now(),
    }
    # 先寫逐筆再寫 meta：中途失敗的話清單裡不會出現一筆點開沒資料的回測。
    _write_json_atomic(_trades_path(run_id), {"trades": trades, "benchmark": benchmark, "equity": equity})
    _write_json_atomic(_meta_path(run_id), meta)
    return meta


def get_meta(run_id: str) -> Optional[dict]:
    try:
        return json.loads(_meta_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_runs() -> List[dict]:
    """所有回測的 meta，最近更新的排前面。壞掉的單一檔案跳過，不影響其他。"""
    if not RUNS_DIR.exists():
        return []
    metas = []
    for path in RUNS_DIR.glob("*.json"):
        if path.name.endswith(".trades.json"):
            continue
        try:
            metas.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    metas.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return metas


def load_trades(run_id: str) -> Tuple[List[dict], List[list], List[list]]:
    """回傳 (逐筆交易, 標的每日收盤價, 逐日權益)。檔案不存在/壞掉會丟例外，由呼叫端決定怎麼提示。"""
    data = json.loads(_trades_path(run_id).read_text(encoding="utf-8"))
    return data["trades"], data.get("benchmark", []), data.get("equity", [])


def rename_run(run_id: str, name: str) -> None:
    meta = get_meta(run_id)
    if meta is None:
        raise FileNotFoundError(f"找不到回測 {run_id}")
    meta["name"] = name
    meta["updated_at"] = _now()
    _write_json_atomic(_meta_path(run_id), meta)


def delete_run(run_id: str) -> None:
    for path in (_meta_path(run_id), _trades_path(run_id)):
        path.unlink(missing_ok=True)
