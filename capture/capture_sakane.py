#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
需給コックピット — S高/S安キャプチャ (v1・最小)

引け後に全銘柄の日足を取得し、S高/S安を分類して JSON 出力する。
用途:
  1. コックピットの「本日S高/S安」パネルのソース
  2. ⑩R候補（前日S高引け銘柄）の自動抽出
  3. breadth gate 較正用の S高引け数の日次履歴

データ源 (J-Quants V2):
  - 認証: ヘッダ x-api-key（トークン交換なし）。キーは環境変数 JQUANTS_API_KEY。
  - ベースURL: https://api.jquants.com/v2/
  - 日足(全銘柄): GET /equities/bars/daily?date=YYYYMMDD
  - 銘柄一覧(市場区分): GET /equities/master?date=YYYYMMDD
  - ページング: レスポンスに pagination_key があれば、それを引数に付けて全件ループ取得。

要確認①②③ の確認状況（V2公式リファレンス）:
  ② ページング方式 …… 確定。レスポンスの "pagination_key" を次リクエストに渡してループ
     （公式 V2 Quick Start ノートブックで確認）。
  ③ 銘柄一覧のパス …… 確定。V2 では /equities/master（V1 の /listed/info から改称）。
     市場区分フィールド/コード値は本スクリプトでは MarketCode を第一候補に、名称フィールドや
     別名・大小文字ゆらぎも吸収して判定する（下記 _get / MARKET_NAMES 参照）。
  ① ストップ高/安フラグのフィールド名 …… V1 は UpperLimit / LowerLimit（"0"/"1"）。
     V2 のレスポンス実物で命名が変わっている可能性に備え、別名・大小文字を吸収して判定する。
     初回ライブ実行時は --inspect、または起動時の「キー一覧」警告で実フィールド名を確認できる。
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
BASE_URL = "https://api.jquants.com/v2"
API_KEY_ENV = "JQUANTS_API_KEY"

JST = timezone(timedelta(hours=9))

# リトライ/レート配慮
MAX_RETRIES = 5            # 429 / 5xx の最大リトライ回数
BACKOFF_BASE = 2.0         # 指数バックオフの基数（秒）: 2,4,8,16,32
PAGE_SLEEP = 0.2           # ページング取得の合間スリープ（秒）
HTTP_TIMEOUT = 30          # 1リクエストのタイムアウト（秒）

# ⑩R で除外する市場（V1基準: 0111=プライム, 0105=TOKYO PRO MARKET）。
# V2 でも JPX 共通の市場区分コードを踏襲する前提。名称ベースの保険判定も併用する。
RYU_R_EXCLUDE_CODES = {"0111", "0105"}
RYU_R_EXCLUDE_NAME_KEYS = ("プライム", "PRO")  # 名称に含まれていたら除外

# 市場区分コード → 日本語名（出力の market 表示・名称ベース判定に使用）
# 出典: J-Quants 市場区分コード（April 2022 TSE 再編後の JPX 共通コード）。
MARKET_NAMES = {
    "0101": "東証一部",
    "0102": "東証二部",
    "0104": "マザーズ",
    "0105": "TOKYO PRO MARKET",
    "0106": "JASDAQ(スタンダード)",
    "0107": "JASDAQ(グロース)",
    "0109": "その他",
    "0111": "プライム",
    "0112": "スタンダード",
    "0113": "グロース",
}

# breadth gate 閾値（引けS高ベース）
GATE_THIN = 9     # <= 9 → 厚く張れる
GATE_NORMAL = 15  # 10–15 → 通常 / >15 → 見送り


# ---------------------------------------------------------------------------
# 汎用ヘルパ（フィールド名のゆらぎ吸収）
# ---------------------------------------------------------------------------
def _get(rec, *names):
    """レコードから値を取得。完全一致 → 大小文字無視 の順に、与えた別名候補で探す。"""
    if not isinstance(rec, dict):
        return None
    for n in names:
        if n in rec:
            return rec[n]
    lower = {k.lower(): v for k, v in rec.items()}
    for n in names:
        v = lower.get(n.lower())
        if v is not None:
            return v
    return None


