"""
Iron Condor 機械化規則的合成回測引擎。

規則(對應這次討論定案的版本):
  - 30~45 DTE 進場(預設用中間值 entry_dte 天)，短腳選在 target_delta
  - 出場/調整三個觸發，哪個先到就出場:
      1. 剩餘天數 <= exit_dte(預設 10~20 的中間值)
      2. 浮動獲利 >= entry_credit * profit_target_pct(預設 50%)
      3. 浮動虧損 >= entry_credit * stop_loss_multiple(預設 1 倍)
  - 出場後不留空手，下一個交易日立刻用當天報價重新進場(對應「都沒有空手」)

v1 簡化，尚未做的事:
  - 不模擬美式提前履約
  - 不模擬恐慌期間的滑價/買賣價差擴大、也沒算手續費
  - 出場是整組一起平倉，沒有做「只滾動被測試那一腳」的單腳調整
  - 進場天期固定用 entry_dte 一個數字，沒有在 30~45 之間依實際到期日曆變動
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import black_scholes as bs


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


@dataclass
class Trade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    short_put: float
    long_put: float
    short_call: float
    long_call: float
    entry_credit: float
    exit_value: float
    pnl: float
    max_loss: float
    exit_reason: str


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


def _iron_condor_value(S: float, K_sp: float, K_lp: float, K_sc: float, K_lc: float, T: float, r: float, sigma: float) -> float:
    """賣短腳、買長腳的淨值：進場時 = 收到的淨信用；之後任何時點重算 = 現在要平倉的成本。"""
    return (
        bs.price(S, K_sp, T, r, sigma, "P") - bs.price(S, K_lp, T, r, sigma, "P")
        + bs.price(S, K_sc, T, r, sigma, "C") - bs.price(S, K_lc, T, r, sigma, "C")
    )


def _open_position(S: float, sigma: float, params: Params) -> dict:
    T = params.entry_dte / 365.0
    r = params.risk_free_rate
    width = max(params.strike_round, round(S * params.width_pct / params.strike_round) * params.strike_round)

    K_sp = _find_strike(S, T, r, sigma, params.target_delta, "P", params.strike_round)
    K_lp = K_sp - width
    K_sc = _find_strike(S, T, r, sigma, params.target_delta, "C", params.strike_round)
    K_lc = K_sc + width

    credit = _iron_condor_value(S, K_sp, K_lp, K_sc, K_lc, T, r, sigma)
    return {
        "K_sp": K_sp, "K_lp": K_lp, "K_sc": K_sc, "K_lc": K_lc,
        "width": width, "entry_credit": credit,
    }


def run_backtest(df: pd.DataFrame, params: Params) -> tuple[list[Trade], pd.Series]:
    """df 需要有 close/vix 兩欄，index 是遞增排序的交易日期。
    回傳 (逐筆交易紀錄, 逐日累計損益的權益曲線，同一種價格單位、未乘合約乘數)。
    """
    trades: list[Trade] = []
    equity = pd.Series(0.0, index=df.index)

    position: dict | None = None
    entry_date = None
    realized = 0.0
    r = params.risk_free_rate

    for d in df.index:
        S = float(df.loc[d, "close"])
        sigma = float(df.loc[d, "vix"]) / 100.0
        if S <= 0 or sigma <= 0:
            equity.loc[d] = realized
            continue

        if position is None:
            position = _open_position(S, sigma, params)
            entry_date = d
            equity.loc[d] = realized
            continue

        days_held = (d - entry_date).days
        remaining_dte = params.entry_dte - days_held
        T = max(remaining_dte, 1) / 365.0

        value_now = _iron_condor_value(
            S, position["K_sp"], position["K_lp"], position["K_sc"], position["K_lc"], T, r, sigma
        )
        floating_pnl = position["entry_credit"] - value_now
        max_loss = position["width"] - position["entry_credit"]

        exit_reason = None
        if remaining_dte <= params.exit_dte:
            exit_reason = "dte"
        elif floating_pnl >= params.profit_target_pct * position["entry_credit"]:
            exit_reason = "profit_target"
        elif floating_pnl <= -params.stop_loss_multiple * position["entry_credit"]:
            exit_reason = "stop_loss"

        if exit_reason:
            realized += floating_pnl
            trades.append(Trade(
                entry_date=entry_date, exit_date=d,
                short_put=position["K_sp"], long_put=position["K_lp"],
                short_call=position["K_sc"], long_call=position["K_lc"],
                entry_credit=position["entry_credit"], exit_value=value_now,
                pnl=floating_pnl, max_loss=max_loss, exit_reason=exit_reason,
            ))
            position = None
            entry_date = None
            equity.loc[d] = realized
        else:
            equity.loc[d] = realized + floating_pnl

    return trades, equity


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity - running_max
    return float(drawdown.min())


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
    }
