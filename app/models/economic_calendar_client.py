"""
未來一週全球財經日曆 (Fed 利率決議、非農、CPI...)。

Finnhub 的財經日曆 endpoint 實測是 Premium 限定，免費 key 一律 403，改用
ForexFactory 對外公開、免金鑰的 JSON feed (业界很多交易機器人/EA 都在用這個
來源)。非官方 API，格式如果哪天跑掉了，換掉這個模組即可，不影響其他部分。

限制：這個 feed 目前只提供 "thisweek"（本週一~週日），沒有 "nextweek"，
所以如果在週中(例如週四、週五)執行分析，實際只看得到本週剩下幾天的事件，
看不到下週一開始的部分。免費資源目前只能先接受這個限制。
"""
import datetime

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_{period}.json"


def _fetch_period(period: str) -> list[dict]:
    resp = requests.get(FEED_URL.format(period=period), timeout=15)
    if resp.status_code == 429:
        raise RuntimeError("財經日曆來源暫時被限流（免費資源，短時間內請求太多次），請稍後再試一次")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def get_upcoming_events(days: int = 7) -> list[dict]:
    raw_events = _fetch_period("thisweek")

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=days)

    events = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        raw_date = item.get("date")
        try:
            event_dt = datetime.datetime.fromisoformat(raw_date).astimezone(datetime.timezone.utc)
        except (TypeError, ValueError):
            continue
        if not (now <= event_dt <= cutoff):
            continue
        events.append({
            "date": raw_date,
            "country": item.get("country"),
            "event": item.get("title"),
            "impact": item.get("impact"),
            "actual": item.get("actual"),
            "estimate": item.get("forecast"),
            "prev": item.get("previous"),
        })

    events.sort(key=lambda e: e["date"] or "")
    return events
