"""
回測策略的純資料定義：進場規則、出場規則清單、回測設定，以及參數驗證。

*** 這支檔案只用標準庫，不 import pandas/yfinance ***：UI(`app/views/web_backtest_dialog.py`)
和策略清單存檔(`app/services/backtest_store.py`)都要用它，但這兩個都不該為了「只是看一下
參數/列出清單」就把 pandas 這種重量級套件載進來(首頁 timeout 的前車之鑑見
`app/services/lazy_ui.py` 開頭)。真正跑回測的 `engine.py`/`market_data.py` 才會 import pandas，
而且只在使用者按下「執行回測」之後才會被 import。

策略 = 進場規則(`EntrySpec`) + 出場規則清單(`ExitRule` list)。整套語意(履約價怎麼選、規則
怎麼排序、動作代表什麼)是使用者在對話中逐項拍板的，細節註解寫在各 dataclass 上，實際執行
邏輯見 `engine.py`。

支援四種策略(`EntrySpec.kind`)，差別只在「哪一邊有買保護腳」，其餘(短腳條件、出場規則)完全共用：
Iron Condor 兩邊都有、裸雙賣兩邊都沒有、Jade Lizard 只有 call 邊有、Twisted Sister 只有 put 邊有
(見 `PROTECTED_SIDES`)。
"""
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import List, Optional

# 履約價最小跳動(美元)。目前只支援 ETF，ETF 選擇權的履約價最小跳動是 1 美元，所以引擎的履約價格點、
# 長腳寬度都以 1 美元為單位(見 `snap_width`)；日後支援個股要改成依標的/到期日查真實的掛牌履約價。
STRIKE_STEP = 1.0


def snap_width(raw_width: float) -> float:
    """把長腳寬度換算到履約價格點：四捨五入(.5 一律進位，不用 Python 內建 `round()` 的偶數捨入，不然
    0.5→0、2.5→2、3.5→4 會讓相鄰的兩個設定悄悄變成同一個寬度)，最小 1 格。"""
    return max(STRIKE_STEP, math.floor(raw_width / STRIKE_STEP + 0.5) * STRIKE_STEP)


# --- 策略類型 ---------------------------------------------------------------------------------
STRATEGY_IRON_CONDOR = "iron_condor"
STRATEGY_STRANGLE = "strangle"
STRATEGY_JADE_LIZARD = "jade_lizard"
STRATEGY_TWISTED_SISTER = "twisted_sister"
STRATEGIES = (STRATEGY_IRON_CONDOR, STRATEGY_STRANGLE, STRATEGY_JADE_LIZARD, STRATEGY_TWISTED_SISTER)
STRATEGY_LABELS = {
    STRATEGY_IRON_CONDOR: "Iron Condor（鐵禿鷹）",
    STRATEGY_STRANGLE: "Short Strangle（裸雙賣）",
    STRATEGY_JADE_LIZARD: "Jade Lizard（玉蜥蜴）",
    STRATEGY_TWISTED_SISTER: "Twisted Sister（扭曲姊妹）",
}
# 每種策略哪一邊有買保護腳(長腳)。Jade Lizard = 裸賣 put + call 價差；Twisted Sister = 裸賣 call + put 價差。
PROTECTED_SIDES = {
    STRATEGY_IRON_CONDOR: ("put", "call"),
    STRATEGY_STRANGLE: (),
    STRATEGY_JADE_LIZARD: ("call",),
    STRATEGY_TWISTED_SISTER: ("put",),
}
# 「無單邊風險」進場條件只對「一邊裸賣、另一邊價差」的策略有意義。
STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK = (STRATEGY_JADE_LIZARD, STRATEGY_TWISTED_SISTER)

# --- 短腳履約價條件 -----------------------------------------------------------------------------
METRIC_DELTA = "delta"                  # |delta|
METRIC_DISTANCE_PCT = "distance_pct"    # 履約價距離現價的 OTM 距離 / 現價 * 100
METRIC_PREMIUM = "premium"              # 短腳自己的理論價(每股)，不是整個價差的信用
METRICS = (METRIC_DELTA, METRIC_DISTANCE_PCT, METRIC_PREMIUM)
METRIC_LABELS = {METRIC_DELTA: "|Delta|", METRIC_DISTANCE_PCT: "距現價 %", METRIC_PREMIUM: "權利金"}

