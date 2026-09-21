"""
回測功能區：標題列「回測」按鈕彈出的大 dialog，三個分頁——新策略(參數設定)/策略清單(過去跑過的)/
報表。前端逐項設定「進場規則 + 出場規則清單」→ 丟到背景執行緒跑回測 → 逐筆交易存進
`pref/backtest/`(`app/services/backtest_store.py`)→ 在報表分頁切換檢視。

*** 這支檔案的頂層只 import 輕量的東西 ***：nicegui、純標準庫的 `app.models.backtest.spec`、存檔層
`backtest_store`。**`pandas`/`yfinance`/回測引擎(`app.models.backtest.engine`/`market_data`)只在使用者
按下「執行回測」之後、在 `_execute()` 裡才 import**——首頁載入不能被它們拖慢，`index()` 撞
`response_timeout` 的教訓見 `app/services/lazy_ui.py` 開頭。dialog 本身也是點了按鈕才建
(`lazy_open`)。回測跟 IB 連線無關，按鈕永遠可以按。

報表分頁是把舊 `backtest_dashboard.html` 複製、隔離樣式後放在 `app/resources/backtest_report/`(不是
iframe)。*** 報表 JS 必須用 `ui.run_javascript()` 注入，不能用 `ui.add_head_html("<script>…")` ***：頁面
載入完成後 NiceGUI 是用 `insertAdjacentHTML` 補插入 head 內容，瀏覽器不會執行這種方式插入的
`<script>`(只有 CSS 會生效)，所以 CSS 走 `ui.add_css()`、JS 走 `run_javascript()`。所有回測的逐筆資料會
一次送進瀏覽器記憶體(`window.BtReport`)，在報表分頁切換回測完全在前端完成、不回伺服器。
"""
import json
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from nicegui import ui

from app.models.backtest import spec as S
from app.paths import PROJECT_ROOT
from app.services import backtest_store
from app.services.background_tasks import run_blocking
from app.services.lazy_ui import lazy_open

_RESOURCE_DIR = PROJECT_ROOT / "app" / "resources" / "backtest_report"

TAB_NEW, TAB_LIST, TAB_REPORT = "new", "list", "report"

_METRIC_OPTIONS = dict(S.METRIC_LABELS)
_KIND_OPTIONS = dict(S.KIND_LABELS)
_SCOPE_OPTIONS = dict(S.SCOPE_LABELS)
_ACTION_OPTIONS = dict(S.ACTION_LABELS)
_FILL_OPTIONS = dict(S.FILL_LABELS)
_WIDTH_UNIT_OPTIONS = dict(S.WIDTH_UNIT_LABELS)
_STRATEGY_OPTIONS = dict(S.STRATEGY_LABELS)
_FILL_PRICE_OPTIONS = dict(S.FILL_PRICE_LABELS)

