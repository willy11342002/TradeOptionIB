"""
選擇權篩選器的核心邏輯，對應 test.ipynb 最後「選擇權篩選」那個實驗性章
節——用 IB 市場掃描器 (reqScannerDataAsync) 或使用者手動輸入的代碼湊出
候選標的清單 (run_scanner)，再幫每筆候選補公司/產業/衍生商品等中繼資
料 (enrich_candidates)，供 `web_screener_widget.py` 顯示/管理自選清單。

跟 `web_quote_board_page.py` 的報價表格是兩回事：那邊是「使用者已經決
定好一檔標的，長駐訂閱即時報價、隨 tick 更新表格」；這裡是「使用者還沒
決定要看哪一檔，批次跑過一堆候選標的、each 跑完就丟結果」，用不到長駐
訂閱。
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ib_async import IB, Future, ScannerSubscription, Stock, TagValue

from app.services import black_scholes, contract_meta_store

# enrich_candidates() 整批 reqContractDetailsAsync() 的逾時秒數——IB 對
# 查無資料的情況不一定會丟例外，可能永遠不回應，數字給大一點：實測 20
# 檔平行查完約 3.8 秒，但最多取到 50 檔候選，且是各自獨立的 reqId，個別
# 合約慢一點很正常，不能跟單一快照請求共用同一個(較緊繃的)逾時數字。
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
    # 這檔標的支援的衍生商品，固定用 ["期貨", "月選", "週選"] 這個順序
    # (有才列，沒有就不列)，見 enrich_candidates() 的說明。
    products: list[str] = field(default_factory=list)
    # 快照用的合約物件——掃描結果本身就帶(contractDetails.contract)，手
    # 動輸入的要先 qualify 才有；只在這個 process 內部抓快照用，不存進
    # scan_history_store(那邊只存 symbol/source/rank，contract 物件不能
    # 序列化成 JSON)。
    contract: object = None


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


def _third_friday(year: int, month: int) -> int:
    """該年月「第三個星期五」是幾號——標準月選的到期規則。"""
    first_friday = 1 + (4 - datetime(year, month, 1).weekday()) % 7  # weekday() 4 = 星期五
    return first_friday + 14


def _is_monthly_expiry(exp: str) -> bool:
    """"YYYYMMDD" 格式的到期日字串是不是當月第三個星期五——是的話算月
    選，不是的話算週選(不細分季選/其他非標準到期日，先求「查得到、分得
    出兩類」，不需要太複雜)。"""
    try:
        d = datetime.strptime(exp, "%Y%m%d").date()
    except ValueError:
        return False
    return d.weekday() == 4 and d.day == _third_friday(d.year, d.month)


async def _fetch_contract_meta(ib: IB, cand: CandidateStock) -> bool:
    """幫單一候選查名稱/產業/類別(reqContractDetailsAsync)+ 週選/月選
    (reqSecDefOptParamsAsync，取跟 SMART 選擇權報價頁籤
    `web_quote_board_page.py::_do_subscribe_core()` 同一套「exchange ==
    SMART 且 tradingClass == symbol」篩法，避免混進非標準交易所/類別的
    到期日)+ 期貨(reqContractDetailsAsync(Future(...))，不帶到期月份/交
    易所，IB 對這種「欠缺條件」的合約查詢會回傳所有找得到的匹配合約，一
    般美股沒有對應的個股期貨，回傳空清單是常態，不是查詢失敗)三種資料，
    三個平行送出、各自獨立處理例外。回傳這次 reqContractDetailsAsync()
    (名稱/產業/類別)有沒有成功——呼叫端只在這個成功時才存快取，避免週
    選/月選/期貨這幾項因為暫時性失敗被誤存成「真的沒有」，卡住一整天沒
    辦法重試。"""
    details, chains, futures = await asyncio.gather(
        ib.reqContractDetailsAsync(cand.contract),
        ib.reqSecDefOptParamsAsync(cand.contract.symbol, "", cand.contract.secType, cand.contract.conId),
        ib.reqContractDetailsAsync(Future(symbol=cand.contract.symbol, currency="USD")),
        return_exceptions=True,
    )
    if isinstance(details, BaseException) or not details:
        return False
    info = details[0]
    cand.long_name = info.longName or None
    cand.industry = info.industry or None
    cand.category = info.category or None

    expirations: set[str] = set()
    if not isinstance(chains, BaseException):
        chain = next(
            (c for c in chains if c.exchange == "SMART" and c.tradingClass == cand.contract.symbol), None,
        )
        if chain is not None:
            expirations = set(chain.expirations)
    products = []
    if not isinstance(futures, BaseException) and futures:
        products.append("期貨")
    if any(_is_monthly_expiry(exp) for exp in expirations):
        products.append("月選")
    if any(not _is_monthly_expiry(exp) for exp in expirations):
        products.append("週選")
    cand.products = products
    return True


async def enrich_candidates(ib: IB, candidates: list[CandidateStock]) -> None:
    """幫候選清單裡的每一筆補一次公司/ETF 名稱、產業、類別、支援的衍生
    商品(期貨/月選/週選)，就地修改傳進來的 CandidateStock，細節見
    `_fetch_contract_meta()`。*** 用 reqContractDetailsAsync()/
    reqSecDefOptParamsAsync()，不是報價快照(reqTickersAsync) ***：使用
    者明確不要現價/漲跌幅這種即時市場資料，這幾個是靜態的合約中繼資
    料，不佔市場資料線路，也不會因為帳戶沒開通某個市場的即時報價而抓不
    到。

    *** 一定要用 asyncio.gather() 平行送出去，不要 for 迴圈逐檔 await
    ***：這幾個 IB API 一次只能查一檔合約(沒有批次介面)，如果逐檔依序
    await，總耗時會隨候選數線性增加(50 檔=50 次序列等待，體感會很
    慢)；改成一次把所有 Awaitable 排進 gather()，總耗時取決於最慢的那一
    次，跟候選數幾乎無關(實測 20 檔 STK 掃描全部平行查完約 3.8 秒；現在
    每檔多查兩種資料，實測耗時會拉長，但仍跟候選數幾乎無關)。

    已經有 contract 的(市場掃描結果本來就帶 contractDetails.contract)直
    接查；沒有的(使用者手動輸入的代碼、或從歷史紀錄復原、只有 symbol
    字串)先批次 qualify 一次，查無此標的(conId 停在 0，對照
    ib_client.py 開頭的說明)就跳過。查詢失敗或查無資料的候選，欄位維持
    預設值，呼叫端自己決定顯示成空白，不當成整批失敗(`gather(...,
    return_exceptions=True)`，單一候選查詢例外不影響其他候選)。

    *** 先查本機快取(`app/services/contract_meta_store.py`)，同一天查過
    的 conId 直接填欄位、不用再打一次 IB ***(使用者要求)：自選清單展開
    這種一次平行查十幾二十檔的場景最有感，qualify(拿 conId)還是要做，
    但這步本來就快，真正慢的是逐檔 IB round-trip，快取命中就完全省
    掉。"""
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
            cand.products = cached.get("products") or []
        else:
            to_query.append(cand)
    if not to_query:
        return

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_fetch_contract_meta(ib, c) for c in to_query), return_exceptions=True),
            ENRICH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return
    fresh: dict[int, dict] = {
        cand.contract.conId: {
            "long_name": cand.long_name, "industry": cand.industry,
            "category": cand.category, "products": cand.products,
        }
        for cand, ok in zip(to_query, results) if ok is True
    }
    contract_meta_store.save_many(fresh)

