"""
選擇權篩選器的核心邏輯，對應 test.ipynb 最後「選擇權篩選」那個實驗性章節
——目標是重現截圖裡那種「先掃出一批候選股票，再套用選擇權天期/流動性條
件做二次篩選」的兩階段流程：

    1. 初篩 (run_scanner)：用 IB 市場掃描器 (reqScannerDataAsync) 或使用
       者手動輸入的代碼湊出候選標的清單。
    2. 復篩 (screen_one)：對每檔候選標的查選擇權鏈，挑落在 DTE 區間內最
       接近下限的到期日，取 ATM 履約價，讀 Call/Put 快照報價算價差%，並
       沿用 main_window.py 現有的 Black-Scholes 反推 IV 邏輯——不額外打
       reqHistoricalData 抓歷史 IV/Market Cap/財報日這些欄位，這隻篩選
       器要一次跑一整批標的，每多一種歷史資料查詢就是乘以候選數的額外
       負擔，先求「可動」，這些留給以後有需要再加。

跟 main_window.py 的報價表格是兩回事：那邊是「使用者已經決定好一檔標
的，長駐訂閱即時報價、隨 tick 更新表格」；這裡是「使用者還沒決定要看哪
一檔，批次跑過一堆候選標的、each 跑完就丟結果」，用不到長駐訂閱——改用
reqTickersAsync()對每檔候選各要一次快照 (snapshot=True)，IB 收到快照後
會自動結束該次請求，不用像 IBQuoteClient 那樣自己 cancelMktData()。
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ib_async import IB, ScannerSubscription, Stock, TagValue

from app.models.option_utils import build_option
from app.services import black_scholes, contract_meta_store

# 跟 main_window.py 用同一個近似無風險利率、同一套 Black-Scholes 反推邏輯。
RISK_FREE_RATE = 0.04

# 單一批次快照(reqTickersAsync)的逾時秒數——帳戶對某些標的可能沒有市場資
# 料權限，IB 這種情況不一定會丟例外，而是永遠不送資料，沒有逾時保護會卡
# 住整輪復篩。
SNAPSHOT_TIMEOUT_SEC = 8.0

# enrich_candidates() 整批 reqContractDetailsAsync() 的逾時秒數——理由跟
# SNAPSHOT_TIMEOUT_SEC 一樣(IB 對查無資料的情況不一定會丟例外，可能永遠
# 不回應)，數字給大一點：實測 20 檔平行查完約 3.8 秒，但最多取到 50 檔候
# 選，且是各自獨立的 reqId，個別合約慢一點很正常，不能跟單一快照請求共用
# 同一個(較緊繃的)逾時數字。
ENRICH_TIMEOUT_SEC = 15.0


@dataclass
class ScanFilterValue:
    code: str  # TagValue 的 tag，對應 app.models.scanner_catalog.FilterFieldDef.code
    value: float


@dataclass
class ScannerParams:
    scan_code: str = "HIGH_OPT_IMP_VOLAT"
    instrument: str = "STK"
    location_code: str = "STK.US.MAJOR"
    max_results: int = 50
    filters: list[ScanFilterValue] = field(default_factory=list)


@dataclass
class CandidateStock:
    symbol: str
    source: str  # "scanner" 或 "manual"
    rank: Optional[int] = None
    # 下面三個是 enrich_candidates() 事後補的合約中繼資料，不是掃描/手動
    # 輸入當下就有的——初篩清單原本只有 symbol/來源/排名，使用者看不出
    # 候選有沒有意義。故意不用即時報價(現價/漲跌幅)這種市場資料，改用
    # reqContractDetailsAsync() 抓的靜態分類資訊，讓使用者一眼看出「這
    # 是哪個產業/類別的標的」，不用先跳去查。
    long_name: Optional[str] = None
    industry: Optional[str] = None
    category: Optional[str] = None
    # industry/category 的中文翻譯——IB 回傳的是固定的英文分類字串，這裡
    # 不在 screener.py 裡翻譯(這支模組不碰 AI/翻譯快取，維持「純市場資
    # 料查詢」的分工)，由呼叫端(app/views/web_screener_widget.py)事後填
    # 上，查不到翻譯就維持 None，呼叫端自己決定退回顯示英文原文。
    industry_zh: Optional[str] = None
    category_zh: Optional[str] = None
    # 快照用的合約物件——掃描結果本身就帶(contractDetails.contract)，手
    # 動輸入的要先 qualify 才有；只在這個 process 內部抓快照用，不存進
    # scan_history_store(那邊只存 symbol/source/rank，contract 物件不能
    # 序列化成 JSON)。
    contract: object = None


@dataclass
class ScreenFilters:
    min_dte: int = 20
    max_dte: int = 45
    max_spread_pct: float = 15.0
    min_iv: float = 0.0  # 0 代表不限


@dataclass
class ScreenResult:
    symbol: str
    passed: bool
    reason: str = ""
    underlying_price: Optional[float] = None
    expiry: Optional[str] = None
    dte: Optional[int] = None
    strike: Optional[float] = None
    call_bid: Optional[float] = None
    call_ask: Optional[float] = None
    call_spread_pct: Optional[float] = None
    call_iv: Optional[float] = None
    put_bid: Optional[float] = None
    put_ask: Optional[float] = None
    put_spread_pct: Optional[float] = None
    put_iv: Optional[float] = None
    call_contract: object = None
    put_contract: object = None


def _clean(value):
    """IB 用 NaN 或 -1 代表「這個欄位沒有值」，兩種都要濾掉——跟
    ib_quote_client.py 的 _clean() 是同一個道理(踩過的實測結論，不是防禦
    性多寫)。"""
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


async def run_scanner(ib: IB, params: ScannerParams) -> list[CandidateStock]:
    """執行一次性市場掃描。reqScannerDataAsync 本身就是「訂閱→等第一批結
    果→自動取消」的封裝，不用自己管訂閱生命週期。"""
    sub = ScannerSubscription()
    sub.instrument = params.instrument
    sub.locationCode = params.location_code
    sub.scanCode = params.scan_code
    sub.numberOfRows = params.max_results

    filter_tags = [TagValue(tag=f.code, value=str(f.value)) for f in params.filters]
    scan_data = await ib.reqScannerDataAsync(sub, scannerSubscriptionFilterOptions=filter_tags)
    candidates = [
        CandidateStock(
            symbol=item.contractDetails.contract.symbol, source="scanner", rank=item.rank,
            contract=item.contractDetails.contract,
        )
        for item in scan_data
    ]
    await enrich_candidates(ib, candidates)
    return candidates


async def enrich_candidates(ib: IB, candidates: list[CandidateStock]) -> None:
    """幫候選清單裡的每一筆補一次公司/ETF 名稱、產業、類別，就地修改傳
    進來的 CandidateStock。*** 用 reqContractDetailsAsync()，不是報價快
    照(reqTickersAsync) ***：使用者明確不要現價/漲跌幅這種即時市場資
    料，這幾個是靜態的合約中繼資料，不佔市場資料線路，也不會因為帳戶沒
    開通某個市場的即時報價而抓不到。

    *** 一定要用 asyncio.gather() 平行送出去，不要 for 迴圈逐檔 await
    ***：reqContractDetailsAsync() 跟 reqTickersAsync 不同，一次只能查
    一檔合約(IB API 沒有提供批次介面)，如果逐檔依序 await，總耗時會隨
    候選數線性增加(50 檔=50 次序列等待，體感會很慢)；改成一次把所有
    Awaitable 排進 gather()，總耗時取決於最慢的那一次，跟候選數幾乎無
    關(實測 20 檔 STK 掃描全部平行查完約 3.8 秒)。

    已經有 contract 的(市場掃描結果本來就帶 contractDetails.contract)直
    接查；沒有的(使用者手動輸入的代碼、或從歷史紀錄復原、只有 symbol
    字串)先批次 qualify 一次，查無此標的(conId 停在 0，對照
    ib_client.py 開頭的說明)就跳過。查詢失敗或查無資料的候選，三個欄位
    維持 None，呼叫端自己決定顯示成空白，不當成整批失敗(`gather(...,
    return_exceptions=True)`，單一候選查詢例外不影響其他候選)。

    *** 先查本機快取(`app/services/contract_meta_store.py`)，同一天查過
    的 conId 直接填欄位、不用再打一次 reqContractDetailsAsync() ***(使
    用者要求)：自選清單展開這種一次平行查十幾二十檔的場景最有感，qualify
    (拿 conId)還是要做，但這步本來就快，真正慢的是逐檔 IB round-trip 的
    reqContractDetailsAsync()，快取命中就完全省掉。"""
    missing = [c for c in candidates if c.contract is None]
    if missing:
        stocks = {c.symbol: Stock(c.symbol, "SMART", "USD") for c in missing}
        try:
            await ib.qualifyContractsAsync(*stocks.values())
        except Exception:  # noqa: BLE001
            pass
        for cand in missing:
            stock = stocks[cand.symbol]
            if stock.conId:
                cand.contract = stock

    targets = [c for c in candidates if c.contract is not None]
    if not targets:
        return

    to_query = []
    for cand in targets:
        cached = contract_meta_store.load(cand.contract.conId)
        if cached is not None:
            cand.long_name = cached.get("long_name")
            cand.industry = cached.get("industry")
            cand.category = cached.get("category")
        else:
            to_query.append(cand)
    if not to_query:
        return

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(ib.reqContractDetailsAsync(c.contract) for c in to_query), return_exceptions=True),
            ENRICH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return
    fresh: dict[int, dict] = {}
    for cand, result in zip(to_query, results):
        if isinstance(result, BaseException) or not result:
            continue
        details = result[0]
        cand.long_name = details.longName or None
        cand.industry = details.industry or None
        cand.category = details.category or None
        fresh[cand.contract.conId] = {
            "long_name": cand.long_name, "industry": cand.industry, "category": cand.category,
        }
    contract_meta_store.save_many(fresh)


def _pick_expiry(expirations, min_dte: int, max_dte: int) -> Optional[str]:
    """從到期日清單挑落在 [min_dte, max_dte] 天數區間內、最接近 min_dte
    的一個——跟 test.ipynb 的 pick_expiry() 邏輯相同。"""
    today = datetime.now()
    candidates = sorted(
        (dte, exp)
        for exp in expirations
        if min_dte <= (dte := (datetime.strptime(exp, "%Y%m%d") - today).days) <= max_dte
    )
    return candidates[0][1] if candidates else None


async def _underlying_price(ib: IB, stock: Stock) -> Optional[float]:
    """拿標的參考價：先試一次快照，拿不到(帳戶沒開通即時/延遲訂閱時常
    見)就退回昨收(歷史資料)，跟 test.ipynb 的 get_underlying_price() 是
    同一套退回邏輯。"""
    try:
        tickers = await asyncio.wait_for(ib.reqTickersAsync(stock), SNAPSHOT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        tickers = []
    if tickers:
        price = _clean(tickers[0].marketPrice())
        if price is not None:
            return price
    try:
        bars = await ib.reqHistoricalDataAsync(
            stock, endDateTime="", durationStr="5 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True,
        )
    except Exception:  # noqa: BLE001
        return None
    return bars[-1].close if bars else None


def _side_metrics(ticker, is_call: bool, underlying_price: float, strike: float, time_to_expiry: Optional[float]):
    """回傳 (bid, ask, spread_pct, iv)。bid/ask 任一邊拿不到(NaN/-1)或
    <=0 時 spread_pct/iv 留 None，不硬湊。"""
    if ticker is None:
        return None, None, None, None
    bid, ask = _clean(ticker.bid), _clean(ticker.ask)
    spread_pct = None
    iv = None
    if bid and ask and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid else None
        if time_to_expiry:
            iv = black_scholes.implied_vol(is_call, underlying_price, strike, RISK_FREE_RATE, time_to_expiry, mid)
    return bid, ask, spread_pct, iv


async def screen_one(ib: IB, symbol: str, filters: ScreenFilters) -> ScreenResult:
    """對單一候選標的跑完整的選擇權復篩：查選擇權鏈→挑到期日→挑 ATM 履
    約價→查 Call/Put 快照報價、算價差%/近似IV，套用流動性/IV 濾網。結果
    表格「每檔股票一列」，只留 ATM 那一檔當代表列(跟 test.ipynb 的
    screen_options() 抓一整排履約價不同，這裡只需要一列摘要)。"""
    stock = Stock(symbol, "SMART", "USD")
    try:
        await ib.qualifyContractsAsync(stock)
    except Exception as exc:  # noqa: BLE001
        return ScreenResult(symbol=symbol, passed=False, reason=f"查無此標的：{exc}")
    if not stock.conId:
        return ScreenResult(symbol=symbol, passed=False, reason="查無此標的")

    try:
        chains = await ib.reqSecDefOptParamsAsync(symbol, "", "STK", stock.conId)
    except Exception as exc:  # noqa: BLE001
        return ScreenResult(symbol=symbol, passed=False, reason=f"查詢選擇權鏈失敗：{exc}")
    chain = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == symbol), None)
    if chain is None:
        return ScreenResult(symbol=symbol, passed=False, reason="查無標準選擇權鏈(SMART)")

    expiry = _pick_expiry(chain.expirations, filters.min_dte, filters.max_dte)
    if expiry is None:
        return ScreenResult(symbol=symbol, passed=False,
                             reason=f"找不到 {filters.min_dte}~{filters.max_dte} 天內的到期日")

    price = await _underlying_price(ib, stock)
    if price is None or not chain.strikes:
        return ScreenResult(symbol=symbol, passed=False, reason="查無標的現價/履約價", expiry=expiry)

    strike = min(chain.strikes, key=lambda s: abs(s - price))
    call = build_option(symbol, expiry, strike, "C")
    put = build_option(symbol, expiry, strike, "P")
    try:
        await ib.qualifyContractsAsync(call, put)
    except Exception as exc:  # noqa: BLE001
        return ScreenResult(symbol=symbol, passed=False, reason=f"合約查詢失敗：{exc}",
                             underlying_price=price, expiry=expiry, strike=strike)
    # qualifyContracts 對「這個履約價在這個到期日根本不存在」不會丟例
    # 外，只會讓 conId 停在 0——main_window.py::_do_subscribe_core() 踩過
    # 同一個坑，這裡一樣要濾。
    if not call.conId or not put.conId:
        return ScreenResult(symbol=symbol, passed=False, reason=f"{expiry} 到期日查無履約價 {strike:g}",
                             underlying_price=price, expiry=expiry, strike=strike)

    expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
    dte = (expiry_date - datetime.now().date()).days
    time_to_expiry = dte / 365.0 if dte > 0 else None

    try:
        tickers = await asyncio.wait_for(ib.reqTickersAsync(call, put), SNAPSHOT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return ScreenResult(symbol=symbol, passed=False, reason="報價逾時，可能沒有市場資料權限",
                             underlying_price=price, expiry=expiry, dte=dte, strike=strike)
    call_ticker = next((t for t in tickers if t.contract.conId == call.conId), None)
    put_ticker = next((t for t in tickers if t.contract.conId == put.conId), None)

    call_bid, call_ask, call_spread_pct, call_iv = _side_metrics(call_ticker, True, price, strike, time_to_expiry)
    put_bid, put_ask, put_spread_pct, put_iv = _side_metrics(put_ticker, False, price, strike, time_to_expiry)

    result = ScreenResult(
        symbol=symbol, passed=True, underlying_price=price, expiry=expiry, dte=dte, strike=strike,
        call_bid=call_bid, call_ask=call_ask, call_spread_pct=call_spread_pct, call_iv=call_iv,
        put_bid=put_bid, put_ask=put_ask, put_spread_pct=put_spread_pct, put_iv=put_iv,
        call_contract=call, put_contract=put,
    )

    reasons = []
    if call_spread_pct is not None and call_spread_pct > filters.max_spread_pct:
        reasons.append(f"Call價差{call_spread_pct:.1f}%過大")
    if put_spread_pct is not None and put_spread_pct > filters.max_spread_pct:
        reasons.append(f"Put價差{put_spread_pct:.1f}%過大")
    if filters.min_iv > 0:
        ivs = [iv for iv in (call_iv, put_iv) if iv is not None]
        if ivs and max(ivs) < filters.min_iv:
            reasons.append(f"IV低於{filters.min_iv:.0%}")
    if reasons:
        result.passed = False
        result.reason = "；".join(reasons)
    return result
