"""
Black-Scholes 歐式選擇權定價與 delta，只用標準庫 math(不額外依賴 scipy)。

這是「合成回測」用的定價引擎：不查真實歷史選擇權報價，而是拿歷史標的收盤
價 + 歷史 VIX(當作隱含波動率的代理)去反推當時理論上的選擇權價格。用 VIX
而不是已實現波動率當 sigma，是為了讓合成價格反映市場當時真正收的風險溢酬
(VRP)——用已實現波動率回推等於用事後真相定價，會把 VRP 直接算沒了。
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError(f"S/K/T/sigma 都必須是正數才能定價: S={S}, K={K}, T={T}, sigma={sigma}")
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """right: "C" 或 "P"。回傳歐式選擇權理論價格，不考慮美式提前履約。"""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if right == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    if right == "P":
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    raise ValueError(f"right 只能是 C 或 P，收到: {right}")


def delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """call delta 落在 [0,1]，put delta 落在 [-1,0]。"""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    if right == "C":
        return _norm_cdf(d1)
    if right == "P":
        return _norm_cdf(d1) - 1.0
    raise ValueError(f"right 只能是 C 或 P，收到: {right}")