OPS = ("<", "<=", ">", ">=")
COMBINE_AND = "and"
COMBINE_OR = "or"
COMBINES = (COMBINE_AND, COMBINE_OR)

# --- 長腳寬度 -----------------------------------------------------------------------------------
WIDTH_USD = "usd"
WIDTH_PCT = "pct"   # 現價的百分比
WIDTH_UNITS = (WIDTH_USD, WIDTH_PCT)
WIDTH_UNIT_LABELS = {WIDTH_USD: "美元", WIDTH_PCT: "現價 %"}

# --- 出場規則 -----------------------------------------------------------------------------------
SCOPE_GROUP = "group"   # 整組(目前持有的所有單邊價差合計)
SCOPE_LEG = "leg"       # 單邊(put 邊或 call 邊各自獨立判斷)
SCOPES = (SCOPE_GROUP, SCOPE_LEG)
SCOPE_LABELS = {SCOPE_GROUP: "整組", SCOPE_LEG: "單邊"}

KIND_TAKE_PROFIT = "take_profit"
KIND_STOP_LOSS = "stop_loss"
KIND_DTE = "dte"
KINDS = (KIND_TAKE_PROFIT, KIND_STOP_LOSS, KIND_DTE)
KIND_LABELS = {KIND_TAKE_PROFIT: "停利", KIND_STOP_LOSS: "停損", KIND_DTE: "到期天數"}
# 同一個範圍內每天的判斷順序(不依使用者填寫順序)：到期天數 -> 停利 -> 停損。沿用舊回測引擎的
# 順序，方便對帳；盤中觸價時同一天先判斷停利再判斷停損，對策略偏樂觀，UI 上有標註。
KIND_EVAL_ORDER = (KIND_DTE, KIND_TAKE_PROFIT, KIND_STOP_LOSS)

UNIT_CREDIT_PCT = "credit_pct"   # 進場信用的百分比(停損 100% = 一倍)
UNIT_POINTS = "points"           # 每股價格點數，跟即時自動平倉規則的 threshold_points 同一種單位
UNIT_DAYS = "days"               # 只給到期天數規則用：剩餘天數 <= 這個值就出場
UNIT_LABELS = {UNIT_CREDIT_PCT: "權利金 %", UNIT_POINTS: "點數", UNIT_DAYS: "天"}

ACTION_CLOSE = "close"
ACTION_CLOSE_REOPEN = "close_reopen"
ACTIONS = (ACTION_CLOSE, ACTION_CLOSE_REOPEN)
ACTION_LABELS = {ACTION_CLOSE: "只平倉", ACTION_CLOSE_REOPEN: "平倉後重開"}

FILL_CLOSE = "close"
FILL_INTRADAY = "intraday"
FILLS = (FILL_CLOSE, FILL_INTRADAY)
FILL_LABELS = {FILL_CLOSE: "收盤價", FILL_INTRADAY: "盤中觸價"}

# 有對應隱含波動率指數的標的 -> 指數代號。不能整個回測都套 VIX：QQQ(那斯達克 100)歷史上波動率通常
# 比 SPX 高，用 VIX 幫 QQQ 定價會讓履約價/權利金都偏離真實水位。SPX/NDX/RUT 這些指數在 yfinance
# 需要 ^ 前綴，先不放，只放 ETF。
SUPPORTED_TICKERS = {
    "SPY": "^VIX",
    "IVV": "^VIX",
    "VOO": "^VIX",
    "QQQ": "^VXN",
    "IWM": "^RVX",
}

