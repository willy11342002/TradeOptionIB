"""
IB 報價封裝，取代 app/models/capital_quote_client.py。

跟群益 SKQuoteLib 不一樣的地方：
    1. 不需要「先 EnterMonitorLONG 連線、等 STOCKS_READY 才能訂閱」那套
       兩階段狀態機——`ib_async` 的 `IB` 物件本身連線好了就能直接呼叫
       `reqMktData`。
    2. symbol_key 用 `str(contract.conId)`，不是代碼字串——conId 對任
       何美股/選擇權合約都是唯一、穩定的，不需要像 TAIFEX 那樣維護一
       套代碼編碼規則。
    3. *** 這個帳戶股票有即時資料權限，選擇權沒有(要靠 reqMarketDataType
       (4) 才退成延遲，不設的話預設走 Live 直接被拒絕，見建構子)。就算
       這樣，Delta/成交量這類欄位有時還是拿不到，IB 用 NaN 或 -1 代表
       「這個欄位沒有值」——**兩種都要濾掉，只濾 NaN 會讓 -1 這個哨兵值
       被當成真的價格顯示出來**(實測踩過：畫面上數字閃一下正常報價，
       下一次 tick 因為某個欄位是 -1 就把剛剛的正常值蓋掉，看起來像「查
       到又消失」，其實兩次都是真的收到的 tick，只是第二次的內容根本
       是無效值)。
    4. ticker.last 拿不到(NaN/-1)時退回 reqHistoricalData 查最近一筆收
       盤價(股票日線 TRADES，選擇權小時線 MIDPOINT，見
       scripts/ib_test_historical.py 的核對記錄)。**這個 fallback 查詢
       絕對不能同步、每次 tick 都重打**：`_on_pending_tickers` 每個
       tick(可能一秒好幾次、同時橫跨四五十檔合約)都會呼叫到，同步版的
       `reqHistoricalData` 本質是「在目前這個 asyncio/Qt 事件迴圈裡等
       一個 await 完成」，在事件迴圈自己驅動的回呼裡面又同步等一個新的
       網路來回，等於巢狀重入——實測會造成查詢大量堆積、log 狂噴
       (0DTE選擇權常常查無歷史資料，見 error 162)、UI 更新變慢。改成：
       用 `reqHistoricalDataAsync` 非阻塞地排一個背景查詢(每個合約同時
       間最多一個在飛，_fallback_pending 防重複)，查到的結果快取
       `_FALLBACK_TTL_SEC` 秒，這段時間內都直接用快取、不重新查詢。
"""
import asyncio
import math
import time
from typing import Dict, Optional, Tuple

from ib_async import Contract

from app.models.ib_client import IBClient
from app.services.signal import Signal

# (durationStr, barSizeSetting, whatToShow)，依序嘗試，第一個有資料的就
# 用。*** 選擇權在 SMART/BEST 路由查日線 100% 會回 code 162(查無EOD資
# 料)，不是偶爾失敗——直接依 secType 分開兩套清單，不要對選擇權也硬試
# 一次注定失敗的日線查詢(浪費一次網路來回，log 也會被洗版)。***
_FALLBACK_HISTORICAL_ATTEMPTS_STOCK = [
    ("5 D", "1 day", "TRADES"),      # 股票日線最穩定
]
_FALLBACK_HISTORICAL_ATTEMPTS_OPTION = [
    ("2 D", "1 hour", "MIDPOINT"),   # 選擇權日線在SMART/BEST查無EOD資料，小時線MIDPOINT才穩定查得到
]
_FALLBACK_TTL_SEC = 60.0  # 這段時間內重複拿不到即時價，直接用快取，不重新打 reqHistoricalData


def _fallback_attempts_for(contract):
    return _FALLBACK_HISTORICAL_ATTEMPTS_STOCK if contract.secType == "STK" else _FALLBACK_HISTORICAL_ATTEMPTS_OPTION


def _clean(value):
    """IB 用 NaN 或 -1 代表「這個欄位沒有值」，兩種都要濾掉——見檔案開
    頭的說明，這不是防禦性多寫，是實測踩過的真實 bug。"""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        return value
    if value == -1:
        return None
    return value


