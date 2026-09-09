"""
標準 Black-Scholes(-Merton)，不含股利調整——輕量、無外部依賴 (只用
math.erf)，不用群益 SKQuoteLib_Delta 那個「計算機」函式：那個函式一樣
要先給 sigma 才能算，SKCOM 本身沒有幫你反推 IV，等於自己一定要寫 IV
solver，那乾脆整套公式自己寫比較透明，不用另外猜 SKQuoteLib_Delta 的
nCallPut/符號慣例。

用途：T 字報價要顯示 Delta (概略等於價內機率，交易員常用來感受勝率)，
但群益的基礎報價沒有這個資料，只能拿目前的買賣中價反推隱含波動率，再算
出 Delta。這是近似值 (歐式、無股利調整、無風險利率用固定概略值)，適合
「感受一下勝率」，不是精確風控用途。
"""
import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def price(is_call: bool, S: float, K: float, r: float, T: float, sigma: float) -> float:
    """歐式選擇權理論價。T<=0 或 sigma<=0 時回傳內含價值。"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def delta(is_call: bool, S: float, K: float, r: float, T: float, sigma: float) -> Optional[float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    nd1 = _norm_cdf(d1)
    return nd1 if is_call else nd1 - 1.0


def implied_vol(
    is_call: bool, S: float, K: float, r: float, T: float, market_price: float,
    low: float = 0.001, high: float = 5.0, tol: float = 1e-4, max_iter: int = 60,
) -> Optional[float]:
    """用二分法反推隱含波動率——選擇權價格對 sigma 是單調遞增函數，二分
    法比 Newton-Raphson 穩，不會因為極端價內/價外或到期日很近而發散。
    價格低於內含價值 (報價異常/成交價過舊) 或超出 [low,high] 對應的價格
    區間時回傳 None，不硬湊一個沒意義的數字。"""
    if T <= 0 or S <= 0 or K <= 0 or market_price <= 0:
        return None
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if market_price < intrinsic:
        return None
    lo_price = price(is_call, S, K, r, T, low)
    hi_price = price(is_call, S, K, r, T, high)
    if market_price < lo_price or market_price > hi_price:
        return None
    for _ in range(max_iter):
        mid = (low + high) / 2
        mid_price = price(is_call, S, K, r, T, mid)
        if abs(mid_price - market_price) < tol:
            return mid
        if mid_price > market_price:
            high = mid
        else:
            low = mid
    return (low + high) / 2
