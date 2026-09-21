"""
選擇權賣方策略參數化回測引擎(Iron Condor/裸雙賣/Jade Lizard/Twisted Sister)：進場規則
(`EntrySpec`) + 出場規則清單(`ExitRule`)，輸出「一列 = 一邊(價差或單腳裸賣)」的逐筆交易(`Trade`)。
四種策略只差在哪一邊有買保護腳(`spec.PROTECTED_SIDES`)，裸賣的那一邊沒有長腳，價值就是短腳自己的
價格。

價格是真實的：`option_chain.OptionChain` 讀 ThetaData 回補的 EOD 買賣報價——不是理論算出來的。進場信用、
收盤出場的成交價由 `RunConfig.fill_price` 決定：買賣中價(`Quote.mid`，不計買賣價差)，或極端成交價
(賣出的腳收 bid、買進的腳付 ask，每次都付滿買賣價差，悲觀下界)。「收盤」永遠是當天收盤的報價，絕不是
最後一筆成交價。到期日/履約價也只挑當天真的有掛牌的，不是任意數字：進場
天期(`EntrySpec.dte`)是「目標」，實際到期日是當天所有掛牌到期日裡剩餘天數最接近這個目標的一個(見
`option_chain.OptionChain.nearest_expiration`)，長腳寬度同理是「目標」，實際履約價是離「短腳 ± 目標
寬度」最接近的真實掛牌履約價(見 `open_spread`)。|Delta| 條件需要的隱含波動率是用買賣中價反推的(免費
方案沒有現成 Greeks)，不是真正的官方 Delta，僅供估算。不模擬滑價、也不模擬美式提前履約。

*** 這支檔案 import 了 pandas，只能在使用者按下「執行回測」之後才 import(見 spec.py 開頭) ***

流程(每個交易日，`df` 需要有 open/high/low/close 欄、DatetimeIndex 遞增；只會走訪跟選擇權鏈資料
有交集的日期，見 `run_backtest`)：
1. 整組規則先判斷(整組 = 目前持有的所有單邊價差合計)，同一範圍內固定順序：到期天數 -> 停利 ->
   停損(不依使用者填寫順序)。觸發就把目前所有持倉平倉。
2. 再逐一判斷 put 邊、call 邊的單邊規則。觸發就平倉那一邊；動作是「平倉後重開」的話，當天收盤價
   用進場規則立刻重開該邊(找不到符合條件的履約價就留空)，動作是「只平倉」的話該邊留空，另一邊照
   常，兩邊都空手才整組重新進場。
3. 沒有任何持倉時，當天收盤價用進場規則開整組(put 邊+call 邊)。任何一邊找不到符合條件的履約
   價，或算出的信用不是正的，或不滿足進場條件(`EntryConditions`)，就整個不進場、隔天再試。
   「立刻重新進場」是使用者定案的做法。
   進場條件「無單邊風險」(Jade Lizard/Twisted Sister)：總權利金 >= 保護價差寬度。只在兩邊都湊齊時
   檢查；單邊「平倉後重開」時另一邊如果是空的(湊不成完整部位)就不檢查，直接重開。

成交模式(每條停利/停損規則各自決定)，停利/停損都是看「整張組合單的淨價」(持有的所有價差價值加總)：
- 收盤價：只看收盤報價(依 `fill_price` 是中價或 bid/ask)，越過門檻就用收盤淨價成交。
- 盤中觸價：模擬掛單。開盤淨價(各腳開盤成交價加總)已經越過門檻(跳空)用開盤淨價成交；否則收盤淨價越過
  門檻，代表當天價格一定走過掛價，以掛價(門檻)成交。只有 EOD 資料，沒有同一時刻的盤中組合報價，所以
  只用開盤跟收盤兩個時間點判斷：盤中碰到掛價、收盤又拉回來的日子會漏掉——對停利是保守(少賺)，對停損
  是樂觀(少賠)。價差價值夾在 0 到寬度之間(各腳成交時間對不上時湊出的值可能超出範圍)。同一天先判斷
  停利再判斷停損，對策略偏樂觀。
"""
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.models.backtest.option_chain import OptionChain, Quote
from app.models.backtest.spec import (
    ACTION_CLOSE_REOPEN, COMBINE_AND, COMBINE_OR, FILL_CLOSE, KIND_DTE, KIND_EVAL_ORDER,
    KIND_TAKE_PROFIT, METRIC_DELTA, METRIC_DISTANCE_PCT, METRIC_PREMIUM, PROTECTED_SIDES, SCOPE_GROUP,
    SCOPE_LEG, STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK, UNIT_CREDIT_PCT, WIDTH_USD, FILL_PRICE_WORST,
    EntrySpec, ExitRule, RunConfig, StrategySpec, snap_width,
)
from app.models.backtest.trade import Trade
from app.services import black_scholes as bs

