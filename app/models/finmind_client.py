"""
FinMind 開放資料 API，抓台股加權指數 (TAIEX) 近期每日價格，給開倉分析拿來
判斷振幅／波動率高低。用 os.environ["FINMIND_API_TOKEN"] 帶 token 換取
較高的請求額度 (未註冊 600/hr、註冊後 1500/hr)。

已用免費帳號實測過：TaiwanOptionVix (選擇權波動率指數) 跟 TaiwanStockNews
(相關新聞) 這兩個 dataset 都是付費限定 (FinMind 回應
"Your level is free. Please update your user level.")，免費方案打不到，
所以這裡改用免費可用的 TaiwanStockPrice(data_id=TAIEX) 抓大盤每日
開高低收，把原始價格數列丟給 LLM 自己質化判斷振幅/波動率，不用付費的
現成波動率指數。市場消息/利多利空改由 openrouter_client 開 web search
讓 LLM 自己查，不再依賴 FinMind 的付費新聞 dataset。

台股指數現在點位在 4 萬多，同樣的漲跌 % 換算成點數會比指數基期低的年代
大很多，只給絕對點數會讓 LLM 誤以為「動輒幾百點」=「振幅一定高」。所以
這裡額外算好 change_pct (較前一日收盤漲跌幅%) 跟 range_pct (當日高低價
差相對前一日收盤的%)，讓 LLM 直接用百分比判斷，不用自己再換算。
"""
import datetime
import os

import requests

BASE_URL = "https://api.finmindtrade.com/api/v4/data"


def get_taiex_price_history(days: int = 30) -> list[dict]:
    token = os.environ.get("FINMIND_API_TOKEN")
    if not token:
        raise RuntimeError("FINMIND_API_TOKEN 未設定，請確認 .env 檔")

    today = datetime.date.today()
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": "TAIEX",
        "start_date": (today - datetime.timedelta(days=days)).isoformat(),
        "end_date": today.isoformat(),
        "token": token,
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    payload = resp.json()

    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind TaiwanStockPrice(TAIEX) 回應異常：{payload.get('msg')}")

    history = []
    prev_close = None
    for item in payload.get("data") or []:
        close = item.get("close")
        high = item.get("max")
        low = item.get("min")

        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else None
        range_pct = round((high - low) / prev_close * 100, 2) if prev_close else None

        history.append({
            "date": item.get("date"),
            "open": item.get("open"),
            "high": high,
            "low": low,
            "close": close,
            "change_pct": change_pct,
            "range_pct": range_pct,
        })
        prev_close = close
    return history


def get_option_chain(data_id: str = "TXO", lookback_days: int = 5) -> list[dict]:
    """抓最近一個有資料的交易日的完整選擇權成交/未平倉狀況，給開倉分析看
    OI (未平倉量)/Put-Call 籌碼分布，藉此判斷市場認為的壓力/支撐履約價。
    lookback_days 只是為了跳過假日往前找「最近一個交易日」的容錯範圍，
    不是要抓好幾天的歷史（一天的完整選擇權鏈本身資料量就不小了）。"""
    token = os.environ.get("FINMIND_API_TOKEN")
    if not token:
        raise RuntimeError("FINMIND_API_TOKEN 未設定，請確認 .env 檔")

    today = datetime.date.today()
    params = {
        "dataset": "TaiwanOptionDaily",
        "data_id": data_id,
        "start_date": (today - datetime.timedelta(days=lookback_days)).isoformat(),
        "end_date": today.isoformat(),
        "token": token,
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
