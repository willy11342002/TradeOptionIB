"""
到期損益(Payoff)計算。純數學，不依賴 Qt/COM，方便獨立測試。

**已知簡化(刻意採用，非疏漏)**：每一腳都用自己履約價的到期內含價值計
算，不同到期日的部位加總在同一條 X 軸(標的價格)上時，忽略各腳實際到期
日不同、忽略時間價值/隱含波動率——這是多數散戶工具「合併損益圖」的算
法，只有在所有腳到期日相同時才是精確的到期損益，混合到期日時是方向性
估算，不是精確預測。使用畫面必須明顯標示這個假設，不能默默假設。
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass(frozen=True)
class PayoffLeg:
    """一腳的損益輸入。這個專案目前沒有裸期貨的 symbol builder(見
    app/models/contracts.py 開頭說明)，全專案唯二建構 PayoffLeg 的地方
    (app/views/payoff_chart_widget.py)一定是從有履約價/買賣權的
    Contract/OrderLeg 來，所以這裡不支援 strike=None 的裸期貨——真的要支
    援時再補上，不要先寫一套沒有呼叫路徑、也沒有真實資料驗證過的分支。"""
    strike: float
    call_put: str              # "C"/"P"
    buy: bool
    qty: int
    premium: float            # 均價/委託價，每點金額換算前的「點數」
    multiplier: float


def leg_payoff_at(S: float, leg: PayoffLeg) -> float:
    """單腳在標的價格 S 時的到期損益(已扣除/計入權利金，NT$)。"""
    if leg.call_put == "C":
        exercise_value = max(S - leg.strike, 0.0)
    elif leg.call_put == "P":
        exercise_value = max(leg.strike - S, 0.0)
    else:
        raise ValueError(f"leg.call_put 必須是 'C'/'P'，收到 {leg.call_put!r}")
    intrinsic = (exercise_value - leg.premium) if leg.buy else (leg.premium - exercise_value)
    return intrinsic * leg.qty * leg.multiplier


def combined_payoff(legs: List[PayoffLeg], price_range: np.ndarray) -> np.ndarray:
    """加總所有腳，回傳跟 price_range 等長的損益陣列(NT$)。"""
    total = np.zeros_like(price_range, dtype=float)
    for leg in legs:
        if leg.call_put == "C":
            exercise_value = np.maximum(price_range - leg.strike, 0.0)
        elif leg.call_put == "P":
            exercise_value = np.maximum(leg.strike - price_range, 0.0)
        else:
            raise ValueError(f"leg.call_put 必須是 'C'/'P'，收到 {leg.call_put!r}")
        intrinsic = (exercise_value - leg.premium) if leg.buy else (leg.premium - exercise_value)
        total += intrinsic * leg.qty * leg.multiplier
    return total


def kink_points(legs: List[PayoffLeg]) -> List[float]:
    """加總後的損益函式是分段線性，轉折點只會出現在各腳自己的履約價上。
    回傳排序去重後的履約價清單。"""
    return sorted({leg.strike for leg in legs})


def find_breakevens(legs: List[PayoffLeg], price_floor: float = 0.0, price_ceiling: float = 1e7) -> List[float]:
    """精確求兩平點：分段線性函式只在 kink_points 之間變號，逐段解線性
    方程式，不做數值逼近(bisection/牛頓法)。price_floor/price_ceiling 是
    加在轉折點集合兩端的虛擬邊界，讓最外側那兩段(向左/向右趨於無限)也能
    被檢查到變號。"""
    kinks = kink_points(legs)
    if not kinks:
        return []
    sample_points = [price_floor] + kinks + [price_ceiling]
    sample_points = sorted(set(sample_points))
    values = [sum(leg_payoff_at(S, leg) for leg in legs) for S in sample_points]

    breakevens = []
    for i in range(len(sample_points) - 1):
        x1, x2 = sample_points[i], sample_points[i + 1]
        y1, y2 = values[i], values[i + 1]
        if y1 == 0.0:
            breakevens.append(x1)
            continue
        if (y1 < 0) != (y2 < 0):  # 變號 (包含其中一端剛好等於0的情況已在上面處理)
            if y2 == y1:
                continue  # 理論上不會發生(變號代表 y2!=y1)，防呆
            x_star = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
            breakevens.append(x_star)
    if values[-1] == 0.0 and sample_points[-1] not in breakevens:
        breakevens.append(sample_points[-1])
    return breakevens


@dataclass(frozen=True)
class PayoffExtremes:
    max_profit: Optional[float]   # None = 沒有任何腳，算不出來(不是「無上限」)
    max_profit_bounded: bool      # False = 標的價格趨近無限大時獲利沒有上限
    max_loss: Optional[float]
    max_loss_bounded: bool        # False = 標的價格趨近無限大時虧損沒有上限


def payoff_extremes(legs: List[PayoffLeg]) -> PayoffExtremes:
    """最大獲利/最大虧損。分段線性函式的極值只會落在「轉折點(履約價)」
    或兩端無限遠處，不用掃描整個 price_range 找最大最小值。

    標的價格現實中不會是負的，所以左端用 S=0 當邊界(不是趨近負無限大)；
    右端(S 趨近正無限大)才有可能真的沒有上限/下限——用最右邊那個履約價
    之後的斜率判斷：斜率>0 代表再往右獲利會一直增加(無上限)，斜率<0 代
    表虧損會一直增加(無下限)，斜率=0 代表右側損益已經走平(有界)。"""
    if not legs:
        return PayoffExtremes(None, True, None, True)
    kinks = kink_points(legs)
    candidates = [0.0] + kinks
    values = [sum(leg_payoff_at(S, leg) for leg in legs) for S in candidates]

    if kinks:
        right_anchor = kinks[-1]
    else:
        right_anchor = 0.0
    right_slope = (
        sum(leg_payoff_at(right_anchor + 1.0, leg) for leg in legs)
        - sum(leg_payoff_at(right_anchor, leg) for leg in legs)
    )

    max_profit = max(values)
    max_loss = min(values)
    max_profit_bounded = right_slope <= 0.0
    max_loss_bounded = right_slope >= 0.0
    return PayoffExtremes(
        max_profit=max_profit, max_profit_bounded=max_profit_bounded,
        max_loss=max_loss, max_loss_bounded=max_loss_bounded,
    )


def price_axis_range(legs: List[PayoffLeg], padding_ratio: float = 0.2, min_span_ratio: float = 0.15) -> Tuple[float, float]:
    """X 軸(標的價格)範圍：以所有履約價的 min~max 為基準，外加留白比
    例，並設最小跨度下限(避免單一履約價時跨度變 0)。

    *** 最小跨度改成「履約價中點的比例」，不是寫死的絕對點數 ***：這支
    app 原本接台指選擇權(TAIFEX)，履約價動輒上萬點，寫死 500 點當最小跨
    度只占標的價格一小部分，看起來很合理；換成美股/ETF 選擇權之後，履
    約價可能只是幾十到幾百美元，寫死 500 反而變成「比整組履約價的實際
    跨度大上好幾倍」，把 X 軸硬拉超寬、線型擠成一條線(使用者在 AAPL 的
    iron condor 上實測到：損平點 324~346，跨度才 22，硬跨到 500 讓圖幾
    乎看不出轉折)。改成跟履約價中點的比例算，才能跟著標的價格量級縮
    放，不會因為換成美股就整組跑掉。"""
    strikes = kink_points(legs)
    if not strikes:
        return (0.0, 100.0)
    low, high = min(strikes), max(strikes)
    span = high - low
    mid = (low + high) / 2
    min_span = max(mid * min_span_ratio, 1.0)  # 1.0 保底，避免履約價本身就接近 0 時跨度變 0
    if span < min_span:
        pad = (min_span - span) / 2
        low -= pad
        high += pad
        span = min_span
    padding = span * padding_ratio
    return (low - padding, high + padding)
