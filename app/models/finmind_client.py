"""
FinMind 開放資料 API，抓台股加權指數 (TAIEX)、台指期每日價格，給開倉分析
拿來判斷振幅／波動率高低、給圖表畫K線用。用 os.environ["FINMIND_API_TOKEN"]
帶 token 換取較高的請求額度 (未註冊 600/hr、註冊後 1500/hr)。

已用免費帳號實測過：TaiwanOptionVix (選擇權波動率指數) 跟 TaiwanStockNews
(相關新聞) 這兩個 dataset 都是付費限定 (FinMind 回應
"Your level is free. Please update your user level.")，免費方案打不到。
但 TaiwanStockPrice(TAIEX) 跟 TaiwanFuturesDaily(TX) 完全沒有這個限制——
實測過免費方案可以一路回溯到 TAIEX 2000 年初、TX 期貨 1998 年剛開始
交易的資料，所以這兩個改成本地 CSV 增量快取：第一次用的時候把免費方案
能拿到的完整歷史一次抓回來存進 data/ 底下，之後每次呼叫只需要跟 FinMind
要「快取裡缺的那一小段」（通常就是最新一天），不用整段重抓，圖表往回拉
看更早的資料時也能直接從本機檔案讀，不會因為等網路才顯示空白。

台股指數現在點位在 4 萬多，同樣的漲跌 % 換算成點數會比指數基期低的年代
大很多，只給絕對點數會讓 LLM 誤以為「動輒幾百點」=「振幅一定高」。所以
這裡額外算好 change_pct (較前一日收盤漲跌幅%) 跟 range_pct (當日高低價
差相對前一日收盤的%)，讓 LLM 直接用百分比判斷，不用自己再換算。
"""
import csv
import datetime
import os
import threading

import requests

from app.paths import PROJECT_ROOT

BASE_URL = "https://api.finmindtrade.com/api/v4/data"

TAIEX_CSV_FILE = PROJECT_ROOT / "data" / "taiex_price_history.csv"
FUTURES_CSV_FILE = PROJECT_ROOT / "data" / "tx_futures_price_history.csv"
TAIEX_FIELDS = ["date", "open", "high", "low", "close", "volume"]
FUTURES_FIELDS = ["date", "close"]

# 圖表(ChartDataService)跟開倉分析(OpeningAnalysisService)是兩條各自獨立
# 的背景執行緒，都可能同時呼叫到這裡讀寫同一份 CSV 快取檔——沒鎖的話兩條
# 執行緒交錯讀寫同一個檔案會讀到寫一半的內容，解析時噴例外。用一個鎖把
# 「讀快取→補抓缺的區間→寫回快取」這整個過程序列化，避免互相干擾。
_cache_lock = threading.Lock()

# FinMind 免費方案實測回溯得到的最早資料日期，第一次用的時候從這裡開始
# 一次抓完整歷史，之後就只需要每天增量補最新一天。
TAIEX_EARLIEST_DATE = datetime.date(2000, 1, 1)
FUTURES_EARLIEST_DATE = datetime.date(1998, 7, 21)


def _require_token() -> str:
    token = os.environ.get("FINMIND_API_TOKEN")
    if not token:
        raise RuntimeError("FINMIND_API_TOKEN 未設定，請確認 .env 檔")
    return token


def _load_price_csv(path, fields: list[str]) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = {}
        for raw in csv.DictReader(f):
            row = {"date": raw["date"]}
            for field in fields[1:]:
                value = raw.get(field)
                row[field] = float(value) if value not in (None, "") else None
            rows[raw["date"]] = row
        return rows


