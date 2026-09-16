"""
NiceGUI 版帳戶權益顯示，取代 `app/views/equity_widget.py`
(`EquityWidget(QWidget)`)。不是獨立分頁/視窗，是嵌在下單面板右側的一個
常駐區塊(見 `web_order_entry_widget.py` 的說明)——使用者下單前後都常常
要看一眼權益/購買力，跟下單面板放在一起比另外開一個按鈕方便。

跟 Qt 版一樣直接讀 `ib_client.ib.accountSummaryAsync()`(第一次連線時
ib_async 就自動訂閱、之後其實是讀本地快取)，`accountSummaryEvent` 有推
播時整批重新查一次、重畫整張表，不逐欄位更新。
"""
import asyncio

from nicegui import ui

from app.models.ib_client import IBClient

# 跟 app/views/equity_widget.py::_TAG_LABELS 同一份對照表，故意保留兩份
# 不合併成共用模組——Qt 版之後遷移完成會整支刪除，屆時這份是唯一留下來
# 的，不需要為了短期共用多繞一層 import。
_TAG_LABELS = {
    "AccountType": "帳戶類型",
    "NetLiquidation": "淨清算價值",
    "TotalCashValue": "現金總額",
    "SettledCash": "已交割現金",
    "AccruedCash": "應計現金",
    "BuyingPower": "購買力",
    "EquityWithLoanValue": "含融資額度的權益",
    "PreviousDayEquityWithLoanValue": "前一日含融資額度的權益",
    "GrossPositionValue": "部位總市值",
    "RegTEquity": "Reg T 權益",
    "RegTMargin": "Reg T 保證金",
    "SMA": "特別備忘錄帳戶(SMA)",
    "InitMarginReq": "原始保證金需求",
    "MaintMarginReq": "維持保證金需求",
    "AvailableFunds": "可用資金",
    "ExcessLiquidity": "剩餘流動性",
    "Cushion": "安全緩衝比例",
    "FullInitMarginReq": "完整原始保證金需求",
    "FullMaintMarginReq": "完整維持保證金需求",
    "FullAvailableFunds": "完整可用資金",
    "FullExcessLiquidity": "完整剩餘流動性",
    "LookAheadNextChange": "下次保證金規則變動時間",
    "LookAheadInitMarginReq": "預期原始保證金需求",
    "LookAheadMaintMarginReq": "預期維持保證金需求",
    "LookAheadAvailableFunds": "預期可用資金",
    "LookAheadExcessLiquidity": "預期剩餘流動性",
    "HighestSeverity": "最高風險等級",
    "DayTradesRemaining": "當日可用當沖次數",
    "DayTradesRemainingT+1": "T+1 可用當沖次數",
    "DayTradesRemainingT+2": "T+2 可用當沖次數",
    "DayTradesRemainingT+3": "T+3 可用當沖次數",
    "DayTradesRemainingT+4": "T+4 可用當沖次數",
    "Leverage": "槓桿倍數",
    "CashBalance": "現金餘額",
    "TotalCashBalance": "現金餘額合計",
    "StockMarketValue": "股票市值",
    "OptionMarketValue": "選擇權市值",
    "FutureOptionValue": "期貨選擇權市值",
    "FuturesPNL": "期貨損益",
    "NetLiquidationByCurrency": "各幣別淨清算價值",
    "UnrealizedPnL": "未實現損益",
    "RealizedPnL": "已實現損益",
    "ExchangeRate": "匯率",
    "AccruedDividend": "應計股利",
    "AccruedInterest": "應計利息",
}

_COLUMNS = [
    {"name": "tag", "label": "欄位", "field": "tag", "align": "left"},
    {"name": "value", "label": "數值", "field": "value", "align": "right"},
    {"name": "currency", "label": "幣別", "field": "currency", "align": "left"},
]


def build(ib_client: IBClient) -> ui.column:
    ib = ib_client.ib

    with ui.column().classes("gap-1 w-64") as container:
        with ui.row().classes("items-center gap-2"):
            ui.label("帳戶權益").classes("text-sm font-semibold")
            refresh_btn = ui.button(icon="refresh").props("flat dense round size=sm")
        status_label = ui.label("尚未查詢").classes("text-xs text-grey")
        table = ui.table(columns=_COLUMNS, rows=[], row_key="tag").classes("w-full text-xs").props(
            "dense flat bordered hide-bottom",
        )

    async def _refresh() -> None:
        rows = [av for av in await ib.accountSummaryAsync() if av.account != "All"]
        table.rows = [
            {"tag": _TAG_LABELS.get(av.tag, av.tag), "value": av.value, "currency": av.currency}
            for av in rows
        ]
        status_label.text = f"已更新 ({len(rows)} 筆)" if rows else "查無資料，確認是否已連線"

    def _on_account_summary_event(_value) -> None:
        # ib_async 自己的 Event callback，不是在 asyncio Task 裡呼叫，要自
        # 己排程——跟 app/views/equity_widget.py::_on_account_summary_event()
        # 同一個理由。
        asyncio.ensure_future(_refresh())

    ib.accountSummaryEvent += _on_account_summary_event
    # *** 這個訂閱沒有解除機制，一定要在分頁斷線時取消，不然每次重新整理
    # 網頁都會多疊一份 handler ***：跟 web_quote_board_page.py 的
    # `ui.context.client.on_disconnect(quote_client.unsubscribe_all)` 同一
    # 個理由，ib_client 是 process 級單例，`ib.accountSummaryEvent` 會跨
    # 頁面重新整理持續存在。
    ui.context.client.on_disconnect(lambda: ib.accountSummaryEvent.disconnect(_on_account_summary_event))

    refresh_btn.on_click(_refresh)
    asyncio.ensure_future(_refresh())

    return container
