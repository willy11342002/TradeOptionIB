"""
用 ib_async 重寫 ib_test_connect.py 的測試內容：連線、查選擇權鏈(到期日
/履約價)、查一檔指定的選擇權合約明細。

*** 跟 ibapi 版本(ib_test_connect.py)比對用 ***：這支腳本應該印出跟那支
一樣的結論(連線正常、AAPL 有 24 個到期日、能查到指定合約的 conId)，用來
驗證 ib_async 重寫後行為一致。

跑法：
    uv run python scripts/ib_async_connect_test.py
    uv run python scripts/ib_async_connect_test.py --list-expiries
    uv run python scripts/ib_async_connect_test.py --expiry 20261218 --strike 230 --right C
"""
import argparse

from ib_async import IB, Option, Stock


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497, help="預設 7497 = TWS 模擬帳戶(paper)")
    parser.add_argument("--client-id", type=int, default=31)
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--list-expiries", action="store_true")
    parser.add_argument("--expiry", default="")
    parser.add_argument("--strike", type=float, default=0.0)
    parser.add_argument("--right", default="C", choices=["C", "P"])
    args = parser.parse_args()

    ib = IB()
    print(f"[connect] {args.host}:{args.port} (clientId={args.client_id}) ...")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=10)
    print(f"[OK] connected={ib.isConnected()} serverVersion={ib.client.serverVersion()}")

    if args.list_expiries:
        stock = Stock(args.symbol, "SMART", "USD")
        ib.qualifyContracts(stock)
        print(f"[qualifyContracts] 標的 conId={stock.conId}")
        chains = ib.reqSecDefOptParams(args.symbol, "", "STK", stock.conId)
        print(f"[reqSecDefOptParams] 共 {len(chains)} 筆(不同交易所/tradingClass 各一筆)")
        for c in chains:
            if c.exchange == "SMART" and c.tradingClass == args.symbol:
                print(f"    exchange={c.exchange} tradingClass={c.tradingClass} multiplier={c.multiplier}")
                print(f"    到期日({len(c.expirations)}個): {sorted(c.expirations)}")
                strikes = sorted(c.strikes)
                print(f"    履約價範圍: {strikes[0]} ~ {strikes[-1]} (共{len(strikes)}檔)")
    elif args.expiry:
        opt = Option(args.symbol, args.expiry, args.strike, args.right, "SMART", currency="USD")
        ib.qualifyContracts(opt)
        print(f"[qualifyContracts] conId={opt.conId} localSymbol={opt.localSymbol!r} "
              f"multiplier={opt.multiplier} tradingClass={opt.tradingClass}")
    else:
        stock = Stock(args.symbol, "SMART", "USD")
        ib.qualifyContracts(stock)
        print(f"[qualifyContracts] 股票 conId={stock.conId} primaryExchange={stock.primaryExchange}")

    ib.disconnect()
    print("[done]")


if __name__ == "__main__":
    main()