def _save_price_csv(path, rows_by_date: dict[str, dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for date in sorted(rows_by_date):
            writer.writerow(rows_by_date[date])


def _sync_price_cache(path, fields: list[str], earliest_date: datetime.date, fetch_range) -> dict[str, dict]:
    """通用的「本地 CSV 增量快取」邏輯：第一次用就從 earliest_date 抓到
    昨天，之後只補快取缺的那一段 (通常是最新一天；如果需要的區間比快取
    裡最舊的資料還早，也會往前補)。fetch_range(start, end) 是實際打
    FinMind API 的函式，回傳 [{"date":..., 其他欄位...}, ...]。"""
    with _cache_lock:
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)

        rows_by_date = _load_price_csv(path, fields)
        dirty = False

        if rows_by_date:
            latest = datetime.date.fromisoformat(max(rows_by_date))
            for row in fetch_range(latest + datetime.timedelta(days=1), yesterday):
                rows_by_date[row["date"]] = row
                dirty = True
        else:
            for row in fetch_range(earliest_date, yesterday):
                rows_by_date[row["date"]] = row
                dirty = True

        if dirty:
            _save_price_csv(path, rows_by_date, fields)
        return rows_by_date


def _fetch_taiex_range(start: datetime.date, end: datetime.date) -> list[dict]:
    if start > end:
        return []
    resp = requests.get(BASE_URL, params={
        "dataset": "TaiwanStockPrice",
        "data_id": "TAIEX",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "token": _require_token(),
    }, timeout=30)
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind TaiwanStockPrice(TAIEX) 回應異常：{payload.get('msg')}")

    return [
        {
            "date": item.get("date"),
            "open": item.get("open"),
            "high": item.get("max"),
            "low": item.get("min"),
            "close": item.get("close"),
            "volume": item.get("Trading_Volume"),
        }
        for item in payload.get("data") or []
    ]


def get_taiex_price_history(days: int = 30) -> list[dict]:
    rows_by_date = _sync_price_cache(TAIEX_CSV_FILE, TAIEX_FIELDS, TAIEX_EARLIEST_DATE, _fetch_taiex_range)

    wanted_start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    history = []
    prev_close = None
    for date in sorted(rows_by_date):
        row = rows_by_date[date]
        close = row["close"]
        if date < wanted_start:
            prev_close = close
            continue

        high, low = row["high"], row["low"]
        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else None
        range_pct = round((high - low) / prev_close * 100, 2) if prev_close else None
        history.append({**row, "change_pct": change_pct, "range_pct": range_pct})
        prev_close = close
    return history


def _fetch_futures_range(data_id: str, start: datetime.date, end: datetime.date) -> list[dict]:
    if start > end:
        return []
    resp = requests.get(BASE_URL, params={
        "dataset": "TaiwanFuturesDaily",
        "data_id": data_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "token": _require_token(),
    }, timeout=30)
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind TaiwanFuturesDaily({data_id}) 回應異常：{payload.get('msg')}")

    # 同一天通常有近月/次近月/季月等多個合約同時掛牌，這裡每天挑成交量
    # 最大的合約當「近月」，形成一條連續的收盤價序列 (換月時價格會有
    # 正常的跳動，這是真實市場行為，不是資料錯誤)。
    front_by_date: dict[str, dict] = {}
    for row in payload.get("data") or []:
        date = row.get("date")
        volume = row.get("volume") or 0
        if date not in front_by_date or volume > (front_by_date[date].get("volume") or 0):
            front_by_date[date] = row
    return [{"date": date, "close": front_by_date[date].get("close")} for date in sorted(front_by_date)]


def get_futures_price_history(data_id: str = "TX", days: int = 60) -> list[dict]:
    def fetch_range(start, end):
        return _fetch_futures_range(data_id, start, end)

    rows_by_date = _sync_price_cache(FUTURES_CSV_FILE, FUTURES_FIELDS, FUTURES_EARLIEST_DATE, fetch_range)

    wanted_start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [rows_by_date[date] for date in sorted(rows_by_date) if date >= wanted_start]


def get_option_chain(data_id: str = "TXO", lookback_days: int = 5) -> list[dict]:
    """抓最近一個有資料的交易日的完整選擇權成交/未平倉狀況，給開倉分析看
    OI (未平倉量)/Put-Call 籌碼分布，藉此判斷市場認為的壓力/支撐履約價。
    lookback_days 只是為了跳過假日往前找「最近一個交易日」的容錯範圍，
    不是要抓好幾天的歷史（一天的完整選擇權鏈本身資料量就不小了）。"""
    today = datetime.date.today()
    params = {
        "dataset": "TaiwanOptionDaily",
        "data_id": data_id,
        "start_date": (today - datetime.timedelta(days=lookback_days)).isoformat(),
        "end_date": today.isoformat(),
        "token": _require_token(),
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    payload = resp.json()

    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind TaiwanOptionDaily({data_id}) 回應異常：{payload.get('msg')}")

    data = payload.get("data") or []
    if not data:
        return []

    latest_date = max(item.get("date") for item in data)
    return [item for item in data if item.get("date") == latest_date]


def get_option_chain_summary(data_id: str = "TXO", lookback_days: int = 5, top_n: int = 8) -> dict:
    """最近一個交易日的完整選擇權鏈單日就有好幾千筆逐檔資料，量太大不適合
    整包丟給 LLM 或存進歷史檔，這裡先在本地彙總：挑出成交量最大的合約
    月份 (通常就是市場最關注的近週/近月合約)，算出該合約每個履約價的
    open_interest 總和，回傳 call/put 未平倉量最大的前 top_n 個履約價
    (常被市場視為潛在壓力/支撐)，以及整體 Put/Call 未平倉量比率。"""
    rows = get_option_chain(data_id=data_id, lookback_days=lookback_days)
    if not rows:
        return {}

    volume_by_contract: dict[str, int] = {}
    for row in rows:
        contract = row.get("contract_date")
        volume_by_contract[contract] = volume_by_contract.get(contract, 0) + (row.get("volume") or 0)
    front_contract = max(volume_by_contract, key=volume_by_contract.get)

    front_rows = [row for row in rows if row.get("contract_date") == front_contract]

    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}
    for row in front_rows:
        strike = row.get("strike_price")
        oi = row.get("open_interest") or 0
        bucket = call_oi if row.get("call_put") == "call" else put_oi
        bucket[strike] = bucket.get(strike, 0) + oi

    def _top(bucket: dict[float, int]) -> list[dict]:
        ranked = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        return [{"strike": strike, "open_interest": oi} for strike, oi in ranked]

    total_call_oi = sum(call_oi.values())
    total_put_oi = sum(put_oi.values())

    return {
        "date": front_rows[0].get("date"),
        "contract_date": front_contract,
        "top_call_open_interest_strikes": _top(call_oi),
        "top_put_open_interest_strikes": _top(put_oi),
        "total_call_open_interest": total_call_oi,
        "total_put_open_interest": total_put_oi,
        "put_call_oi_ratio": round(total_put_oi / total_call_oi, 3) if total_call_oi else None,
    }
