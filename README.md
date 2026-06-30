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

実装前に V2 リファレンスで確認した結果:

- **② ページング** … 確定。レスポンス `{"data": [...], "pagination_key": "..."}`。
  `pagination_key` を次リクエストに付けて全件ループ（公式 V2 Quick Start ノートブックで確認）。
- **③ 銘柄一覧** … 確定。V2 のパスは **`/equities/master`**（V1 の `/listed/info` から改称）。
  市場区分は MarketCode（0111=プライム, 0105=TOKYO PRO MARKET 等、April 2022 再編後の JPX 共通コード）。
- **① ストップ高/安フラグ** … V1 は `UpperLimit` / `LowerLimit`（"0"/"1"）。V2 の実フィールド名は
  公式リファレンスのホストがネットワークポリシーで遮断されており机上では最終確認できなかったため、
  本スクリプトは **大小文字・別名ゆらぎを吸収**して判定する（`UpperLimit` / `upper_limit` 等）。
  初回ライブ実行時に `--inspect` で実フィールド名を確認でき、想定キーが無い場合は起動時に
  実キー一覧を WARN 出力する。万一 V2 が別名なら `capture_sakane.py` の `_get(...)` 候補へ追記する。

> **初回実行前のチェック**: `python capture/capture_sakane.py --inspect` を一度走らせ、
> `/equities/bars/daily` のキーに上限/下限フラグ相当があること、`/equities/master` の市場区分
> フィールド名を確認すること。

## スコープ外・不採用

- 後回し: PO系イベント（⑥/①B/④/⑤/②）、値上がり/値下がり比率、好悪材料突合（Kabutan）、
  ザラ場リアルタイム breadth
- 不採用: 株式分割フィード（③検証でエッジ無し）
