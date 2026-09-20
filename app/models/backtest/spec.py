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
    """長腳 = 距離短腳固定寬度，往價外方向再買一腳做保護。"""
    width_unit: str = WIDTH_PCT
    width: float = 1.0


@dataclass
class EntrySpec:
    dte: int = 40
    short: ShortLegSelector = field(default_factory=ShortLegSelector)
    long: LongLegSelector = field(default_factory=LongLegSelector)


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


def default_strategy() -> StrategySpec:
    """新策略表單的起手式：整組(到期天數 15 / 停利 50% / 停損 100%)、進場天期 40、|Delta|<0.16、
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
            dte=int(entry["dte"]),
            short=ShortLegSelector(
                conditions=[StrikeCondition(c["metric"], c["op"], float(c["value"])) for c in entry["short"]["conditions"]],
                combine=entry["short"]["combine"],
            ),
            long=LongLegSelector(width_unit=entry["long"]["width_unit"], width=float(entry["long"]["width"])),
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
    )


# ------------------------------------------------------------------------------ 驗證
def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def validate_strategy(strategy: StrategySpec) -> List[str]:
    """回傳中文錯誤訊息清單，空清單代表通過。"""
    errors: List[str] = []
    entry = strategy.entry

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
    return errors


# ------------------------------------------------------------------------------ 中文描述
def describe_strategy(strategy: StrategySpec) -> List[str]:
    """把策略參數轉成幾行中文，給報表/策略清單顯示「這份結果是用什麼參數跑的」。"""
    entry = strategy.entry
    joiner = " 且 " if entry.short.combine == COMBINE_AND else " 或 "
    cond_text = joiner.join(f"{METRIC_LABELS[c.metric]} {c.op} {c.value:g}" for c in entry.short.conditions)
    width_text = f"{entry.long.width:g} 美元" if entry.long.width_unit == WIDTH_USD else f"現價 {entry.long.width:g}%"
    lines = [
        f"進場：Iron Condor，{entry.dte} DTE，短腳 {cond_text}，長腳距短腳 {width_text}",
    ]
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
