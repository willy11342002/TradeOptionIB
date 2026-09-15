"""
用 yfinance 查詢股票/ETF 的基本資料/財報/財報發布日/分析師目標價評級，
取代 IB 的 `reqFundamentalDataAsync()`——這個帳號沒有開通 IB 的財報資料
模組(實測 `Error 10358: Fundamentals data is not allowed`，連
`genericTickList="258"` 的精簡版財務比率也一樣被擋)，yfinance 打 Yahoo
Finance 的公開資料，不需要 IB 帳戶權限，免費且涵蓋範圍更廣(含 ETF 的類
別/資產規模)。

*** yfinance 是同步、會真的發 HTTP 請求的 library，一定要透過
app/services/background_tasks.py::run_blocking() 丟到背景執行緒呼叫，
不能直接在 async 函式裡呼叫 ***：跟 openrouter_client.py 呼叫 AI 同一個
理由——這裡是 NiceGUI(單一 asyncio 事件迴圈)，同步 I/O 直接呼叫會卡住整
個事件迴圈，所有使用者(這是本機單人工具，但道理一樣)的畫面都要等這一
次查詢做完才會有反應。

*** 一天只打一次 API，同一天內重複查詢直接讀本機 ***(使用者要求)：
`fetch_fundamentals()` 先查 `app/services/fundamentals_store.py` 的本機
快取，快取存在且查詢日期是今天就直接回傳，不用等網路；沒有快取或快取是
之前某一天查的才真的打 yfinance，查到之後(且沒有 `error`)存回本機快取
蓋掉舊的一份。查詢失敗(`error` 有值)故意不存快取——網路抖動這種暫時性
失敗不該卡住整天，讓使用者下次查詢還有機會重打成功。
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Optional

from app.services import fundamentals_store
from app.services.background_tasks import run_blocking

# 只抓最近幾季，「查看詳細」是給使用者快速掃過近況用，不是完整財報分析
# 工具，抓更多季只會讓表格越滾越長。
_PERIODS = 4


@dataclass
class IncomeStatementPeriod:
    period: str  # "YYYY-MM-DD"，該季結算日
    revenue: Optional[float] = None
    cost_of_revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    rd_expense: Optional[float] = None
    sga_expense: Optional[float] = None
    operating_income: Optional[float] = None
    pretax_income: Optional[float] = None
    tax_provision: Optional[float] = None
    net_income: Optional[float] = None
    basic_eps: Optional[float] = None
    diluted_eps: Optional[float] = None


@dataclass
class BalanceSheetPeriod:
    period: str
    total_assets: Optional[float] = None
    current_assets: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    total_liabilities: Optional[float] = None
    current_liabilities: Optional[float] = None
    total_debt: Optional[float] = None
    long_term_debt: Optional[float] = None
    stockholders_equity: Optional[float] = None
    retained_earnings: Optional[float] = None
    working_capital: Optional[float] = None
    debt_ratio: Optional[float] = None  # 總負債/總資產，已經乘以100(百分比)


@dataclass
class CashFlowPeriod:
    period: str
    operating_cash_flow: Optional[float] = None
    capital_expenditure: Optional[float] = None
    free_cash_flow: Optional[float] = None
    investing_cash_flow: Optional[float] = None
    financing_cash_flow: Optional[float] = None
    dividends_paid: Optional[float] = None
    stock_repurchase: Optional[float] = None
    net_change_in_cash: Optional[float] = None


@dataclass
class EquityChangePeriod:
    """近似版「股東權益變動」——yfinance 沒有提供正式的股東權益變動表
    (這是四大財報之一，但一般財經資料 API 很少直接提供，通常要自己解析
    10-Q/10-K 才有)，這裡用資產負債表的權益科目(當期期末餘額)+現金流量
    表的籌資活動(股票回購/股利發放/普通股發行，當期發生數)拼出一個近似
    版本，不是正式財報格式，UI 上要清楚標示這點，不能讓使用者誤以為是
    公司正式揭露的股東權益變動表。"""
    period: str
    stockholders_equity: Optional[float] = None
    common_stock: Optional[float] = None
    retained_earnings: Optional[float] = None
    stock_repurchase: Optional[float] = None
    dividends_paid: Optional[float] = None
    stock_issuance: Optional[float] = None


@dataclass
class AnalystInfo:
    recommendation_key: Optional[str] = None  # "buy"/"hold"/"sell" 等，Yahoo 原文
    recommendation_mean: Optional[float] = None  # 1(強力買進)~5(強力賣出)，愈低愈樂觀
    number_of_analysts: Optional[int] = None
    target_mean_price: Optional[float] = None
    target_high_price: Optional[float] = None
    target_low_price: Optional[float] = None
    target_median_price: Optional[float] = None
    rating_counts: dict = field(default_factory=dict)  # 最近一期(0m)：{"strongBuy":6,"buy":19,...}


@dataclass
class FundamentalsSnapshot:
    symbol: str
    long_name: Optional[str] = None
    quote_type: Optional[str] = None  # "EQUITY" / "ETF" / ...
    sector: Optional[str] = None
    industry: Optional[str] = None
    category: Optional[str] = None  # ETF 用(例如 "Large Blend")，個股通常是空的
    exchange: Optional[str] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    dividend_yield: Optional[float] = None
    eps_ttm: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    employees: Optional[int] = None
    website: Optional[str] = None
    summary: Optional[str] = None
    next_earnings_date: Optional[str] = None
    income_statements: list[IncomeStatementPeriod] = field(default_factory=list)
    balance_sheets: list[BalanceSheetPeriod] = field(default_factory=list)
    cash_flows: list[CashFlowPeriod] = field(default_factory=list)
    equity_changes: list[EquityChangePeriod] = field(default_factory=list)
    analyst: Optional[AnalystInfo] = None
    error: Optional[str] = None


def _clean(value):
    """yfinance 對缺值有時給 None、有時給 NaN，兩種都要濾掉，跟
    app/models/screener.py::_clean() 同一個道理。"""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        return value
    return value


def _fetch_sync(symbol: str) -> FundamentalsSnapshot:
    import yfinance as yf

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
    except Exception as exc:  # noqa: BLE001
        return FundamentalsSnapshot(symbol=symbol, error=f"查詢失敗：{exc}")

    if not info or (info.get("longName") is None and info.get("shortName") is None):
        return FundamentalsSnapshot(symbol=symbol, error="查無這檔代碼的資料(yfinance/Yahoo Finance 查不到)")

    snapshot = FundamentalsSnapshot(
        symbol=symbol,
        long_name=_clean(info.get("longName")) or _clean(info.get("shortName")),
        quote_type=_clean(info.get("quoteType")),
        sector=_clean(info.get("sector")),
        industry=_clean(info.get("industry")),
        category=_clean(info.get("category")),
        exchange=_clean(info.get("exchange")),
        market_cap=_clean(info.get("marketCap")) or _clean(info.get("totalAssets")),
        pe_ratio=_clean(info.get("trailingPE")),
        forward_pe=_clean(info.get("forwardPE")),
        dividend_yield=_clean(info.get("dividendYield")),
        eps_ttm=_clean(info.get("trailingEps")),
        week52_high=_clean(info.get("fiftyTwoWeekHigh")),
        week52_low=_clean(info.get("fiftyTwoWeekLow")),
        employees=_clean(info.get("fullTimeEmployees")),
        website=_clean(info.get("website")),
        summary=_clean(info.get("longBusinessSummary")),
    )

    # 財報發布日——get_earnings_dates() 由近到遠排序(含未來場次)，還沒公
    # 布結果的那幾筆「Reported EPS」是 NaN，第一筆 NaN 的就是下一次即將
    # 公布的日期；ETF/查詢失敗就沒有下一步，安靜跳過不當成整體失敗。
    try:
        earnings = ticker.get_earnings_dates(limit=8)
        if earnings is not None and not earnings.empty:
            upcoming = earnings[earnings["Reported EPS"].isna()]
            target = upcoming.index[0] if not upcoming.empty else earnings.index[0]
            snapshot.next_earnings_date = target.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        pass

    # 分析師目標價/評級——ETF、部分小型股沒有分析師覆蓋，安靜跳過。
    try:
        targets = ticker.analyst_price_targets or {}
        recommendation_key = _clean(info.get("recommendationKey"))
        recommendation_mean = _clean(info.get("recommendationMean"))
        number_of_analysts = _clean(info.get("numberOfAnalystOpinions"))
        if targets or recommendation_key or recommendation_mean:
            rating_counts = {}
            try:
                rec = ticker.recommendations
                if rec is not None and not rec.empty:
                    latest = rec[rec["period"] == "0m"]
                    row = latest.iloc[0] if not latest.empty else rec.iloc[0]
                    for key in ("strongBuy", "buy", "hold", "sell", "strongSell"):
                        if key in row:
                            rating_counts[key] = int(row[key])
            except Exception:  # noqa: BLE001
                pass
            snapshot.analyst = AnalystInfo(
                recommendation_key=recommendation_key,
                recommendation_mean=recommendation_mean,
                number_of_analysts=number_of_analysts,
                target_mean_price=_clean(targets.get("mean")),
                target_high_price=_clean(targets.get("high")),
                target_low_price=_clean(targets.get("low")),
                target_median_price=_clean(targets.get("median")),
                rating_counts=rating_counts,
            )
    except Exception:  # noqa: BLE001
        pass

    # 損益表/資產負債表/現金流量表——三張表分開查、分開失敗，ETF 通常三
    # 張表都是空的，個股偶爾缺某一張(例如剛上市沒多久缺歷史資料)，缺的
    #那張表對應的 list 就留空，不影響其他表正常顯示。
    try:
        income_stmt = ticker.quarterly_income_stmt
    except Exception:  # noqa: BLE001
        income_stmt = None
    try:
        balance_sheet = ticker.quarterly_balance_sheet
    except Exception:  # noqa: BLE001
        balance_sheet = None
    try:
        cashflow = ticker.quarterly_cashflow
    except Exception:  # noqa: BLE001
        cashflow = None

    if income_stmt is not None and not income_stmt.empty:
        for period_ts in list(income_stmt.columns)[:_PERIODS]:
            col = income_stmt[period_ts]
            snapshot.income_statements.append(IncomeStatementPeriod(
                period=period_ts.strftime("%Y-%m-%d"),
                revenue=_clean(col.get("Total Revenue")),
                cost_of_revenue=_clean(col.get("Cost Of Revenue")),
                gross_profit=_clean(col.get("Gross Profit")),
                rd_expense=_clean(col.get("Research And Development")),
                sga_expense=_clean(col.get("Selling General And Administration")),
                operating_income=_clean(col.get("Operating Income")),
                pretax_income=_clean(col.get("Pretax Income")),
                tax_provision=_clean(col.get("Tax Provision")),
                net_income=_clean(col.get("Net Income")),
                basic_eps=_clean(col.get("Basic EPS")),
                diluted_eps=_clean(col.get("Diluted EPS")),
            ))

    if balance_sheet is not None and not balance_sheet.empty:
        for period_ts in list(balance_sheet.columns)[:_PERIODS]:
            col = balance_sheet[period_ts]
            total_liabilities = _clean(col.get("Total Liabilities Net Minority Interest"))
            total_assets = _clean(col.get("Total Assets"))
            debt_ratio = (
                total_liabilities / total_assets * 100
                if total_liabilities is not None and total_assets else None
            )
            snapshot.balance_sheets.append(BalanceSheetPeriod(
                period=period_ts.strftime("%Y-%m-%d"),
                total_assets=total_assets,
                current_assets=_clean(col.get("Current Assets")),
                cash_and_equivalents=_clean(col.get("Cash And Cash Equivalents")),
                total_liabilities=total_liabilities,
                current_liabilities=_clean(col.get("Current Liabilities")),
                total_debt=_clean(col.get("Total Debt")),
                long_term_debt=_clean(col.get("Long Term Debt")),
                stockholders_equity=_clean(col.get("Stockholders Equity")),
                retained_earnings=_clean(col.get("Retained Earnings")),
                working_capital=_clean(col.get("Working Capital")),
                debt_ratio=debt_ratio,
            ))

    if cashflow is not None and not cashflow.empty:
        for period_ts in list(cashflow.columns)[:_PERIODS]:
            col = cashflow[period_ts]
            snapshot.cash_flows.append(CashFlowPeriod(
                period=period_ts.strftime("%Y-%m-%d"),
                operating_cash_flow=_clean(col.get("Operating Cash Flow")),
                capital_expenditure=_clean(col.get("Capital Expenditure")),
                free_cash_flow=_clean(col.get("Free Cash Flow")),
                investing_cash_flow=_clean(col.get("Investing Cash Flow")),
                financing_cash_flow=_clean(col.get("Financing Cash Flow")),
                dividends_paid=_clean(col.get("Cash Dividends Paid")),
                stock_repurchase=_clean(col.get("Repurchase Of Capital Stock")),
                net_change_in_cash=_clean(col.get("Changes In Cash")),
            ))

    # 近似版股東權益變動——見 EquityChangePeriod 的說明，跟資產負債表用
    # 同一組期別(balance_sheet.columns)，籌資活動數字從現金流量表對應同
    # 一個期別取，缺一邊就讓那幾欄留 None。
    if balance_sheet is not None and not balance_sheet.empty:
        for period_ts in list(balance_sheet.columns)[:_PERIODS]:
            bs_col = balance_sheet[period_ts]
            cf_col = cashflow[period_ts] if cashflow is not None and period_ts in cashflow.columns else None
            snapshot.equity_changes.append(EquityChangePeriod(
                period=period_ts.strftime("%Y-%m-%d"),
                stockholders_equity=_clean(bs_col.get("Stockholders Equity")),
                common_stock=_clean(bs_col.get("Common Stock")) or _clean(bs_col.get("Capital Stock")),
                retained_earnings=_clean(bs_col.get("Retained Earnings")),
                stock_repurchase=_clean(cf_col.get("Repurchase Of Capital Stock")) if cf_col is not None else None,
                dividends_paid=_clean(cf_col.get("Cash Dividends Paid")) if cf_col is not None else None,
                stock_issuance=_clean(cf_col.get("Net Common Stock Issuance")) if cf_col is not None else None,
            ))

    return snapshot


def _dict_to_snapshot(data: dict) -> FundamentalsSnapshot:
    """把 `fundamentals_store.load()` 讀回的原始 dict 重建成
    `FundamentalsSnapshot`——純量欄位跟 dataclass 的建構參數名稱本來就一
    一對應，直接 `**kwargs` 展開；巢狀的期間清單/`AnalystInfo` 另外組回
    對應的 dataclass 實例，`dataclasses.asdict()` 存檔時已經把它們攤平
    成 dict/list of dict，讀回來要手動轉回去。"""
    kwargs = dict(data)
    kwargs["income_statements"] = [IncomeStatementPeriod(**p) for p in data.get("income_statements") or []]
    kwargs["balance_sheets"] = [BalanceSheetPeriod(**p) for p in data.get("balance_sheets") or []]
    kwargs["cash_flows"] = [CashFlowPeriod(**p) for p in data.get("cash_flows") or []]
    kwargs["equity_changes"] = [EquityChangePeriod(**p) for p in data.get("equity_changes") or []]
    kwargs["analyst"] = AnalystInfo(**data["analyst"]) if data.get("analyst") else None
    return FundamentalsSnapshot(**kwargs)


async def fetch_fundamentals(symbol: str) -> FundamentalsSnapshot:
    """`error` 有值代表整份查詢失敗(查無代碼/yfinance 拋例外)；`error`
    是 None 但個別欄位/整張表缺漏是正常情況(ETF 沒有損益表、免費資料源
    偶爾缺項、沒有分析師覆蓋)，呼叫端(web_screener_widget.py)自己決定
    顯示成「查無資料」，不是錯誤。

    先查本機快取(見 module docstring)，今天查過就直接回傳；沒有才真的打
    API，成功才存回快取。"""
    cached = fundamentals_store.load(symbol)
    if cached is not None:
        try:
            return _dict_to_snapshot(cached)
        except Exception:  # noqa: BLE001
            pass  # 快取格式壞掉(例如改版後欄位對不上)就當沒有，退回打 API
    snapshot = await run_blocking(_fetch_sync, symbol)
    if not snapshot.error:
        fundamentals_store.save(symbol, dataclasses.asdict(snapshot))
    return snapshot
