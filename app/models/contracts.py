"""
群益 SKCOM 選擇權/期貨商品資料模型：Contract dataclass、商品乘數表，以及
「商品代碼字串 -> Contract」的反向解析 parse_symbol()。

parse_symbol() 是 capital_symbols.build_symbol()/taifex_symbols.build_symbol()
的反方向操作。格式規則核對依據：

- 單腳格式 (產品代碼+履約價+月份字母(同時代表買賣權)+年末碼) 已經用
  官方文件《策略王COM元件使用說明_V2.13.59.htm》4-2-x OnOpenInterestJson
  章節給的真實回傳範例核對過——範例字串 "TO,F0200009999999,TXU28500K5,
  43,0,0,0,F123456789" 裡的商品欄位 "TXU28500K5"，完全符合
  capital_symbols.build_symbol() 的組字規則 (TXU=產品代碼, 28500=履約價,
  K=月份字母, 5=年末碼)，不是猜的。

- 複式單合併格式 (GetOpenInterestGW 市場別="TM" 時，商品欄位是「履約價1/
  履約價2」中間用斜線合併、後面共用一個月份字母+年末碼)，同一份官方文件
  同一段給的範例 "TXU28500/28250K5" 就是這個格式——兩個履約價共用同一個
  買賣權/到期月份字母，代表這是同一個買賣權、同一個到期月份的價差(垂直
  價差)。這點也是從真實範例逆推出來的，可信。

  **但哪一腳是多頭、哪一腳是空頭，商品代碼字串本身看不出來**——要靠
  GetOpenInterestGW 的「買賣別」欄位，而官方文件列出的 10 欄位表跟文件
  自己給的回傳範例對不起來(範例只有 8 個逗號分隔值，表格列了 10 欄，見
  查證記錄)，真正的欄位對應順序還沒有拿真實帳號資料核對過。所以這裡
  parse_combo_symbol() 只負責把兩個履約價/買賣權/到期日解析出來，不猜測
  兩腳各自的多空方向——那要等 app/models/positions.py 串接真實
  GetOpenInterestGW 資料、核對過買賣別欄位之後才能決定。

- TX(大台指期貨)裸期貨代碼格式：這個專案目前完全沒有裸期貨的 symbol
  builder (taifex_symbols.py/capital_symbols.py 只涵蓋 TXO 及其週選家族)，
  沒有任何一筆真實資料核對過裸期貨代碼格式，所以這裡不猜測、不支援解析
  裸期貨代碼——遇到無法辨識的字串一律拋 ValueError，不要假裝解析成功。
"""
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.models.capital_symbols import _CALL_MONTH_LETTERS, _PUT_MONTH_LETTERS
from app.models.taifex_symbols import FRI_WEEK_CODES, MONTHLY_CODE, WED_WEEK_CODES

# 目前 taifex_symbols.py 涵蓋的產品代碼全部固定 3 碼 (TXO / TX1.../ TXU...)，
# 用固定長度前綴比對就夠了；如果之後新增非 3 碼的產品代碼 (例如小型台指選
# 擇權)，這裡要改成逐長度嘗試的最長前綴比對，不要直接假設還是 3 碼。
_PRODUCT_CODE_LEN = 3
PRODUCT_CODES = frozenset(
    [MONTHLY_CODE, *WED_WEEK_CODES.values(), *FRI_WEEK_CODES.values()]
)

# 每點金額 (新台幣)。這是 TAIFEX 契約規格的常識數字，不是群益 API 欄位，
# 不受 CLAUDE.md「群益 API 要查文件」規則管，但這個表本身沒有被這個專案
# 的任何一筆真實回報資料核對過，先用業界已知常數起頭，如果之後要支援
# TXO 以外的商品要重新確認。
PRODUCT_MULTIPLIERS = {
    "TXO": 50.0,
    "TX1": 50.0,
    "TX2": 50.0,
    "TX4": 50.0,
    "TX5": 50.0,
    "TXU": 50.0,
    "TXV": 50.0,
    "TXX": 50.0,
    "TXY": 50.0,
    "TXZ": 50.0,
}

_CALL_LETTER_TO_MONTH = {letter: i + 1 for i, letter in enumerate(_CALL_MONTH_LETTERS)}
_PUT_LETTER_TO_MONTH = {letter: i + 1 for i, letter in enumerate(_PUT_MONTH_LETTERS)}


