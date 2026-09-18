"""
Iron Condor 機械化規則的合成回測引擎，put 腳、call 腳各自獨立管理(各自有自己的
進場日/剩餘天期)，不綁在同一張整組單上。

規則:
  - 30~45 DTE 進場(預設用中間值 entry_dte 天)，兩腳短腳各自選在 target_delta
  - 每一腳每天檢查三個出場/滾動觸發，哪個先到就處理，處理完當天立刻用當天
    報價重新開一個新的同方向價差(不留空手，天期重置回 entry_dte):
      1. 剩餘天數 <= exit_dte：到期天數出場
      2. 浮動獲利 >= entry_credit * profit_target_pct(預設50%)：停利出場
      3. 浮動虧損 >= entry_credit * stop_loss_multiple(預設1倍)：不是整組平倉，
         只把「被測試」的那一腳滾動——平倉+立即重新開倉
  - 觸發第3點的滾動時，順便檢查另一腳(安全腳)：如果浮動獲利已經達到收到
    權利金的 harvest_threshold_pct 以上(代表已經很深價外、剩餘時間價值
    很少)，也一併滾動收割，賺到的權利金拿去貼補被測試那一腳的虧損。
    harvest_threshold_pct 必須設得比 profit_target_pct 低，不然安全腳會先
    被自己獨立的停利規則平倉重置，永遠沒機會累積到收割門檻——這不是理論
    猜測，是實測 0.8(高於預設停利0.5)跑12年收割次數=0之後才發現的。

停利/停損固定用「預先掛價」的邏輯——想成停利/停損單已經照目標價掛在市場
上被動等成交，不是每天只看收盤價才決定要不要出場：用當天開高低價判斷有
沒有盤中觸價，沒跳空的話假設剛好停在門檻價成交(掛價的精髓：精準停在目標
價，不會多賺也不會多賠)；跳空(開盤就已經越過門檻)的話用開盤價成交，因為
跳空沒辦法要求精準停在門檻上，見 _evaluate_exit。到期天數(DTE)觸發的出場
是行事曆決定的、不是被動成交，一律只看收盤價。

已知限制(還沒做的事):
  - 不模擬美式提前履約
  - 不模擬恐慌期間的滑價/買賣價差擴大(掛價成交假設可以精準拿到目標價，
    現實中大波動時價差會擴大、可能滑價)，也沒算滾動本身的執行成本
  - 兩腳用同一個波動率數字(VIX/VXN)定價，沒有偏斜(skew)
  - 進場天期固定用 entry_dte 一個數字，沒有在 30~45 之間依實際到期日曆變動
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import black_scholes as bs

# IBKR Pro Fixed 美股選擇權費率(2026-09查的官方頁面，月交易量<=10,000口那一階):
# USD 0.65/口，每筆訂單最低 USD 1.00——combo(多腳組合)單的這個最低收費是「每一腳分開算」，
# 不是整張combo單只收一次，這是小口數時手續費佔比會突然暴增的主因。來源:
# https://www.interactivebrokers.com/en/pricing/commissions-options.php
IBKR_RATE_PER_CONTRACT = 0.65
IBKR_MIN_PER_LEG = 1.00
CONTRACT_MULTIPLIER = 100  # 美股選擇權1口=100股
LEGS_PER_TRADE = 2  # 每筆Trade是一個價差(短腳+長腳)，不是整組iron condor(4腳)


@dataclass
class Params:
    entry_dte: int = 40
    exit_dte: int = 15
    target_delta: float = 0.16
    width_pct: float = 0.01
    strike_round: float = 1.0
    profit_target_pct: float = 0.5
    stop_loss_multiple: float = 1.0
    risk_free_rate: float = 0.04
    # 安全腳浮動獲利達到收到權利金的這個比例，就在被測試腳滾動的同時一併滾動收割。
    # 必須設得比 profit_target_pct 低，見上面 module docstring 的說明。
    harvest_threshold_pct: float = 0.3
    # 每筆交易的口數跟IBKR手續費費率，直接影響每一筆 Trade 存的 pnl_usd/commission_usd/
    # net_pnl_usd——不用等到CSV存完才後製套用，CSV裡每一筆就已經扣好手續費了。
    contracts: int = 1
    commission_rate: float = IBKR_RATE_PER_CONTRACT
    commission_min_per_leg: float = IBKR_MIN_PER_LEG


def _round_trip_commission(params: "Params") -> float:
    """一筆交易開倉+平倉的總手續費：LEGS_PER_TRADE(短腳+長腳)每一腳都套用
    max(rate*contracts, min_per_leg)，開倉、平倉各收一次所以乘2。"""
    per_leg_charge = max(params.commission_rate * params.contracts, params.commission_min_per_leg)
    return LEGS_PER_TRADE * 2 * per_leg_charge


def _find_strike(S: float, T: float, r: float, sigma: float, target_delta: float, right: str, strike_round: float) -> float:
    """二分搜尋找出 |delta| 最接近 target_delta 的履約價。"""
    lo, hi = S * 0.5, S * 1.5
    for _ in range(60):
        mid = (lo + hi) / 2
        d = bs.delta(S, mid, T, r, sigma, right)
        if right == "P":
            if -d > target_delta:
                hi = mid  # 太接近價內(|delta|太大)，履約價要往下修
            else:
                lo = mid
        else:
            if d > target_delta:
                lo = mid  # delta太大代表太接近價平，履約價要往上修
            else:
                hi = mid
    strike = (lo + hi) / 2
    return round(strike / strike_round) * strike_round


@dataclass
class Trade:
    """一筆只代表一腳(put或call)的價差，不是整組iron condor。"""
    side: str  # "put" 或 "call"
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    K_short: float
    K_long: float
    entry_credit: float
    exit_value: float
    pnl: float
    max_loss: float
    exit_reason: str  # "dte" / "profit_target" / "stop_loss_roll" / "harvest_roll"
    fill_mode: str  # "close"：到期天數出場，只看收盤價；"intraday"：掛價精準停在目標價成交，見 _evaluate_exit
    contracts: int
    pnl_usd: float           # pnl * CONTRACT_MULTIPLIER * contracts，換算成真實美元
    commission_usd: float    # 這一筆開倉+平倉共4腳次執行的IBKR手續費(combo每腳套用最低收費)
    net_pnl_usd: float       # pnl_usd - commission_usd，才是真正到手的淨損益


def _open_side(S: float, sigma: float, right: str, params: Params) -> dict:
    T = params.entry_dte / 365.0
    r = params.risk_free_rate
    width = max(params.strike_round, round(S * params.width_pct / params.strike_round) * params.strike_round)
    K_short = _find_strike(S, T, r, sigma, params.target_delta, right, params.strike_round)
    K_long = (K_short - width) if right == "P" else (K_short + width)
    credit = bs.price(S, K_short, T, r, sigma, right) - bs.price(S, K_long, T, r, sigma, right)
    return {"K_short": K_short, "K_long": K_long, "width": width, "entry_credit": credit, "right": right}


def _side_value(S: float, T: float, r: float, sigma: float, side: dict) -> float:
    return bs.price(S, side["K_short"], T, r, sigma, side["right"]) - bs.price(S, side["K_long"], T, r, sigma, side["right"])


def _evaluate_exit(side: dict, entry_date, d, S_open: float, S_high: float, S_low: float, S_close: float,
                    r: float, sigma: float, params: Params) -> tuple[int, float, float, str | None]:
    """回傳 (剩餘天數, 平倉值, 浮動損益, 出場原因或None)。

    到期天數觸發還是只看收盤價(這是行事曆決定的，不是被動成交)。停利/停損固定用
    「掛價」邏輯：用當天開高低價模擬停利/停損單已經照目標價掛著、被動成交——沒跳空
    (收盤前的最壞/最好價位在盤中出現，開盤還沒到門檻)就假設剛好精準停在門檻價成交；
    跳空(開盤就已經越過門檻)就用開盤價成交，不能要求跳空後還精準停在門檻上。"""
    days_held = (d - entry_date).days
    remaining = params.entry_dte - days_held
    T = max(remaining, 1) / 365.0
    entry_credit = side["entry_credit"]

    if remaining <= params.exit_dte:
        value_now = _side_value(S_close, T, r, sigma, side)
        return remaining, value_now, entry_credit - value_now, "dte"

    profit_threshold = params.profit_target_pct * entry_credit
    loss_threshold = -params.stop_loss_multiple * entry_credit

    right = side["right"]
    worst_S = S_low if right == "P" else S_high   # put跌破往下最痛，call漲破往上最痛
    best_S = S_high if right == "P" else S_low
    value_open = _side_value(S_open, T, r, sigma, side)
    value_worst = _side_value(worst_S, T, r, sigma, side)
    value_best = _side_value(best_S, T, r, sigma, side)
    floating_open = entry_credit - value_open
    floating_worst = entry_credit - value_worst
    floating_best = entry_credit - value_best

    if floating_open >= profit_threshold:
        return remaining, value_open, floating_open, "profit_target"
    if floating_best >= profit_threshold:
        return remaining, entry_credit - profit_threshold, profit_threshold, "profit_target"
    if floating_open <= loss_threshold:
        return remaining, value_open, floating_open, "stop_loss"
    if floating_worst <= loss_threshold:
        return remaining, entry_credit - loss_threshold, loss_threshold, "stop_loss"

    value_now = _side_value(S_close, T, r, sigma, side)
    return remaining, value_now, entry_credit - value_now, None


def run_backtest(df: pd.DataFrame, params: Params) -> tuple[list[Trade], pd.Series]:
    """put腳、call腳各自獨立管理：被測試那一腳觸發停損就滾動(不是整組平倉)，
    同時檢查安全腳夠不夠深價外，夠的話一併滾動收割。停利/停損固定用掛價邏輯精準
    停在目標價成交，見 module docstring 跟 _evaluate_exit。
    df 需要有 open/high/low/close/vix 欄，index 是遞增排序的交易日期。
    回傳 (逐筆交易紀錄, 逐日累計損益的權益曲線，同一種價格單位、未乘合約乘數)。
    """
    trades: list[Trade] = []
    equity = pd.Series(0.0, index=df.index)
    realized = 0.0
    r = params.risk_free_rate

    sides: dict[str, dict | None] = {"put": None, "call": None}
    entry_dates: dict[str, pd.Timestamp | None] = {"put": None, "call": None}

    def close_and_log(name: str, side: dict, entry_date, exit_date, value_now: float, floating: float, reason: str):
        nonlocal realized
        realized += floating
        pnl_usd = floating * CONTRACT_MULTIPLIER * params.contracts
        commission_usd = _round_trip_commission(params)
        # dte出場是行事曆決定的，永遠只看收盤價；其他出場都是掛價精準停在目標價成交(見 _evaluate_exit)。
        fill_mode = "close" if reason == "dte" else "intraday"
        trades.append(Trade(
            side=name, entry_date=entry_date, exit_date=exit_date,
            K_short=side["K_short"], K_long=side["K_long"],
            entry_credit=side["entry_credit"], exit_value=value_now,
            pnl=floating, max_loss=side["width"] - side["entry_credit"], exit_reason=reason,
            fill_mode=fill_mode, contracts=params.contracts, pnl_usd=pnl_usd, commission_usd=commission_usd,
            net_pnl_usd=pnl_usd - commission_usd,
        ))

    for d in df.index:
        S_open = float(df.loc[d, "open"])
        S_high = float(df.loc[d, "high"])
        S_low = float(df.loc[d, "low"])
        S = float(df.loc[d, "close"])
        sigma = float(df.loc[d, "vix"]) / 100.0
        if S <= 0 or sigma <= 0:
            equity.loc[d] = realized
            continue

        for name, right in (("put", "P"), ("call", "C")):
            if sides[name] is None:
                sides[name] = _open_side(S, sigma, right, params)
                entry_dates[name] = d

        info = {}
        for name in ("put", "call"):
            side = sides[name]
            remaining, value_now, floating, reason = _evaluate_exit(
                side, entry_dates[name], d, S_open, S_high, S_low, S, r, sigma, params
            )
            if reason == "stop_loss":
                reason = "stop_loss_roll"
            info[name] = {"remaining": remaining, "value": value_now, "floating": floating, "reason": reason}

        touched = set()
        rolled_for_stop = None
        for name in ("put", "call"):
            side = sides[name]
            f = info[name]
            reason = f["reason"]
            if reason:
                close_and_log(name, side, entry_dates[name], d, f["value"], f["floating"], reason)
                sides[name] = _open_side(S, sigma, side["right"], params)
                entry_dates[name] = d
                touched.add(name)
                if reason == "stop_loss_roll":
                    rolled_for_stop = name

        if rolled_for_stop is not None:
            other = "call" if rolled_for_stop == "put" else "put"
            if other not in touched:
                other_side = sides[other]
                f = info[other]
                if f["floating"] >= params.harvest_threshold_pct * other_side["entry_credit"]:
                    close_and_log(other, other_side, entry_dates[other], d, f["value"], f["floating"], "harvest_roll")
                    sides[other] = _open_side(S, sigma, other_side["right"], params)
                    entry_dates[other] = d
                    touched.add(other)

        put_floating_today = 0.0 if "put" in touched else info["put"]["floating"]
        call_floating_today = 0.0 if "call" in touched else info["call"]["floating"]
        equity.loc[d] = realized + put_floating_today + call_floating_today

    return trades, equity


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity - running_max
    return float(drawdown.min())


def commission_for_trades(n_trades: int, contracts: int,
                           rate_per_contract: float = IBKR_RATE_PER_CONTRACT,
                           min_per_leg: float = IBKR_MIN_PER_LEG) -> float:
    """整批交易的總手續費(美元)。每一腳都套用 max(rate*contracts, min_per_leg)，
    開倉、平倉各收一次，LEGS_PER_TRADE(短腳+長腳)乘2。"""
    per_leg_charge = max(rate_per_contract * contracts, min_per_leg)
    return n_trades * LEGS_PER_TRADE * 2 * per_leg_charge


def net_equity_curve(trades: list[Trade], contracts: int,
                      rate_per_contract: float = IBKR_RATE_PER_CONTRACT,
                      min_per_leg: float = IBKR_MIN_PER_LEG) -> pd.Series:
    """依出場日期排序，逐筆累加「扣掉IBKR手續費後」的美元損益，做成淨值曲線——
    可以直接丟給 max_drawdown()，量出來的回撤才是真正會發生在帳戶上的回撤，
    不是只看選擇權理論價格、完全沒算交易成本的回撤。"""
    commission_per_trade = LEGS_PER_TRADE * 2 * max(rate_per_contract * contracts, min_per_leg)
    ordered = sorted(trades, key=lambda t: t.exit_date)
    net = [t.pnl * CONTRACT_MULTIPLIER * contracts - commission_per_trade for t in ordered]
    return pd.Series(net, index=pd.DatetimeIndex([t.exit_date for t in ordered])).cumsum()


def net_summary(trades: list[Trade], contracts: int,
                rate_per_contract: float = IBKR_RATE_PER_CONTRACT,
                min_per_leg: float = IBKR_MIN_PER_LEG) -> dict:
    """把 summarize() 的每股權利金單位換算成美元(乘 CONTRACT_MULTIPLIER * contracts)，
    扣掉 IBKR combo 手續費，回傳含毛利/手續費/淨利/淨報酬率/淨最大回撤的完整數字。"""
    gross_pnl_per_share = sum(t.pnl for t in trades)
    gross_usd = gross_pnl_per_share * CONTRACT_MULTIPLIER * contracts
    commission_usd = commission_for_trades(len(trades), contracts, rate_per_contract, min_per_leg)
    net_usd = gross_usd - commission_usd
    avg_margin_usd = (sum(t.max_loss for t in trades) / len(trades)) * CONTRACT_MULTIPLIER * contracts
    net_curve = net_equity_curve(trades, contracts, rate_per_contract, min_per_leg)
    return {
        "contracts": contracts,
        "n_trades": len(trades),
        "gross_usd": gross_usd,
        "commission_usd": commission_usd,
        "net_usd": net_usd,
        "commission_drag_pct": (commission_usd / gross_usd) if gross_usd else float("nan"),
        "net_return_on_margin": (net_usd / avg_margin_usd) if avg_margin_usd else float("nan"),
        "net_max_drawdown_usd": max_drawdown(net_curve),
    }


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {}
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
    total_pnl = sum(t.pnl for t in trades)
    avg_margin = sum(t.max_loss for t in trades) / n
    worst = min(trades, key=lambda t: t.pnl)
    gross_usd = sum(t.pnl_usd for t in trades)
    commission_usd = sum(t.commission_usd for t in trades)
    net_usd = sum(t.net_pnl_usd for t in trades)
    return {
        "trades": n,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": win_rate * avg_win + (1 - win_rate) * avg_loss,
        "total_pnl": total_pnl,
        "avg_margin": avg_margin,
        "return_on_margin": (total_pnl / avg_margin) if avg_margin else float("nan"),
        "worst_trade": worst,
        "contracts": trades[0].contracts,
        "gross_usd": gross_usd,
        "commission_usd": commission_usd,
        "net_usd": net_usd,
        "commission_drag_pct": (commission_usd / gross_usd) if gross_usd else float("nan"),
    }