def _is_hit(v):
    """ストップ高/安フラグの真偽判定。V1 は文字列 "1"。bool / 数値 / "true" も許容。"""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "1.0", "true", "yes", "y")


def _num(v):
    """数値化。null/空/'-' は None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _eq(a, b):
    """価格の同値判定（float 比較の保険として微小許容）。"""
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6)


def _disp_code(code):
    """表示用コード。5桁(末尾0)なら4桁に丸める。それ以外は素のまま。"""
    c = str(code).strip()
    if len(c) == 5 and c.endswith("0"):
        return c[:4]
    return c


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def build_session(api_key):
    s = requests.Session()
    s.headers.update({"x-api-key": api_key})
    return s


def get_paged(session, path, params):
    """pagination_key を辿って全件取得し、data レコードのリストを返す。"""
    out = []
    params = dict(params)
    page = 0
    while True:
        page += 1
        body = _request_with_retry(session, path, params)
        data = body.get("data")
        if data is None:
            # data キーが無い形なら、本体がリストの可能性も一応拾う
            data = body if isinstance(body, list) else []
        out.extend(data)
        pk = body.get("pagination_key") if isinstance(body, dict) else None
        if not pk:
            break
        params["pagination_key"] = pk
        time.sleep(PAGE_SLEEP)  # レート配慮
    return out


def _request_with_retry(session, path, params):
    """1リクエスト。429/5xx は指数バックオフでリトライ。最終的に失敗なら例外。"""
    url = f"{BASE_URL}{path}"
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            _backoff(attempt, reason=f"network error: {e}")
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as e:
                last_err = e
                _backoff(attempt, reason="invalid JSON in 200 response")
                continue

        if resp.status_code in (401, 403):
            # 認証失敗は即終了（リトライしても無駄）
            raise SystemExit(
                f"[FATAL] 認証失敗 ({resp.status_code})。{API_KEY_ENV} を確認してください。"
                f" body={resp.text[:300]}"
            )

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            retry_after = resp.headers.get("Retry-After")
            last_err = f"HTTP {resp.status_code}"
            _backoff(attempt, reason=f"HTTP {resp.status_code}", retry_after=retry_after)
            continue

        # その他の 4xx は恒久的エラー扱い
        raise RuntimeError(
            f"[FATAL] {path} で HTTP {resp.status_code}: {resp.text[:300]}"
        )

    raise RuntimeError(f"[FATAL] {path} がリトライ上限に到達: {last_err}")


def _backoff(attempt, reason, retry_after=None):
    if attempt >= MAX_RETRIES:
        return  # 呼び出し側で例外化
    if retry_after:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = BACKOFF_BASE ** (attempt + 1)
    else:
        wait = BACKOFF_BASE ** (attempt + 1)
    print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {reason} -> {wait:.0f}s 待機",
          file=sys.stderr)
    time.sleep(wait)


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------
def fetch_daily(session, date_yyyymmdd):
    """指定日の全銘柄日足を取得。"""
    return get_paged(session, "/equities/bars/daily", {"date": date_yyyymmdd})


def fetch_master(session, date_yyyymmdd):
    """銘柄一覧を取得し、コード → {code, market_code, market_name} の索引を返す。"""
    records = get_paged(session, "/equities/master", {"date": date_yyyymmdd})
    index = {}
    resolved = 0
    for rec in records:
        code = _get(rec, "Code", "code", "LocalCode")
        if code is None:
            continue
        # V2 master は短縮名: Mkt=市場区分コード, MktNm=市場区分名。V1 longhand も保険で許容。
        mcode = _get(rec, "Mkt", "MarketCode", "market_code", "marketcode")
        mname = _get(rec, "MktNm", "MarketCodeName", "MarketName", "market_name", "market")
        mcode_s = str(mcode).strip() if mcode is not None else None
        if not mname:
            mname = MARKET_NAMES.get(mcode_s, mcode_s or "")
        if mcode_s or mname:
            resolved += 1
        info = {"market_code": mcode_s, "market_name": mname}
        _index_code(index, str(code).strip(), info)
    if records and resolved == 0:
        print("[WARN] /equities/master の市場区分フィールドが解決できませんでした。"
              "⑩Rのプライム/PRO除外が効きません。", file=sys.stderr)
        print("[WARN] master 実際のキー一覧: " + ", ".join(sorted(records[0].keys())),
              file=sys.stderr)
        print("[WARN] fetch_master() の市場フィールド別名(_get 引数)に追記してください。",
              file=sys.stderr)
    return index


def _index_code(index, code, info):
    """4桁/5桁(末尾0)の双方で引けるよう索引登録。"""
    index[code] = info
    if len(code) == 5 and code.endswith("0"):
        index.setdefault(code[:4], info)
    elif len(code) == 4:
        index.setdefault(code + "0", info)


def lookup_market(index, code):
    c = str(code).strip()
    for key in (c, _disp_code(c), c + "0" if len(c) == 4 else None):
        if key and key in index:
            return index[key]
    return {"market_code": None, "market_name": ""}


# ---------------------------------------------------------------------------
# 判定ロジック
# ---------------------------------------------------------------------------
def classify(daily_records, market_index):
    """日足レコード群を S高/S安に分類して各リストと breadth を返す。"""
    s_high_close, s_high_touch_only = [], []
    s_low_close, s_low_touch_only = [], []

    for rec in daily_records:
        # V2 フィールド名は短縮形（C/H/L/O, UL/LL）。V1 longhand も保険で許容。
        close = _num(_get(rec, "C", "Close", "close"))
        if close is None:
            continue  # 売買不成立・終日停止 → 除外

        high = _num(_get(rec, "H", "High", "high"))
        low = _num(_get(rec, "L", "Low", "low"))
        upper = _is_hit(_get(rec, "UL", "UpperLimit", "upper_limit", "upperlimit"))
        lower = _is_hit(_get(rec, "LL", "LowerLimit", "lower_limit", "lowerlimit"))

        code = _get(rec, "Code", "code", "LocalCode")
        mkt = lookup_market(market_index, code)
        entry = {
            "code": _disp_code(code),
            "close": close,
            "market": mkt["market_name"],
            "_market_code": mkt["market_code"],  # ⑩R判定用（出力時に除去）
        }

        if upper:
            if _eq(close, high):
                s_high_close.append(entry)        # S高引け（張り付き）
            else:
                s_high_touch_only.append(entry)   # タッチのみ（剥がれ）
        if lower:
            if _eq(close, low):
                s_low_close.append(entry)
            else:
                s_low_touch_only.append(entry)

    breadth = {
        "s_high_close": len(s_high_close),
        "s_high_touch_total": len(s_high_close) + len(s_high_touch_only),
        "s_low_close": len(s_low_close),
        "s_low_touch_total": len(s_low_close) + len(s_low_touch_only),
    }
    return {
        "s_high_close": s_high_close,
        "s_high_touch_only": s_high_touch_only,
        "s_low_close": s_low_close,
        "s_low_touch_only": s_low_touch_only,
        "breadth": breadth,
    }


def is_excluded_for_ryu_r(entry):
    """⑩R: プライム/PRO Market を除外。"""
    mcode = entry.get("_market_code")
    if mcode in RYU_R_EXCLUDE_CODES:
        return True
    name = entry.get("market") or ""
    return any(k in name for k in RYU_R_EXCLUDE_NAME_KEYS)


def build_ryu_r_candidates(s_high_close):
    """S高引け かつ プライム/PRO 以外。"""
    out = []
    for e in s_high_close:
        if is_excluded_for_ryu_r(e):
            continue
        out.append({"code": e["code"], "close": e["close"], "market": e["market"]})
    return out


def gate_label(s_high_close_count):
    if s_high_close_count <= GATE_THIN:
        return "厚く張れる"
    if s_high_close_count <= GATE_NORMAL:
        return "通常"
    return "見送り"


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def _strip_internal(entries):
    return [{"code": e["code"], "close": e["close"], "market": e["market"]} for e in entries]


def build_output(date_iso, classified):
    return {
        "date": date_iso,
        "breadth": classified["breadth"],
        "ryu_r_candidates": build_ryu_r_candidates(classified["s_high_close"]),
        "s_high_close": _strip_internal(classified["s_high_close"]),
        "s_high_touch_only": _strip_internal(classified["s_high_touch_only"]),
        "s_low_close": _strip_internal(classified["s_low_close"]),
        "s_low_touch_only": _strip_internal(classified["s_low_touch_only"]),
    }


def write_output(out, out_dir, date_yyyymmdd):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"sakane_{date_yyyymmdd}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def update_history(out, out_dir):
    """日次 breadth を data/history.json に upsert（同日は上書き）。サイトのトレンド用。"""
    path = os.path.join(out_dir, "history.json")
    hist = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
        except (ValueError, OSError):
            hist = []
    row = {
        "date": out["date"],
        "s_high_close": out["breadth"]["s_high_close"],
        "s_high_touch_total": out["breadth"]["s_high_touch_total"],
        "s_low_close": out["breadth"]["s_low_close"],
        "s_low_touch_total": out["breadth"]["s_low_touch_total"],
        "ryu_r": len(out["ryu_r_candidates"]),
    }
    hist = [r for r in hist if r.get("date") != out["date"]]
    hist.append(row)
    hist.sort(key=lambda r: r.get("date", ""))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def print_summary(out, date_yyyymmdd):
    b = out["breadth"]
    gate = gate_label(b["s_high_close"])
    print("=" * 56)
    print(f"  需給キャプチャ速報  {out['date']}  ({date_yyyymmdd})")
    print("=" * 56)
    print(f"  S高 引け(張り付き) : {b['s_high_close']}   "
          f"(タッチ込み総数 {b['s_high_touch_total']})")
    print(f"  S安 引け(張り付き) : {b['s_low_close']}   "
          f"(タッチ込み総数 {b['s_low_touch_total']})")
    print(f"  breadth gate       : S高引け {b['s_high_close']} -> 【{gate}】"
          f"  (<= {GATE_THIN}:厚く / {GATE_THIN+1}-{GATE_NORMAL}:通常 / >{GATE_NORMAL}:見送り)")
    cand = out["ryu_r_candidates"]
    print(f"  ⑩R候補 (S高引け・プライム/PRO除外) : {len(cand)}件")
    if cand:
        print("    " + " ".join(f"{c['code']}({c['market']})" for c in cand))
    print("=" * 56)


# ---------------------------------------------------------------------------
# 日付ユーティリティ
# ---------------------------------------------------------------------------
def today_jst():
    return datetime.now(JST).date()


def to_yyyymmdd(d):
    return d.strftime("%Y%m%d")


def to_iso(d):
    return d.strftime("%Y-%m-%d")


def parse_date_arg(s):
    s = s.strip().replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


# ---------------------------------------------------------------------------
# inspect（要確認①③の実フィールド確認用）
# ---------------------------------------------------------------------------
def inspect_fields(session, date_yyyymmdd):
    print(f"[inspect] /equities/bars/daily?date={date_yyyymmdd} の先頭レコードのキー:")
    daily = fetch_daily(session, date_yyyymmdd)
    if not daily:
        print("  (データ無し。非営業日かもしれません)")
    else:
        print("  keys:", sorted(daily[0].keys()))
        print("  sample:", json.dumps(daily[0], ensure_ascii=False)[:500])
    print(f"[inspect] /equities/master?date={date_yyyymmdd} の先頭レコードのキー:")
    master = get_paged(session, "/equities/master", {"date": date_yyyymmdd})
    if not master:
        print("  (データ無し)")
    else:
        print("  keys:", sorted(master[0].keys()))
        print("  sample:", json.dumps(master[0], ensure_ascii=False)[:500])


def warn_if_fields_missing(daily_records):
    """先頭レコードに想定フィールドが無ければ、実キーを警告出力（要確認①の自己検証）。"""
    if not daily_records:
        return
    rec = daily_records[0]
    upper = _get(rec, "UL", "UpperLimit", "upper_limit", "upperlimit")
    lower = _get(rec, "LL", "LowerLimit", "lower_limit", "lowerlimit")
    close = _get(rec, "C", "Close", "close")
    missing = []
    if upper is None:
        missing.append("UL/LL(上限・下限フラグ) 相当")
    if close is None:
        missing.append("C(Close) 相当")
    if missing:
        print("[WARN] 想定フィールドが見つかりません: " + ", ".join(missing),
              file=sys.stderr)
        print("[WARN] 実際のキー一覧: " + ", ".join(sorted(rec.keys())), file=sys.stderr)
        print("[WARN] capture/capture_sakane.py の別名候補(_get 引数)を実フィールド名に"
              "合わせて修正してください。", file=sys.stderr)
        return
    # フィールドは解決済み。UL/LL が 0/1 フラグか制限値の価格かを確認できるよう
    # 先頭レコードの実値をサンプル出力する（検証フェーズ用の INFO）。
    print(f"[INFO] サンプル(先頭 Code={_get(rec, 'Code', 'code')}): "
          f"O={_get(rec, 'O', 'Open', 'open')} H={_get(rec, 'H', 'High', 'high')} "
          f"L={_get(rec, 'L', 'Low', 'low')} C={close} UL={upper} LL={lower}")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def resolve_target_date(session, args):
    """取得対象日を決める。
    - --date 指定: その日（lookback=0）。
    - evening: 当日（lookback=0）。非営業日ならデータ無しで正常終了。
    - morning: 前日から遡って直近の営業日（lookback=args.lookback）。
    戻り値: (date_obj, daily_records)  営業日が見つからなければ daily_records は []。
    """
    if args.date:
        start = parse_date_arg(args.date)
        lookback = 0
    elif args.mode == "morning":
        start = today_jst() - timedelta(days=1)
        lookback = args.lookback
    else:  # evening / default
        start = today_jst()
        lookback = 0

    for i in range(lookback + 1):
        d = start - timedelta(days=i)
        ymd = to_yyyymmdd(d)
        print(f"[fetch] /equities/bars/daily?date={ymd} ...")
        daily = fetch_daily(session, ymd)
        if daily:
            return d, daily
        print(f"  データ無し（非営業日?）: {ymd}")
    return None, []


def main(argv=None):
    parser = argparse.ArgumentParser(description="J-Quants V2 S高/S安キャプチャ")
    parser.add_argument("--date", help="取得日 YYYYMMDD（指定するとmode無視）")
    parser.add_argument("--mode", choices=["evening", "morning"], default="evening",
                        help="evening=当日 / morning=直近営業日を遡って再取得")
    parser.add_argument("--lookback", type=int, default=7,
                        help="morningで遡る最大日数（既定7）")
    parser.add_argument("--out-dir", default=None,
                        help="出力ディレクトリ（既定: リポジトリ直下 data/）")
    parser.add_argument("--inspect", action="store_true",
                        help="判定せず、実レスポンスのフィールド名を表示して終了")
    args = parser.parse_args(argv)

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"[FATAL] 環境変数 {API_KEY_ENV} が未設定です。", file=sys.stderr)
        return 2

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    session = build_session(api_key)

    if args.inspect:
        ymd = to_yyyymmdd(parse_date_arg(args.date)) if args.date \
            else to_yyyymmdd(today_jst())
        inspect_fields(session, ymd)
        return 0

    # 1) 対象日 + 日足
    date_obj, daily = resolve_target_date(session, args)
    if not daily:
        print("[OK] 対象範囲に営業日データがありませんでした。何もせず正常終了します。")
        return 0
    ymd = to_yyyymmdd(date_obj)
    print(f"[fetch] 取得 {len(daily)} 銘柄  (date={ymd})")
    warn_if_fields_missing(daily)

    # 2) 銘柄一覧（市場区分）
    print(f"[fetch] /equities/master?date={ymd} ...")
    market_index = fetch_master(session, ymd)
    print(f"[fetch] 銘柄一覧 {len(market_index)} エントリ")

    # 3) 判定 + 出力
    classified = classify(daily, market_index)
    out = build_output(to_iso(date_obj), classified)
    path = write_output(out, out_dir, ymd)
    print(f"[write] {path}")
    hpath = update_history(out, out_dir)
    print(f"[write] {hpath}")
    print_summary(out, ymd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
