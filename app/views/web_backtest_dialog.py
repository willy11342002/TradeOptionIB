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
from datetime import datetime
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
_TICKER_OPTIONS = {t: f"{t}（{idx}）" for t, idx in S.SUPPORTED_TICKERS.items()}

_SEMANTICS_HINT = (
    "短腳條件：在 OTM 履約價（間距 1 美元）上逐一檢查，and = 全部條件都滿足、or = 任一條件滿足，取通過者中離"
    "現價最近的一個；沒有履約價通過就當天不進場、隔天再試。沒有任何持倉時當天收盤價立刻重新進場。"
)
_WIDTH_HINT = (
    f"ETF 選擇權履約價最小跳動 {S.STRIKE_STEP:g} 美元：單位選「美元」時寬度必須是 {S.STRIKE_STEP:g} 的整數倍"
    f"（例如 1、2、5）；選「現價 %」時，換算出來的寬度會四捨五入到 {S.STRIKE_STEP:g} 美元格點（最少 {S.STRIKE_STEP:g} 美元）。"
)
_RULES_HINT = (
    "每天判斷順序固定：整組規則先於單邊規則；同一範圍內 到期天數 → 停利 → 停損（不依填寫順序）。每個「範圍＋類型」"
    "最多一條，且至少要有一條到期天數規則。單邊「只平倉」＝該邊留空、另一邊照常，兩邊都空手才整組重新進場；"
    "整組平倉後一定會立刻重新進場，所以整組的動作固定為平倉。盤中觸價：開盤已越過門檻用開盤價成交，"
    "否則以門檻價成交（整組用開高低收四個取樣點近似）；同一天先判斷停利再判斷停損，對策略偏樂觀。"
)


def build() -> Callable:
    """回傳可以直接掛在按鈕 on_click 上的 open 函式(第一次呼叫才真的建 dialog，見 lazy_ui.lazy_open)。"""
    return lazy_open(_build_dialog)


# ------------------------------------------------------------------------------ 背景執行緒
def _execute(strategy: S.StrategySpec, config: S.RunConfig) -> Tuple[List[dict], List[list], dict]:
    """在背景執行緒跑：抓資料 → 回測 → 摘要。*** pandas/yfinance/引擎在這裡才第一次 import ***
    (見模組開頭)。回傳 (逐筆交易 dict 清單, 標的每日收盤價 [[日期, 收盤]], 摘要)。"""
    from app.models.backtest import engine, market_data, stats

    df = market_data.load_market_data(config.ticker, config.start, config.end)
    trades = engine.run_backtest(df, strategy, config)
    if not trades:
        raise ValueError("這組參數在回測期間沒有產生任何交易(例如短腳條件太嚴格、找不到履約價)，請調整條件")
    benchmark = [[d.strftime("%Y-%m-%d"), round(float(c), 4)] for d, c in df["close"].items()]
    return [t.to_dict() for t in trades], benchmark, stats.summarize(trades)


