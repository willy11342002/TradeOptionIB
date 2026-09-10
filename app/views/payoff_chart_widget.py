"""
到期損益圖：目前未平倉部位(+可選下單匣未成交委託)的到期損益曲線、兩平
點。用 pyqtgraph 畫(跟 candlestick_chart.py 同一套 pg 元件)，這裡只需要
單一座標軸，不需要多面板連動，所以不重用 PriceChartWidget，另外開一個
widget。

*** 刻意採用的簡化(沒有在畫面上顯示，只寫在這裡跟 payoff.py 開頭) ***
每一腳都用自己履約價的到期內含價值計算，不同到期日的部位加總在同一條
X 軸(標的價格)上時，忽略各腳實際到期日不同、忽略時間價值/隱含波動率。
見 app/services/payoff.py 開頭的說明。

*** 只納入方向已確認的部位 ***
app/models/positions.py 的 Position.buy 在買賣別欄位無法判讀時會是
None，這種部位不會出現在這張圖裡(不能瞎猜方向去畫，猜錯損益方向會完全
相反，比不顯示更危險)。下單匣的委託(OrderLeg.buy)是使用者下單當下自己
決定的，不是從 broker 猜的，方向可靠，會正常畫進曲線B。

*** 複式單(TM合併列)：兩腳的方向/淨權利金怎麼分配，交給
app/models/positions.py 的 Position.payoff_legs() 統一決定(目前的結論
是「一買一賣的價差組合」，不是兩腳同方向——這個結論被使用者現場糾正過一
次，細節跟核對過程見那支檔案開頭的說明)，這裡不重複寫一份，只負責把
payoff_legs() 給的 (Contract, buy, premium) 攤平成 PayoffLeg 加總。
"""
import numpy as np
import pyqtgraph as pg
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QLabel

# 圖表跟上面那兩行文字資訊的垂直空間比例(圖表佔七成)，用 QVBoxLayout 的
# stretch 權重控制，不是寫死高度(dock 大小使用者可以自己拖動調整)。
INFO_AREA_STRETCH = 30
CHART_AREA_STRETCH = 70

from app.models.contracts import parse_symbol
from app.models.order_book import OrderBookManager, TERMINAL_STATUSES
from app.models.positions import PositionManager
from app.services import margin
from app.services.payoff import (
    PayoffLeg, combined_payoff, find_breakevens, payoff_extremes, price_axis_range,
)

POSITION_CURVE_COLOR = "#1a5fb4"
PENDING_CURVE_COLOR = "#e5a50a"
ZERO_LINE_COLOR = "#888888"
BREAKEVEN_COLOR = "#c0392b"
UNDERLYING_PRICE_COLOR = "#9141ac"

# 跟 main_window.py:44-45 同一組紅漲綠跌顏色常數(避免循環 import，數值保
# 持同步即可，見 position_widgets.py 開頭同樣的做法)。
COLOR_PROFIT = "#e05050"
COLOR_LOSS = "#3ecf6e"

SAMPLE_POINTS = 400


def _position_legs(manager: PositionManager):
    legs = []
    for position in manager.positions:
        payoff_legs = position.payoff_legs()
        if payoff_legs is None:
            continue  # 買賣別欄位無法判讀，見 Position.payoff_legs() 的說明，不能瞎猜方向
        for leg, buy, premium in payoff_legs:
            legs.append(PayoffLeg(
                strike=leg.strike, call_put=leg.call_put, buy=buy,
                qty=position.qty, premium=premium, multiplier=leg.multiplier,
            ))
    return legs


def _pending_legs(order_book_manager: OrderBookManager):
    """*** record.price 是複式單的「淨價」(net price)，不是每一腳各自的
    權利金 (見 capital_order_client.py:257 bstrPrice/CLAUDE.md 的說明)。
    之前這裡誤把 record.price 當成兩腳「各自」的權利金去算，兩腳的
    -price/+price 剛好互相抵消，等於完全沒把淨權利金/收付方向算進損益，
    畫出來的最大虧損/最大獲利都是錯的(缺淨權利金那一截)。***

    正確作法：兩腳先各自用履約價算「純內含價值」(premium 都先當 0)，淨
    價只記一次、記在「buy 跟 record.net_buyer 相同的第一腳」上，其餘腳一
    律記 0——裸買賣只有一腳，一定符合，直接吃到全部淨價；一般價差(兩腳
    方向相反)剛好只有一腳符合，效果等於「兩腳純內含價值價差 ± 淨權利
    金」，才是正確的價差損益(net_buyer 付淨價就是減，net_buyer 收淨價就
    是加)。用「第一腳」而不是「每一腳各自比對」是為了防呆：萬一兩腳方
    向剛好相同(理論上複式單不會這樣下，但沒有程式碼強制擋)，也不會兩腳
    都符合、把淨價重複記兩次。"""
    legs = []
    for record in order_book_manager.records:
        if record.status in TERMINAL_STATUSES:
            continue
        price_assigned = False
        for leg in record.legs:
            if leg.call_put is None or leg.strike is None:
                continue  # 沒有履約價/買賣權資訊的腳，理論上不會發生，防呆跳過
            try:
                multiplier = parse_symbol(leg.symbol).multiplier
            except ValueError:
                continue  # 商品代碼解析不出來，寧可少畫這一腳，不要用猜的乘數
            if not price_assigned and leg.buy == record.net_buyer:
                premium = record.price
                price_assigned = True
            else:
                premium = 0.0
            legs.append(PayoffLeg(
                strike=leg.strike, call_put=leg.call_put, buy=leg.buy,
                qty=record.qty, premium=premium, multiplier=multiplier,
            ))
    return legs


