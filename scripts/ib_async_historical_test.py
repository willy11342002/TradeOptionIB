"""
用 ib_async 驗證：不透過即時/延遲報價，改用 reqHistoricalData 查最近的
K棒(收盤價/中價)，用來驗證「拿不到即時報價時，還能不能拿到最新價格」
這件事。

股票日線應該拿到跟之前一樣的收盤價(SPY 2026-09-10 收盤 757.83)；選擇
權要用小時線 + MIDPOINT 才查得到(日線在 BEST 路由查無 EOD 資料，
ib_quote_client.py 的 fallback 邏輯就是用這裡驗證過的參數)。

跑法：
    uv run python scripts/ib_async_historical_test.py --symbol SPY
    uv run python scripts/ib_async_historical_test.py --symbol SPY --sectype OPT \\
        --expiry 20261016 --strike 760 --right C --bar-size "1 hour" --duration "2 D" --what-to-show MIDPOINT
"""
import argparse

from ib_async import IB, Option, Stock


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002, help="預設 4002 = IB Gateway 模擬帳戶(paper)")
    parser.add_argument("--client-id", type=int, default=32)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--sectype", default="STK", choices=["STK", "OPT"])
    parser.add_argument("--expiry", default="")
    parser.add_argument("--strike", type=float, default=0.0)
    parser.add_argument("--right", default="C", choices=["C", "P"])
    parser.add_argument("--duration", default="5 D")
    parser.add_argument("--bar-size", default="1 day")
    parser.add_argument("--what-to-show", default="TRADES", choices=["TRADES", "MIDPOINT", "BID", "ASK"])
    args = parser.parse_args()

    ib = IB()
    print(f"[connect] {args.host}:{args.port} (clientId={args.client_id}) ...")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=10)

    if args.sectype == "OPT":
        contract = Option(args.symbol, args.expiry, args.strike, args.right, "SMART", currency="USD")
    else:
        contract = Stock(args.symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    print(f"[qualifyContracts] conId={contract.conId}")

    print(f"[reqHistoricalData] duration={args.duration} barSize={args.bar_size} whatToShow={args.what_to_show} ...")
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr=args.duration, barSizeSetting=args.bar_size,
        whatToShow=args.what_to_show, useRTH=True,
    )

    if not bars:
        print("[WARN] 沒有拿到任何K棒。")
    else:
        for b in bars:
            print(f"    {b.date} open={b.open} high={b.high} low={b.low} close={b.close} volume={b.volume}")
        print(f"\n[OK] 共 {len(bars)} 根K棒，最新一根: {bars[-1].date} 收盤={bars[-1].close}")

    ib.disconnect()
    print("[done]")


if __name__ == "__main__":
    main()