# --- 定價模型：波動率偏斜 ------------------------------------------------------------------------
# 引擎用 Black-Scholes + 波動率指數當 sigma。單一 sigma 等於假設所有履約價的隱含波動率一樣(沒有偏斜)，
# 真實市場價外 put 比價外 call 貴，這會直接影響 put 邊/call 邊誰比較賺(見 `engine._Vol`)。兩個參數：
#   skew(偏斜強度)：sigma_K = ATM × (1 − skew × ln(K/S))。0 = 沒有偏斜；越大 put 越貴、call 越便宜。
#   atm_ratio：ATM 隱含波動率 ÷ 波動率指數。VIX 是一整排價外 put/call 算出來的，通常高於平價選擇權的
#              IV，1.0 = 直接把 VIX 當 ATM。
# 預設值(4.0 和 0.96)是拿 2026-09-18 收盤的 SPY 真實選擇權鏈(到期日 2026-10-23/10-30，35~42 天)校準的：
# 用 bid/ask 中間價、以引擎同一套定價慣例(r=4%、不含股息)反推隱含波動率，再對 VIX(14.81)做線性擬合
# ratio*(1 − skew*ln(K/S))，範圍取 16 delta 附近(現價 ±3%~9%)，殘差 RMS 約 0.9 個波動率點；那天 16 delta
# put 約 16~17%、16 delta call 約 10.5~11%，兩邊差約 6 個波動率點。
# *** 限制：(1) 只是低波動(VIX 14.8)當天的單一快照，不是 2015~2026 的歷史；真實偏斜會隨行情狀態變(崩
# 盤時 put 更貴)。(2) 真實曲線是彎的(價外 put 側更陡)，針對價外區擬合出來的 ratio 會比真正 ATM 比例
# (當天約 0.90)高一點，所以它是「擬合截距」，不是嚴格的 ATM。(3) 舊版回測(沒有這兩個參數的時候)等於
# skew=0/ratio=1.0，已明確寫進舊存檔，跟新預設值跑出來的結果不能直接比。適合用來掃不同數值看策略排名穩
# 不穩，不要當成精確的定價 ***
DEFAULT_SKEW = 4.0
DEFAULT_ATM_RATIO = 0.96
SKEW_MAX = 10.0
ATM_RATIO_RANGE = (0.5, 1.5)

IBKR_RATE_PER_CONTRACT = 0.65   # IBKR Pro Fixed 美股選擇權，每口每腳(月量 <=10,000 口那一階)
IBKR_MIN_PER_LEG = 1.00         # combo 單每一腳的最低收費


@dataclass
class StrikeCondition:
    metric: str
    op: str
    value: float


@dataclass
class ShortLegSelector:
    """短腳履約價選擇。在 OTM 履約價格點(間距 1 美元)上逐一檢查條件，combine="and" 要全部條件滿
    足、"or" 任一條件滿足就算通過，取「通過者中離現價最近」的一個(權利金最高)；沒有任何履約價通
    過就當天不進場、隔天再試。"""
    conditions: List[StrikeCondition] = field(default_factory=list)
    combine: str = COMBINE_AND


@dataclass
class LongLegSelector:
    """長腳 = 距離短腳固定寬度，往價外方向再買一腳做保護。只用在有保護腳的那一邊(`PROTECTED_SIDES`)，
    裸賣的那一邊沒有長腳；裸雙賣完全沒有長腳，這個設定整個被忽略。"""
    width_unit: str = WIDTH_PCT
    width: float = 1.0


@dataclass
class EntryConditions:
    """進場條件：跟「短腳履約價條件」(挑履約價用)是兩回事，這裡是「整個部位湊齊之後，要不要真的進場」的
    門檻，不滿足就當天不進場、隔天再試。目前只有一個參數，日後有新的進場條件加在這裡。

    no_single_side_risk(無單邊風險)：總權利金 >= 保護價差的寬度。Jade Lizard 的 call 價差、Twisted
    Sister 的 put 價差如果被總權利金完全蓋過，那一側就沒有虧損風險(這也是這兩種策略名稱的由來)。
    只對 Jade Lizard/Twisted Sister 有意義，其他策略忽略這個值。"""
    no_single_side_risk: bool = True


@dataclass
class EntrySpec:
    kind: str = STRATEGY_IRON_CONDOR
    dte: int = 40
    short: ShortLegSelector = field(default_factory=ShortLegSelector)
    long: LongLegSelector = field(default_factory=LongLegSelector)
    conditions: EntryConditions = field(default_factory=EntryConditions)


@dataclass
class ExitRule:
    """一條出場規則。每個 (範圍, 類型) 組合最多一條(整份清單最多 6 條)。

    - 範圍 group：把目前持有的所有單邊價差當一整組看(信用/損益都是加總)，觸發後整組平倉。
      在「立刻重新進場」的前提下，整組平倉之後一定會馬上用進場規則重新進場，所以 action 對整
      組範圍沒有意義，驗證時固定要求 ACTION_CLOSE，UI 也不給選。
    - 範圍 leg：put 邊、call 邊各自獨立判斷。ACTION_CLOSE 是該邊留空(另一邊照常，兩邊都空手才
      整組重新進場)；ACTION_CLOSE_REOPEN 是當天收盤價用進場規則立刻重開該邊，天期重置。
    - 類型 dte：threshold 是天數，unit 固定 days，成交固定收盤價(到期日是行事曆決定的)。
    - 類型 take_profit/stop_loss：threshold + unit(credit_pct/points)；fill 決定用收盤價還是
      盤中高低價模擬掛單成交。
    """
    scope: str
    kind: str
    threshold: float
    unit: str
    action: str = ACTION_CLOSE
    fill: str = FILL_CLOSE


