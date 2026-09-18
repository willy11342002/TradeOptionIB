"""
跑 Iron Condor 合成回測：用歷史 VIX 當隱含波動率的代理反推 Black-Scholes 價格，
驗證機械化規則(30-45 DTE 進場、10-20 DTE 出場、50%停利、可調停損倍數)的期望值
跟最大回撤。已知限制(不模擬滑價/提前履約/單腳調整)見 engine.py 開頭的說明。

跑法:
    uv run python -m scripts.options_backtest.run --ticker SPY --start 2015-01-01 --end 2025-01-01
    uv run python -m scripts.options_backtest.run --ticker SPY --stop-loss-multiple 2.0
    uv run python -m scripts.options_backtest.run --ticker SPY --csv-out trades.csv
    uv run python -m scripts.options_backtest.run --ticker SPY --rolling --csv-out trades_rolling.csv
        (--rolling: 被測試那一腳觸發停損時改成滾動那一腳，不是整組平倉；
         同時檢查安全腳有沒有深到可以順便滾動收權利金，見 engine.py 說明)
    uv run python -m scripts.options_backtest.run --ticker SPY --rolling --intraday-fills
        (--intraday-fills: 只有搭配 --rolling 才有效，停利/停損改用當天開高低價
         模擬盤中掛單被動成交，沒跳空就精準停在門檻價，跳空就用開盤價，見 engine.py)
    uv run python -m scripts.options_backtest.run --ticker SPY --rolling --intraday-fills --contracts 1,5,10
        (毛利/手續費/淨利/淨報酬率/淨最大回撤，每個口數各一行——
         combo多腳單的最低收費是每一腳分開算，小口數時手續費佔比會遠高於大口數)
    uv run python -m scripts.options_backtest.run --ticker SPY --rolling --intraday-fills --verbose
        (--verbose 才印勝率/盈虧比/最慘交易這些細節，預設只印精簡的口數比較表)
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
    parser.add_argument("--rolling", action="store_true",
                         help="啟用單腳滾動：被測試那一腳觸發停損時滾動而非整組平倉，並檢查安全腳能否順便收割")
    parser.add_argument("--harvest-threshold-pct", type=float, default=0.3,
                         help="只有 --rolling 有效：安全腳浮動獲利達到收到權利金的這個比例，就一併滾動收割"
                              "(必須比 --profit-target-pct 低，不然安全腳會先被自己的停利規則平倉重置，永遠收割不到)")
    parser.add_argument("--intraday-fills", action="store_true",
                         help="只有 --rolling 有效：停利/停損改用當天開高低價模擬盤中掛單被動成交"
                              "(沒跳空精準停在門檻價，跳空用開盤價)，不加這個旗標就跟以前一樣只看收盤價")
    parser.add_argument("--csv-out", default="", help="把逐筆交易紀錄存成csv，方便自己另外檢查")
    parser.add_argument("--contracts", default="1,5,10",
                         help="逗號分隔的口數清單，第一個數字是CSV裡每筆pnl_usd/commission_usd/net_pnl_usd用的口數，"
                              "全部數字都會列進毛利/手續費/淨利比較表，例如 1,5,10")
    parser.add_argument("--commission-rate", type=float, default=engine.IBKR_RATE_PER_CONTRACT,
                         help="每口每腳手續費(美元)，預設IBKR Pro Fixed <=10,000口那一階的0.65")
    parser.add_argument("--commission-min", type=float, default=engine.IBKR_MIN_PER_LEG,
                         help="combo單每一腳的最低收費(美元)，預設IBKR的1.00")
    parser.add_argument("--verbose", action="store_true", help="印出勝率/盈虧比/最慘交易/滾動次數等細節，預設不印")
    args = parser.parse_args()

    if args.intraday_fills and not args.rolling:
        print("[警告] --intraday-fills 只有 --rolling 有效，v1(整組平倉)目前還是只看收盤價，這個旗標會被忽略。")

    contract_list = [int(x) for x in args.contracts.split(",")]
    csv_contracts = contract_list[0]  # 每筆Trade/LegTrade裡的pnl_usd/commission_usd用這個口數算

    df = data.load_underlying_and_vix(args.ticker, args.start, args.end)
    params = engine.Params(
        entry_dte=args.entry_dte,
        exit_dte=args.exit_dte,
        target_delta=args.target_delta,
        width_pct=args.width_pct,
        profit_target_pct=args.profit_target_pct,
        stop_loss_multiple=args.stop_loss_multiple,
        risk_free_rate=args.risk_free_rate,
        harvest_threshold_pct=args.harvest_threshold_pct,
        intraday_fills=args.intraday_fills,
        contracts=csv_contracts,
        commission_rate=args.commission_rate,
        commission_min_per_leg=args.commission_min,
    )
    if args.rolling:
        trades, equity = engine.run_backtest_rolling(df, params)
    else:
        trades, equity = engine.run_backtest(df, params)
    stats = engine.summarize(trades)

    mode = ('單腳滾動' if args.rolling else '整組平倉(v1)') + ('+盤中觸價' if (args.rolling and args.intraday_fills) else '')
    print(f"{args.ticker}  {args.start}~{args.end}  {mode}  {stats['trades']}筆  勝率{stats['win_rate']:.1%}")
    if not trades:
        print("沒有任何交易，檢查資料範圍或參數是否合理。")
        return

    legs_per_trade = 2 if args.rolling else 4
    print(f"\n{'口數':>4} {'毛利':>12} {'手續費':>10} {'淨利':>12} {'淨報酬率':>9} {'淨最大回撤':>11}")
    for c in contract_list:
        ns = engine.net_summary(trades, legs_per_trade, c, args.commission_rate, args.commission_min)
        marker = " ←CSV" if c == csv_contracts else ""
        print(f"{ns['contracts']:>3}口 ${ns['gross_usd']:>10,.0f} ${ns['commission_usd']:>8,.0f} "
              f"${ns['net_usd']:>10,.0f} {ns['net_return_on_margin']:>8.1%} ${ns['net_max_drawdown_usd']:>9,.0f}{marker}")

    if args.verbose:
        print(f"\n平均獲利: {stats['avg_win']:.3f}   平均虧損: {stats['avg_loss']:.3f}   單筆期望值(每股): {stats['expectancy']:.3f}")
        worst = stats["worst_trade"]
        print(f"最慘的一筆: {worst.entry_date.date()} ~ {worst.exit_date.date()}  pnl={worst.pnl:.3f}  reason={worst.exit_reason}")

        stop_reason = "stop_loss_roll" if args.rolling else "stop_loss"
        stop_losses = [t for t in trades if t.exit_reason == stop_reason]
        if stop_losses:
            worst5 = sorted(stop_losses, key=lambda t: t.pnl)[:5]
            print(f"\n觸發{'滾動' if args.rolling else '停損'}最慘的5筆:")
            for t in worst5:
                side = f"[{t.side}] " if args.rolling else ""
                print(f"  {side}{t.entry_date.date()} ~ {t.exit_date.date()}  pnl={t.pnl:.3f}")

        if args.rolling:
            harvests = [t for t in trades if t.exit_reason == "harvest_roll"]
            print(f"\n被測試腳滾動次數: {len(stop_losses)}   安全腳收割滾動次數: {len(harvests)}")
            if harvests:
                print(f"收割滾動平均每次多收: {sum(t.pnl for t in harvests) / len(harvests):.3f}")

    if args.csv_out:
        import pandas as pd
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.csv_out, index=False)
        print(f"\n已輸出逐筆交易紀錄: {args.csv_out}(每筆已含 pnl_usd/commission_usd/net_pnl_usd，{csv_contracts}口)")


if __name__ == "__main__":
    main()