def _estimate_short_margin(effective_legs, underlying_price: float):
    """效果同 app/services/margin.py 開頭公式，回傳目前顯示中部位的「原始
    保證金」估計總額(NT$)。underlying_price=None(還沒收到現貨報價)或部位
    含非 TXO 家族商品(乘數不是50，A/B/C數字不適用，不硬套)時回傳 None，
    畫面要顯示成空白，不能顯示一個算錯的數字。

    *** 刻意採用的簡化 ***：只有「剛好一腳空頭call+一腳空頭put、口數相
    同」才套用混合部位(跨式/勒式)的C值折抵；其餘情形(單腳、或口數不對
    等、或超過兩腳空頭)一律逐腳加總各自的單腳保證金，不套用任何跨腳折
    抵。這是保守估計(可能比 TAIFEX 實際收取的更高，但不會低估)，不是精確
    模擬全帳戶所有可能的組合部位折抵規則。"""
    if underlying_price is None:
        return None
    short_legs = [
        leg for leg in effective_legs
        if not leg.buy and leg.call_put is not None and leg.strike is not None
    ]
    if not short_legs:
        return 0.0
    if any(leg.multiplier != margin.TXO_MULTIPLIER for leg in short_legs):
        return None

    calls = [leg for leg in short_legs if leg.call_put == "C"]
    puts = [leg for leg in short_legs if leg.call_put == "P"]
    if len(short_legs) == 2 and len(calls) == 1 and len(puts) == 1 and calls[0].qty == puts[0].qty:
        call_leg, put_leg = calls[0], puts[0]
        call_margin = margin.short_option_margin(
            call_leg.premium, "C", call_leg.strike, underlying_price, call_leg.multiplier,
            margin.TXO_ORIGINAL_A, margin.TXO_ORIGINAL_B,
        )
        put_margin = margin.short_option_margin(
            put_leg.premium, "P", put_leg.strike, underlying_price, put_leg.multiplier,
            margin.TXO_ORIGINAL_A, margin.TXO_ORIGINAL_B,
        )
        per_lot = margin.mixed_margin(
            call_margin, call_leg.premium * call_leg.multiplier,
            put_margin, put_leg.premium * put_leg.multiplier,
            margin.TXO_ORIGINAL_C,
        )
        return per_lot * call_leg.qty

    return sum(
        margin.short_option_margin(
            leg.premium, leg.call_put, leg.strike, underlying_price, leg.multiplier,
            margin.TXO_ORIGINAL_A, margin.TXO_ORIGINAL_B,
        ) * leg.qty
        for leg in short_legs
    )