@dataclass
class StrategySpec:
    entry: EntrySpec = field(default_factory=EntrySpec)
    exit_rules: List[ExitRule] = field(default_factory=list)


@dataclass
class RunConfig:
    ticker: str = "SPY"
    start: str = "2015-01-01"
    end: str = "2025-01-01"
    contracts: int = 1
    risk_free_rate: float = 0.04
    commission_rate: float = IBKR_RATE_PER_CONTRACT
    commission_min_per_leg: float = IBKR_MIN_PER_LEG
    skew: float = DEFAULT_SKEW
    atm_ratio: float = DEFAULT_ATM_RATIO


def default_strategy() -> StrategySpec:
    """新策略表單的起手式：Iron Condor、整組(到期天數 15 / 停利 50% / 停損 100%)、進場天期 40、|Delta|<0.16、
    寬度現價 1%。純粹是表單預設值，使用者全部可改。"""
    return StrategySpec(
        entry=EntrySpec(
            dte=40,
            short=ShortLegSelector(conditions=[StrikeCondition(METRIC_DELTA, "<", 0.16)], combine=COMBINE_AND),
            long=LongLegSelector(width_unit=WIDTH_PCT, width=1.0),
        ),
        exit_rules=[
            ExitRule(SCOPE_GROUP, KIND_DTE, 15, UNIT_DAYS, ACTION_CLOSE, FILL_CLOSE),
            ExitRule(SCOPE_GROUP, KIND_TAKE_PROFIT, 50, UNIT_CREDIT_PCT, ACTION_CLOSE, FILL_CLOSE),
            ExitRule(SCOPE_GROUP, KIND_STOP_LOSS, 100, UNIT_CREDIT_PCT, ACTION_CLOSE, FILL_CLOSE),
        ],
    )


# ------------------------------------------------------------------------------ 序列化
def strategy_to_dict(strategy: StrategySpec) -> dict:
    return asdict(strategy)


def strategy_from_dict(d: dict) -> StrategySpec:
    entry = d["entry"]
    return StrategySpec(
        entry=EntrySpec(
            kind=entry["kind"],
            dte=int(entry["dte"]),
            short=ShortLegSelector(
                conditions=[StrikeCondition(c["metric"], c["op"], float(c["value"])) for c in entry["short"]["conditions"]],
                combine=entry["short"]["combine"],
            ),
            long=LongLegSelector(width_unit=entry["long"]["width_unit"], width=float(entry["long"]["width"])),
            conditions=EntryConditions(no_single_side_risk=bool(entry["conditions"]["no_single_side_risk"])),
        ),
        exit_rules=[
            ExitRule(
                scope=r["scope"], kind=r["kind"], threshold=float(r["threshold"]), unit=r["unit"],
                action=r.get("action", ACTION_CLOSE), fill=r.get("fill", FILL_CLOSE),
            )
            for r in d.get("exit_rules", [])
        ],
    )


def config_to_dict(config: RunConfig) -> dict:
    return asdict(config)


def config_from_dict(d: dict) -> RunConfig:
    return RunConfig(
        ticker=d["ticker"], start=d["start"], end=d["end"], contracts=int(d.get("contracts", 1)),
        risk_free_rate=float(d.get("risk_free_rate", 0.04)),
        commission_rate=float(d.get("commission_rate", IBKR_RATE_PER_CONTRACT)),
        commission_min_per_leg=float(d.get("commission_min_per_leg", IBKR_MIN_PER_LEG)),
        # 定價模型的設定會影響結果，不給預設值：缺欄位就丟 KeyError，逼舊存檔明確遷移成 skew=0/atm_ratio=1
        skew=float(d["skew"]), atm_ratio=float(d["atm_ratio"]),
    )