CONTRACT_MULTIPLIER = 100  # 美股選擇權 1 口 = 100 股
SIDES = ("put", "call")

_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


@dataclass
class _Spread:
    """一邊的部位：有長腳就是價差，K_long 是 None 就是單腳裸賣(此時 width 是 0，entry_credit 就是短腳的
    價格)。`short_quotes`/`long_quotes` 是開倉當下一次撈出來的「這個履約價從進場到到期日」完整每日報價
    (見 `option_chain.OptionChain.contract_series`)，整段持有期間逐日出場判斷都查這個小表，不用每天重
    新查一次整個選擇權鏈。"""
    side: str
    K_short: float
    K_long: Optional[float]
    expiration: date
    entry_date: date
    entry_S: float
    entry_credit: float
    width: float
    short_quotes: Dict[date, Quote]
    long_quotes: Optional[Dict[date, Quote]]
    margin: Optional[float] = None   # 開倉當天收盤時，整組同時持有的部位的保證金(每股)；當天收盤前是 None

    @property
    def naked(self) -> bool:
        return self.K_long is None


# ---------------------------------------------------------------------------- 保證金
NAKED_MARGIN_PCT = 0.20        # Reg-T 裸賣選擇權：現價的 20%
NAKED_MARGIN_MIN_PCT = 0.10    # 下限：put 是履約價的 10%、call 是現價的 10%


def _naked_requirement(sp: _Spread) -> float:
    """簡化的 Reg-T 裸賣需求(每股，不含權利金)：max(20% × 現價 − 價外距離, 10% × 參考價)，參考價 put 用履約
    價、call 用現價。用開倉當下的現價，不隨後續價格變動重算(這是「初始」保證金)。"""
    S = sp.entry_S
    otm = (S - sp.K_short) if sp.side == "put" else (sp.K_short - S)
    floor_ref = sp.K_short if sp.side == "put" else S
    return max(NAKED_MARGIN_PCT * S - otm, NAKED_MARGIN_MIN_PCT * floor_ref)


def position_margin(spreads: List[_Spread]) -> float:
    """整組同時持有的部位的估計保證金(每股)，已扣掉整組收到的權利金(跟舊報表「寬度 − 權利金」同一種淨額算法)。

    put 邊、call 邊不會同時虧損(現價只會越過其中一邊)，所以整組只取需求最大的那一邊，不是兩邊相加：
    - 價差邊的需求 = 寬度
    - 裸賣邊的需求 = `_naked_requirement` + 該邊權利金
    - 需求最大的一邊之外，如果另一邊是裸賣，要再加上那一邊的權利金(Reg-T 的裸雙賣算法)
    - 最後扣掉整組收到的全部權利金；不會小於 0。
    Iron Condor 化簡後 = 較寬的寬度 − 兩邊權利金合計；裸雙賣 = 兩邊裸賣需求較大的那一個。"""
    if not spreads:
        return 0.0
    gross = {sp.side: (_naked_requirement(sp) + sp.entry_credit) if sp.naked else sp.width for sp in spreads}
    top = max(spreads, key=lambda sp: gross[sp.side])
    total = gross[top.side] + sum(sp.entry_credit for sp in spreads if sp is not top and sp.naked)
    return max(total - sum(sp.entry_credit for sp in spreads), 0.0)


