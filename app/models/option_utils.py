"""
IB Option/Stock 合約建構的小工具，取代 app/models/contracts.py 舊有的
TAIFEX 符號字串編碼角色。

IB 不需要「商品代碼+履約價+月份字母+年末碼」這種字串編碼——`Option(symbol,
expiry, strike, right, exchange, currency)` 直接是結構化欄位，
`ib.qualifyContracts()` 會自動補上 conId/multiplier/tradingClass，
contracts.py 裡 parse_symbol/parse_combo_symbol/build_leg_symbol 那整套
字串編解碼邏輯完全不需要，整個刪除，不是這裡的取代對象。

但 `build_vertical_spread_legs()` 裡「垂直價差兩腳該怎麼買賣」這條規則
是真正的選擇權數學，不是 TAIFEX 特有的東西，這裡原封不動保留這條規則，
只是把「組商品代碼字串」的部分拿掉，回傳單純的 (履約價, 買賣方向)。
"""
from typing import Tuple

from ib_async import Option, Stock

BUY = "BUY"
SELL = "SELL"


def build_option(symbol: str, expiry: str, strike: float, right: str,
                  exchange: str = "SMART", currency: str = "USD") -> Option:
    return Option(symbol, expiry, strike, right, exchange, currency=currency)


def build_stock(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    return Stock(symbol, exchange, currency)


def vertical_spread_legs(
    anchor_strike: float, width: float, right: str, buy_spread: bool,
) -> Tuple[Tuple[float, str], Tuple[float, str]]:
    """算一組垂直價差兩腳的履約價/買賣方向，回傳
    ((low_strike, low_action), (high_strike, high_action))，action 是
    "BUY"/"SELL"。

    這條規則從 app/models/contracts.py 的 build_vertical_spread_legs()
    原封不動搬過來(那份的核對記錄見該檔案，是核對官方文件的Call/Put多
    頭/空頭價差範例算出來的，不是猜的)，只拿掉組「商品代碼字串」那部
    分——IB 用 conId 識別合約，不需要字串編碼，兩腳直接各自組一個
    `Option(...)` 再各自 `qualifyContracts()` 就好。

    規則本身(Call/Put 剛好相反，不能只看買方/賣方選擇)：
        買權(Call)：較低履約價買進 = 買方(debit)；較低履約價賣出 = 賣方(credit)
        賣權(Put)：較低履約價買進 = 賣方(credit)；較低履約價賣出 = 買方(debit)

    anchor_strike 是其中一腳的履約價(通常是使用者手動指定的那一支)，另
    一腳 = anchor_strike 加/減 width(買權加、賣權減)。buy_spread=True
    代表淨買進這組價差(付淨權利金)，False 代表淨賣出(收淨權利金)。
    """
    is_call = right == "C"
    sign = 1 if is_call else -1
    other_strike = anchor_strike + sign * width

    low_strike, high_strike = sorted((anchor_strike, other_strike))
    low_buy = buy_spread if is_call else not buy_spread
    low_action = BUY if low_buy else SELL
    high_action = SELL if low_buy else BUY

    return (low_strike, low_action), (high_strike, high_action)
