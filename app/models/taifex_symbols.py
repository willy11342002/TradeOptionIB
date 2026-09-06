"""
台指選擇權(TXO) 合約代碼產生規則。

依據臺灣期交所公告的商品代碼 (https://www.taifex.com.tw/cht/4/contractName1)：
    月到期            TXO
    第1/2/4/5週三到期  TX1 / TX2 / TX4 / TX5   (每月第3個週三固定是月選，不獨立掛週三選)
    第1~5週五到期      TXU / TXV / TXX / TXY / TXZ

合約代碼組成 (已用華南 XQ RTD 實際回傳的商品名稱比對驗證過，4 筆不同
類別/月份/買賣權的真實範例互相印證，可信度高)：

    產品代碼 + 年份碼(1碼，目前固定'N') + 月份(2碼，直接西元月份數字) +
    買賣權(C/P) + 履約價(整數)

例如：2026/09 週三選第2週、履約價 40900 的 Call = TX2N09C40900
     (經 RTD TF-Name 欄位驗證回傳 "台指選09W2 C 40900"，正確)

注意：開頭那個固定字母 'N' 目前不確定實際代表什麼 (可能是年份碼)，
4 筆範例都是 2026 年合約所以都是 N，還沒有跨年份的範例可以驗證，
如果用到 2027 年之後的合約發現代碼兜不起來，要重新確認這個字母。
"""
import datetime
from dataclasses import dataclass
from typing import List, Optional

XQ_YEAR_CODE = "N"  # 目前(2026)合約用這個字母，尚未驗證跨年份是否會變

MONTHLY_CODE = "TXO"
WED_WEEK_CODES = {1: "TX1", 2: "TX2", 4: "TX4", 5: "TX5"}
FRI_WEEK_CODES = {1: "TXU", 2: "TXV", 3: "TXX", 4: "TXY", 5: "TXZ"}

CATEGORY_MONTHLY = "monthly"
CATEGORY_WED = "wed"
CATEGORY_FRI = "fri"

CATEGORY_LABELS = {
    CATEGORY_MONTHLY: "月選 (TXO)",
    CATEGORY_WED: "週三選",
    CATEGORY_FRI: "週五選",
}


@dataclass
class ContractExpiry:
    category: str
    product_code: str
    expiry_date: datetime.date
    label: str
    week_no: int = 0


def _weekdays_in_month(year: int, month: int, weekday: int) -> List[datetime.date]:
    """weekday: Monday=0 ... Sunday=6 (Wed=2, Fri=4)"""
    d = datetime.date(year, month, 1)
    days = []
    while d.month == month:
        if d.weekday() == weekday:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def build_symbol(product_code: str, strike: float, expiry_date: datetime.date, is_call: bool) -> str:
    cp = "C" if is_call else "P"
    return f"{product_code}{XQ_YEAR_CODE}{expiry_date.month:02d}{cp}{int(strike)}"


def _iter_months(months_ahead: int, today: datetime.date):
    y, m = today.year, today.month
    for i in range(months_ahead):
        yy = y + (m - 1 + i) // 12
        mm = (m - 1 + i) % 12 + 1
        yield yy, mm


def list_monthly_expiries(months_ahead: int = 6, today: Optional[datetime.date] = None) -> List[ContractExpiry]:
    today = today or datetime.date.today()
    result = []
    for yy, mm in _iter_months(months_ahead, today):
        wednesdays = _weekdays_in_month(yy, mm, 2)
        if len(wednesdays) < 3:
            continue
        third_wed = wednesdays[2]
        if third_wed < today:
            continue
        result.append(ContractExpiry(
            category=CATEGORY_MONTHLY,
            product_code=MONTHLY_CODE,
            expiry_date=third_wed,
            label=f"{yy}{mm:02d}",
        ))
    return result


def list_wed_weekly_expiries(months_ahead: int = 3, today: Optional[datetime.date] = None) -> List[ContractExpiry]:
    today = today or datetime.date.today()
    result = []
    for yy, mm in _iter_months(months_ahead, today):
        wednesdays = _weekdays_in_month(yy, mm, 2)
        for idx, d in enumerate(wednesdays, start=1):
            code = WED_WEEK_CODES.get(idx)
            if not code or d < today:
                continue
            result.append(ContractExpiry(
                category=CATEGORY_WED,
                product_code=code,
                expiry_date=d,
                label=f"{yy}{mm:02d}W{idx}",
                week_no=idx,
            ))
    return result


def list_fri_weekly_expiries(months_ahead: int = 3, today: Optional[datetime.date] = None) -> List[ContractExpiry]:
    today = today or datetime.date.today()
    result = []
    for yy, mm in _iter_months(months_ahead, today):
        fridays = _weekdays_in_month(yy, mm, 4)
        for idx, d in enumerate(fridays, start=1):
            code = FRI_WEEK_CODES.get(idx)
            if not code or d < today:
                continue
            result.append(ContractExpiry(
                category=CATEGORY_FRI,
                product_code=code,
                expiry_date=d,
                label=f"{yy}{mm:02d}F{idx}",
                week_no=idx,
            ))
    return result


def list_expiries(category: str, months_ahead: int = 3, today: Optional[datetime.date] = None) -> List[ContractExpiry]:
    if category == CATEGORY_MONTHLY:
        return list_monthly_expiries(max(months_ahead, 6), today)
    if category == CATEGORY_WED:
        return list_wed_weekly_expiries(months_ahead, today)
    if category == CATEGORY_FRI:
        return list_fri_weekly_expiries(months_ahead, today)
    raise ValueError(f"未知類別: {category}")


def list_all_expiries(months_ahead: int = 3, today: Optional[datetime.date] = None) -> List[ContractExpiry]:
    """月選 + 週三選 + 週五選 合併成一個依到期日排序的清單，給單一下拉選單用。"""
    all_expiries = (
        list_monthly_expiries(max(months_ahead, 6), today)
        + list_wed_weekly_expiries(months_ahead, today)
        + list_fri_weekly_expiries(months_ahead, today)
    )
    all_expiries.sort(key=lambda e: (e.expiry_date, e.label))
    return all_expiries