# ---------------------------------------------------------------------------- 進場：選履約價
def _otm_sorted(strikes: List[Tuple[float, Quote]], S: float, side: str) -> List[Tuple[float, Quote]]:
    """由近到遠排序 OTM 履約價(put 在現價下方、call 在現價上方)。"""
    if side == "put":
        return sorted((k, q) for k, q in strikes if k < S)[::-1]
    return sorted((k, q) for k, q in strikes if k > S)


def _metric_value(metric: str, S: float, K: float, side: str, T: float, r: float, quote: Quote) -> Optional[float]:
    if metric == METRIC_PREMIUM:
        return quote.mid
    if metric == METRIC_DISTANCE_PCT:
        return ((K - S) if side == "call" else (S - K)) / S * 100.0
    if metric == METRIC_DELTA:
        iv = bs.implied_vol(side == "call", S, K, r, T, quote.mid)
        if iv is None:
            return None
        d = bs.delta(side == "call", S, K, r, T, iv)
        return abs(d) if d is not None else None
    raise ValueError(f"不支援的指標: {metric}")


def _conditions_pass(entry: EntrySpec, S: float, K: float, side: str, T: float, r: float, quote: Quote) -> bool:
    selector = entry.short
    for c in selector.conditions:
        v = _metric_value(c.metric, S, K, side, T, r, quote)
        ok = v is not None and _OPS[c.op](v, c.value)
        if selector.combine == COMBINE_AND and not ok:
            return False
        if selector.combine == COMBINE_OR and ok:
            return True
    return selector.combine == COMBINE_AND


def open_spread(
    chain: OptionChain, S: float, side: str, entry: EntrySpec, r: float, d: date, dates: List[date],
    worst: bool = False,
) -> Optional[_Spread]:
    """依進場規則開一邊的部位，找不到符合條件的短腳履約價、當天沒有可用的到期日、或算出的信用不是
    正的就回傳 None。

    到期日：當天實際掛牌的到期日裡，剩餘天數最接近 `entry.dte` 的一個。
    短腳：在該到期日的 OTM 履約價上由近到遠找，取第一個通過條件的(= 通過者中離現價最近、權利金最高)。
    長腳：只有這一邊有保護腳才買(`spec.PROTECTED_SIDES`)，在「短腳 ± 目標寬度」附近找最接近的真實
    掛牌履約價，往價外方向；裸賣的那一邊沒有長腳。
    信用：`worst` 為 False 用兩腳買賣中價；True 用極端成交價——賣短腳收買價(bid)、買長腳付賣價(ask)。
    挑履約價的條件(權利金/Delta)不管哪種都看中價，只有最後算進場信用才換成 bid/ask。"""
    expiration = chain.nearest_expiration(d, entry.dte)
    if expiration is None:
        return None
    T = (expiration - d).days / 365.0
    if T <= 0:
        return None

    candidates = _otm_sorted(chain.strikes_on(d, expiration, side), S, side)
    K_short = short_quote = None
    for k, q in candidates:
        if _conditions_pass(entry, S, k, side, T, r, q):
            K_short, short_quote = k, q
            break
    if K_short is None:
        return None

    K_long: Optional[float] = None
    long_quote: Optional[Quote] = None
    width = 0.0
    credit = short_quote.bid if worst else short_quote.mid
    if side in PROTECTED_SIDES[entry.kind]:
        raw_width = entry.long.width if entry.long.width_unit == WIDTH_USD else S * entry.long.width / 100.0
        target_width = snap_width(raw_width)
        target_K = K_short - target_width if side == "put" else K_short + target_width
        further = [(k, q) for k, q in candidates if (k < K_short if side == "put" else k > K_short)]
        if not further:
            return None
        K_long, long_quote = min(further, key=lambda kq: abs(kq[0] - target_K))
        width = abs(K_long - K_short)
        credit -= long_quote.ask if worst else long_quote.mid
    if credit <= 0:
        return None

    short_series = chain.contract_series(expiration, K_short, side, d, dates)
    long_series = chain.contract_series(expiration, K_long, side, d, dates) if K_long is not None else None
    return _Spread(
        side=side, K_short=K_short, K_long=K_long, expiration=expiration, entry_date=d, entry_S=S,
        entry_credit=credit, width=width, short_quotes=short_series, long_quotes=long_series,
    )