class IBQuoteClient:
    def __init__(self, ib_client: IBClient):
        self.quote_updated = Signal()  # symbol_key(conId字串), dict同capital_quote_client.py的欄位
        self.quote_error = Signal()    # symbol_key或動作說明, 錯誤訊息
        self._ib_client = ib_client
        self._ib = ib_client.ib
        self._contracts: Dict[str, Contract] = {}  # symbol_key -> 已訂閱的 Contract
        self._latest: Dict[str, dict] = {}  # symbol_key -> 最後一次 quote_updated 的資料，給 get_cached() 用
        self._fallback_cache: Dict[str, Tuple[float, Optional[float]]] = {}  # symbol_key -> (查詢時間, 收盤價或None)
        self._fallback_pending: set = set()  # 正在背景查詢中的 symbol_key，避免同一檔重複排查詢
        self._ib.pendingTickersEvent += self._on_pending_tickers

    def subscribe(self, contracts) -> None:
        for contract in contracts:
            key = str(contract.conId)
            if key in self._contracts:
                continue
            self._contracts[key] = contract
            self._ib.reqMktData(contract, "", False, False)

    def unsubscribe(self, contracts) -> None:
        for contract in contracts:
            key = str(contract.conId)
            if key not in self._contracts:
                continue
            del self._contracts[key]
            try:
                self._ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001
                pass

    def unsubscribe_all(self) -> None:
        self.unsubscribe(list(self._contracts.values()))

    def prime_fallback(self, contract) -> None:
        """訂閱後立刻主動排一次退回查詢(reqHistoricalData)，不等
        pendingTickersEvent 先觸發——`_get_fallback_cached()` 原本只有
        _on_pending_tickers 收到 tick 時才會被呼叫到，如果這檔合約訂閱
        之後 IB 一次 tick 都沒送(常見於盤中沒有任何成交、或帳戶對這檔沒
        有市場資料權限又剛好連 frozen tick 都不給)，畫面就會永遠停在
        「尚未查詢」，因為連「退回查歷史資料」這條路都沒被觸發過。只對
        標的股票這種「只有一檔、每次查詢只會呼叫一次」的合約用這個——
        不要對整批選擇權合約也這樣做，reqHistoricalData 有頻率限制，選
        擇權那邊繼續靠 tick 觸發的 fallback 就夠了。"""
        key = str(contract.conId)
        if key in self._fallback_pending:
            return
        self._fallback_pending.add(key)
        asyncio.ensure_future(self._refresh_fallback_async(key, contract))

    def fetch_snapshot(self, contract) -> dict:
        """主動查一次目前的報價快照(不需要等事件)，隨時可以呼叫——這是
        使用者主動觸發的單次動作(不是每個 tick 都會呼叫)，用同步版
        fallback 沒有 _on_pending_tickers 那種高頻重入的風險。"""
        key = str(contract.conId)
        already_subscribed = key in self._contracts
        if not already_subscribed:
            self._ib.reqMktData(contract, "", False, False)
        self._ib.sleep(2)
        ticker = self._ib.ticker(contract)
        data = self._ticker_to_data(ticker) if ticker else self._empty_data_sync(contract)
        if not already_subscribed:
            try:
                self._ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001
                pass
        self._emit(key, data)
        return data

    def get_cached(self, symbol_key: str) -> Optional[dict]:
        """回傳最後一次收到的報價(不等事件、不主動查)，給下單面板即時算
        價差淨價這種「有就用、沒有就算了」的用途。"""
        return self._latest.get(symbol_key)

    def _emit(self, symbol_key: str, data: dict) -> None:
        self._latest[symbol_key] = data
        self.quote_updated.emit(symbol_key, data)

    def _on_pending_tickers(self, tickers) -> None:
        for ticker in tickers:
            key = str(ticker.contract.conId)
            if key not in self._contracts:
                continue
            self._emit(key, self._ticker_to_data(ticker))

    def _ticker_to_data(self, ticker) -> dict:
        key = str(ticker.contract.conId)
        last = _clean(ticker.last)
        if last is None:
            last = self._get_fallback_cached(key, ticker.contract)
        return {
            "bid": _clean(ticker.bid),
            "ask": _clean(ticker.ask),
            "last": last,
            "open": _clean(ticker.open),
            "high": _clean(ticker.high),
            "low": _clean(ticker.low),
            "preclose": _clean(ticker.close),
            # 美股/選擇權沒有台股那種漲跌停概念，這兩個欄位保留 key 是
            # 為了讓 order_book.py/positions.py 現有的 .get() 讀法不用改。
            "up_limit": None,
            "down_limit": None,
            "tick_qty": _clean(ticker.lastSize),
        }

    def _empty_data_sync(self, contract) -> dict:
        """fetch_snapshot() 專用：完全查不到 ticker 物件時的保底，同步
        查一次歷史資料——這條路只有使用者主動呼叫 fetch_snapshot 才會
        走到，不是每個 tick 都會執行，用同步呼叫可以接受。"""
        last = self._fallback_last_price_sync(contract)
        return {
            "bid": None, "ask": None, "last": last, "open": None, "high": None,
            "low": None, "preclose": None, "up_limit": None, "down_limit": None, "tick_qty": None,
        }

    def _get_fallback_cached(self, key: str, contract) -> Optional[float]:
        """回傳快取的退回收盤價，快取過期(或從沒查過)就在背景排一個非
        阻塞的查詢，不在這裡等結果——這個函式被 _on_pending_tickers 高
        頻呼叫，絕對不能在這裡面做同步的網路等待(見檔案開頭的說明)。"""
        cached = self._fallback_cache.get(key)
        now = time.time()
        if cached is not None and now - cached[0] < _FALLBACK_TTL_SEC:
            return cached[1]
        if key not in self._fallback_pending:
            self._fallback_pending.add(key)
            asyncio.ensure_future(self._refresh_fallback_async(key, contract))
        return cached[1] if cached is not None else None

    async def _refresh_fallback_async(self, key: str, contract) -> None:
        price = None
        for duration, bar_size, what_to_show in _fallback_attempts_for(contract):
            try:
                bars = await self._ib.reqHistoricalDataAsync(
                    contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
                    whatToShow=what_to_show, useRTH=True,
                )
            except Exception:  # noqa: BLE001
                continue
            if bars:
                price = bars[-1].close
                break
        self._fallback_cache[key] = (time.time(), price)
        self._fallback_pending.discard(key)
        # 查到的話主動補推一次，畫面不用等下一次不相干的 tick 才更新；
        # 這檔可能已經被取消訂閱(使用者換了查詢範圍)，這種情況就不用補推了。
        if price is not None and key in self._contracts:
            ticker = self._ib.ticker(contract)
            if ticker is not None:
                self._emit(key, self._ticker_to_data(ticker))

    def _fallback_last_price_sync(self, contract) -> Optional[float]:
        for duration, bar_size, what_to_show in _fallback_attempts_for(contract):
            try:
                bars = self._ib.reqHistoricalData(
                    contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
                    whatToShow=what_to_show, useRTH=True,
                )
            except Exception:  # noqa: BLE001
                continue
            if bars:
                return bars[-1].close
        return None