_SEMANTICS_HINT = (
    "短腳條件：在當天實際掛牌的 OTM 履約價上逐一檢查，and = 全部條件都滿足、or = 任一條件滿足，取通過者中離"
    "現價最近的一個；沒有履約價通過就當天不進場、隔天再試。沒有任何持倉時當天收盤價立刻重新進場。"
)
_STRATEGY_HINT = (
    "前四種策略只差在哪一邊有買保護腳：Iron Condor 兩邊都有、裸雙賣兩邊都沒有、Jade Lizard 只有 call 邊有"
    "（put 邊裸賣）、Twisted Sister 只有 put 邊有（call 邊裸賣）。短腳條件和出場規則四種策略共用。"
    "裸賣沒有虧損上限，保證金用簡化的 Reg-T 估算（見報表說明），停損規則請務必設定。"
    "蝶式兩種：Iron Butterfly 賣出最接近現價的跨式、買進兩側翼，收權利金，現價停在中心附近獲利（報酬形狀等同「買進蝶式」）；"
    "Reverse Iron Butterfly 買進跨式、賣出兩側翼，付權利金，最大虧損 = 付出的權利金，現價離開中心獲利（報酬形狀等同「賣出蝶式」）。"
    "蝶式不用短腳條件，中心固定是最接近現價的履約價；單邊「平倉後重開」會照進場規則重算那一邊的中心（兩邊中心可能不同）；反向版的停利/停損百分比以「付出的權利金」為基準。"
)
_CENTER_HINT = (
    "建議起手值（切換到蝶式時自動帶入，可改）：天期 30、翼 2%、偏移 0。大概的合理範圍：天期 30～45、翼 1.5%～3%、"
    "偏多/偏空偏移 ±1%～3%；偏移超過約 ±3% 會讓一邊變成深價內、另一邊深價外，部位退化成只剩手續費。"
    "中心履約價 = 「現價 + 偏移」附近最接近的真實履約價，0 = 最接近現價(ATM)。正值 = 中心在現價上方，負值 = 下方。"
    "Iron Butterfly：中心在上方偏多（現價漲到中心附近獲利）、下方偏空；Reverse Iron Butterfly 相反：中心在上方偏空"
    "（現價往下離開中心獲利）、下方偏多。"
)
_LONG_LEG_LABEL = "長腳（距離短腳固定寬度，往價外再買一腳保護；裸賣的那一邊沒有長腳）"
_WING_LABEL = "翼（put 翼在中心下方、call 翼在中心上方，距離中心的目標寬度）"
_PRICING_HINT = (
    "價格全部來自 ThetaData 真實選擇權買賣報價（EOD，每日收盤後的 NBBO），不是理論算出來的。"
    "標的僅限本機已用 scripts/backfill_thetadata.py 回補過真實資料的（目前只有 SPY，"
    "要新增其他標的先跑那支腳本回補）。到期日：每天在實際掛牌的到期日裡，找剩餘天數最接近「進場天期」設定"
    "的一個，不是任意數字；長腳履約價同理，找離「短腳 ± 寬度」最接近的真實掛牌履約價。|Delta| 條件用買賣"
    "中價反推的隱含波動率計算（免費方案沒有現成 Greeks），是估算值，不是真正的官方 Delta。"
    "成交價假設：「買賣中價」不計買賣價差，偏樂觀；「極端」是賣出的腳用買價(bid)、買進的腳用賣價(ask)成交，"
    "進場賣短腳收 bid、買長腳付 ask，收盤平倉買回短腳付 ask、賣出長腳收 bid，每次都付滿整個買賣價差，是悲觀"
    "下界。只影響進場信用和收盤價出場；挑履約價的權利金/Delta 條件仍看中價；盤中觸價掛單以掛價成交，不受這個參數影響。"
)
_NO_SINGLE_SIDE_RISK_HINT = (
    "總權利金 ≥ 保護價差的寬度才進場（Jade Lizard 的 call 價差、Twisted Sister 的 put 價差被總權利金完全蓋過，"
    "那一側就沒有虧損風險）；不滿足就當天不進場、隔天再試。寬度設太大會讓這個條件幾乎永遠不成立。"
)
_WIDTH_HINT = (
    "這個寬度只是目標值：引擎會在當天實際掛牌的履約價裡，找離「短腳履約價 ± 寬度」最接近的一個當長腳，"
    f"不保證剛好等於設定的寬度（真實掛牌間距越遠離現價通常越寬，常見 $5/$10，不是處處 {S.STRIKE_STEP:g} 美元）。"
)
_RULES_HINT = (
    "每天判斷順序固定：整組規則先於單邊規則；同一範圍內 到期天數 → 停損 → 停利（不依填寫順序）。每個「範圍＋類型」"
    "最多一條，且至少要有一條到期天數規則。單邊「只平倉」＝該邊留空、另一邊照常，兩邊都空手才整組重新進場；"
    "整組平倉後一定會立刻重新進場，所以整組的動作固定為平倉（停損規則可另設「停損後不進場」天數）。停利/停損看的是"
    "整張組合單的淨價（持有的所有價差加總），例如收 1 掛 0.5 停利、掛 2 停損。盤中觸價：開盤淨價已越過門檻（跳空）用"
    "開盤價成交；否則用當天各合約的開/高/低成交價算「現價在日內低點」「現價在日內高點」兩個情境的整組淨價"
    "（put 邊與 call 邊取相反的極端價、同一邊價差兩腳取同方向），停損看最不利、停利看最有利，越過門檻就以掛價成交；"
    "明顯不合理的成交價（異常單）會被過濾、改用收盤中價。停損判斷是精確的，停利的最小值可能落在兩個情境之間，會漏掉"
    "一部分（保守）。同一天停損、停利都被碰到時先後不明，停損優先（保守）。"
)


def build() -> Callable:
    """回傳可以直接掛在按鈕 on_click 上的 open 函式(第一次呼叫才真的建 dialog，見 lazy_ui.lazy_open)。"""
    return lazy_open(_build_dialog)


