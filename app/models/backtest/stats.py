"""
從逐筆交易算一份精簡摘要，給策略清單顯示用。純標準庫(不 import pandas)。完整的績效分析(逐年損
益、回撤曲線、連續勝負…)是報表分頁的瀏覽器端 JS 在做的，這裡只算清單需要的幾個數字。
"""
from typing import Dict, List

from app.models.backtest.trade import Trade


def summarize(trades: List[Trade]) -> Dict:
    """淨損益/手續費/最大回撤都是美元，用每筆交易存的口數(pnl_usd 是照那個口數算的)。最大回撤是逐筆
    已實現淨損益依出場日期累加的曲線的最大回落，不是逐日 mark-to-market。"""
    ordered = sorted(trades, key=lambda t: t.exit_date)
    wins = sum(1 for t in ordered if t.net_pnl_usd > 0)

    cumulative = peak = max_drawdown = 0.0
    for t in ordered:
        cumulative += t.net_pnl_usd
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)

    return {
        "trades": len(ordered),
        "contracts": ordered[0].contracts if ordered else 0,
        "gross_usd": round(sum(t.pnl_usd for t in ordered), 2),
        "commission_usd": round(sum(t.commission_usd for t in ordered), 2),
        "net_usd": round(sum(t.net_pnl_usd for t in ordered), 2),
        "win_rate": (wins / len(ordered)) if ordered else 0.0,
        "max_drawdown_usd": round(max_drawdown, 2),
        "first_date": min(t.entry_date for t in ordered) if ordered else None,
        "last_date": ordered[-1].exit_date if ordered else None,
    }