class PayoffChartWidget(QWidget):
    def __init__(self, position_manager: PositionManager, order_book_manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._position_manager = position_manager
        self._order_book_manager = order_book_manager
        self._underlying_price = None
        self._effective_legs = []  # 上次 _redraw 算出來的那組 legs，set_underlying_price 每次跳價都要重算保證金，不能只靠 positions_changed/records_changed 觸發

        layout = QVBoxLayout(self)

        info_area = QWidget()
        info_layout = QVBoxLayout(info_area)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_line1 = QHBoxLayout()
        self.max_profit_label = QLabel("")
        self.max_loss_label = QLabel("")
        info_line1.addWidget(self.max_profit_label)
        info_line1.addWidget(self.max_loss_label)
        info_line1.addStretch(1)
        info_layout.addLayout(info_line1)
        self.breakeven_label = QLabel("")
        info_layout.addWidget(self.breakeven_label)
        self.margin_label = QLabel("")
        info_layout.addWidget(self.margin_label)
        layout.addWidget(info_area, INFO_AREA_STRETCH)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("bottom", "標的價格")
        self.plot_widget.setLabel("left", "到期損益 (NT$)")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        # 這張圖是「隨部位/下單匣即時變化的儀表板」，不是給使用者手動探索
        # 的圖表——鎖死縮放/拖曳，X/Y 範圍永遠自動貼齊目前資料範圍(見文件
        # `enableAutoRange`)，不會被使用者不小心滾動/拖走之後卡在舊範圍。
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        self.plot_widget.enableAutoRange()
        layout.addWidget(self.plot_widget, CHART_AREA_STRETCH)

        self._position_curve = self.plot_widget.plot([], [], pen=pg.mkPen(POSITION_CURVE_COLOR, width=2), name="目前部位")
        self._pending_curve = self.plot_widget.plot([], [], pen=pg.mkPen(PENDING_CURVE_COLOR, width=2, style=pg.QtCore.Qt.DashLine), name="含下單匣")
        self._zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(ZERO_LINE_COLOR, width=1))
        self.plot_widget.addItem(self._zero_line)
        self._underlying_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(UNDERLYING_PRICE_COLOR, width=1, style=pg.QtCore.Qt.DotLine))
        self._underlying_line.setVisible(False)
        self.plot_widget.addItem(self._underlying_line)
        self._breakeven_lines = []

        self._position_manager.positions_changed.connect(self._redraw)
        self._order_book_manager.records_changed.connect(self._redraw)
        self._redraw()

    def set_underlying_price(self, price: float) -> None:
        self._underlying_price = price
        self._underlying_line.setPos(price)
        self._underlying_line.setVisible(True)
        self._update_margin_label()

    def _clear_breakeven_lines(self) -> None:
        for line in self._breakeven_lines:
            self.plot_widget.removeItem(line)
        self._breakeven_lines = []

    def _update_info_labels(self, effective_legs, breakevens) -> None:
        """最大獲利/最大虧損/損平點，都是照「目前顯示中那條最完整的曲
        線」算(有下單匣就含下單匣，跟畫兩平點虛線用同一組 legs，不要兩邊
        算的東西對不起來)。"""
        extremes = payoff_extremes(effective_legs)
        if extremes.max_profit is None:
            self.max_profit_label.setText("")
            self.max_loss_label.setText("")
        else:
            profit_text = "無上限" if not extremes.max_profit_bounded else f"{extremes.max_profit:,.0f}"
            loss_text = "無下限" if not extremes.max_loss_bounded else f"{extremes.max_loss:,.0f}"
            self.max_profit_label.setText(
                f"<b>最大獲利：<span style='color:{COLOR_PROFIT}'>{profit_text}</span></b>&nbsp;&nbsp;&nbsp;"
            )
            self.max_loss_label.setText(
                f"<b>最大虧損：<span style='color:{COLOR_LOSS}'>{loss_text}</span></b>"
            )

        if not breakevens:
            self.breakeven_label.setText("損平點：無" if effective_legs else "")
        else:
            points = "、".join(f"{p:,.0f}" for p in sorted(breakevens))
            self.breakeven_label.setText(f"損平點：{points}")

    def _update_margin_label(self) -> None:
        """獨立於 _update_info_labels 之外，因為現貨價每跳一次(main_
        window.py 呼叫 set_underlying_price)就要重算，不是只有部位/下單
        匣變化(positions_changed/records_changed)才重算。"""
        estimate = _estimate_short_margin(self._effective_legs, self._underlying_price)
        if estimate is None:
            self.margin_label.setText("")
        else:
            self.margin_label.setText(f"預估保證金(原始，僅TXO家族空頭部位)：{estimate:,.0f}")

    def _redraw(self) -> None:
        position_legs = _position_legs(self._position_manager)
        pending_legs = _pending_legs(self._order_book_manager)
        all_legs = position_legs + pending_legs
        effective_legs = all_legs if pending_legs else position_legs

        self._clear_breakeven_lines()
        if not all_legs:
            self._effective_legs = []
            self._position_curve.setData([], [])
            self._pending_curve.setData([], [])
            self._update_info_labels([], [])
            self._update_margin_label()
            return

        low, high = price_axis_range(all_legs)
        prices = np.linspace(low, high, SAMPLE_POINTS)

        position_pnl = combined_payoff(position_legs, prices) if position_legs else np.zeros_like(prices)
        self._position_curve.setData(prices, position_pnl)

        if pending_legs:
            combined_pnl = combined_payoff(all_legs, prices)
            self._pending_curve.setData(prices, combined_pnl)
        else:
            self._pending_curve.setData([], [])

        breakevens = find_breakevens(effective_legs, price_floor=low, price_ceiling=high) if effective_legs else []
        self._update_info_labels(effective_legs, breakevens)
        self._effective_legs = effective_legs
        self._update_margin_label()

        for price in breakevens:
            line = pg.InfiniteLine(
                pos=price, angle=90, pen=pg.mkPen(BREAKEVEN_COLOR, width=1, style=pg.QtCore.Qt.DashLine),
                label=f"{price:.0f}", labelOpts={"position": 0.05, "color": QColor(BREAKEVEN_COLOR)},
            )
            self.plot_widget.addItem(line)
            self._breakeven_lines.append(line)
