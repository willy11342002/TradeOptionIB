"""
`app/models/screener.py::enrich_candidates()` 查到的公司/ETF 名稱、產
業、類別(`reqContractDetailsAsync()`)+ 支援的衍生商品(期貨/月選/週
選，`reqSecDefOptParamsAsync()`+`reqContractDetailsAsync(Future(...))`)
——本機快取，用 conId 當 key(conId 才是合約穩定、唯一的識別碼，同一個
理由見 CLAUDE.md)，同一天內查過的標的直接讀本機，不用再打一次 IB。自
選清單展開(`app/views/web_screener_widget.py::_toggle_watchlist_
expand()`)20 檔平行查詢時尤其有感——這幾項幾乎都是靜態合約中繼資料，
幾乎不會變，「一天」只是方便理解的統一邏輯，不是因為這批資料真的需要
每天重查。

跟 `chart_drawing_store.py`/`industry_translations.py` 同一套慣例：本機
小 json 檔放 `PREF_DIR` 根目錄，一個檔案存全部 conId(每筆資料量很小，
不像 `historical_bars_store.py` 那樣拆成一個 conId 一個檔案)，讀寫失敗
一律靜默吞掉——退回真的去打 IB 就好。
"""
import datetime
import json
from typing import Optional

from app.paths import PREF_DIR

_FILE = PREF_DIR / "contract_meta.json"


def _today() -> str:
    return datetime.date.today().isoformat()


def _load_all() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load(con_id: int) -> Optional[dict]:
    """查詢日期不是今天、沒有這個 conId 的紀錄、或解析失敗都回傳
    `None`，呼叫端一律當「沒有可用的本機快取」處理，退回打 IB。"""
    record = _load_all().get(str(con_id))
    if record is None or record.get("date") != _today():
        return None
    return record


def save_many(entries: dict[int, dict]) -> None:
    """`entries`：`{conId: {"long_name":..., "industry":..., "category":...,
    "products": [...]}}`——一次查詢(`enrich_candidates()` 一批候選)結束後
    統一存一次，不要每個候選各自呼叫，一批幾十檔就是幾十次「讀整個檔
    案、改一筆、寫整個檔案」，沒必要。"""
    if not entries:
        return
    data = _load_all()
    today = _today()
    for con_id, fields in entries.items():
        data[str(con_id)] = {"date": today, **fields}
    _save_all(data)
