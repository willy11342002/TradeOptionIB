"""
選擇權賣方策略參數化回測引擎(Iron Condor/裸雙賣/Jade Lizard/Twisted Sister)：進場規則
(`EntrySpec`) + 出場規則清單(`ExitRule`)，輸出「一列 = 一邊(價差或單腳裸賣)」的逐筆交易(`Trade`)。
四種策略只差在哪一邊有買保護腳(`spec.PROTECTED_SIDES`)，裸賣的那一邊沒有長腳，價值就是短腳自己的價格。價格是合成的：Black-Scholes(`app/services/black_scholes.py`)+
歷史隱含波動率指數(VIX/VXN/RVX)當 sigma(可用 `RunConfig.atm_ratio`/`skew` 調整 ATM 水位與偏斜，見 `_Vol`)，不是真實選擇權報價，也不模擬滑價/買賣價差/美式提前
履約——只適合看策略機制的相對差異，不是報酬率預測。用隱含波動率指數而不是已實現波動率，是為
了讓賣方長期的風險溢酬(VRP)保留在合成價格裡，見 `market_data.py`。

*** 這支檔案 import 了 pandas，只能在使用者按下「執行回測」之後才 import(見 spec.py 開頭) ***

流程(每個交易日，`df` 需要有 open/high/low/close/vix 欄、DatetimeIndex 遞增)：
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

成交模式(每條停利/停損規則各自決定)：
- 收盤價：只看收盤價，觸發就用收盤價成交。
- 盤中觸價：用當天開/高/低/收四個取樣點各自重新定價，模擬「掛著停利/停損單被動成交」——開盤就已
  經越過門檻(跳空)用開盤價成交；否則高/低任一取樣點越過就假設剛好停在門檻價成交；只有收盤越過用
  收盤價成交。單邊價差對價格單調，這個判斷跟舊回測引擎完全等價；整組(put 邊+call 邊合計)沒有日內路
  徑資料，只能用這四個取樣點近似，門檻價成交時會在開盤價到越過的極端價之間二分搜尋出剛好等於門檻的
  價格，再拆算兩邊各自的平倉價值。
"""
import math
import operator
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.models.backtest.spec import (
    ACTION_CLOSE_REOPEN, COMBINE_AND, COMBINE_OR, FILL_CLOSE, KIND_DTE, KIND_EVAL_ORDER,
    KIND_TAKE_PROFIT, METRIC_DELTA, METRIC_DISTANCE_PCT, METRIC_PREMIUM, PROTECTED_SIDES, SCOPE_GROUP,
    SCOPE_LEG, STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK, STRIKE_STEP, UNIT_CREDIT_PCT, WIDTH_USD,
    EntrySpec, ExitRule, RunConfig, StrategySpec, snap_width,
)
from app.models.backtest.trade import Trade
from app.services import black_scholes as bs

CONTRACT_MULTIPLIER = 100  # 美股選擇權 1 口 = 100 股
MIN_VOL = 0.02             # 偏斜把遠價外 call 的波動率壓到接近 0 或負數時的下限(2%)
SIDES = ("put", "call")

_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


@dataclass
class _Spread:
    """一邊的部位：有長腳就是價差，K_long 是 None 就是單腳裸賣(此時 width 是 0，entry_credit 就是短腳的價格)。"""
    side: str
    K_short: float
    K_long: Optional[float]
    entry_date: pd.Timestamp
    entry_S: float
    entry_credit: float
    width: float
    margin: Optional[float] = None   # 開倉當天收盤時，整組同時持有的部位的保證金(每股)；當天收盤前是 None

    @property
    def naked(self) -> bool:
        return self.K_long is None


@dataclass(frozen=True)
class _Vol:
    """當天的波動率設定：每個履約價的隱含波動率 = atm × (1 − skew × ln(K/S))(見 `spec.SKEW_MAX` 附近的說明)。
    skew=0 時所有履約價一樣，跟加偏斜參數之前的引擎完全等價。用當下的現價 S 當基準(sticky-moneyness：
    現價變了，偏斜曲線跟著移動)。"""
    atm: float
    skew: float

    def at(self, S: float, K: float) -> float:
        if self.skew == 0 or S <= 0 or K <= 0:
            return self.atm
        return max(self.atm * (1.0 - self.skew * math.log(K / S)), MIN_VOL)


