"""
自選清單「查看詳細」查到的基本資料/財報本機快取——用代碼當 key，一天只
打一次 yfinance/Yahoo Finance，同一天內查詢同一個代碼直接讀本機，不重
打。跟 `historical_bars_store.py`/`chart_drawing_store.py` 同一套慣例：
本機小 json 檔案放 `PREF_DIR`，讀寫失敗一律靜默吞掉，退回真的去打 API
就好。

存的是 `dataclasses.asdict()` 攤平後的原始 dict + 查詢日期(本機時區的
"YYYY-MM-DD")，不是 `FundamentalsSnapshot` 物件本身——json 沒辦法直接存
dataclass，讀回來要靠 `app/models/fundamentals_client.py` 自己重建成
dataclass。
"""
import datetime
import json
from typing import Optional

from app.paths import PREF_DIR

_DIR = PREF_DIR / "fundamentals"
_DIR.mkdir(exist_ok=True)


def _file(symbol: str):
    return _DIR / f"{symbol.upper()}.json"


def _today() -> str:
    return datetime.date.today().isoformat()


def load(symbol: str) -> Optional[dict]:
    """查詢日期不是今天、檔案不存在、或解析失敗都回傳 `None`，呼叫端一
    律當「沒有可用的本機快取」處理，退回打 API——回傳的是存檔時的原始
    dict(`data` 欄位)，不含 `date` 這個時間戳記欄位。"""
    try:
        record = json.loads(_file(symbol).read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.get("date") != _today():
        return None
    return record.get("data")


def save(symbol: str, data: dict) -> None:
    try:
        _file(symbol).write_text(
            json.dumps({"date": _today(), "data": data}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass
