"""
群益 SKCOM 選擇權合約代碼 (bstrStockNo)。

到期日曆邏輯 (月選/週三選/週五選的到期日、期交所官方商品代碼字首) 完全沿
用 app/models/taifex_symbols.py，那部分是期交所的合約規格、跟哪一家券商
無關。這裡只重寫「代碼組成規則」這一段，因為群益 bstrStockNo 的格式跟原
本用來配華南 XQ RTD 的格式不一樣。

*** 重要：這裡用的是台灣期貨交易所公開的「電子式交易代碼」慣例 ***
    字首(期交所官方商品代碼) + 履約價 + 月份代碼(1碼英文字母，同時代表
    月份跟買賣權：Call 用 A-L 對應 1-12 月，Put 用 M-X 對應 1-12 月) +
    西元年末碼(1碼數字)
例如 2026 年 9 月到期、履約價 22000 的 Call：TXO22000I6
(I = A+8 = 第9碼 = 9月的 Call；6 = 2026 的年末碼)

這個慣例是很多台灣券商 API 共用的期交所標準代碼格式 (跟 kgisuperpy
FutOrder.py 內部 compose_symbol() 用的規則一致)，但沒有拿群益的即時報價
實際核對過，正式送出真實委託前務必先用小口數量、或群益官方
SKCOMTester.exe 工具核對過代碼確實對得上目標合約，避免打錯商品。
"""
import datetime

from app.models.taifex_symbols import (  # noqa: F401  (重新匯出，main_window 只需要 import 這支)
    CATEGORY_FRI,
    CATEGORY_MONTHLY,
    CATEGORY_WED,
    CATEGORY_LABELS,
    ContractExpiry,
    list_all_expiries,
)

_CALL_MONTH_LETTERS = "ABCDEFGHIJKL"  # 1~12 月
_PUT_MONTH_LETTERS = "MNOPQRSTUVWX"   # 1~12 月


def build_symbol(product_code: str, strike: float, expiry_date: datetime.date, is_call: bool) -> str:
    letters = _CALL_MONTH_LETTERS if is_call else _PUT_MONTH_LETTERS
    month_letter = letters[expiry_date.month - 1]
    year_digit = str(expiry_date.year % 10)
    return f"{product_code}{int(strike)}{month_letter}{year_digit}"