# ------------------------------------------------------------------------------ 背景執行緒
def _execute(strategy: S.StrategySpec, config: S.RunConfig) -> Tuple[List[dict], List[list], List[list], dict]:
    """在背景執行緒跑：抓資料 → 回測 → 摘要。*** pandas/polars/yfinance/引擎在這裡才第一次 import ***
    (見模組開頭)。回傳 (逐筆交易 dict 清單, 標的每日收盤價 [[日期, 收盤]], 逐日權益 [[日期, 每股毛損益]], 摘要)。"""
    from app.models.backtest import engine, market_data, option_chain, stats

    df = market_data.load_market_data(config.ticker, config.start, config.end)
    chain = option_chain.OptionChain(config.ticker, date.fromisoformat(config.start), date.fromisoformat(config.end))
    trades, equity = engine.run_backtest_with_equity(df, chain, strategy, config)
    if not trades:
        raise ValueError("這組參數在回測期間沒有產生任何交易(例如短腳條件太嚴格、找不到履約價)，請調整條件")
    benchmark = [[d.strftime("%Y-%m-%d"), round(float(c), 4)] for d, c in df["close"].items()]
    return [t.to_dict() for t in trades], benchmark, [list(p) for p in equity], stats.summarize(trades)


# ------------------------------------------------------------------------------ 送資料進報表
def _run_payload(meta: dict) -> Optional[dict]:
    """把一次回測整理成報表 JS 吃的格式；逐筆檔案不存在/壞掉回傳 None。"""
    try:
        trades, benchmark, equity = backtest_store.load_trades(meta["id"])
        cfg = meta["config"]
        return {
            "id": meta["id"], "name": meta["name"], "ticker": cfg["ticker"], "start": cfg["start"],
            "end": cfg["end"], "updated_at": meta.get("updated_at", ""),
            "description": S.describe_strategy(S.strategy_from_dict(meta["strategy"]))
            + S.describe_config(S.config_from_dict(cfg)),
            "trades": trades, "benchmark": benchmark, "equity": equity,
        }
    except (OSError, ValueError, KeyError):
        return None