def _price(side: str, S: float, K: float, T: float, r: float, vol: _Vol) -> float:
    return bs.price(side == "call", S, K, r, T, vol.at(S, K))


def _spread_value(sp: _Spread, S: float, T: float, r: float, vol: _Vol) -> float:
    """賣短腳、買長腳(裸賣就沒有長腳)的淨值：進場時 = 收到的淨信用；之後任何時點重算 = 現在要平倉的成本。"""
    value = _price(sp.side, S, sp.K_short, T, r, vol)
    return value if sp.naked else value - _price(sp.side, S, sp.K_long, T, r, vol)


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
def _otm_strikes(S: float, side: str):
    """由近到遠列舉 OTM 履約價格點(put 在現價下方、call 在現價上方)，範圍限制在現價的 50%~150%。"""
    if side == "put":
        k = math.floor(S / STRIKE_STEP) * STRIKE_STEP
        if k >= S:
            k -= STRIKE_STEP
        while k >= max(S * 0.5, STRIKE_STEP):
            yield k
            k -= STRIKE_STEP
    else:
        k = math.ceil(S / STRIKE_STEP) * STRIKE_STEP
        if k <= S:
            k += STRIKE_STEP
        while k <= S * 1.5:
            yield k
            k += STRIKE_STEP


def _metric_value(metric: str, S: float, K: float, side: str, T: float, r: float, vol: _Vol) -> Optional[float]:
    if metric == METRIC_DELTA:
        d = bs.delta(side == "call", S, K, r, T, vol.at(S, K))
        return abs(d) if d is not None else None
    if metric == METRIC_DISTANCE_PCT:
        return ((K - S) if side == "call" else (S - K)) / S * 100.0
    if metric == METRIC_PREMIUM:
        return _price(side, S, K, T, r, vol)
    raise ValueError(f"不支援的指標: {metric}")


def _conditions_pass(entry: EntrySpec, S: float, K: float, side: str, T: float, r: float, vol: _Vol) -> bool:
    selector = entry.short
    for c in selector.conditions:
        v = _metric_value(c.metric, S, K, side, T, r, vol)
        ok = v is not None and _OPS[c.op](v, c.value)
        if selector.combine == COMBINE_AND and not ok:
            return False
        if selector.combine == COMBINE_OR and ok:
            return True
    return selector.combine == COMBINE_AND


def open_spread(S: float, vol: _Vol, side: str, entry: EntrySpec, r: float, entry_date: pd.Timestamp) -> Optional[_Spread]:
    """依進場規則開一邊的部位，找不到符合條件的短腳履約價、或算出的信用不是正的就回傳 None。

    短腳：在 OTM 履約價格點上由近到遠找，取第一個通過條件的(= 通過者中離現價最近、權利金最高)。
    長腳：只有這一邊有保護腳才買(`spec.PROTECTED_SIDES`)，距離短腳固定寬度(美元，或現價的百分比、換算到
    1 美元格點、最小 1 格，見 `spec.snap_width`)，往價外方向；裸賣的那一邊沒有長腳。"""
    T = entry.dte / 365.0
    K_short = None
    for k in _otm_strikes(S, side):
        if _conditions_pass(entry, S, k, side, T, r, vol):
            K_short = k
            break
    if K_short is None:
        return None

    K_long: Optional[float] = None
    width = 0.0
    credit = _price(side, S, K_short, T, r, vol)
    if side in PROTECTED_SIDES[entry.kind]:
        raw_width = entry.long.width if entry.long.width_unit == WIDTH_USD else S * entry.long.width / 100.0
        width = snap_width(raw_width)
        K_long = K_short - width if side == "put" else K_short + width
        if K_long <= 0:
            return None
        credit -= _price(side, S, K_long, T, r, vol)
    if credit <= 0:
        return None
    return _Spread(side=side, K_short=K_short, K_long=K_long, entry_date=entry_date, entry_S=S,
                   entry_credit=credit, width=width)


