import datetime
from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QComboBox, QDoubleSpinBox,
    QSpinBox, QLabel, QPushButton, QTabWidget, QCheckBox,
    QMessageBox,
)

from app.models import capital_symbols as sym
from app.models.capital_order_client import (
    TIF_ROD, TIF_IOC, TIF_FOK, NEW_POSITION, CLOSE_POSITION,
)
from app.models.contracts import PRODUCT_MULTIPLIERS
from app.models.order_book import OrderBookManager, CONDITION_LE, CONDITION_GE
from app.services import margin

_TIF_LABELS = {"ROD": TIF_ROD, "IOC": TIF_IOC, "FOK": TIF_FOK}
_NEW_CLOSE_LABELS = {"新倉": NEW_POSITION, "平倉": CLOSE_POSITION}
_SPREAD_POINT_CHOICES = ["50", "100", "150", "200"]

NO_CONTEXT_TEXT = "尚未選擇履約價 (雙擊 T 字報價任一買價/賣價格開始下單)"

# 連續IOC 限價條件的方向：完全自動判斷，不開放使用者手動選——買賣別/新
# 倉平倉決定「成交價要比委託價更好還是更差才送」，跟 order_book.py 內部
# OrderRecord.condition_op (CONDITION_LE/GE) 不是同一個座標系：內部欄位比
# 的是「買方視角淨成本 vs 門檻」，賣方那一腳算成本時用的是 -bid，所以賣
# 方時符號要翻面才能換算成內部欄位，見 _to_internal_condition_op。


def _opposite_op(op: str) -> str:
    return CONDITION_GE if op == CONDITION_LE else CONDITION_LE


def _to_internal_condition_op(net_buyer: bool, raw_op: str) -> str:
    """raw_op 是「真實成交價」座標系的≦/≧ (見上方模組註解)。net_buyer=True
    (買方)時跟 order_book.py 的 cost-space 定義方向一致，直接照抄；
    net_buyer=False(賣方)時因為賣方那一腳的成本是 -bid、門檻也是 -price，
    符號要翻面。"""
    return raw_op if net_buyer else _opposite_op(raw_op)


def _default_raw_op(buy: bool, is_close: bool) -> str:
    """新倉：追一個有利的價格——買方≦(價格跌到才買)、賣方≧(價格漲到才
    賣)。平倉相反(使用者明確要求)：買方≧、賣方≦——例如空單平倉(買進)變
    成「價格漲到才回補」的停損方向，多單平倉(賣出)變成「價格跌到也出
    場」的停損方向。這是唯一決定觸發方向的地方，UI 不再開放手動覆蓋。"""
    natural = CONDITION_LE if buy else CONDITION_GE
    return _opposite_op(natural) if is_close else natural


