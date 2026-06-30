# 需給コックピット — S高/S安キャプチャ

引け後に J-Quants V2 API から全銘柄の日足を取得し、S高/S安を分類して日次 JSON
(`data/sakane_YYYYMMDD.json`) に蓄積する最小モジュール。

用途:

1. コックピットの「本日S高/S安」パネルのソース
2. ⑩R候補（前日S高引け銘柄）の自動抽出
3. breadth gate 較正用の S高引け数の日次履歴

> 重要: これは引け後の EOD データ。ザラ場リアルタイムの「今この瞬間のS高数」は出せない。
> エントリー直前のライブ gate は従来どおり証券会社側で目視する。ここで生成するのは
> (a) 閾値較正用の日次履歴、(b) 前日S高引け数（翌日のレジーム入力）。

## セットアップ

1. J-Quants の API キーを取得（V2 はダッシュボードで発行、トークン交換なし・無期限）。
2. GitHub リポジトリの **Settings → Secrets and variables → Actions** に
   `JQUANTS_API_KEY` を登録する。
3. ローカル実行時は環境変数で渡す:

   ```bash
   pip install -r capture/requirements.txt
   export JQUANTS_API_KEY="..."
   python capture/capture_sakane.py --mode evening
   ```

## 実行

| コマンド | 説明 |
| --- | --- |
| `python capture/capture_sakane.py --mode evening` | 当日分を取得（非営業日ならデータ無しで正常終了） |
| `python capture/capture_sakane.py --mode morning` | 前日から遡って直近営業日分を再取得・上書き |
| `python capture/capture_sakane.py --date 20260630` | 特定日を取得（mode無視） |
| `python capture/capture_sakane.py --inspect` | 判定せず、実レスポンスのフィールド名を表示（要確認①③の確認用） |

コンソールには S高/S安の引け・タッチ数、gate 判定、⑩R候補コード一覧の速報を出力する。

## 判定ロジック

各銘柄レコードについて:

- `Close` が null（売買不成立・終日停止）→ 除外
- **S高タッチ**: 上限フラグ == 1
- **S高引け**: 上限フラグ == 1 かつ `Close == High`（比例配分での張り付き引けを含む）
- **S安タッチ**: 下限フラグ == 1
- **S安引け**: 下限フラグ == 1 かつ `Close == Low`

### breadth gate（引けベース）

gate 参照値 = **S高引け数（張り付き）**。タッチのみ（剥がれ）は数えない。較正用に
タッチ込み総数も併記する。

| S高引け数 | 判定 |
| --- | --- |
| ≤ 9 | 厚く張れる |
| 10–15 | 通常 |
| > 15 | 見送り |

### ⑩R候補

S高引け かつ **プライム / PRO Market を除外**（MarketCode 0111=プライム, 0105=PRO）。
売り禁（空売り規制・借株不可）の判定は本モジュール未実装。寄り前に手動でチェックする前提。

## 出力スキーマ

`data/sakane_YYYYMMDD.json`:

```json
{
  "date": "2026-06-30",
  "breadth": {
    "s_high_close": 0,
    "s_high_touch_total": 0,
    "s_low_close": 0,
    "s_low_touch_total": 0
  },
  "ryu_r_candidates": [
    {"code": "XXXX", "close": 0, "market": "スタンダード"}
  ],
  "s_high_close":      [{"code": "...", "close": 0, "market": "..."}],
  "s_high_touch_only": [{"code": "...", "close": 0, "market": "..."}],
  "s_low_close":       [{"code": "...", "close": 0, "market": "..."}],
  "s_low_touch_only":  [{"code": "...", "close": 0, "market": "..."}]
}
```

## スケジュール（GitHub Actions）

`.github/workflows/capture.yml`。cron は UTC（JST = UTC + 9h）:

- **夕方便**: 平日 18:00 JST（`0 9 * * 1-5`）。当日分を取得（17:00 以降に反映）。
- **翌朝便**: 翌営業日 08:30 JST（`30 23 * * 1-5` UTC）。直近営業日分を再取得し、翌朝の
  微修正を吸収して上書き確定。

生成された JSON は `data/` にコミットされ、履歴として蓄積される。

## J-Quants V2 リファレンス確認状況（要確認①〜③）

初回ライブ取得（2026-06-29）で実フィールド名を確定済み。**V2 は短縮フィールド名**を使う:

- **② ページング** … 確定。レスポンス `{"data": [...], "pagination_key": "..."}`。
  `pagination_key` を次リクエストに付けて全件ループ。
- **① ストップ高/安フラグ** … 確定。`/equities/bars/daily` の実キーは
  `O/H/L/C`（OHLC）, `UL`（上限到達フラグ）, `LL`（下限到達フラグ）, `Vo/Va`（出来高/代金）,
  `AdjO/AdjH/AdjL/AdjC/AdjVo/AdjFactor`（調整後）。`UL`/`LL` は `0`/`1` フラグ。
- **③ 銘柄一覧** … 確定。V2 のパスは **`/equities/master`**（V1 の `/listed/info` から改称）。
  市場区分は **`Mkt`（コード）/ `MktNm`（名称）**。除外対象はコード 0111=プライム / 0105=PRO、
  もしくは名称に「プライム」「PRO」を含むもの（コード体系が変わっても名称で除外できる）。

> いずれも V1 longhand（`UpperLimit` / `MarketCode` 等）も保険で受け付ける。
> フィールド名が解決できない場合は起動時に実キー一覧を WARN 出力し、`--inspect` でも確認できる。

## スコープ外・不採用

- 後回し: PO系イベント（⑥/①B/④/⑤/②）、値上がり/値下がり比率、好悪材料突合（Kabutan）、
  ザラ場リアルタイム breadth
- 不採用: 株式分割フィード（③検証でエッジ無し）