def _entry_conditions_pass(entry: EntrySpec, put: Optional[_Spread], call: Optional[_Spread]) -> bool:
    """進場條件(`spec.EntryConditions`)。目前只有「無單邊風險」：總權利金 >= 保護價差的寬度，只對 Jade
    Lizard/Twisted Sister 有意義。兩邊沒湊齊(其中一邊是空的)就不檢查——湊不成完整部位，沒有「總權利金」可比。"""
    if entry.kind not in STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK or not entry.conditions.no_single_side_risk:
        return True
    if put is None or call is None:
        return True
    protected = put if put.side in PROTECTED_SIDES[entry.kind] else call
    return put.entry_credit + call.entry_credit >= protected.width - 1e-9


def _open_position(S: float, vol: _Vol, entry: EntrySpec, r: float, d: pd.Timestamp) -> Optional[Dict[str, _Spread]]:
    """開整組(put 邊+call 邊)。任何一邊選不到、或不滿足進場條件就回傳 None(整個不進場)。"""
    put = open_spread(S, vol, "put", entry, r, d)
    call = open_spread(S, vol, "call", entry, r, d) if put is not None else None
    if put is None or call is None or not _entry_conditions_pass(entry, put, call):
        return None
    return {"put": put, "call": call}


# ---------------------------------------------------------------------------- 出場：判斷觸發
Prices = Tuple[float, float, float, float]  # 當天 (開, 高, 低, 收)
Values = Dict[str, float]                   # side -> 平倉時的價差價值


def _remaining_days(sp: _Spread, d: pd.Timestamp, entry_dte: int) -> int:
    return entry_dte - (d - sp.entry_date).days


def _values_at(spreads: List[_Spread], S: float, d: pd.Timestamp, r: float, vol: _Vol, entry_dte: int) -> Values:
    return {
        sp.side: _spread_value(sp, S, max(_remaining_days(sp, d, entry_dte), 1) / 365.0, r, vol)
        for sp in spreads
    }


def _total_pnl(spreads: List[_Spread], values: Values) -> float:
    return sum(sp.entry_credit - values[sp.side] for sp in spreads)


def _evaluate(spreads: List[_Spread], rule: ExitRule, d: pd.Timestamp, prices: Prices,
              r: float, vol: _Vol, entry_dte: int) -> Optional[Values]:
    """這條規則今天有沒有觸發。觸發回傳各邊的平倉價值，沒觸發回傳 None。"""
    S_open, S_high, S_low, S_close = prices

    if rule.kind == KIND_DTE:
        # 整組範圍：只要最近到期的那一邊剩餘天數到了就整組出場。
        if min(_remaining_days(sp, d, entry_dte) for sp in spreads) <= rule.threshold:
            return _values_at(spreads, S_close, d, r, vol, entry_dte)
        return None

    credit_total = sum(sp.entry_credit for sp in spreads)
    threshold = rule.threshold / 100.0 * credit_total if rule.unit == UNIT_CREDIT_PCT else rule.threshold
    is_take_profit = rule.kind == KIND_TAKE_PROFIT
    level = threshold if is_take_profit else -threshold  # 損益達到這個水位就觸發(停損是負的)

    def breached(pnl: float) -> bool:
        return pnl >= level if is_take_profit else pnl <= level

    def pnl_at(S: float) -> Tuple[float, Values]:
        values = _values_at(spreads, S, d, r, vol, entry_dte)
        return _total_pnl(spreads, values), values

    if rule.fill == FILL_CLOSE:
        pnl, values = pnl_at(S_close)
        return values if breached(pnl) else None

    # 盤中觸價：見模組開頭說明。
    pnl_open, values_open = pnl_at(S_open)
    if breached(pnl_open):
        return values_open

    extremes = [(S, pnl) for S, pnl in ((S_low, pnl_at(S_low)[0]), (S_high, pnl_at(S_high)[0])) if breached(pnl)]
    if extremes:
        # 高低都越過(整組才可能)就取離開盤價較近的那個，價格路徑比較可能先碰到它。
        S_extreme = min(extremes, key=lambda e: abs(e[0] - S_open))[0]
        if len(spreads) == 1:
            # 單邊價差對價格單調，剛好停在門檻價成交，平倉價值可以直接由門檻反推。
            return {spreads[0].side: spreads[0].entry_credit - level}
        lo, hi = S_open, S_extreme
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if breached(pnl_at(mid)[0]):
                hi = mid
            else:
                lo = mid
        return pnl_at(hi)[1]

    pnl_close, values_close = pnl_at(S_close)
    return values_close if breached(pnl_close) else None