class OrderEntryWidget(QWidget):
    """雙擊 T 字報價任一 Call/Put 價格格時要用的下單面板，兩個分頁：
    裸買賣 (單一商品) 跟 價差單 (群益原生複式單 SendDuplexOrder)。

    這是常駐的 dock widget 內容，不是彈出式 QDialog：UI 只建一次，雙擊
    不同的履約價時呼叫 set_context() 換掉目前鎖定的商品，不會重新開窗。

    按「送出」不會直接打到交易所，只是把這筆委託送進下單匣 (staged)，
    真正送出/暫停/改條件/刪除都在下單匣視窗做。"""

    # 目前這個下單面板算價格實際會用到的「商品代碼+要看買價還是賣價」清單
    # (裸買賣一筆、價差單兩筆)，main_window.py 接這個訊號在 T 字報價表格
    # 對應的儲存格畫框線，讓使用者一眼看出現在的委託價/現價是抓哪幾格算出
    # 來的。list 內容是 (symbol, "bid"|"ask") tuple。
    active_legs_changed = pyqtSignal(list)

    def __init__(self, order_book_manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._manager = order_book_manager
        self._product_code = None
        self._expiry_date = None
        self._strike = None
        self._is_call = None
        self._call_symbol = None
        self._put_symbol = None
        self._market_bid = None
        self._market_ask = None
        self._strike_step = 100
        self._underlying_price = None  # 標的指數現價，估算「賣出裸選擇權」保證金用(app/services/margin.py)，價差單不需要這個

        self.title_label = QLabel(NO_CONTEXT_TEXT)
        self.title_label.setWordWrap(True)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_outright_tab(), "裸買賣")
        self.tabs.addTab(self._build_duplex_tab(), "價差單")
        self.tabs.setEnabled(False)  # 還沒雙擊選過履約價之前不能下單
        self.tabs.currentChanged.connect(self._emit_active_legs)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status_label)
        layout.addStretch()

    # ------------------------------------------------------------ 換商品
    def set_context(
        self,
        product_code: str,
        expiry_date: datetime.date,
        strike: float,
        is_call: bool,
        call_bid: float,
        call_ask: float,
        put_bid: float,
        put_ask: float,
        strike_step: int,
    ) -> None:
        self._product_code = product_code
        self._expiry_date = expiry_date
        self._strike = strike
        self._is_call = is_call
        self._call_symbol = sym.build_symbol(product_code, strike, expiry_date, True)
        self._put_symbol = sym.build_symbol(product_code, strike, expiry_date, False)
        self._strike_step = strike_step
        self._market_bid = call_bid if is_call else put_bid
        self._market_ask = call_ask if is_call else put_ask

        leg1_symbol = self._call_symbol if is_call else self._put_symbol
        self.title_label.setText(f"{leg1_symbol} ({'Call' if is_call else 'Put'} {strike:g})")
        self.symbol_label.setText(leg1_symbol)
        self.status_label.setText("")
        self.tabs.setEnabled(True)

        self._on_outright_side_changed(self.out_side_combo.currentIndex())

        self.spread_cp_combo.setCurrentText("買權" if is_call else "賣權")
        default_points = str(strike_step) if str(strike_step) in _SPREAD_POINT_CHOICES else "100"
        self.spread_points_combo.setCurrentText(default_points)
        # setCurrentText 在新值跟舊值相同時不會發訊號 (例如兩次都雙擊 Call
        # 的格子)，這裡明確補呼叫一次，確保換履約價後價差單的現價/框線一
        # 定會重算，不是依賴訊號有沒有觸發。
        self._on_duplex_leg_selectors_changed()
        self._update_outright_margin_label()
        self._update_duplex_margin_label()

    def set_underlying_price(self, price: float) -> None:
        """標的指數現價，裸買賣分頁「賣出新倉」的保證金估算要用(價差單
        分頁的保證金公式跟現價無關，不受影響)。main_window.py 現貨每跳一
        次就要呼叫，跟 payoff_chart_widget.py 的 set_underlying_price 是
        同一份現價、各自獨立呼叫。"""
        self._underlying_price = price
        self._update_outright_margin_label()

    def _has_context(self) -> bool:
        return self._product_code is not None

    # ---------------------------------------------------------------- 裸買賣
    def _build_outright_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self.symbol_label = QLabel("")

        form.addRow("商品代碼", self.symbol_label)

        self.out_side_combo = QComboBox()
        self.out_side_combo.addItems(["買進", "賣出"])
        self.out_side_combo.currentIndexChanged.connect(self._on_outright_side_changed)

        self.out_price_limit_label = QLabel("")
        self.out_price_spin = QDoubleSpinBox()
        self.out_price_spin.setRange(0.1, 99999)
        self.out_price_spin.setDecimals(1)
        self.out_price_spin.setSingleStep(0.1)

        self.out_qty_spin = QSpinBox()
        self.out_qty_spin.setRange(1, 999)
        self.out_qty_spin.setValue(1)

        self.out_new_close_combo = QComboBox()
        self.out_new_close_combo.addItems(list(_NEW_CLOSE_LABELS.keys()))

        self.out_tif_combo = QComboBox()
        self.out_tif_combo.addItems(list(_TIF_LABELS.keys()))
        self.out_tif_combo.setCurrentText("ROD")
        self.out_tif_combo.currentIndexChanged.connect(self._on_outright_tif_changed)

        self.out_auto_retry_checkbox = QCheckBox("沒成交就連續送出，直到成交或取消")
        self.out_auto_retry_checkbox.setEnabled(False)  # ROD 不適用，預設關閉

        self.out_margin_label = QLabel("")

        send_btn = QPushButton("送進下單匣")
        send_btn.clicked.connect(self._on_stage_outright)

        self.out_side_combo.currentIndexChanged.connect(self._update_outright_margin_label)
        self.out_price_spin.valueChanged.connect(self._update_outright_margin_label)
        self.out_qty_spin.valueChanged.connect(self._update_outright_margin_label)
        self.out_new_close_combo.currentIndexChanged.connect(self._update_outright_margin_label)
        self.out_new_close_combo.currentIndexChanged.connect(self._update_outright_price_limit_label)

        form.addRow("買賣別", self.out_side_combo)
        form.addRow(self.out_price_limit_label, self.out_price_spin)
        form.addRow("口數", self.out_qty_spin)
        form.addRow("新倉/平倉", self.out_new_close_combo)
        form.addRow("委託條件", self.out_tif_combo)
        form.addRow("", self.out_auto_retry_checkbox)
        form.addRow("預估保證金", self.out_margin_label)
        form.addRow(send_btn)
        return tab

    def _on_outright_side_changed(self, _index: int):
        self._update_outright_price_limit_label()
        if not self._has_context():
            return
        buying = self.out_side_combo.currentText() == "買進"
        price = self._market_ask if buying else self._market_bid
        self.out_price_spin.setValue(price or self.out_price_spin.value() or 1.0)
        self._emit_active_legs()

    def _update_outright_price_limit_label(self, *_args):
        # 這個文字要跟 _on_stage_outright 送單時實際算出來的 condition_op
        # 一致，不能只看買賣別：新倉是「權利金要更好才送」(買方小於等於、
        # 賣方大於等於)，平倉是相反的停損方向 (買方大於等於、賣方小於等
        # 於)，見 _default_raw_op 說明。之前這裡寫死只看買賣別、跟新倉/平
        # 倉無關，平倉時顯示的文字其實是反的，會誤導使用者。
        buying = self.out_side_combo.currentText() == "買進"
        is_close = self.out_new_close_combo.currentText() == "平倉"
        raw_op = _default_raw_op(buying, is_close)
        self.out_price_limit_label.setText("權利金小於等於" if raw_op == CONDITION_LE else "權利金大於等於")

    def _update_outright_margin_label(self, *_args):
        """賣出裸選擇權(新倉)才需要新增保證金——買進、或平倉(不論買賣別，
        都是在減少既有部位、不會新增風險)都不需要，顯示 0 並註明原因，
        不能顯示一個對這種情況沒有意義的數字。公式/A值B值來源見
        app/services/margin.py 模組開頭。"""
        if not self._has_context():
            self.out_margin_label.setText("")
            return
        is_open = self.out_new_close_combo.currentText() == "新倉"
        is_sell = self.out_side_combo.currentText() == "賣出"
        if not (is_open and is_sell):
            self.out_margin_label.setText("0（買進/平倉不需新增保證金）")
            return
        multiplier = PRODUCT_MULTIPLIERS.get(self._product_code)
        if multiplier != margin.TXO_MULTIPLIER or self._underlying_price is None:
            self.out_margin_label.setText("")  # 非TXO家族商品或還沒有現貨報價，不硬套數字
            return
        call_put = "C" if self._is_call else "P"
        estimate = margin.short_option_margin(
            self.out_price_spin.value(), call_put, self._strike, self._underlying_price,
            multiplier, margin.TXO_ORIGINAL_A, margin.TXO_ORIGINAL_B,
        ) * self.out_qty_spin.value()
        self.out_margin_label.setText(f"{estimate:,.0f}")

    def _on_outright_tif_changed(self, _index: int):
        # ROD 送出後停在委託簿等成交，連續重送只會疊出一堆重複委託，
        # 只有 IOC/FOK 才適合「沒成交就連續送出」。
        is_rod = self.out_tif_combo.currentText() == "ROD"
        self.out_auto_retry_checkbox.setEnabled(not is_rod)
        if is_rod:
            self.out_auto_retry_checkbox.setChecked(False)
        else:
            self.out_auto_retry_checkbox.setChecked(True)

    def _on_stage_outright(self):
        if not self._has_context():
            QMessageBox.warning(self, "提醒", "請先雙擊 T 字報價選一個履約價")
            return
        leg1_symbol = self._call_symbol if self._is_call else self._put_symbol
        buy = self.out_side_combo.currentText() == "買進"
        price = self.out_price_spin.value()
        qty = self.out_qty_spin.value()
        new_close = _NEW_CLOSE_LABELS[self.out_new_close_combo.currentText()]
        tif = _TIF_LABELS[self.out_tif_combo.currentText()]
        auto_retry = self.out_auto_retry_checkbox.isChecked() and tif != TIF_ROD
        is_close = self.out_new_close_combo.currentText() == "平倉"
        condition_op = _to_internal_condition_op(buy, _default_raw_op(buy, is_close))
        self._manager.stage_outright(
            leg1_symbol, buy, price, qty, tif, new_close, auto_retry,
            call_put="C" if self._is_call else "P", strike=self._strike,
            condition_op=condition_op,
        )
        self.status_label.setText(f"已送進下單匣：{leg1_symbol}")

    # ---------------------------------------------------------------- 價差單
    def _build_duplex_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        # 買權/賣權：這一腳(雙擊那格＝leg1)是用 Call 還是 Put 建價差，預設
        # 依雙擊的格子，但兩種履約價本來就同時列在 T 字報價上，開放這裡
        # 直接切換，不用關掉重新雙擊另一邊。
        self.spread_cp_combo = QComboBox()
        self.spread_cp_combo.addItems(["買權", "賣權"])
        self.spread_cp_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        # 買方/賣方：你是這組價差的買方(付權利金)還是賣方(收權利金)。
        # 第二腳履約價的方向(較高/較低)不用另外選，固定規則：買權價差＝
        # leg1(雙擊那腳)+點數 較高的那腳；賣權價差＝leg1-點數 較低的那
        # 腳，買方/賣方只決定 leg1/leg2 誰買誰賣，不影響哪一腳履約價比較
        # 高——這是標準的買權/賣權多頭/空頭價差組法。
        self.spread_side_combo = QComboBox()
        self.spread_side_combo.addItems(["買方", "賣方"])
        self.spread_side_combo.currentIndexChanged.connect(self._update_price_limit_label)
        self.spread_side_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        self.spread_points_combo = QComboBox()
        self.spread_points_combo.addItems(_SPREAD_POINT_CHOICES)
        self.spread_points_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        self.spread_new_close_combo = QComboBox()
        self.spread_new_close_combo.addItems(list(_NEW_CLOSE_LABELS.keys()))
        self.spread_new_close_combo.currentIndexChanged.connect(self._update_price_limit_label)

        self.price_limit_label = QLabel("")
        self.spread_price_spin = QDoubleSpinBox()
        self.spread_price_spin.setRange(0.1, 99999)
        self.spread_price_spin.setDecimals(1)
        self.spread_price_spin.setSingleStep(0.1)
        self.spread_price_spin.setValue(1.0)
        self._update_price_limit_label()

        self.spread_qty_spin = QSpinBox()
        self.spread_qty_spin.setRange(1, 999)
        self.spread_qty_spin.setValue(1)

        self.spread_tif_combo = QComboBox()
        self.spread_tif_combo.addItems(["IOC", "FOK"])  # 交易所規則：複式單不開放 ROD

        self.spread_auto_retry_checkbox = QCheckBox("沒成交就連續送出，直到成交或取消")
        self.spread_auto_retry_checkbox.setChecked(True)

        self.spread_margin_label = QLabel("")

        self.spread_send_btn = QPushButton("送進下單匣")
        self.spread_send_btn.clicked.connect(self._on_stage_duplex)

        self.spread_cp_combo.currentIndexChanged.connect(self._update_duplex_margin_label)
        self.spread_side_combo.currentIndexChanged.connect(self._update_duplex_margin_label)
        self.spread_points_combo.currentIndexChanged.connect(self._update_duplex_margin_label)
        self.spread_new_close_combo.currentIndexChanged.connect(self._update_duplex_margin_label)
        self.spread_qty_spin.valueChanged.connect(self._update_duplex_margin_label)

        form.addRow("買權/賣權", self.spread_cp_combo)
        form.addRow("買方/賣方", self.spread_side_combo)
        form.addRow("價差點數", self.spread_points_combo)
        form.addRow(self.price_limit_label, self.spread_price_spin)
        form.addRow("口數", self.spread_qty_spin)
        form.addRow("新倉/平倉", self.spread_new_close_combo)
        form.addRow("委託條件", self.spread_tif_combo)
        form.addRow("", self.spread_auto_retry_checkbox)
        form.addRow("預估保證金", self.spread_margin_label)
        form.addRow(self.spread_send_btn)
        return tab

    def _update_price_limit_label(self, *_args):
        # 這個文字要跟 _on_stage_duplex 送單時實際算出來的 condition_op
        # 一致，不能只看買方/賣方：新倉是「權利金要更好才送」(買方小於等
        # 於、賣方大於等於)，平倉是相反的停損方向 (買方大於等於、賣方小
        # 於等於)，見 _default_raw_op 說明。之前這裡寫死只看買方/賣方、跟
        # 新倉/平倉無關，平倉時顯示的文字其實是反的，會誤導使用者。
        buying = self.spread_side_combo.currentText() == "買方"
        is_close = self.spread_new_close_combo.currentText() == "平倉"
        raw_op = _default_raw_op(buying, is_close)
        self.price_limit_label.setText("權利金小於等於" if raw_op == CONDITION_LE else "權利金大於等於")

    def _update_duplex_margin_label(self, *_args):
        """價差組合部位(同一到期日、同一買賣權、一買一賣)的保證金：淨收
        取權利金(你是賣方)才需要＝履約價差×契約乘數，淨付出權利金(你是
        買方)則不需要——跟標的現價/A值B值無關，公式來源見
        app/services/margin.py 的 vertical_spread_margin() 開頭說明。平
        倉一律不需要新增保證金(在減少既有部位)。"""
        if not self._has_context():
            self.spread_margin_label.setText("")
            return
        if self.spread_new_close_combo.currentText() == "平倉":
            self.spread_margin_label.setText("0（平倉不需新增保證金）")
            return
        multiplier = PRODUCT_MULTIPLIERS.get(self._product_code)
        if multiplier is None:
            self.spread_margin_label.setText("")
            return
        net_credit = self.spread_side_combo.currentText() == "賣方"
        width = int(self.spread_points_combo.currentText())
        estimate = margin.vertical_spread_margin(net_credit, width, multiplier) * self.spread_qty_spin.value()
        self.spread_margin_label.setText(f"{estimate:,.0f}" if estimate > 0 else "0（淨付出權利金，不需新增保證金）")

    def _leg2_strike(self) -> float:
        points = int(self.spread_points_combo.currentText())
        is_call = self.spread_cp_combo.currentText() == "買權"
        sign = 1 if is_call else -1
        return self._strike + sign * points

    def _compute_duplex_legs(self):
        """算出目前價差單分頁選的兩腳(商品代碼+買賣方向)，跟送單/畫框線/
        算現價共用同一套邏輯，不要各自重算一份——這段編碼規則已經拿官方
        文件範例的真實報價數字核對過(見下面註解)，抄兩份容易漏改其中一
        份造成兩邊行為不一致。沒有履約價context或第二腳履約價無效時回傳
        None。"""
        if not self._has_context():
            return None
        is_call = self.spread_cp_combo.currentText() == "買權"
        buy_spread = self.spread_side_combo.currentText() == "買方"

        other_strike = self._leg2_strike()
        if other_strike <= 0:
            return None
        origin_symbol = self._call_symbol if is_call else self._put_symbol
        other_symbol = sym.build_symbol(self._product_code, other_strike, self._expiry_date, is_call)

        # *** 期交所複式單編碼規則 (核對自官方文件《7.下單-國內期選.docx》
        # 的 Call/Put 多頭/空頭價差範例，不是猜的) ***
        # leg1/leg2 放哪一腳只跟「履約價高低」有關，跟你雙擊的是哪一腳、
        # 加點還是減點無關：
        #     買權(Call) leg1 = 較高履約價，leg2 = 較低履約價
        #     賣權(Put)  leg1 = 較低履約價，leg2 =較高履約價
        #
        # *** 買賣方向：用文件範例的真實報價數字驗證過，Call/Put 剛好相反
        # (先前版本兩者用同一套規則，導致 Put 價差的買方/賣方送反，已造
        # 成使用者實際下單方向錯誤、成交在不想要的方向) ***：
        # 用文件Call範例的報價(高履約價 bid46.5/ask47，低履約價 bid97/
        # ask99)代入「低履約價買進+高履約價賣出」算淨成本 = ask(低,99)-
        # bid(高,46.5) = +52.5 (正值=淨付出=買方付權利金)；「低賣高買」
        # 則是 -50 (負值=淨收入=賣方收權利金)——Call 的話「較低履約價那
        # 腳買進」＝買方(debit)。
        # 但 Put 履約價跟價格的關係相反 (Put 高履約價比較貴，不是低履約
        # 價)：用文件Put範例的報價(低履約價 bid62/ask64，高履約價
        # bid109/ask113)代入同樣算法，「低買高賣」= ask(低,64)-
        # bid(高,109) = -45 (負值=淨收入=賣方)，「低賣高買」= +51 (正
        # 值=買方)——Put 剛好反過來，「較低履約價那腳買進」＝賣方
        # (credit)，不是買方。
        # 所以「較低履約價那腳該買進還是賣出」不能只看買方/賣方選擇本
        # 身，要連 Call/Put 一起看：
        #     買權：較低履約價買進 = 買方；較低履約價賣出 = 賣方
        #     賣權：較低履約價買進 = 賣方；較低履約價賣出 = 買方 (相反)
        if self._strike <= other_strike:
            low_symbol, low_strike = origin_symbol, self._strike
            high_symbol, high_strike = other_symbol, other_strike
        else:
            low_symbol, low_strike = other_symbol, other_strike
            high_symbol, high_strike = origin_symbol, self._strike
        low_buy = buy_spread if is_call else not buy_spread
        low_leg = (low_symbol, low_buy, low_strike)
        high_leg = (high_symbol, not low_buy, high_strike)
        leg1_symbol, buy1, strike1 = high_leg if is_call else low_leg
        leg2_symbol, buy2, strike2 = low_leg if is_call else high_leg
        return leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2, is_call, buy_spread

    def _on_stage_duplex(self):
        legs = self._compute_duplex_legs()
        if legs is None:
            if not self._has_context():
                QMessageBox.warning(self, "提醒", "請先雙擊 T 字報價選一個履約價")
            else:
                QMessageBox.warning(self, "提醒", "第二腳履約價不能小於等於 0，請確認價差點數")
            return
        leg1_symbol, buy1, strike1, leg2_symbol, buy2, strike2, is_call, buy_spread = legs

        price = self.spread_price_spin.value()
        qty = self.spread_qty_spin.value()
        new_close = _NEW_CLOSE_LABELS[self.spread_new_close_combo.currentText()]
        tif = _TIF_LABELS[self.spread_tif_combo.currentText()]
        auto_retry = self.spread_auto_retry_checkbox.isChecked()
        call_put = "C" if is_call else "P"
        is_close = self.spread_new_close_combo.currentText() == "平倉"
        condition_op = _to_internal_condition_op(buy_spread, _default_raw_op(buy_spread, is_close))

        self._manager.stage_duplex(
            leg1_symbol, buy1, leg2_symbol, buy2,
            price, qty, tif, new_close, auto_retry,
            call_put1=call_put, strike1=strike1,
            call_put2=call_put, strike2=strike2,
            net_buyer=buy_spread,
            condition_op=condition_op,
        )
        self.status_label.setText(f"已送進下單匣：{leg1_symbol} / {leg2_symbol}")

    # ------------------------------------------------- 現價自動計算/表格框線
    def _on_duplex_leg_selectors_changed(self, *_args):
        # 買權/賣權、履約價點數、買方/賣方任何一個改變都會換掉兩腳的商品
        # 代碼或成本方向，現價要重算、T字表格的框線也要換位置。
        self._update_duplex_price_from_quotes()
        self._emit_active_legs()

    @staticmethod
    def _leg_cost(quote: Optional[dict], buy: bool) -> Optional[float]:
        if not quote:
            return None
        bid, ask = quote.get("bid"), quote.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        return ask if buy else -bid

    def _update_duplex_price_from_quotes(self):
        """用兩腳目前的即時買賣價自動算一個現價填進委託價欄位，公式跟
        order_book.py _condition_met 的 total_cost 算法一致(買方那腳用
        賣價、賣方那腳用買價、加總——哪一腳是期交所編碼規則的leg1/leg2跟
        這個加總無關，加法可交換)，取絕對值就是「現在要做這組價差要付出
        /收到多少權利金」，使用者不用自己心算兩腳報價的加減。任何一腳目
        前查不到報價(還沒訂閱到，或報價是0)就不動舊值，不要用0蓋掉使用
        者已經打好的價格。"""
        legs = self._compute_duplex_legs()
        if legs is None:
            return
        leg1_symbol, buy1, _, leg2_symbol, buy2, _, _, _ = legs
        cost1 = self._leg_cost(self._manager.get_quote(leg1_symbol), buy1)
        cost2 = self._leg_cost(self._manager.get_quote(leg2_symbol), buy2)
        if cost1 is None or cost2 is None:
            return
        self.spread_price_spin.setValue(max(round(abs(cost1 + cost2), 1), 0.1))

    def _active_legs_duplex(self) -> list:
        legs = self._compute_duplex_legs()
        if legs is None:
            return []
        leg1_symbol, buy1, _, leg2_symbol, buy2, _, _, _ = legs
        return [
            (leg1_symbol, "ask" if buy1 else "bid"),
            (leg2_symbol, "ask" if buy2 else "bid"),
        ]

    def _active_legs_outright(self) -> list:
        if not self._has_context():
            return []
        symbol = self._call_symbol if self._is_call else self._put_symbol
        buy = self.out_side_combo.currentText() == "買進"
        return [(symbol, "ask" if buy else "bid")]

    def _emit_active_legs(self, *_args):
        if not self._has_context():
            self.active_legs_changed.emit([])
            return
        if self.tabs.currentIndex() == 0:
            self.active_legs_changed.emit(self._active_legs_outright())
        else:
            self.active_legs_changed.emit(self._active_legs_duplex())
