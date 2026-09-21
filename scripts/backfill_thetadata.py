"""
把 ThetaData 的選擇權每日收盤資料(EOD：OHLC/成交量/收盤買賣價)回補到本機，給日後用「真實報價」跑回測用。

跑法(商品用 --symbol 指定，必填，可以一次給多個)：
    uv run python scripts/backfill_thetadata.py --symbol SPY
    uv run python scripts/backfill_thetadata.py --symbol SPY QQQ IWM
    uv run python scripts/backfill_thetadata.py --symbol SPY --start 2026-08-01 --end 2026-09-18
    uv run python scripts/backfill_thetadata.py --symbol SPY --dry-run     # 只列出會抓哪些月份，不打 API
    uv run python scripts/backfill_thetadata.py --symbol SPY --refetch     # 已經抓過的月份也重抓

API key 讀專案根目錄 .env 的 THETADATA_API_KEY。存到 pref/backtest/thetadata/<商品>/YYYY-MM.parquet
(整個 pref/ 已 gitignore)，一個月一檔，*** 可以隨時中斷、重跑：已經抓齊的月份會自動跳過 ***；
還沒收完的當月、以及上次只抓一半的月份會重抓。每天跑一次就能補上新的交易日。

*** 免費帳號的限制(2026-09 實測) ***：只能抓 2023-06-01 之後的 EOD 資料(更早的日期會回
PERMISSION_DENIED，這支腳本遇到就直接停下來，不會重試)；資料延遲 1 天；限制 30 次請求/分鐘。
這裡一個月只打一次請求(expiration='*' 一次拿整個月所有到期日/履約價)，一個月約 20~30 秒，
整段約 40 個月、20 分鐘上下。`greeks_eod`(現成的 IV/Delta)需要 Standard 訂閱，免費版沒有，
IV 之後要自己用 Black-Scholes 從買賣價反推。

只保留回測用得到的欄位(丟掉交易所代碼、條件碼、快照時間)：
    date, expiration, strike, right(CALL/PUT), open, high, low, close, volume, bid, ask, bid_size, ask_size
其中 bid/ask 是收盤後的買賣價(NBBO)，close 是當天最後一筆成交價(可能是很早以前的成交，流動性差的
合約 close 不能拿來當報價，用 bid/ask)。

*** 選填的雲端鏡像(Cloudflare R2，見 `app/models/backtest/cloud_store.py`) ***：`.env` 有設定
R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_ENDPOINT_URL/R2_BUCKET_NAME 四個變數時，抓到新月份存檔
後會順便上傳；本機缺的月份會先試著從 R2 下載，下載成功就不用再打一次 ThetaData API(換機器/雲端
session 也不會受 30 次/分鐘限制拖慢)。四個變數沒設定就完全不會有網路動作，行為跟原本一樣只用本機。
"""
import argparse
import calendar
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 讓 `python scripts/xxx.py` 找得到 app 套件

from dotenv import load_dotenv  # noqa: E402

from app.models.backtest import cloud_store  # noqa: E402
from app.paths import PREF_DIR, PROJECT_ROOT  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")  # R2_* 這幾個變數要進 os.environ，cloud_store.enabled() 才讀得到

THETADATA_DIR = PREF_DIR / "backtest" / "thetadata"
FREE_TIER_FIRST_DATE = date(2023, 6, 1)   # 免費帳號 EOD 最早可查的日期
KEEP_COLUMNS = ["expiration", "strike", "right", "open", "high", "low", "close", "volume", "bid", "ask", "bid_size", "ask_size"]
MIN_REQUEST_INTERVAL = 2.5    # 秒。30 次/分鐘 = 每 2 秒一次，抓一個月本身就要 20 秒，這只是保險
RETRY_ATTEMPTS = 4
COMPLETE_MARGIN_DAYS = 5      # 判斷「這個月抓齊了」時容許的頭尾空隙(週末/假日/資料延遲 1 天)


class PermissionDenied(Exception):
    """帳號方案不含這段日期的資料，或認證失敗。不是暫時性錯誤，重試沒有用。"""


def theta_today() -> date:
    """ThetaData 判斷「今天」用的是美股交易日(美東時區)，不能用系統本地時區的
    date.today()：跑這支腳本的機器如果是 UTC(比美東快 4~5 小時)，在美東還沒
    跨到隔天前，date.today() 就已經超前算出隔天的日期，當成 --end 預設值送出
    去會變成 ThetaData 眼中的未來日期，直接被 INVALID_ARGUMENT 拒絕、重試也沒用。"""
    return datetime.now(ZoneInfo("America/New_York")).date()