# ---------------------------------------------------------------------------- 主迴圈
def run_backtest(df: pd.DataFrame, strategy: StrategySpec, config: RunConfig) -> List[Trade]:
    """df 需要有 open/high/low/close/vix 欄(vix 是波動率指數收盤，20.0 代表 20%)，index 是遞增
    排序的交易日期。回傳逐筆交易(依出場順序)。策略要先通過 `spec.validate_strategy()`。"""
    entry = strategy.entry
    rules = {(rule.scope, rule.kind): rule for rule in strategy.exit_rules}
    r = config.risk_free_rate
    per_leg_charge = max(config.commission_rate * config.contracts, config.commission_min_per_leg)

    def row_commission(sp: _Spread) -> float:
        return 2 * (1 if sp.naked else 2) * per_leg_charge  # 價差 2 腳(短+長)、裸賣 1 腳，開倉+平倉各一次

    trades: List[Trade] = []
    spreads: Dict[str, Optional[_Spread]] = {"put": None, "call": None}

    def log_close(sp: _Spread, d: pd.Timestamp, exit_value: float, reason: str, fill_mode: str) -> None:
        pnl = sp.entry_credit - exit_value
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

    index = df.index
    # tolist() 轉成純 Python float：交易紀錄要直接存 JSON，不要夾帶 numpy 型別。
    opens, highs, lows, closes = (df[c].to_numpy(dtype=float).tolist() for c in ("open", "high", "low", "close"))
    vols = df["vix"].to_numpy(dtype=float).tolist()

    for i in range(len(index)):
        d = index[i]
        S = closes[i]
        atm = vols[i] / 100.0 * config.atm_ratio
        if not (S > 0 and atm > 0 and opens[i] > 0 and highs[i] > 0 and lows[i] > 0):
            continue
        vol = _Vol(atm, config.skew)
        prices: Prices = (opens[i], highs[i], lows[i], S)

        # 1. 整組規則
        held = [spreads[side] for side in SIDES if spreads[side] is not None]
        if held:
            for kind in KIND_EVAL_ORDER:
                rule = rules.get((SCOPE_GROUP, kind))
                if rule is None:
                    continue
                values = _evaluate(held, rule, d, prices, r, vol, entry.dte)
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
                values = _evaluate([sp], rule, d, prices, r, vol, entry.dte)
                if values is not None:
                    fill_mode = FILL_CLOSE if kind == KIND_DTE else rule.fill
                    log_close(sp, d, values[side], f"{SCOPE_LEG}_{kind}", fill_mode)
                    spreads[side] = None
                    if rule.action == ACTION_CLOSE_REOPEN:
                        new = open_spread(S, vol, side, entry, r, d)
                        pair = {**spreads, side: new}
                        if new is not None and _entry_conditions_pass(entry, pair["put"], pair["call"]):
                            spreads[side] = new
                    break

        # 3. 完全空手 -> 立刻重新進場(整組，兩邊都選得到、且滿足進場條件才進)
        if spreads["put"] is None and spreads["call"] is None:
            opened = _open_position(S, vol, entry, r, d)
            if opened is not None:
                spreads.update(opened)

        # 4. 今天新開的部位，記下「整組同時持有」的保證金(兩邊同時開的兩列共用同一個數字)
        held_now = [sp for sp in spreads.values() if sp is not None]
        if any(sp.margin is None for sp in held_now):
            group_margin = position_margin(held_now)
            for sp in held_now:
                if sp.margin is None:
                    sp.margin = group_margin

    return trades
