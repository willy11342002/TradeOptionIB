"""
臺灣期交所股價指數選擇權保證金試算(委託人下單所需的「原始保證金」)。

公式來源：TAIFEX官網「結算業務>保證金>保證金訂定>股價指數選擇權」
(https://www.taifex.com.tw/cht/5/margingReqIndexOpt)，2026-09-10 查證，
逐字對照如下：

  單一部位(賣出call / 賣出put)：
    保證金 ＝ 權利金市值 ＋ MAXIMUM(A值-價外值, B值)
    call價外值：MAXIMUM((履約價格-標的指數價格)×契約乘數, 0)
    put價外值： MAXIMUM((標的指數價格-履約價格)×契約乘數, 0)
    (買進call/買進put：無，也就是 0)

  買權及賣權混合部位(同時賣出call與賣出put，履約價相同為跨式、不同為勒
  式)：
    保證金 ＝ MAXIMUM(賣出call之保證金, 賣出put之保證金)
             ＋ 保證金較低方之權利金市值 ＋ 混合部位風險保證金(C值)
    (頁面註記：C值僅適用特定身分碼，含「1 本國自然人」——本專案是自然人
    下單帳戶，屬於適用範圍，故一律套用C值折抵。)

A值/B值/C值不是這頁公式的一部分，是期交所另外公告、會隨時間調整的金額，
不受這支模組管——目前數字來自「保證金一覽表-股價指數類」
(https://www.taifex.com.tw/cht/5/indexMarging)「原始保證金」欄，
2026/08/12 版(頁面本身查證於 2026-09-10)。這裡只填了 TXO(臺指選擇權)家族
一組數字，因為 app/models/contracts.py 目前也只支援 TXO 家族商品(TX1/
TX2/TX4/TX5/TXU/TXV/TXX/TXY/TXZ 都是 TXO 的週選/月選變體，乘數同為50)。
這些金額會隨期交所公告調整，這裡沒有自動同步機制，如果之後要支援其他商
品或這組數字過期，要回去查上面那個網址重新核對，不要憑印象改。
"""

TXO_MULTIPLIER = 50.0

# 原始保證金層級(委託人開倉用)，來源見上方模組說明，2026/08/12版，單位：元
TXO_ORIGINAL_A = 187_000.0
TXO_ORIGINAL_B = 94_000.0
TXO_ORIGINAL_C = 18_800.0


def out_of_money_value(call_put: str, strike: float, underlying_price: float, multiplier: float) -> float:
    """價外值，公式定義見模組開頭。call_put 必須是 'C'/'P'。"""
    if call_put == "C":
        return max((strike - underlying_price) * multiplier, 0.0)
    if call_put == "P":
        return max((underlying_price - strike) * multiplier, 0.0)
    raise ValueError(f"call_put 必須是 'C'/'P'，收到 {call_put!r}")


def short_option_margin(premium: float, call_put: str, strike: float, underlying_price: float,
                         multiplier: float, a_value: float, b_value: float) -> float:
    """單一空頭(賣出)選擇權部位的保證金(每口，NT$)。premium 是每點權利
    金，換算權利金市值時要乘上 multiplier。"""
    premium_value = premium * multiplier
    otm_value = out_of_money_value(call_put, strike, underlying_price, multiplier)
    return premium_value + max(a_value - otm_value, b_value)


def mixed_margin(call_margin: float, call_premium_value: float,
                  put_margin: float, put_premium_value: float, c_value: float) -> float:
    """賣出call+賣出put混合部位(跨式/勒式)的保證金(每口，NT$)。
    call_margin/put_margin 是各自單獨用 short_option_margin 算出的保證
    金；「保證金較低方之權利金市值」要對應到 call_margin/put_margin 兩者
    中較小的那一邊，不是直接拿兩腳的權利金市值取小。"""
    lower_premium_value = call_premium_value if call_margin <= put_margin else put_premium_value
    return max(call_margin, put_margin) + lower_premium_value + c_value


def vertical_spread_margin(net_credit: bool, strike_width: float, multiplier: float) -> float:
    """同一到期日、同一買賣權、兩個不同履約價各一買一賣(價差組合部位)的
    保證金(每口，NT$)。公式來源同一份 TAIFEX 頁面「(二)價差組合部位」表
    格，逐字對照：
      買進低履約價call、賣出高履約價call(call多頭價差)：無
      買進高履約價put、賣出低履約價put(put空頭價差)：無
      買進高履約價call，賣出低履約價call(call空頭價差)：買進與賣出部位
        之履約價差×契約乘數
      買進低履約價put，賣出高履約價put(put多頭價差)：買進與賣出部位之
        履約價差×契約乘數

    這四種組合翻譯成「淨收付權利金」只有兩種情形(跟 call/put 本身無
    關)：淨付出權利金(net debit，你是買方付錢)＝0；淨收取權利金(net
    credit，你是賣方收錢)＝履約價差×契約乘數。net_credit=True 對應「你
    是這組價差的賣方」。

    ***注意***：這個公式假設買進/賣出兩腳到期日相同(頁面備註明文要求)，
    這支函式本身不檢查——呼叫端要自己保證兩腳到期日一致，不一致時本函式
    算出來的數字沒有意義。strike_width 必須是非負值(兩腳履約價之差的絕
    對值)。"""
    if strike_width < 0:
        raise ValueError(f"strike_width 不能是負值，收到 {strike_width!r}")
    return strike_width * multiplier if net_credit else 0.0