def last_available_date() -> date:
    """EOD 整條鏈(expiration='*')能請求的最後一天：美東「昨天」。

    當天(含盤中與剛收盤)的資料 ThetaData 不允許用萬用到期日抓，會回
    INVALID_ARGUMENT「Cannot fetch current-day data without specifying an
    expiration」，重試沒有用；當天 EOD 也還沒定稿，所以一律截到昨天。"""
    return theta_today() - timedelta(days=1)


def month_ranges(start: date, end: date):
    """依序列出涵蓋 [start, end] 的每個月：(該月檔名 YYYY-MM, 要請求的起日, 要請求的迄日)，頭尾裁到 start/end。"""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        yield f"{year:04d}-{month:02d}", max(first, start), min(last, end)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def parquet_path(symbol: str, month: str) -> Path:
    return THETADATA_DIR / symbol / f"{month}.parquet"


def r2_key(symbol: str, month: str) -> str:
    return f"{symbol}/{month}.parquet"


def already_complete(path: Path, want_start: date, want_end: date) -> bool:
    """檔案存在，而且資料的日期範圍已經涵蓋要求的區間(頭尾各容許 COMPLETE_MARGIN_DAYS 天的空隙)。
    這樣「只抓了月中一段」或「當月還沒收完」的檔案，之後範圍變大或日子過了都會被判定要重抓。"""
    if not path.exists():
        return False
    import polars as pl

    try:
        bounds = pl.scan_parquet(path).select(pl.col("date").min().alias("lo"), pl.col("date").max().alias("hi")).collect().row(0)
    except Exception:  # noqa: BLE001 — 檔案壞掉就當作沒抓過，直接重抓覆蓋
        return False
    lo, hi = bounds
    if lo is None or hi is None:
        return False
    return lo <= want_start + timedelta(days=COMPLETE_MARGIN_DAYS) and hi >= want_end - timedelta(days=COMPLETE_MARGIN_DAYS)


def fetch_month(client, symbol: str, start: date, end: date):
    """抓 [start, end] 整個區間的整條選擇權鏈，回傳 polars DataFrame(可能是空的)。暫時性錯誤重試，權限問題直接丟 PermissionDenied。"""
    import polars as pl
    from thetadata.errors import AuthenticationError, NoDataFoundError

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            df = client.option_history_eod(start_date=start, end_date=end, symbol=symbol, expiration="*")
            break
        except NoDataFoundError:   # 沒有資料(商品沒有選擇權/那段時間沒有交易日)，不是錯誤，也不用重試
            return pl.DataFrame()
        except AuthenticationError as e:   # API key 錯誤/過期，重試沒有用
            raise PermissionDenied(f"認證失敗，檢查 .env 的 THETADATA_API_KEY：{e}") from e
        except Exception as e:  # noqa: BLE001 — SDK 丟的是 grpc 的 _MultiThreadedRendezvous，不同版本型別不同，用 code() 判斷
            code = e.code().name if callable(getattr(e, "code", None)) else ""
            detail = e.details() if callable(getattr(e, "details", None)) else str(e)
            if code == "PERMISSION_DENIED":
                raise PermissionDenied(detail) from e
            if code == "NOT_FOUND":
                return pl.DataFrame()
            if attempt == RETRY_ATTEMPTS:
                raise RuntimeError(f"{symbol} {start}~{end} 重試 {RETRY_ATTEMPTS} 次仍失敗：{code or type(e).__name__} {detail}") from e
            wait = 5 * attempt
            print(f"    暫時性錯誤({code or type(e).__name__})，{wait} 秒後重試 {attempt}/{RETRY_ATTEMPTS - 1}：{str(detail)[:120]}")
            time.sleep(wait)
    if df.is_empty():
        return df
    return df.with_columns(
        pl.col("created").dt.date().alias("date"),
        pl.col("expiration").str.to_date(),
        pl.col("right").cast(pl.String),
    ).select(["date"] + KEEP_COLUMNS)


def check_month(df, symbol: str, month: str) -> None:
    """抓下來的月資料做基本檢查，有問題只警告(資料還是存，讓你自己決定要不要用)。"""
    import polars as pl

    dup = df.group_by(["date", "expiration", "strike", "right"]).len().filter(pl.col("len") > 1).height
    crossed = df.filter((pl.col("bid") > pl.col("ask")) & (pl.col("ask") > 0)).height
    dates = sorted(df["date"].unique().to_list())
    gaps = [(a, b) for a, b in zip(dates, dates[1:]) if (b - a).days > 4]
    warn = []
    if dup:
        warn.append(f"{dup} 組重複的 (日期,到期日,履約價,買賣權)")
    if crossed:
        warn.append(f"{crossed} 筆 bid>ask")
    if gaps:
        warn.append("日期缺口 " + "、".join(f"{a}~{b}" for a, b in gaps))
    if warn:
        print(f"    !! {symbol} {month} 資料檢查警告：{'；'.join(warn)}")


