import datetime

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QDoubleSpinBox,
    QSpinBox, QLabel, QPushButton, QTabWidget, QWidget, QCheckBox,
    QMessageBox,
)

from app.models import capital_symbols as sym
from app.models.capital_order_client import (
    TIF_ROD, TIF_IOC, TIF_FOK, NEW_POSITION, CLOSE_POSITION,
)
from app.models.order_book import OrderBookManager

_TIF_LABELS = {"ROD": TIF_ROD, "IOC": TIF_IOC, "FOK": TIF_FOK}
_NEW_CLOSE_LABELS = {"新倉": NEW_POSITION, "平倉": CLOSE_POSITION}
_SPREAD_POINT_CHOICES = ["50", "100", "150", "200"]


class OrderDialog(QDialog):
    """雙擊 T 字報價任一 Call/Put 價格格時跳出的下單視窗，兩個分頁：
    裸買賣 (單一商品) 跟 價差單 (群益原生複式單 SendDuplexOrder)。

    按「送出」不會直接打到交易所，只是把這筆委託送進下單匣 (staged)，
    真正送出/暫停/改條件/刪除都在下單匣視窗做，這裡按完就直接關窗。"""

    def __init__(
        self,
        order_book_manager: OrderBookManager,
        product_code: str,
        expiry_date: datetime.date,
        strike: float,
        is_call: bool,
        call_bid: float,
        call_ask: float,
        put_bid: float,
        put_ask: float,
        strike_step: int,
        parent=None,
    ):
        super().__init__(parent)
        self._manager = order_book_manager
        self._product_code = product_code
        self._expiry_date = expiry_date
        self._strike = strike
        self._is_call = is_call
        self._call_symbol = sym.build_symbol(product_code, strike, expiry_date, True)
        self._put_symbol = sym.build_symbol(product_code, strike, expiry_date, False)
        leg1_symbol = self._call_symbol if is_call else self._put_symbol

        self.setWindowTitle(f"下單 - {leg1_symbol} ({'Call' if is_call else 'Put'} {strike:g})")
        self.setMinimumWidth(380)

        tabs = QTabWidget()
        tabs.addTab(self._build_outright_tab(leg1_symbol, call_bid, call_ask, put_bid, put_ask), "裸買賣")
        tabs.addTab(self._build_duplex_tab(strike_step), "價差單")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

    # ---------------------------------------------------------------- 裸買賣
    def _build_outright_tab(self, leg1_symbol: str, call_bid, call_ask, put_bid, put_ask) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self._market_bid = call_bid if self._is_call else put_bid
        self._market_ask = call_ask if self._is_call else put_ask

        form.addRow("商品代碼", QLabel(leg1_symbol))

        self.out_side_combo = QComboBox()
        self.out_side_combo.addItems(["買進", "賣出"])
        self.out_side_combo.currentIndexChanged.connect(self._on_outright_side_changed)

        self.out_price_spin = QDoubleSpinBox()
        self.out_price_spin.setRange(0.1, 99999)
        self.out_price_spin.setDecimals(1)
        self.out_price_spin.setSingleStep(0.1)
        self.out_price_spin.setValue(self._market_ask or self._market_bid or 1.0)

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

        send_btn = QPushButton("送進下單匣")
        send_btn.clicked.connect(self._on_stage_outright)

        form.addRow("買賣別", self.out_side_combo)
        form.addRow("委託價格", self.out_price_spin)
        form.addRow("口數", self.out_qty_spin)
        form.addRow("新倉/平倉", self.out_new_close_combo)
        form.addRow("委託條件", self.out_tif_combo)
        form.addRow("", self.out_auto_retry_checkbox)
        form.addRow(send_btn)
        return tab

    def _on_outright_side_changed(self, _index: int):
        buying = self.out_side_combo.currentText() == "買進"
        price = self._market_ask if buying else self._market_bid
        if price:
            self.out_price_spin.setValue(price)

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
        leg1_symbol = self._call_symbol if self._is_call else self._put_symbol
        buy = self.out_side_combo.currentText() == "買進"
        price = self.out_price_spin.value()
        qty = self.out_qty_spin.value()
        new_close = _NEW_CLOSE_LABELS[self.out_new_close_combo.currentText()]
        tif = _TIF_LABELS[self.out_tif_combo.currentText()]
        auto_retry = self.out_auto_retry_checkbox.isChecked() and tif != TIF_ROD
        self._manager.stage_outright(
            leg1_symbol, buy, price, qty, tif, new_close, auto_retry,
            call_put="C" if self._is_call else "P", strike=self._strike,
        )
        self.accept()

    # ---------------------------------------------------------------- 價差單
    def _build_duplex_tab(self, strike_step: int) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        # 買權/賣權：這一腳(雙擊那格＝leg1)是用 Call 還是 Put 建價差，預設
        # 依雙擊的格子，但兩種履約價本來就同時列在 T 字報價上，開放這裡
        # 直接切換，不用關掉重新雙擊另一邊。
        self.spread_cp_combo = QComboBox()
        self.spread_cp_combo.addItems(["買權", "賣權"])
        self.spread_cp_combo.setCurrentText("買權" if self._is_call else "賣權")

        # 買方/賣方：你是這組價差的買方(付權利金)還是賣方(收權利金)。
        # 第二腳履約價的方向(較高/較低)不用另外選，固定規則：買權價差＝
        # leg1(雙擊那腳)+點數 較高的那腳；賣權價差＝leg1-點數 較低的那
        # 腳，買方/賣方只決定 leg1/leg2 誰買誰賣，不影響哪一腳履約價比較
        # 高——這是標準的買權/賣權多頭/空頭價差組法。
        self.spread_side_combo = QComboBox()
        self.spread_side_combo.addItems(["買方", "賣方"])
        self.spread_side_combo.currentIndexChanged.connect(self._update_price_limit_label)

        self.spread_points_combo = QComboBox()
        self.spread_points_combo.addItems(_SPREAD_POINT_CHOICES)
        default_points = str(strike_step) if str(strike_step) in _SPREAD_POINT_CHOICES else "100"
        self.spread_points_combo.setCurrentText(default_points)

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

        self.spread_new_close_combo = QComboBox()
        self.spread_new_close_combo.addItems(list(_NEW_CLOSE_LABELS.keys()))

        self.spread_tif_combo = QComboBox()
        self.spread_tif_combo.addItems(["IOC", "FOK"])  # 交易所規則：複式單不開放 ROD

        self.spread_auto_retry_checkbox = QCheckBox("沒成交就連續送出，直到成交或取消")
        self.spread_auto_retry_checkbox.setChecked(True)

        self.spread_send_btn = QPushButton("送進下單匣")
        self.spread_send_btn.clicked.connect(self._on_stage_duplex)

        form.addRow("買權/賣權", self.spread_cp_combo)
        form.addRow("買方/賣方", self.spread_side_combo)
        form.addRow("價差點數", self.spread_points_combo)
        form.addRow(self.price_limit_label, self.spread_price_spin)
        form.addRow("口數", self.spread_qty_spin)
        form.addRow("新倉/平倉", self.spread_new_close_combo)
        form.addRow("委託條件", self.spread_tif_combo)
        form.addRow("", self.spread_auto_retry_checkbox)
        form.addRow(self.spread_send_btn)
        return tab

    def _update_price_limit_label(self):
        buying = self.spread_side_combo.currentText() == "買方"
        # 買方：付出的權利金要 <= 這個限價；賣方：收到的權利金要 >= 這個限價。
        self.price_limit_label.setText("權利金小於等於" if buying else "權利金大於等於")

    def _leg2_strike(self) -> float:
        points = int(self.spread_points_combo.currentText())
        is_call = self.spread_cp_combo.currentText() == "買權"
        sign = 1 if is_call else -1
        return self._strike + sign * points

    def _on_stage_duplex(self):
        is_call = self.spread_cp_combo.currentText() == "買權"
        buy_spread = self.spread_side_combo.currentText() == "買方"

        other_strike = self._leg2_strike()
        if other_strike <= 0:
            QMessageBox.warning(self, "提醒", "第二腳履約價不能小於等於 0，請確認價差點數")
            return
        origin_symbol = self._call_symbol if is_call else self._put_symbol
        other_symbol = sym.build_symbol(self._product_code, other_strike, self._expiry_date, is_call)

        # *** 期交所複式單編碼規則 (核對自官方文件《7.下單-國內期選.docx》
        # 的 Call/Put 多頭/空頭價差範例，不是猜的) ***
        # leg1/leg2 放哪一腳只跟「履約價高低」有關，跟你雙擊的是哪一腳、
        # 加點還是減點無關：
        #     買權(Call) leg1 = 較高履約價，leg2 = 較低履約價
        #     賣權(Put)  leg1 = 較低履約價，leg2 = 較高履約價
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

        price = self.spread_price_spin.value()
        qty = self.spread_qty_spin.value()
        new_close = _NEW_CLOSE_LABELS[self.spread_new_close_combo.currentText()]
        tif = _TIF_LABELS[self.spread_tif_combo.currentText()]
        auto_retry = self.spread_auto_retry_checkbox.isChecked()
        call_put = "C" if is_call else "P"

        self._manager.stage_duplex(
            leg1_symbol, buy1, leg2_symbol, buy2,
            price, qty, tif, new_close, auto_retry,
            call_put1=call_put, strike1=strike1,
            call_put2=call_put, strike2=strike2,
            net_buyer=buy_spread,
        )
        self.accept()