def _entry_conditions_pass(entry: EntrySpec, put: Optional[_Spread], call: Optional[_Spread]) -> bool:
    """進場條件(`spec.EntryConditions`)。目前只有「無單邊風險」：總權利金 >= 保護價差的寬度，只對 Jade
    Lizard/Twisted Sister 有意義。兩邊沒湊齊(其中一邊是空的)就不檢查——湊不成完整部位，沒有「總權利金」可比。"""
    if entry.kind not in STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK or not entry.conditions.no_single_side_risk:
        return True
    if put is None or call is None:
        return True
    protected = put if put.side in PROTECTED_SIDES[entry.kind] else call
    return put.entry_credit + call.entry_credit >= protected.width - 1e-9


def _open_position(
    S: float, chain: OptionChain, entry: EntrySpec, r: float, d: date, dates: List[date], worst: bool = False,
) -> Optional[Dict[str, _Spread]]:
    """開整組(put 邊+call 邊)。任何一邊選不到、或不滿足進場條件就回傳 None(整個不進場)。"""
    put = open_spread(chain, S, "put", entry, r, d, dates, worst)
    call = open_spread(chain, S, "call", entry, r, d, dates, worst) if put is not None else None
    if put is None or call is None or not _entry_conditions_pass(entry, put, call):
        return None
    return {"put": put, "call": call}


# ---------------------------------------------------------------------------- 出場：判斷觸發
Values = Dict[str, float]   # side -> 平倉時的價差價值


def _leg_price(q: Quote, point: str, is_short: bool, worst: bool) -> float:
    """平倉時這一腳的價格。"close" 是當天收盤的報價(不是最後一筆成交價——那可能是很早以前的成交)：
    `worst` 為 False 是買賣中價；True 是極端成交價，短腳要買回付賣價(ask)、長腳要賣出收買價(bid)。
    "open" 是當天開盤的實際成交價；這支選擇權當天沒有真的成交過(traded=False，遠價外常見)就沒有開盤
    價可用，退回收盤報價。"""
    if point == "close" or not q.traded:
        return (q.ask if is_short else q.bid) if worst else q.mid
    return q.open


def _spread_exit_value(sp: _Spread, d: date, point: str, worst: bool) -> Optional[float]:
    """一邊價差在 `point`("open"/"close")的平倉價值 = 短腳價 − 長腳價(裸賣就只有短腳價)。價差不可能低於 0、
    也不可能高於寬度(否則有無風險套利)，兩腳的價格是各自成交的、時間點不一定對得上，湊出範圍外的值時夾回
    範圍內。"""
    sq = sp.short_quotes.get(d)
    if sq is None:
        return None
    v = _leg_price(sq, point, True, worst)
    if sp.K_long is None:
        return v
    lq = sp.long_quotes.get(d)
    if lq is None:
        return None
    return min(max(v - _leg_price(lq, point, False, worst), 0.0), sp.width)


def _values_at(spreads: List[_Spread], d: date, point: str, worst: bool) -> Optional[Values]:
    out: Values = {}
    for sp in spreads:
        v = _spread_exit_value(sp, d, point, worst)
        if v is None:
            return None
        out[sp.side] = v
    return out


def _total_pnl(spreads: List[_Spread], values: Values) -> float:
    return sum(sp.entry_credit - values[sp.side] for sp in spreads)


def _values_at_level(spreads: List[_Spread], values: Values, level: float) -> Values:
    """組合單掛在門檻價被碰到時成交：總損益剛好等於 `level`。`values` 是已經越過門檻的那個時間點的各邊價
    值，從進場狀態(損益 0)往它線性內插到總損益 = level 的位置，再拆回各邊(報表一列 = 一邊)。加總一定
    等於 level，各邊怎麼拆只是分配，不影響整組損益。"""
    f = level / _total_pnl(spreads, values)
    return {sp.side: sp.entry_credit - f * (sp.entry_credit - values[sp.side]) for sp in spreads}