@dataclass(frozen=True)
class Contract:
    symbol: str                        # 群益 bstrStockNo，單腳部位才有意義
    product_code: str
    strike: float
    call_put: str                      # "C"/"P"
    expiry_year_digit: str             # 西元年末碼(1碼)，年份本身有歧義(見下)
    expiry_month: int                  # 1~12
    multiplier: float                  # 每點金額(新台幣)

    @property
    def expiry_label(self) -> str:
        """年份只有末碼，無法唯一還原西元年，只給「末碼+月」的顯示用標
        籤，不要拿來做日期運算。"""
        return f"20{self.expiry_year_digit}? / {self.expiry_month:02d}"


def _split_product_code(symbol: str) -> tuple:
    prefix = symbol[:_PRODUCT_CODE_LEN]
    if prefix not in PRODUCT_CODES:
        raise ValueError(f"無法辨識的產品代碼: {symbol!r} (開頭 {prefix!r} 不在已知清單中)")
    return prefix, symbol[_PRODUCT_CODE_LEN:]


def _split_trailing_month_year(remainder: str) -> tuple:
    """remainder 是去掉產品代碼之後的部分，格式固定是「...履約價}月份字母
    年末碼」，月份字母+年末碼一定是最後 2 碼。"""
    if len(remainder) < 2:
        raise ValueError(f"商品代碼格式不完整，無法解析月份/年末碼: {remainder!r}")
    month_letter, year_digit = remainder[-2], remainder[-1]
    if not year_digit.isdigit():
        raise ValueError(f"年末碼不是數字: {remainder!r}")
    if month_letter in _CALL_LETTER_TO_MONTH:
        call_put = "C"
        month = _CALL_LETTER_TO_MONTH[month_letter]
    elif month_letter in _PUT_LETTER_TO_MONTH:
        call_put = "P"
        month = _PUT_LETTER_TO_MONTH[month_letter]
    else:
        raise ValueError(f"無法辨識的月份字母: {month_letter!r} (來自 {remainder!r})")
    strikes_part = remainder[:-2]
    return strikes_part, call_put, month, year_digit


def parse_symbol(symbol: str) -> Contract:
    """反向解析單腳商品代碼字串 (product_code+履約價+月份字母+年末碼)。

    複式單合併字串(含 "/") 請改用 parse_combo_symbol()，這支函式遇到
    "/" 會直接拋 ValueError，不會偷偷只解析前半段。"""
    if "/" in symbol:
        raise ValueError(f"這是複式單合併代碼，請用 parse_combo_symbol(): {symbol!r}")
    product_code, remainder = _split_product_code(symbol)
    strikes_part, call_put, month, year_digit = _split_trailing_month_year(remainder)
    if not strikes_part.isdigit():
        raise ValueError(f"履約價不是數字: {strikes_part!r} (來自 {symbol!r})")
    multiplier = PRODUCT_MULTIPLIERS.get(product_code)
    if multiplier is None:
        raise ValueError(f"沒有 {product_code!r} 的每點金額設定，不能猜")
    return Contract(
        symbol=symbol,
        product_code=product_code,
        strike=float(strikes_part),
        call_put=call_put,
        expiry_year_digit=year_digit,
        expiry_month=month,
        multiplier=multiplier,
    )


def parse_combo_symbol(symbol: str) -> List[Contract]:
    """反向解析 GetOpenInterestGW 市場別="TM" 的複式單合併代碼 (格式：
    產品代碼+履約價1/履約價2+月份字母+年末碼，兩腳共用同一個買賣權/到期
    月份)。回傳兩個 Contract，順序是代碼字串裡出現的順序 (履約價1 在
    前、履約價2 在後)——**哪一個是多頭、哪一個是空頭，這支函式不判斷**，
    要靠 GetOpenInterestGW 的買賣別欄位，該欄位的真正位置還沒有拿真實資
    料核對過(見本檔案開頭說明)，這裡不猜。

    每一腳回傳的 Contract.symbol 用單腳格式重新組回去 (product_code+單一
    履約價+月份字母+年末碼)，方便跟其他單腳部位用同一個 symbol 格式比
    對/去重。"""
    if "/" not in symbol:
        raise ValueError(f"這不是複式單合併代碼 (沒有 '/'): {symbol!r}，請用 parse_symbol()")
    product_code, remainder = _split_product_code(symbol)
    strikes_part, call_put, month, year_digit = _split_trailing_month_year(remainder)
    if strikes_part.count("/") != 1:
        raise ValueError(f"複式單履約價部分格式不對，預期恰好一個 '/': {strikes_part!r}")
    strike1_str, strike2_str = strikes_part.split("/")
    if not (strike1_str.isdigit() and strike2_str.isdigit()):
        raise ValueError(f"複式單履約價不是數字: {strikes_part!r} (來自 {symbol!r})")
    multiplier = PRODUCT_MULTIPLIERS.get(product_code)
    if multiplier is None:
        raise ValueError(f"沒有 {product_code!r} 的每點金額設定，不能猜")

    def _leg(strike_str: str) -> Contract:
        return Contract(
            symbol=build_leg_symbol(product_code, float(strike_str), call_put, month, year_digit),
            product_code=product_code,
            strike=float(strike_str),
            call_put=call_put,
            expiry_year_digit=year_digit,
            expiry_month=month,
            multiplier=multiplier,
        )

    return [_leg(strike1_str), _leg(strike2_str)]