# ------------------------------------------------------------------------------ 驗證
def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def validate_strategy(strategy: StrategySpec) -> List[str]:
    """回傳中文錯誤訊息清單，空清單代表通過。"""
    errors: List[str] = []
    entry = strategy.entry

    if entry.kind not in STRATEGIES:
        errors.append(f"不支援的策略類型 {entry.kind}")
    if not isinstance(entry.dte, int) or entry.dte < 2:
        errors.append("進場天期必須是 2 以上的整數")

    if not entry.short.conditions:
        errors.append("短腳履約價至少要有 1 個條件")
    if entry.short.combine not in COMBINES:
        errors.append("短腳條件的合併方式只能是 and 或 or")
    for i, c in enumerate(entry.short.conditions, 1):
        label = f"短腳條件 {i}"
        if c.metric not in METRICS:
            errors.append(f"{label}：不支援的指標 {c.metric}")
            continue
        if c.op not in OPS:
            errors.append(f"{label}：不支援的比較符號 {c.op}")
        if not _is_finite_number(c.value):
            errors.append(f"{label}：數值必須是數字")
            continue
        if c.metric == METRIC_DELTA and not 0 < c.value < 1:
            errors.append(f"{label}：|Delta| 必須介於 0 和 1 之間")
        if c.metric == METRIC_DISTANCE_PCT and not 0 <= c.value < 100:
            errors.append(f"{label}：距現價 % 必須介於 0 和 100 之間")
        if c.metric == METRIC_PREMIUM and c.value < 0:
            errors.append(f"{label}：權利金不可為負")

    if PROTECTED_SIDES.get(entry.kind):   # 裸雙賣沒有長腳，寬度不必驗證
        if entry.long.width_unit not in WIDTH_UNITS:
            errors.append("長腳寬度單位只能是美元或現價 %")
        if not _is_finite_number(entry.long.width) or entry.long.width <= 0:
            errors.append("長腳寬度必須大於 0")
        elif entry.long.width_unit == WIDTH_PCT and entry.long.width > 50:
            errors.append("長腳寬度(現價 %)不可超過 50")
        elif entry.long.width_unit == WIDTH_USD and (
            entry.long.width < STRIKE_STEP or entry.long.width % STRIKE_STEP != 0
        ):
            errors.append(f"長腳寬度(美元)必須是 {STRIKE_STEP:g} 美元的整數倍(ETF 履約價最小跳動 {STRIKE_STEP:g} 美元)")

    seen = set()
    has_dte = False
    for r in strategy.exit_rules:
        label = f"{SCOPE_LABELS.get(r.scope, r.scope)}{KIND_LABELS.get(r.kind, r.kind)}"
        if r.scope not in SCOPES or r.kind not in KINDS:
            errors.append(f"出場規則 {label}：範圍或類型不合法")
            continue
        if (r.scope, r.kind) in seen:
            errors.append(f"出場規則 {label} 重複，每個範圍/類型最多一條")
        seen.add((r.scope, r.kind))
        if not _is_finite_number(r.threshold) or r.threshold <= 0:
            errors.append(f"出場規則 {label}：門檻必須大於 0")
            continue
        if r.fill not in FILLS:
            errors.append(f"出場規則 {label}：成交模式不合法")
        if r.action not in ACTIONS:
            errors.append(f"出場規則 {label}：動作不合法")
        if r.scope == SCOPE_GROUP and r.action != ACTION_CLOSE:
            errors.append(f"出場規則 {label}：整組範圍的動作固定為平倉(平倉後會立刻依進場規則重新進場)")
        if r.kind == KIND_DTE:
            has_dte = True
            if r.unit != UNIT_DAYS:
                errors.append(f"出場規則 {label}：單位必須是天")
            if r.fill != FILL_CLOSE:
                errors.append(f"出場規則 {label}：到期天數的成交一律是收盤價")
            if r.threshold != int(r.threshold) or r.threshold < 1:
                errors.append(f"出場規則 {label}：天數必須是 1 以上的整數")
            elif isinstance(entry.dte, int) and r.threshold >= entry.dte:
                errors.append(f"出場規則 {label}：天數必須小於進場天期 {entry.dte}")
        else:
            if r.unit not in (UNIT_CREDIT_PCT, UNIT_POINTS):
                errors.append(f"出場規則 {label}：單位必須是權利金 % 或點數")
            elif r.kind == KIND_TAKE_PROFIT and r.unit == UNIT_CREDIT_PCT and r.threshold > 100:
                errors.append(f"出場規則 {label}：停利超過收到權利金的 100% 不可能觸發")
    if not has_dte:
        errors.append("至少要有一條到期天數規則(否則持倉會活過到期日)")
    return errors