def _remaining_days(sp: _Spread, d: date) -> int:
    return (sp.expiration - d).days


def _evaluate(spreads: List[_Spread], rule: ExitRule, d: date, worst: bool = False) -> Optional[Values]:
    """這條規則今天有沒有觸發。觸發回傳各邊的平倉價值，沒觸發回傳 None。缺報價(理論上不該發生，
    `contract_series` 已經補過值)一律當作沒觸發，不讓回測因為一天的資料洞而崩潰。

    停利/停損看的是「整張組合單的淨價」(所有持倉的價差價值加總)，不是哪一腳自己：收 1 就掛 0.5 停利、
    掛 2 停損。"""
    if rule.kind == KIND_DTE:
        if min(_remaining_days(sp, d) for sp in spreads) <= rule.threshold:
            return _values_at(spreads, d, "close", worst)
        return None

    credit_total = sum(sp.entry_credit for sp in spreads)
    threshold = rule.threshold / 100.0 * credit_total if rule.unit == UNIT_CREDIT_PCT else rule.threshold
    is_take_profit = rule.kind == KIND_TAKE_PROFIT
    level = threshold if is_take_profit else -threshold

    def breached(pnl: float) -> bool:
        return pnl >= level if is_take_profit else pnl <= level

    values_close = _values_at(spreads, d, "close", worst)
    if values_close is None:
        return None
    if rule.fill == FILL_CLOSE:
        return values_close if breached(_total_pnl(spreads, values_close)) else None

    # 盤中觸價：開盤淨價已經越過門檻(跳空)，掛單在開盤成交；否則收盤淨價越過門檻，代表當天價格一定
    # 走過掛價(價格是連續的)，以掛價成交。
    values_open = _values_at(spreads, d, "open", worst)
    if values_open is not None and breached(_total_pnl(spreads, values_open)):
        return values_open
    if breached(_total_pnl(spreads, values_close)):
        return _values_at_level(spreads, values_close, level)
    return None


# ---------------------------------------------------------------------------- 主迴圈
def run_backtest(df: pd.DataFrame, chain: OptionChain, strategy: StrategySpec, config: RunConfig) -> List[Trade]:
    """只要逐筆交易的版本，見 `run_backtest_with_equity`。"""
    return run_backtest_with_equity(df, chain, strategy, config)[0]