def _fmt_usd(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _int_if_whole(v):
    """ui.number 給的是 float，整數欄位(天期/口數)轉成 int；不是整數就原樣留著，交給驗證擋下來。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer():
        return int(v)
    return v


def _new_rule_defaults(existing: List[S.ExitRule]) -> Optional[S.ExitRule]:
    """新增規則：挑第一個還沒用過的 (範圍, 類型) 組合，並帶入該類型合理的預設值。"""
    used = {(r.scope, r.kind) for r in existing}
    for scope in (S.SCOPE_LEG, S.SCOPE_GROUP):
        for kind in (S.KIND_TAKE_PROFIT, S.KIND_STOP_LOSS, S.KIND_DTE):
            if (scope, kind) in used:
                continue
            action = S.ACTION_CLOSE_REOPEN if scope == S.SCOPE_LEG else S.ACTION_CLOSE
            if kind == S.KIND_DTE:
                return S.ExitRule(scope, kind, 15, S.UNIT_DAYS, action, S.FILL_CLOSE)
            return S.ExitRule(scope, kind, 50 if kind == S.KIND_TAKE_PROFIT else 100, S.UNIT_CREDIT_PCT,
                              action, S.FILL_CLOSE)
    return None


def _build_dialog() -> Callable:
    # ---- 報表資源：CSS 走 add_css，JS 走 run_javascript(見模組開頭說明)
    ui.add_css((_RESOURCE_DIR / "report.css").read_text(encoding="utf-8"))
    report_html = (_RESOURCE_DIR / "report.html").read_text(encoding="utf-8")
    ui.run_javascript((_RESOURCE_DIR / "report.js").read_text(encoding="utf-8"))

    strategy = S.default_strategy()   # 表單目前的策略狀態(進場/出場規則的可變 dataclass)
    sent_ids: set = set()             # 已經送進瀏覽器記憶體的回測 id
    state = {"busy": False}
    # 每次開對話框才重新掃一次(不是 import 時算一次)：使用者可能在 app 開著的期間另外跑
    # backfill_thetadata.py 回補新標的，掃資料夾很便宜，不用留著舊清單。
    _TICKER_OPTIONS = {t: t for t in S.available_tickers()}
    _default_ticker = "SPY" if "SPY" in _TICKER_OPTIONS else next(iter(_TICKER_OPTIONS), "")

    def push_runs(metas: List[dict]) -> None:
        payload = [p for p in (_run_payload(m) for m in metas) if p is not None]
        if len(payload) != len(metas):
            ui.notify("有回測的逐筆資料讀取失敗，已略過", type="warning")
        if payload:
            ui.run_javascript(f"window.BtReport.upsertRuns({json.dumps(payload, ensure_ascii=False)});")
            sent_ids.update(p["id"] for p in payload)

    def show_in_report(run_id: str) -> None:
        tabs.value = TAB_REPORT
        ui.run_javascript(f"window.BtReport.show({json.dumps(run_id)});")

    # =============================================================================== 執行回測
    async def run_and_save(run_id: Optional[str], name: str, strat: S.StrategySpec, cfg: S.RunConfig) -> bool:
        if state["busy"]:
            ui.notify("已有回測執行中，請等它跑完", type="warning")
            return False
        state["busy"] = True
        busy_row.set_visibility(True)
        try:
            trades, benchmark, equity, summary = await run_blocking(_execute, strat, cfg)
            meta = backtest_store.save_run(
                run_id or backtest_store.new_run_id(), name, S.strategy_to_dict(strat), S.config_to_dict(cfg),
                summary, trades, benchmark, equity,
            )
        except Exception as e:  # noqa: BLE001 — 任何失敗(網路/參數/存檔)都要明確告知使用者，不能吞掉
            ui.notify(f"回測失敗：{e}", type="negative", multi_line=True, close_button=True, timeout=0)
            return False
        finally:
            state["busy"] = False
            busy_row.set_visibility(False)
        push_runs([meta])
        runs_list.refresh()
        show_in_report(meta["id"])
        ui.notify(f"完成：{name}（{summary['trades']} 筆，淨利 {_fmt_usd(summary['net_usd'])}）", type="positive")
        return True

    # =============================================================================== 分頁 1：新策略
    def collect() -> Tuple[S.StrategySpec, S.RunConfig]:
        strategy.entry.kind = kind_select.value
        strategy.entry.conditions.no_single_side_risk = bool(no_single_side_risk_check.value)
        strategy.entry.dte = _int_if_whole(dte_input.value)
        strategy.entry.short.combine = combine_toggle.value
        strategy.entry.long.width = width_input.value
        strategy.entry.long.width_unit = width_unit_select.value
        strategy.entry.center_offset = center_offset_input.value if center_offset_input.value is not None else 0.0
        strategy.entry.center_offset_unit = center_offset_unit_select.value
        for rule in strategy.exit_rules:
            rule.threshold = _int_if_whole(rule.threshold) if rule.kind == S.KIND_DTE else rule.threshold
            rule.cooldown_days = _int_if_whole(rule.cooldown_days or 0)   # 清空欄位是 None，當 0
        cfg = S.RunConfig(
            ticker=ticker_select.value, start=(start_input.value or "").strip(), end=(end_input.value or "").strip(),
            contracts=_int_if_whole(contracts_input.value), fill_price=fill_price_select.value,
        )
        return strategy, cfg

    async def on_run_clicked() -> None:
        strat, cfg = collect()
        errors = S.validate_strategy(strat) + S.validate_config(cfg)
        name = (name_input.value or "").strip()
        if not name:
            errors.append("請輸入策略名稱")
        error_box.clear()
        error_box.set_visibility(bool(errors))
        if errors:
            with error_box:
                for msg in errors:
                    ui.label(f"• {msg}").classes("text-negative text-sm")
            return
        await run_and_save(None, name, strat, cfg)

    def apply_to_form(meta: dict) -> None:
        """把一次回測的參數帶回「新策略」表單(修改後可以存成新策略)，並切到新策略分頁。"""
        loaded = S.strategy_from_dict(meta["strategy"])
        cfg = S.config_from_dict(meta["config"])
        strategy.entry = loaded.entry
        strategy.exit_rules = loaded.exit_rules
        name_input.value = f"{meta['name']} 複製"
        if cfg.ticker in _TICKER_OPTIONS:
            ticker_select.value = cfg.ticker
        start_input.value, end_input.value, contracts_input.value = cfg.start, cfg.end, cfg.contracts
        fill_price_select.value = cfg.fill_price
        kind_select.value = loaded.entry.kind
        no_single_side_risk_check.value = loaded.entry.conditions.no_single_side_risk
        dte_input.value = loaded.entry.dte
        combine_toggle.value = loaded.entry.short.combine
        width_input.value = loaded.entry.long.width
        width_unit_select.value = loaded.entry.long.width_unit
        center_offset_input.value = loaded.entry.center_offset
        center_offset_unit_select.value = loaded.entry.center_offset_unit
        render_conditions.refresh()
        render_rules.refresh()
        error_box.clear()
        error_box.set_visibility(False)
        tabs.value = TAB_NEW
        ui.notify("已帶入參數，修改後按「執行回測」會存成新的一筆", type="info")

    def reset_form() -> None:
        fresh = S.default_strategy()
        strategy.entry, strategy.exit_rules = fresh.entry, fresh.exit_rules
        kind_select.value = fresh.entry.kind
        no_single_side_risk_check.value = fresh.entry.conditions.no_single_side_risk
        dte_input.value = fresh.entry.dte
        combine_toggle.value = fresh.entry.short.combine
        width_input.value = fresh.entry.long.width
        width_unit_select.value = fresh.entry.long.width_unit
        center_offset_input.value = fresh.entry.center_offset
        center_offset_unit_select.value = fresh.entry.center_offset_unit
        render_conditions.refresh()
        render_rules.refresh()

    @ui.refreshable
    def render_conditions() -> None:
        conds = strategy.entry.short.conditions
        for cond in conds:
            with ui.row().classes("items-center gap-2"):
                metric = ui.select(_METRIC_OPTIONS, value=cond.metric, label="指標").classes("w-32")
                op = ui.select(list(S.OPS), value=cond.op, label="比較").classes("w-20")
                value = ui.number("數值", value=cond.value, format="%g", step=0.01).classes("w-28")
                metric.on_value_change(lambda e, c=cond: setattr(c, "metric", e.value))
                op.on_value_change(lambda e, c=cond: setattr(c, "op", e.value))
                value.on_value_change(lambda e, c=cond: setattr(c, "value", e.value))
                ui.button(icon="delete", on_click=lambda _e, c=cond: remove_condition(c)).props("flat round dense color=negative")
        if not conds:
            ui.label("（尚未設定任何條件）").classes("text-caption text-grey")

    def add_condition() -> None:
        strategy.entry.short.conditions.append(S.StrikeCondition(S.METRIC_DISTANCE_PCT, ">", 3.0))
        render_conditions.refresh()

    def remove_condition(cond: S.StrikeCondition) -> None:
        strategy.entry.short.conditions.remove(cond)
        render_conditions.refresh()

    @ui.refreshable
    def render_rules() -> None:
        for rule in strategy.exit_rules:
            build_rule_row(rule)
        if not strategy.exit_rules:
            ui.label("（尚未設定任何出場規則）").classes("text-caption text-grey")

    def build_rule_row(rule: S.ExitRule) -> None:
        def unit_options(kind: str) -> Dict[str, str]:
            if kind == S.KIND_DTE:
                return {S.UNIT_DAYS: S.UNIT_LABELS[S.UNIT_DAYS]}
            return {S.UNIT_CREDIT_PCT: S.UNIT_LABELS[S.UNIT_CREDIT_PCT], S.UNIT_POINTS: S.UNIT_LABELS[S.UNIT_POINTS]}

        with ui.row().classes("items-center gap-2"):
            scope_sel = ui.select(_SCOPE_OPTIONS, value=rule.scope, label="範圍").classes("w-24")
            kind_sel = ui.select(_KIND_OPTIONS, value=rule.kind, label="類型").classes("w-28")
            threshold = ui.number("門檻", value=rule.threshold, format="%g", step=1).classes("w-24")
            unit_sel = ui.select(unit_options(rule.kind), value=rule.unit, label="單位").classes("w-32")
            action_sel = ui.select(_ACTION_OPTIONS, value=rule.action, label="動作").classes("w-36")
            fill_sel = ui.select(_FILL_OPTIONS, value=rule.fill, label="成交").classes("w-28")
            cooldown = ui.number("停損後不進場(天)", value=rule.cooldown_days, format="%d", step=1, min=0).classes("w-36")
            cooldown.tooltip("停損觸發後，這麼多個日曆天內不開任何新部位(0 = 當天收盤立刻重新進場)。只有停損規則能設。")
            ui.button(icon="delete", on_click=lambda _e, r=rule: remove_rule(r)).props("flat round dense color=negative")

        def sync_constraints() -> None:
            """範圍/類型改變時，把不能自由選的欄位鎖成固定值(整組動作固定平倉、到期天數單位固定天且只看收盤價)。"""
            is_dte = rule.kind == S.KIND_DTE
            if is_dte:
                rule.unit, rule.fill = S.UNIT_DAYS, S.FILL_CLOSE
            elif rule.unit == S.UNIT_DAYS:
                rule.unit = S.UNIT_CREDIT_PCT
            if rule.scope == S.SCOPE_GROUP:
                rule.action = S.ACTION_CLOSE
            unit_sel.set_options(unit_options(rule.kind), value=rule.unit)
            fill_sel.value = rule.fill
            action_sel.value = rule.action
            fill_sel.set_enabled(not is_dte)
            action_sel.set_enabled(rule.scope == S.SCOPE_LEG)
            if rule.kind != S.KIND_STOP_LOSS:
                rule.cooldown_days = 0
                cooldown.value = 0
            cooldown.set_enabled(rule.kind == S.KIND_STOP_LOSS)

        def on_scope(e) -> None:
            rule.scope = e.value
            sync_constraints()

        def on_kind(e) -> None:
            rule.kind = e.value
            sync_constraints()

        scope_sel.on_value_change(on_scope)
        kind_sel.on_value_change(on_kind)
        threshold.on_value_change(lambda e: setattr(rule, "threshold", e.value))
        unit_sel.on_value_change(lambda e: setattr(rule, "unit", e.value))
        action_sel.on_value_change(lambda e: setattr(rule, "action", e.value))
        fill_sel.on_value_change(lambda e: setattr(rule, "fill", e.value))
        cooldown.on_value_change(lambda e: setattr(rule, "cooldown_days", e.value))
        sync_constraints()

    def add_rule() -> None:
        rule = _new_rule_defaults(strategy.exit_rules)
        if rule is None:
            ui.notify("「範圍＋類型」組合都已經有規則了", type="warning")
            return
        strategy.exit_rules.append(rule)
        render_rules.refresh()

    def remove_rule(rule: S.ExitRule) -> None:
        strategy.exit_rules.remove(rule)
        render_rules.refresh()

    # =============================================================================== 分頁 2：策略清單
    async def prompt_rename(meta: dict) -> None:
        with ui.dialog() as d, ui.card().classes("w-96 gap-3"):
            ui.label("改名").classes("text-lg font-semibold")
            new_name = ui.input("策略名稱", value=meta["name"]).classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=lambda: d.submit(None)).props("flat")
                ui.button("儲存", on_click=lambda: d.submit((new_name.value or "").strip()))
        result = await d
        d.delete()
        if not result or result == meta["name"]:
            return
        try:
            backtest_store.rename_run(meta["id"], result)
        except OSError as e:
            ui.notify(f"改名失敗：{e}", type="negative")
            return
        ui.run_javascript(f"window.BtReport.renameRun({json.dumps(meta['id'])}, {json.dumps(result, ensure_ascii=False)});")
        runs_list.refresh()

    async def prompt_delete(meta: dict) -> None:
        with ui.dialog() as d, ui.card().classes("w-96 gap-3"):
            ui.label("刪除回測").classes("text-lg font-semibold")
            ui.label(f"確定要刪除「{meta['name']}」？逐筆交易資料會一併刪除，無法復原。")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=lambda: d.submit(False)).props("flat")
                ui.button("刪除", on_click=lambda: d.submit(True)).props("color=negative")
        confirmed = await d
        d.delete()
        if not confirmed:
            return
        try:
            backtest_store.delete_run(meta["id"])
        except OSError as e:
            ui.notify(f"刪除失敗：{e}", type="negative")
            return
        sent_ids.discard(meta["id"])
        ui.run_javascript(f"window.BtReport.removeRun({json.dumps(meta['id'])});")
        runs_list.refresh()

    async def rerun(meta: dict) -> None:
        await run_and_save(meta["id"], meta["name"], S.strategy_from_dict(meta["strategy"]), S.config_from_dict(meta["config"]))

    def view_run(meta: dict) -> None:
        if meta["id"] not in sent_ids:
            push_runs([meta])
        show_in_report(meta["id"])

    @ui.refreshable
    def runs_list() -> None:
        metas = backtest_store.list_runs()
        if not metas:
            ui.label("還沒有回測紀錄。到「新策略」分頁設定並執行一次。").classes("text-grey q-pa-md")
            return
        with ui.column().classes("w-full gap-1"):
            with ui.row().classes("w-full items-center text-caption text-grey gap-2 no-wrap"):
                for label, cls in (("策略名稱", "w-56"), ("標的", "w-16"), ("期間", "w-52"), ("筆數", "w-14"),
                                   ("淨利(1口)", "w-24"), ("最大回撤", "w-24"), ("更新時間", "w-40")):
                    ui.label(label).classes(cls)
            for meta in metas:
                summary, cfg = meta.get("summary", {}), meta.get("config", {})
                with ui.row().classes("w-full items-center gap-2 no-wrap q-py-xs").style("border-top: 1px solid #2b3040"):
                    ui.label(meta["name"]).classes("w-56 ellipsis").tooltip("\n".join(
                        S.describe_strategy(S.strategy_from_dict(meta["strategy"])) + S.describe_config(S.config_from_dict(cfg))))
                    ui.label(cfg.get("ticker", "")).classes("w-16")
                    ui.label(f"{cfg.get('start', '')} ~ {cfg.get('end', '')}").classes("w-52")
                    ui.label(str(summary.get("trades", ""))).classes("w-14")
                    net = summary.get("net_usd", 0.0)
                    ui.label(_fmt_usd(net)).classes("w-24 " + ("text-positive" if net > 0 else "text-negative" if net < 0 else ""))
                    ui.label(_fmt_usd(summary.get("max_drawdown_usd", 0.0))).classes("w-24 text-negative")
                    ui.label(meta.get("updated_at", "").replace("T", " ")).classes("w-40")
                    ui.button("查看", on_click=lambda _e, m=meta: view_run(m)).props("flat dense")
                    ui.button("重跑", on_click=lambda _e, m=meta: rerun(m)).props("flat dense")
                    ui.button("套用參數", on_click=lambda _e, m=meta: apply_to_form(m)).props("flat dense")
                    ui.button("改名", on_click=lambda _e, m=meta: prompt_rename(m)).props("flat dense")
                    ui.button("刪除", on_click=lambda _e, m=meta: prompt_delete(m)).props("flat dense color=negative")

    # =============================================================================== 組畫面
    def on_tab_change(e) -> None:
        if e.value == TAB_LIST:
            runs_list.refresh()
        elif e.value == TAB_REPORT:
            # 第一次進報表才把存檔裡的回測送進瀏覽器記憶體；之後只補送還沒送過的。
            missing = [m for m in backtest_store.list_runs() if m["id"] not in sent_ids]
            if missing:
                push_runs(missing)
            ui.run_javascript("window.BtReport.refresh();")

    with ui.dialog().props("maximized") as dialog, ui.card().classes("w-full h-full no-wrap gap-2"):
        with ui.row().classes("w-full items-center"):
            ui.label("選擇權回測").classes("text-xl font-semibold")
            with ui.row().classes("items-center gap-2") as busy_row:
                ui.spinner(size="sm")
                ui.label("回測執行中…（第一次會下載歷史資料，需要一點時間）").classes("text-caption text-grey")
            busy_row.set_visibility(False)
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat round")

        with ui.tabs().classes("w-full") as tabs:
            ui.tab(TAB_NEW, label="新策略")
            ui.tab(TAB_LIST, label="策略清單")
            ui.tab(TAB_REPORT, label="報表")
        tabs.on_value_change(on_tab_change)

        with ui.tab_panels(tabs, value=TAB_NEW).classes("w-full flex-grow overflow-auto"):
            # ------------------------------------------------------------- 新策略
            with ui.tab_panel(TAB_NEW).classes("gap-3"):
                with ui.card().props("flat bordered").classes("w-full gap-2"):
                    ui.label("基本設定").classes("text-subtitle1 font-semibold")
                    with ui.row().classes("items-center gap-3"):
                        name_input = ui.input("策略名稱", value=f"策略 {datetime.now():%m-%d %H:%M}").classes("w-64")
                        ticker_select = ui.select(_TICKER_OPTIONS, value=_default_ticker, label="標的").classes("w-40")
                        start_input = ui.input("起始日期", value=S.RunConfig().start).props('mask="####-##-##"').classes("w-36")
                        end_input = ui.input("結束日期", value="2025-01-01").props('mask="####-##-##"').classes("w-36")
                        contracts_input = ui.number("口數", value=1, format="%d", min=1, step=1).classes("w-24")
                        fill_price_select = ui.select(
                            _FILL_PRICE_OPTIONS, value=S.RunConfig().fill_price, label="成交價假設").classes("w-72")
                    if not _TICKER_OPTIONS:
                        ui.label("本機還沒有任何真實選擇權資料，先跑 `uv run python scripts/backfill_thetadata.py --symbol SPY` 回補").classes("text-negative text-caption")
                    ui.label(_PRICING_HINT).classes("text-caption text-grey")

                with ui.card().props("flat bordered").classes("w-full gap-2"):
                    ui.label("進場規則").classes("text-subtitle1 font-semibold")
                    with ui.row().classes("items-center gap-3"):
                        kind_select = ui.select(_STRATEGY_OPTIONS, value=strategy.entry.kind, label="策略").classes("w-64")
                        dte_input = ui.number("進場天期 (DTE)", value=strategy.entry.dte, format="%d", min=2, step=1).classes("w-36")
                    with ui.column().classes("gap-2") as short_leg_box:
                        ui.label("短腳履約價條件").classes("text-body2")
                        with ui.row().classes("items-center gap-3"):
                            ui.label("合併方式")
                            combine_toggle = ui.toggle(
                                {S.COMBINE_AND: "全部滿足 (and)", S.COMBINE_OR: "任一滿足 (or)"},
                                value=strategy.entry.short.combine,
                            )
                        render_conditions()
                        ui.button("新增條件", icon="add", on_click=add_condition).props("flat dense")
                    with ui.column().classes("gap-2") as long_leg_box:
                        long_leg_label = ui.label(_LONG_LEG_LABEL).classes("text-body2")
                        with ui.row().classes("items-center gap-3"):
                            width_input = ui.number("寬度", value=strategy.entry.long.width, format="%g").classes("w-28")
                            width_unit_select = ui.select(_WIDTH_UNIT_OPTIONS, value=strategy.entry.long.width_unit, label="單位").classes("w-32")
                        ui.label(_WIDTH_HINT).classes("text-caption text-grey")
                    with ui.column().classes("gap-2") as center_box:
                        ui.label("中心履約價偏移（蝶式）").classes("text-body2")
                        with ui.row().classes("items-center gap-3"):
                            center_offset_input = ui.number(
                                "偏移", value=strategy.entry.center_offset, format="%g", step=1).classes("w-28")
                            center_offset_unit_select = ui.select(
                                _WIDTH_UNIT_OPTIONS, value=strategy.entry.center_offset_unit, label="單位").classes("w-32")
                        ui.label(_CENTER_HINT).classes("text-caption text-grey")
                    with ui.column().classes("gap-1") as entry_conditions_box:
                        ui.label("進場條件").classes("text-body2")
                        no_single_side_risk_check = ui.checkbox(
                            "無單邊風險（總權利金 ≥ 保護價差寬度）", value=strategy.entry.conditions.no_single_side_risk)
                        ui.label(_NO_SINGLE_SIDE_RISK_HINT).classes("text-caption text-grey")

                    def sync_width_input() -> None:
                        # 美元寬度必須是最小跳動的整數倍；現價 % 換算出來的寬度由引擎四捨五入到格點。
                        if width_unit_select.value == S.WIDTH_USD:
                            width_input.props(f"min={S.STRIKE_STEP:g} step={S.STRIKE_STEP:g}")
                        else:
                            width_input.props("min=0.1 step=0.5")

                    width_unit_select.on_value_change(lambda _: sync_width_input())
                    sync_width_input()

                    was_centered = {"v": kind_select.value in S.STRATEGIES_CENTERED}

                    def sync_kind_widgets() -> None:
                        # 裸雙賣沒有長腳，寬度整區隱藏；「無單邊風險」只對一邊裸賣、一邊價差的策略有意義。
                        long_leg_box.set_visibility(bool(S.PROTECTED_SIDES.get(kind_select.value)))
                        # 蝶式的中心履約價固定是最接近現價的履約價，不用短腳條件；寬度是「翼」距離中心的寬度。
                        centered = kind_select.value in S.STRATEGIES_CENTERED
                        short_leg_box.set_visibility(not centered)
                        center_box.set_visibility(centered)
                        if centered and not was_centered["v"]:   # 從其他策略切到蝶式：帶入建議的進場起手值
                            d = S.CENTERED_ENTRY_DEFAULTS
                            dte_input.value, width_input.value, width_unit_select.value = d["dte"], d["width"], d["width_unit"]
                            center_offset_input.value, center_offset_unit_select.value = d["center_offset"], d["center_offset_unit"]
                        was_centered["v"] = centered
                        long_leg_label.set_text(_WING_LABEL if centered else _LONG_LEG_LABEL)
                        entry_conditions_box.set_visibility(kind_select.value in S.STRATEGIES_WITH_SINGLE_SIDE_RISK_CHECK)

                    kind_select.on_value_change(lambda _: sync_kind_widgets())
                    sync_kind_widgets()
                    ui.label(_STRATEGY_HINT).classes("text-caption text-grey")
                    ui.label(_SEMANTICS_HINT).classes("text-caption text-grey")

                with ui.card().props("flat bordered").classes("w-full gap-2"):
                    ui.label("出場規則").classes("text-subtitle1 font-semibold")
                    render_rules()
                    ui.button("新增規則", icon="add", on_click=add_rule).props("flat dense")
                    ui.label(_RULES_HINT).classes("text-caption text-grey")

                error_box = ui.column().classes("w-full gap-0")
                error_box.set_visibility(False)
                with ui.row().classes("items-center gap-3"):
                    ui.button("執行回測", icon="play_arrow", on_click=on_run_clicked)
                    ui.button("重設為預設值", on_click=reset_form).props("flat")

            # ------------------------------------------------------------- 策略清單
            with ui.tab_panel(TAB_LIST):
                runs_list()

            # ------------------------------------------------------------- 報表
            with ui.tab_panel(TAB_REPORT):
                ui.html(report_html, sanitize=False).classes("w-full")

    return dialog.open