# ------------------------------------------------------------------------------ 送資料進報表
def _run_payload(meta: dict) -> Optional[dict]:
    """把一次回測整理成報表 JS 吃的格式；逐筆檔案不存在/壞掉回傳 None。"""
    try:
        trades, benchmark = backtest_store.load_trades(meta["id"])
        cfg = meta["config"]
        return {
            "id": meta["id"], "name": meta["name"], "ticker": cfg["ticker"], "start": cfg["start"],
            "end": cfg["end"], "updated_at": meta.get("updated_at", ""),
            "description": S.describe_strategy(S.strategy_from_dict(meta["strategy"])),
            "trades": trades, "benchmark": benchmark,
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
            trades, benchmark, summary = await run_blocking(_execute, strat, cfg)
            meta = backtest_store.save_run(
                run_id or backtest_store.new_run_id(), name, S.strategy_to_dict(strat), S.config_to_dict(cfg),
                summary, trades, benchmark,
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
        strategy.entry.dte = _int_if_whole(dte_input.value)
        strategy.entry.short.combine = combine_toggle.value
        strategy.entry.long.width = width_input.value
        strategy.entry.long.width_unit = width_unit_select.value
        for rule in strategy.exit_rules:
            rule.threshold = _int_if_whole(rule.threshold) if rule.kind == S.KIND_DTE else rule.threshold
        cfg = S.RunConfig(
            ticker=ticker_select.value, start=(start_input.value or "").strip(), end=(end_input.value or "").strip(),
            contracts=_int_if_whole(contracts_input.value),
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
        ticker_select.value = cfg.ticker
        start_input.value, end_input.value, contracts_input.value = cfg.start, cfg.end, cfg.contracts
        dte_input.value = loaded.entry.dte
        combine_toggle.value = loaded.entry.short.combine
        width_input.value = loaded.entry.long.width
        width_unit_select.value = loaded.entry.long.width_unit
        render_conditions.refresh()
        render_rules.refresh()
        error_box.clear()
        error_box.set_visibility(False)
        tabs.value = TAB_NEW
        ui.notify("已帶入參數，修改後按「執行回測」會存成新的一筆", type="info")

    def reset_form() -> None:
        fresh = S.default_strategy()
        strategy.entry, strategy.exit_rules = fresh.entry, fresh.exit_rules
        dte_input.value = fresh.entry.dte
        combine_toggle.value = fresh.entry.short.combine
        width_input.value = fresh.entry.long.width
        width_unit_select.value = fresh.entry.long.width_unit
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
        sync_constraints()

    def add_rule() -> None:
        rule = _new_rule_defaults(strategy.exit_rules)
        if rule is None:
            ui.notify("六種「範圍＋類型」組合都已經有規則了", type="warning")
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
                        S.describe_strategy(S.strategy_from_dict(meta["strategy"]))))
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
                        ticker_select = ui.select(_TICKER_OPTIONS, value="SPY", label="標的").classes("w-40")
                        start_input = ui.input("起始日期", value="2015-01-01").props('mask="####-##-##"').classes("w-36")
                        end_input = ui.input("結束日期", value="2025-01-01").props('mask="####-##-##"').classes("w-36")
                        contracts_input = ui.number("口數", value=1, format="%d", min=1, step=1).classes("w-24")
                    ui.label(
                        "標的限有對應波動率指數的 ETF。價格是 Black-Scholes + 歷史波動率指數合成的，不是真實選擇權報價。"
                    ).classes("text-caption text-grey")

                with ui.card().props("flat bordered").classes("w-full gap-2"):
                    ui.label("進場規則（Iron Condor）").classes("text-subtitle1 font-semibold")
                    with ui.row().classes("items-center gap-3"):
                        dte_input = ui.number("進場天期 (DTE)", value=strategy.entry.dte, format="%d", min=2, step=1).classes("w-36")
                    ui.label("短腳履約價條件").classes("text-body2")
                    with ui.row().classes("items-center gap-3"):
                        ui.label("合併方式")
                        combine_toggle = ui.toggle(
                            {S.COMBINE_AND: "全部滿足 (and)", S.COMBINE_OR: "任一滿足 (or)"},
                            value=strategy.entry.short.combine,
                        )
                    render_conditions()
                    ui.button("新增條件", icon="add", on_click=add_condition).props("flat dense")
                    ui.label("長腳（距離短腳固定寬度，往價外再買一腳保護）").classes("text-body2")
                    with ui.row().classes("items-center gap-3"):
                        width_input = ui.number("寬度", value=strategy.entry.long.width, format="%g").classes("w-28")
                        width_unit_select = ui.select(_WIDTH_UNIT_OPTIONS, value=strategy.entry.long.width_unit, label="單位").classes("w-32")
                    ui.label(_WIDTH_HINT).classes("text-caption text-grey")

                    def sync_width_input() -> None:
                        # 美元寬度必須是最小跳動的整數倍；現價 % 換算出來的寬度由引擎四捨五入到格點。
                        if width_unit_select.value == S.WIDTH_USD:
                            width_input.props(f"min={S.STRIKE_STEP:g} step={S.STRIKE_STEP:g}")
                        else:
                            width_input.props("min=0.1 step=0.5")

                    width_unit_select.on_value_change(lambda _: sync_width_input())
                    sync_width_input()
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