def save_parquet(df, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.sort(["date", "expiration", "strike", "right"]).write_parquet(tmp, compression="zstd")
    os.replace(tmp, path)   # 先寫暫存檔再取代，中途中斷不會留下寫一半的壞檔


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", nargs="+", required=True, metavar="SYMBOL", help="要回補的商品代號，可以一次給多個，例如 --symbol SPY QQQ")
    parser.add_argument("--start", type=date.fromisoformat, default=FREE_TIER_FIRST_DATE,
                        help=f"起始日期 YYYY-MM-DD，預設 {FREE_TIER_FIRST_DATE}(免費帳號最早可查的日期)；有付費方案可以往前調")
    parser.add_argument("--end", type=date.fromisoformat, default=last_available_date(),
                        help="結束日期 YYYY-MM-DD，預設美東昨天(當天資料不能用萬用到期日抓，超過的日期會自動截到昨天)")
    parser.add_argument("--refetch", action="store_true", help="已經抓齊的月份也重抓")
    parser.add_argument("--dry-run", action="store_true", help="只列出會抓/會跳過哪些月份，不呼叫 API")
    args = parser.parse_args()
    if args.end > last_available_date():
        print(f"提醒：--end {args.end} 是美東今天或未來，當天資料抓不到，改成 {last_available_date()}")
        args.end = last_available_date()
    if args.start > args.end:
        parser.error("--start 必須早於或等於 --end")
    args.symbol = list(dict.fromkeys(s.strip().upper() for s in args.symbol if s.strip()))
    return args


def main() -> int:
    args = parse_args()
    client = None
    exit_code = 0

    for symbol in args.symbol:
        print(f"\n=== {symbol}  {args.start} ~ {args.end} ===")
        fetched = skipped = empty = rows_total = 0
        last_request = 0.0
        for month, m_start, m_end in month_ranges(args.start, args.end):
            path = parquet_path(symbol, month)
            if not args.refetch and not path.exists() and not args.dry_run and cloud_store.enabled():
                if cloud_store.download(r2_key(symbol, month), path):
                    print(f"  {month}  從 R2 下載(換機器/雲端 session 免重打 ThetaData API)")
            if not args.refetch and already_complete(path, m_start, m_end):
                print(f"  {month}  跳過(已抓齊)")
                skipped += 1
                continue
            if args.dry_run:
                print(f"  {month}  會抓 {m_start} ~ {m_end}")
                continue

            if client is None:   # 延後到真的要打 API 才建立(--dry-run 不需要 API key，polars/thetadata 也只在這裡才載入)
                from thetadata import ThetaClient

                client = ThetaClient(dotenv_path=PROJECT_ROOT / ".env")
            time.sleep(max(0.0, MIN_REQUEST_INTERVAL - (time.time() - last_request)))
            t0 = time.time()
            last_request = t0
            try:
                df = fetch_month(client, symbol, m_start, m_end)
            except PermissionDenied as e:
                print(f"  {month}  !! 無法取得這段資料，停止：{e}")
                print("     免費帳號只能抓 2023-06-01 之後(更早的資料要升級方案)；已經抓到的月份都有保留。")
                return 2
            except RuntimeError as e:
                print(f"  {month}  !! {e}")
                exit_code = 1
                continue   # 這個月失敗不影響其他月份，最後用非 0 的結束碼提醒，重跑會補抓

            if df.is_empty():
                print(f"  {month}  沒有資料({m_start} ~ {m_end})，不存檔")
                empty += 1
                continue
            check_month(df, symbol, month)
            save_parquet(df, path)
            if cloud_store.enabled():
                cloud_store.upload(path, r2_key(symbol, month))
            days = df["date"].n_unique()
            rows_total += df.height
            fetched += 1
            print(f"  {month}  {m_start} ~ {m_end}  {days} 個交易日  {df.height:,} 筆  {os.path.getsize(path) / 1e6:.1f} MB  {time.time() - t0:.0f} 秒")

        if not args.dry_run:
            print(f"  --- {symbol}：抓了 {fetched} 個月({rows_total:,} 筆)、跳過 {skipped} 個月、沒有資料 {empty} 個月")
            if fetched + skipped == 0:
                print(f"  !! {symbol} 完全沒有資料，檢查商品代號是否正確(例如是否真的有選擇權)")
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
