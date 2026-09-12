"""
用 ib_async 重寫 ib_test_combo_order.py：組一個兩腳的選擇權垂直價差
(BAG combo + ComboLeg)，驗證 ib_async 建構複式單這條路跟 ibapi 版本比對
起來是不是一樣的結果。

一樣保留兩道安全設計 (跟 ibapi 版本相同的理由)：
    1. 預設不呼叫 placeOrder，只把組好的 Contract/Order 印出來 (dry-run)。
       要真的送出去要加 --place。
    2. 就算 --place，Order.transmit 預設 False——單子會建立但停在 TWS 的
       Orders 頁籤等你手動 Transmit。要讓程式自己送到交易所要另外加
       --transmit。

跑法：
    uv run python scripts/ib_async_combo_order_test.py --symbol SPY --expiry 20261016 \\
        --buy-strike 760 --sell-strike 765 --right C --net-action BUY --net-price 1.50
    uv run python scripts/ib_async_combo_order_test.py ... --place
    uv run python scripts/ib_async_combo_order_test.py ... --place --transmit
"""
import argparse
import time

from ib_async import IB, Bag, ComboLeg, LimitOrder, Option


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=34)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", required=True)
    parser.add_argument("--right", default="C", choices=["C", "P"])
    parser.add_argument("--buy-strike", type=float, required=True)
    parser.add_argument("--sell-strike", type=float, required=True)
    parser.add_argument("--qty", type=float, default=1)
    parser.add_argument("--net-price", type=float, required=True)
    parser.add_argument("--net-action", default="BUY", choices=["BUY", "SELL"])
    parser.add_argument("--place", action="store_true")
    parser.add_argument("--transmit", action="store_true")
    args = parser.parse_args()

    ib = IB()
    print(f"[connect] {args.host}:{args.port} (clientId={args.client_id}) ...")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=10)

    buy_leg_contract = Option(args.symbol, args.expiry, args.buy_strike, args.right, "SMART", currency="USD")
    sell_leg_contract = Option(args.symbol, args.expiry, args.sell_strike, args.right, "SMART", currency="USD")
    ib.qualifyContracts(buy_leg_contract, sell_leg_contract)
    print(f"[qualifyContracts] buy_leg conId={buy_leg_contract.conId} localSymbol={buy_leg_contract.localSymbol!r}")
    print(f"[qualifyContracts] sell_leg conId={sell_leg_contract.conId} localSymbol={sell_leg_contract.localSymbol!r}")

    combo = Bag(
        symbol=args.symbol, exchange="SMART", currency="USD",
        comboLegs=[
            ComboLeg(conId=buy_leg_contract.conId, ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=sell_leg_contract.conId, ratio=1, action="SELL", exchange="SMART"),
        ],
    )
    order = LimitOrder(args.net_action, args.qty, args.net_price)
    order.tif = "DAY"  # LimitOrder() 建構子不會預設，跟 ibapi 版本統一都明確指定
    order.transmit = args.transmit

    print("\n===== 組出來的複式單 =====")
    print(f"Contract: secType={combo.secType} symbol={combo.symbol} exchange={combo.exchange}")
    for leg in combo.comboLegs:
        print(f"  ComboLeg: conId={leg.conId} ratio={leg.ratio} action={leg.action} exchange={leg.exchange}")
    print(f"Order: action={order.action} orderType={order.orderType} totalQuantity={order.totalQuantity} "
          f"lmtPrice={order.lmtPrice} tif={order.tif} transmit={order.transmit}")

    if not args.place:
        print("\n[DRY-RUN] 沒有加 --place，不會真的送出。")
        ib.disconnect()
        return

    print(f"\n[placeOrder] transmit={order.transmit} ...")
    trade = ib.placeOrder(combo, order)
    ib.sleep(3)
    print(f"[trade] status={trade.orderStatus.status} filled={trade.orderStatus.filled} "
          f"remaining={trade.orderStatus.remaining} isDone={trade.isDone()} isActive={trade.isActive()}")
    for log_entry in trade.log:
        print(f"[log] {log_entry.time} {log_entry.status} {log_entry.message}")

    ib.disconnect()
    print("[done]")


if __name__ == "__main__":
    main()
