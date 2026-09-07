"""
台指選擇權波動率指數 (期交所自己版本的 VIX)。FinMind 的對應 dataset
(TaiwanOptionVix) 實測是付費限定，這裡改直接接期交所自己的內部 AJAX
endpoint (使用者用瀏覽器開發者工具「複製為 cURL」抓到的，存在
data/curl 裡當參考)。實測過不需要瀏覽器的 cookie/referer，純 POST JSON
就會回資料；但 endDate 不能等於或晚於今天，只能查到「昨天」為止。

本地用 CSV 做增量快取 (data/taiwan_option_vix.csv)：每次呼叫只補抓
本地缺的那段區間（新的一段接在後面、或需要的區間比本地最舊的資料還早
就往前補），不會每次都整段重抓期交所，抓到的新資料直接 append 進 CSV
長期累積。也可以用 import_historical_json() 把手動下載的歷史資料檔
(例如過去三年) 一次匯入 CSV，當作起始資料。
"""
import csv
import datetime
import json
from pathlib import Path

import requests

from app.paths import PROJECT_ROOT

URL = "https://www.taifex.com.tw/indes/index.aspx/GetStockDayPrices"
CSV_FILE = PROJECT_ROOT / "data" / "taiwan_option_vix.csv"


def _fetch_range(start: datetime.date, end: datetime.date) -> list[dict]:
    """抓 [start, end] 區間 (含頭尾)，end 不能是今天或之後。"""
    if start > end:
        return []
    resp = requests.post(
        URL,
        json={
            "syid": "TAIWANVIX",
            "flag": "MS",
            "startDate": start.strftime("%Y/%m/%d"),
            "endDate": end.strftime("%Y/%m/%d"),
        },
        headers={
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/json; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        },
        timeout=15,
    )
    resp.raise_for_status()
    inner = json.loads(resp.json()["d"])
    if "TrendData" not in inner:
        raise RuntimeError(f"期交所波動率指數回應異常：{inner}")

    return [
        {
            "date": f"{item['Time'][:4]}-{item['Time'][4:6]}-{item['Time'][6:]}",
            "vix": item["Price"],
        }
        for item in inner["TrendData"]
    ]


def _load_csv() -> dict[str, dict]:
    if not CSV_FILE.exists():
        return {}
    with CSV_FILE.open("r", encoding="utf-8", newline="") as f:
        return {row["date"]: {"date": row["date"], "vix": float(row["vix"])} for row in csv.DictReader(f)}


def _save_csv(rows_by_date: dict[str, dict]) -> None:
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "vix"])
        writer.writeheader()
        for date in sorted(rows_by_date):
            writer.writerow(rows_by_date[date])


def import_historical_json(json_path) -> int:
    """把手動從期交所網站下載的完整歷史資料 (跟這支程式打 API 拿到的格式
    一樣，是 {"d": "...內層是字串化的 JSON..."} 包一層) 匯入本地 CSV
    快取，用來一次補齊比單次 API 查詢範圍還久的歷史 (例如過去三年)。
    同一天重複匯入會直接覆蓋成最新匯入的值，不會重複。回傳匯入後 CSV
    的總筆數。"""
    outer = json.loads(Path(json_path).read_text(encoding="utf-8"))
    inner = json.loads(outer["d"])
    if "TrendData" not in inner:
        raise RuntimeError(f"歷史資料檔格式異常：{inner}")

    rows_by_date = _load_csv()
    for item in inner["TrendData"]:
        date = f"{item['Time'][:4]}-{item['Time'][4:6]}-{item['Time'][6:]}"
        rows_by_date[date] = {"date": date, "vix": item["Price"]}

    _save_csv(rows_by_date)
    return len(rows_by_date)


def get_option_vix_history(days: int = 30) -> list[dict]:
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    wanted_start = today - datetime.timedelta(days=days)

    rows_by_date = _load_csv()
    dirty = False

    if rows_by_date:
        earliest = datetime.date.fromisoformat(min(rows_by_date))
        latest = datetime.date.fromisoformat(max(rows_by_date))

        for row in _fetch_range(latest + datetime.timedelta(days=1), yesterday):
            rows_by_date[row["date"]] = row
            dirty = True

        if wanted_start < earliest:
            for row in _fetch_range(wanted_start, earliest - datetime.timedelta(days=1)):
                rows_by_date[row["date"]] = row
                dirty = True
    else:
        for row in _fetch_range(wanted_start, yesterday):
            rows_by_date[row["date"]] = row
            dirty = True

    if dirty:
        _save_csv(rows_by_date)

    return [rows_by_date[d] for d in sorted(rows_by_date) if d >= wanted_start.isoformat()]