def run_backtest_with_equity(
    df: pd.DataFrame, chain: OptionChain, strategy: StrategySpec, config: RunConfig,
) -> Tuple[List[Trade], List[Tuple[str, float]]]:
    """`df` 需要有 open/high/low/close 欄(標的每日開高低收)，index 是遞增排序的交易日期。`chain` 是
    同一段期間的真實選擇權鏈(`option_chain.OptionChain`)。只會走訪標的資料跟選擇權鏈資料都有的交易
    日(兩邊資料來源不同，日期可能有小出入)。策略要先通過 `spec.validate_strategy()`。

    回傳 (逐筆交易依出場順序, 逐日權益 [(日期, 每股毛損益)])。逐日權益 = 到當天收盤為止「已平倉損益 +
    未平倉部位的浮動損益」，每股單位、不含手續費(報表依口數/手續費即時換算，見 report.js)。未平倉部位用
    當天收盤報價按平倉價值計(跟出場用同一套 `_values_at`，`fill_price` 是 worst 就用極端價，所以是「現在
    平倉能拿到多少」的保守值)；回測結束時還沒平倉的部位不會出現在逐筆交易裡，但會反映在逐日權益上。"""
    entry = strategy.entry
    rules = {(rule.scope, rule.kind): rule for rule in strategy.exit_rules}
    r = config.risk_free_rate
    worst = config.fill_price == FILL_PRICE_WORST
    per_leg_charge = max(config.commission_rate * config.contracts, config.commission_min_per_leg)

    def row_commission(sp: _Spread) -> float:
        return 2 * (1 if sp.naked else 2) * per_leg_charge  # 價差 2 腳(短+長)、裸賣 1 腳，開倉+平倉各一次

    trades: List[Trade] = []
    equity: List[Tuple[str, float]] = []
    realized = 0.0        # 已平倉的每股毛損益累計
    unrealized = 0.0      # 目前持倉的浮動損益；某天缺報價就沿用前一天的值
    spreads: Dict[str, Optional[_Spread]] = {"put": None, "call": None}

    def log_close(sp: _Spread, d: date, exit_value: float, reason: str, fill_mode: str) -> None:
        nonlocal realized
        pnl = sp.entry_credit - exit_value
        realized += pnl
        pnl_usd = pnl * CONTRACT_MULTIPLIER * config.contracts
        commission = row_commission(sp)
        trades.append(Trade(
            side=sp.side, entry_date=sp.entry_date.strftime("%Y-%m-%d"), exit_date=d.strftime("%Y-%m-%d"),
            K_short=sp.K_short, K_long=sp.K_long,
            entry_credit=round(sp.entry_credit, 6), exit_value=round(exit_value, 6), pnl=round(pnl, 6),
            margin=round(sp.margin, 6), exit_reason=reason, fill_mode=fill_mode,
            contracts=config.contracts, pnl_usd=round(pnl_usd, 2), commission_usd=round(commission, 2),
            net_pnl_usd=round(pnl_usd - commission, 2),
        ))

    closes = df["close"].to_numpy(dtype=float).tolist()
    underlying_by_date = {ts.date(): c for ts, c in zip(df.index, closes)}
    dates = sorted(set(underlying_by_date) & set(chain.trading_dates))

    for d in dates:
        S = underlying_by_date[d]
        if not (S > 0):
            continue

        # 1. 整組規則
        held = [spreads[side] for side in SIDES if spreads[side] is not None]
        if held:
            for kind in KIND_EVAL_ORDER:
                rule = rules.get((SCOPE_GROUP, kind))
                if rule is None:
                    continue
                values = _evaluate(held, rule, d, worst)
                if values is not None:
                    fill_mode = FILL_CLOSE if kind == KIND_DTE else rule.fill
                    for sp in held:
                        log_close(sp, d, values[sp.side], f"{SCOPE_GROUP}_{kind}", fill_mode)
                        spreads[sp.side] = None
                    break

        # 2. 單邊規則
        for side in SIDES:
            sp = spreads[side]
            if sp is None:
                continue
            for kind in KIND_EVAL_ORDER:
                rule = rules.get((SCOPE_LEG, kind))
                if rule is None:
                    continue
                values = _evaluate([sp], rule, d, worst)
                if values is not None:
                    fill_mode = FILL_CLOSE if kind == KIND_DTE else rule.fill
                    log_close(sp, d, values[side], f"{SCOPE_LEG}_{kind}", fill_mode)
                    spreads[side] = None
                    if rule.action == ACTION_CLOSE_REOPEN:
                        new = open_spread(chain, S, side, entry, r, d, dates, worst)
                        pair = {**spreads, side: new}
                        if new is not None and _entry_conditions_pass(entry, pair["put"], pair["call"]):
                            spreads[side] = new
                    break

        # 3. 完全空手 -> 立刻重新進場(整組，兩邊都選得到、且滿足進場條件才進)
        if spreads["put"] is None and spreads["call"] is None:
            opened = _open_position(S, chain, entry, r, d, dates, worst)
            if opened is not None:
                spreads.update(opened)

        # 4. 今天新開的部位，記下「整組同時持有」的保證金(兩邊同時開的兩列共用同一個數字)
        held_now = [sp for sp in spreads.values() if sp is not None]
        if any(sp.margin is None for sp in held_now):
            group_margin = position_margin(held_now)
            for sp in held_now:
                if sp.margin is None:
                    sp.margin = group_margin

        # 5. 逐日權益：已平倉 + 未平倉浮動損益(全部平倉後浮動就是 0)
        if held_now:
            marks = _values_at(held_now, d, "close", worst)
            if marks is not None:
                unrealized = _total_pnl(held_now, marks)
        else:
            unrealized = 0.0
        equity.append((d.strftime("%Y-%m-%d"), round(realized + unrealized, 6)))

    return trades, equity