def validate_config(config: RunConfig) -> List[str]:
    errors: List[str] = []
    if config.ticker not in SUPPORTED_TICKERS:
        errors.append(f"標的 {config.ticker} 沒有對應的波動率指數，目前只支援：{'、'.join(SUPPORTED_TICKERS)}")
    start = end = None
    try:
        start = date.fromisoformat(config.start)
    except (TypeError, ValueError):
        errors.append("起始日期格式必須是 YYYY-MM-DD")
    try:
        end = date.fromisoformat(config.end)
    except (TypeError, ValueError):
        errors.append("結束日期格式必須是 YYYY-MM-DD")
    if start and end and start >= end:
        errors.append("起始日期必須早於結束日期")
    if not isinstance(config.contracts, int) or config.contracts < 1:
        errors.append("口數必須是 1 以上的整數")
    if not _is_finite_number(config.skew) or not 0 <= config.skew <= SKEW_MAX:
        errors.append(f"偏斜強度必須介於 0 和 {SKEW_MAX:g} 之間")
    lo, hi = ATM_RATIO_RANGE
    if not _is_finite_number(config.atm_ratio) or not lo <= config.atm_ratio <= hi:
        errors.append(f"ATM 相對 VIX 比例必須介於 {lo:g} 和 {hi:g} 之間")
    return errors


# ------------------------------------------------------------------------------ 中文描述
def describe_strategy(strategy: StrategySpec) -> List[str]:
    """把策略參數轉成幾行中文，給報表/策略清單顯示「這份結果是用什麼參數跑的」。"""
    entry = strategy.entry
    joiner = " 且 " if entry.short.combine == COMBINE_AND else " 或 "
    cond_text = joiner.join(f"{METRIC_LABELS[c.metric]} {c.op} {c.value:g}" for c in entry.short.conditions)
    width_text = f"{entry.long.width:g} 美元" if entry.long.width_unit == WIDTH_USD else f"現價 {entry.long.width:g}%"
    entry_line = f"進場：{STRATEGY_LABELS.get(entry.kind, entry.kind)}，{entry.dte} DTE，短腳 {cond_text}"
    if PROTECTED_SIDES.get(entry.kind):
        entry_line += f"，長腳距短腳 {width_text}"
    lines = [entry_line]
    if entry.kind in STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK and entry.conditions.no_single_side_risk:
        lines.append("進場條件：無單邊風險（總權利金 ≥ 保護價差寬度）")
    order = {(s, k): i for i, (s, k) in enumerate((s, k) for s in SCOPES for k in KIND_EVAL_ORDER)}
    threshold_text = {
        UNIT_CREDIT_PCT: lambda v: f"{v:g}% 權利金", UNIT_POINTS: lambda v: f"{v:g} 點", UNIT_DAYS: lambda v: f"{v:g} 天",
    }
    for r in sorted(strategy.exit_rules, key=lambda r: order.get((r.scope, r.kind), 99)):
        threshold = threshold_text.get(r.unit, lambda v: f"{v:g}")(r.threshold)
        parts = [f"{SCOPE_LABELS.get(r.scope, r.scope)}{KIND_LABELS.get(r.kind, r.kind)} {threshold}"]
        if r.kind != KIND_DTE:
            parts.append(FILL_LABELS.get(r.fill, r.fill))
        if r.scope == SCOPE_LEG:
            parts.append(ACTION_LABELS.get(r.action, r.action))
        lines.append("出場：" + "，".join(parts))
    return lines


def describe_config(config: RunConfig) -> List[str]:
    """把定價設定轉成中文，給報表/策略清單明確記錄「這份結果是用什麼定價假設跑的」。"""
    if config.skew == 0:
        skew_text = "無偏斜（所有履約價用同一個波動率）"
    else:
        skew_text = f"偏斜強度 {config.skew:g}（價外 put 波動率較高、價外 call 較低）"
    vol_index = SUPPORTED_TICKERS.get(config.ticker, "波動率指數")
    return [
        f"定價：Black-Scholes，ATM 波動率 = {vol_index} × {config.atm_ratio:g}，{skew_text}，"
        f"無風險利率 {config.risk_free_rate * 100:g}%，不含股息",
    ]