def _month_letter(call_put: str, month: int) -> str:
    letters = _CALL_MONTH_LETTERS if call_put == "C" else _PUT_MONTH_LETTERS
    return letters[month - 1]


def build_leg_symbol(product_code: str, strike: float, call_put: str, month: int, year_digit: str) -> str:
    """組出單腳商品代碼字串(product_code+履約價+月份字母+年末碼)，是
    parse_symbol() 的反方向操作，抽出來給自動平倉「換履約價、其餘不變」
    重開倉共用——重開倉一定跟原部位同一個到期月份/年末碼，只換履約價，
    不需要(也無法可靠取得)完整西元日期去走
    capital_symbols.build_symbol() 那套路(Contract.expiry_year_digit 只
    有個位數，年份本身有歧義，見本檔案開頭說明)，直接用既有 Contract 上
    已確認過的欄位組字串最安全。"""
    return f"{product_code}{int(strike)}{_month_letter(call_put, month)}{year_digit}"


def build_vertical_spread_legs(
    product_code: str, anchor_strike: float, call_put: str, month: int, year_digit: str,
    width: float, buy_spread: bool,
) -> Tuple[str, bool, float, str, bool, float]:
    """算一組垂直價差兩腳的 symbol/買賣方向/履約價，依照期交所複式單編碼
    規則(核對自官方文件《7.下單-國內期選.docx》Call/Put多頭/空頭價差範
    例，完整核對記錄見 app/views/order_entry_widget.py 的
    _compute_duplex_legs())：
        買權(Call) leg1 = 較高履約價，leg2 = 較低履約價
        賣權(Put)  leg1 = 較低履約價，leg2 = 較高履約價
        較低履約價那腳該買進還是賣出，Call/Put 剛好相反：
            買權：較低履約價買進 = 買方；較低履約價賣出 = 賣方
            賣權：較低履約價買進 = 賣方；較低履約價賣出 = 買方

    這是 _compute_duplex_legs() 拿掉 GUI 狀態依賴後的純函式版本，給
    app/models/auto_close_manager.py 重開倉/開新倉共用，避免另外寫一份
    容易漏改其中一份、兩邊行為不一致(那段註解列的真實報價數字核對記錄，
    這裡不重複抄一次)。

    anchor_strike 是其中一腳的履約價(通常是使用者手動指定的那一支)，另
    一腳 = anchor_strike 加/減 width(買權加、賣權減，跟下單面板
    _leg2_strike() 同一套規則)。回傳
    (leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2)。"""
    is_call = call_put == "C"
    sign = 1 if is_call else -1
    other_strike = anchor_strike + sign * width
    origin_symbol = build_leg_symbol(product_code, anchor_strike, call_put, month, year_digit)
    other_symbol = build_leg_symbol(product_code, other_strike, call_put, month, year_digit)

    if anchor_strike <= other_strike:
        low_symbol, low_strike = origin_symbol, anchor_strike
        high_symbol, high_strike = other_symbol, other_strike
    else:
        low_symbol, low_strike = other_symbol, other_strike
        high_symbol, high_strike = origin_symbol, anchor_strike
    low_buy = buy_spread if is_call else not buy_spread
    low_leg = (low_symbol, low_buy, low_strike)
    high_leg = (high_symbol, not low_buy, high_strike)
    leg1_symbol, buy1, strike1 = high_leg if is_call else low_leg
    leg2_symbol, buy2, strike2 = low_leg if is_call else high_leg
    return leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2
