"""
一次跑完「直接平倉 vs 單腳滾動」幾種情境，把逐筆交易 CSV 存到專案裡、並產出一份
資料已經內嵌好的獨立 HTML 報表，雙擊就能看，不用在 dashboard 手動選 CSV。

跑法:
    uv run python -m scripts.options_backtest.report --ticker SPY
    uv run python -m scripts.options_backtest.report --ticker SPY --stop-loss-multiple 2.0 --start 2018-01-01

輸出在 scripts/options_backtest/output/<ticker>/ (已 gitignore，隨時可重跑)：
    report.html  內嵌全部資料的獨立報表
    *.csv        各情境的逐筆交易、標的每日收盤價(也可以手動丟進原本的 dashboard)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import data, engine

HERE = Path(__file__).parent
TEMPLATE = HERE / "backtest_dashboard.html"
OUTPUT_DIR = HERE / "output"
MARKER = "<!--EMBEDDED_REPORT-->"


def _to_csv_text(trades) -> str:
    return pd.DataFrame([t.__dict__ for t in trades]).to_csv(index=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2025-01-01")
    parser.add_argument("--stop-loss-multiple", type=float, default=1.0)
    parser.add_argument("--contracts", type=int, default=1, help="CSV 裡每筆 pnl_usd 用的口數(dashboard 的口數選單會即時重算)")
    args = parser.parse_args()

    df = data.load_underlying_and_vix(args.ticker, args.start, args.end)

    def params(**overrides) -> engine.Params:
        return engine.Params(stop_loss_multiple=args.stop_loss_multiple, contracts=args.contracts, **overrides)

    tag = f"{args.ticker.lower()}_sl{args.stop_loss_multiple:g}x"
    scenarios = {
        f"{tag}_直接平倉.csv": engine.run_backtest(df, params())[0],
        f"{tag}_滾動_收盤價成交.csv": engine.run_backtest_rolling(df, params())[0],
        f"{tag}_滾動_盤中觸價成交.csv": engine.run_backtest_rolling(df, params(intraday_fills=True))[0],
    }

    out_dir = OUTPUT_DIR / args.ticker.upper()
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_texts = {}
    for name, trades in scenarios.items():
        text = _to_csv_text(trades)
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
        csv_texts[name] = text
        print(f"{name}: {len(trades)}筆")

    bench_name = f"{args.ticker.lower()}_benchmark.csv"
    bench_text = df[["close"]].rename_axis("date").to_csv()
    (out_dir / bench_name).write_text(bench_text, encoding="utf-8", newline="\n")

    embedded = {"datasets": csv_texts, "benchmark": {"name": bench_name, "text": bench_text}, "default": next(iter(csv_texts))}
    # "</" 換掉，避免資料裡剛好出現 </script> 把內嵌的 script 標籤提早關掉
    payload = json.dumps(embedded, ensure_ascii=False).replace("</", "<\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in html:
        raise RuntimeError(f"{TEMPLATE.name} 裡找不到 {MARKER}，沒辦法注入資料")
    report_path = out_dir / "report.html"
    report_path.write_text(html.replace(MARKER, f"<script>window.EMBEDDED_REPORT = {payload};</script>", 1),
                           encoding="utf-8", newline="\n")
    print(f"\n報表: {report_path}")


if __name__ == "__main__":
    main()
