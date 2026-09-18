"""
跑 Iron Condor 合成回測：用歷史 VIX 當隱含波動率的代理反推 Black-Scholes 價格，
驗證機械化規則(30-45 DTE 進場、10-20 DTE 出場、50%停利、可調停損倍數)的期望值
跟最大回撤。已知限制(不模擬滑價/提前履約/單腳調整)見 engine.py 開頭的說明。

跑法:
    uv run python -m scripts.options_backtest.run --ticker SPY --start 2015-01-01 --end 2025-01-01
    uv run python -m scripts.options_backtest.run --ticker SPY --stop-loss-multiple 2.0
    uv run python -m scripts.options_backtest.run --ticker SPY --csv-out trades.csv
"""
from __future__ import annotations

import argparse

from . import data, engine


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2025-01-01")
    parser.add_argument("--entry-dte", type=int, default=40, help="30~45的中間值")
    parser.add_argument("--exit-dte", type=int, default=15, help="10~20的中間值")
    parser.add_argument("--target-delta", type=float, default=0.16)
    parser.add_argument("--width-pct", type=float, default=0.01, help="價差寬度=現價*這個比例")
    parser.add_argument("--profit-target-pct", type=float, default=0.5)
    parser.add_argument("--stop-loss-multiple", type=float, default=1.0)
    parser.add_argument("--risk-free-rate", type=float, default=0.04)
    parser.add_argument("--csv-out", default="", help="把逐筆交易紀錄存成csv，方便自己另外檢查")
    args = parser.parse_args()

    df = data.load_underlying_and_vix(args.ticker, args.start, args.end)
    params = engine.Params(
        entry_dte=args.entry_dte,
        exit_dte=args.exit_dte,
        target_delta=args.target_delta,
        width_pct=args.width_pct,
        profit_target_pct=args.profit_target_pct,
        stop_loss_multiple=args.stop_loss_multiple,
        risk_free_rate=args.risk_free_rate,
    )
    trades, equity = engine.run_backtest(df, params)
    stats = engine.summarize(trades)

    print(f"標的: {args.ticker}  期間: {args.start} ~ {args.end}  資料筆數: {len(df)}")
    print(f"進場DTE={params.entry_dte}  出場DTE={params.exit_dte}  短腳delta={params.target_delta}  "
          f"寬度%={params.width_pct}  停利={params.profit_target_pct:.0%}  停損倍數={params.stop_loss_multiple}")
    print("-" * 70)
    if not trades:
        print("沒有任何交易，檢查資料範圍或參數是否合理。")
        return

    print(f"交易次數: {stats['trades']}")
    print(f"勝率: {stats['win_rate']:.1%}")
    print(f"平均獲利: {stats['avg_win']:.3f}   平均虧損: {stats['avg_loss']:.3f}")
    print(f"單筆期望值: {stats['expectancy']:.3f}")
    print(f"總損益(選擇權價格單位，未乘合約乘數): {stats['total_pnl']:.3f}")
    print(f"平均保證金(最大虧損): {stats['avg_margin']:.3f}")
    print(f"總報酬率(對平均保證金): {stats['return_on_margin']:.1%}")
    print(f"最大回撤(逐日mark-to-market權益曲線): {engine.max_drawdown(equity):.3f}")

    worst = stats["worst_trade"]
    print(f"最慘的一筆: {worst.entry_date.date()} ~ {worst.exit_date.date()}  "
          f"pnl={worst.pnl:.3f}  reason={worst.exit_reason}")

    stop_losses = [t for t in trades if t.exit_reason == "stop_loss"]
    if stop_losses:
        worst5 = sorted(stop_losses, key=lambda t: t.pnl)[:5]
        print("\n觸發停損的交易裡最慘的5筆(可以拿日期去對照2018/2/2020/3這類尾部事件):")
        for t in worst5:
            print(f"  {t.entry_date.date()} ~ {t.exit_date.date()}  pnl={t.pnl:.3f}")

    if args.csv_out:
        import pandas as pd
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.csv_out, index=False)
        print(f"\n已輸出逐筆交易紀錄: {args.csv_out}")


if __name__ == "__main__":
    main()
