# neo-shoko-newsletter

中小企業の社長向け AI 活用記事を、Google スプレッドシート → WordPress 下書き → 編集レビュー → 公開 → LINE 配信、まで自動でつなぐシステムです。中核は [webhook-server/](webhook-server/) ディレクトリにある Cloud Run サーバーで、Cloud Scheduler から週1で起動されます。

このドキュメントは **「このシステムで何が起きているかを社長が辞書のように引ける」こと** を目的に書いています。詳細は各章を参照してください。

---

## 目次

1. [概要](#1-概要)
2. [システム全体像](#2-システム全体像)
3. [運用フロー（時系列）](#3-運用フロー時系列)
4. [エンドポイント一覧](#4-エンドポイント一覧)
5. [自動実行スケジュール](#5-自動実行スケジュール)
6. [データの保管場所](#6-データの保管場所)
7. [LINE 公式アカウント](#7-line-公式アカウント)
8. [WordPress](#8-wordpress)
9. [Google Cloud（GCP）構成](#9-google-cloudgcp構成)
10. [環境変数](#10-環境変数)
11. [アカウント・認証情報の所有者](#11-アカウント認証情報の所有者)
12. [ファイル構成](#12-ファイル構成)
13. [デプロイ手順](#13-デプロイ手順)
14. [ローカル動作確認](#14-ローカル動作確認)
15. [トラブルシュート](#15-トラブルシュート)

---

## 1. 概要

| 項目 | 内容 |
|---|---|
| プロジェクトの目的 | 中小企業の社長向けに、AI 活用ニュース・ノウハウ記事を月2回まとめて LINE 配信する |
| 配信メディア | WordPress ブログ [blog.neo-shoko.jp](https://blog.neo-shoko.jp) ＋ LINE 公式アカウント「NEO商工公式アカウント」 |
| 記事の出どころ | GAS（Google Apps Script）が外部メディアからリライトしてスプレッドシートに書き溜める |
| このサーバーの役割 | スプレッドシートに溜まった記事から「配信する価値が高いもの」を Gemini で自動選定し、WP 下書きを作成 → 担当者のレビュー後に公開 → 月2回まとめて LINE 配信 |
| 配信頻度 | 第1・第3水曜の 12:00 JST に LINE broadcast |

> **「webhook-server」とは何か**: Cloud Run 上で動く Flask アプリ。LINE から飛んでくる Webhook を受ける役目（`/webhook`）と、Cloud Scheduler から定期実行される役目（`/select-and-draft`、`/digest`）の両方を兼ねているのでこの名前。

---

## 2. システム全体像

```
┌────────────────────────────────────────────────────────────────────────┐
│  [GAS]                                                                 │
│  外部メディアから記事をリライトして                                    │
│  「chaen-ai-lab&aismileyのリライト」シート                             │
│  の aismiley_rewrite タブに書き溜める                                  │
└────────────────────────────────────────────────────────────────────────┘
                                │
                                │ 毎週金曜 12:00 JST
                                ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Cloud Scheduler: select-draft-friday                                  │
│         │                                                              │
│         ▼                                                              │
│  Cloud Run: POST /select-and-draft                                     │
│    1. 直近 7 日分の記事を読み込む                                      │
│    2. Gemini (gemini-2.5-flash) で配信価値の高い記事を最大 5 件選定    │
│    3. WordPress に「下書き (draft)」として作成                          │
│    4. LINE グループ「記事公開＿NEO商工運営」に                         │
│       「下書きを N 件作成しました」と通知                              │
└────────────────────────────────────────────────────────────────────────┘
                                │
                                │ 担当者（社長が割り振り）がレビュー・修正
                                │ → 公開してよいと判断したら、
                                │   グループに「$」＋編集URL を送信
                                ▼
┌────────────────────────────────────────────────────────────────────────┐
│  LINE: 「記事公開＿NEO商工運営」グループ                              │
│         │                                                              │
│         ▼                                                              │
│  Cloud Run: POST /webhook                                              │
│    1. メッセージから post=<id> を抽出                                  │
│    2. WordPress 側で status=draft → publish に変更                     │
│       （スラッグは ai-NNNN で自動採番）                                │
│    3. 公開した記事を「ニュースレター記事テスト」シートに追記           │
│       （これが「LINE 配信待ち」のキューになる）                        │
│    4. グループに「公開とLINE配信予約が完了しました」と返信             │
└────────────────────────────────────────────────────────────────────────┘
                                │
                                │ 毎週水曜 12:00 JST
                                ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Cloud Scheduler: digest-wednesday                                     │
│         │                                                              │
│         ▼                                                              │
│  Cloud Run: POST /digest                                               │
│    1. 第1・第3水曜以外はスキップ（毎週起動するが何もしない）           │
│    2. 「ニュースレター記事テスト」シートから                           │
│       E列（送信済み日時）が空の行を取得                                │
│    3. 「NEO商工公式アカウント」から友だち全員に LINE broadcast         │
│    4. 「ニュースレター記事」シートにアーカイブ                         │
│    5. 元の行の E列に送信日（YYYY/MM/DD）を記録 → 次回の重複を防ぐ      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 運用フロー（時系列）

| タイミング | 主体 | 何が起きる |
|---|---|---|
| 月〜金 随時 | GAS（自動） | `chaen-ai-lab&aismileyのリライト` シートに記事が追加される |
| **毎週金曜 12:00 JST** | Cloud Scheduler → サーバー（自動） | 直近1週間の記事から Gemini が最大 5 件選定し、WP に下書き作成。LINE グループ「記事公開＿NEO商工運営」に通知が飛ぶ |
| 金〜火 | 編集担当者 | 通知された下書きを WP 管理画面でレビュー・修正 |
| 担当者の判断時点 | 編集担当者 | 公開してよい記事の編集 URL を「記事公開＿NEO商工運営」グループに `$` 付きで投稿 → 即時に WP 上で公開され、配信キューに積まれる |
| **第1・第3水曜 12:00 JST** | Cloud Scheduler → サーバー（自動） | 配信キューに積まれた記事を「NEO商工公式アカウント」から友だち全員へ LINE broadcast。送信済みフラグが立つ |
| 第2・第4水曜 12:00 JST | Cloud Scheduler → サーバー（自動） | 起動はするが「第1・第3水曜ではない」のでスキップ。記事は次の配信タイミングまで溜まる |

> **ポイント**: 公開タイミング（`/webhook`）と配信タイミング（`/digest`）は分離されています。担当者が記事を公開した時点では LINE には流れず、第1・第3水曜にまとめて配信される設計です。

---

## 4. エンドポイント一覧

| Path | Method | 認証 | 起動元 | 何をするか |
|---|---|---|---|---|
| `/` | GET | なし | 任意 | ヘルスチェック。`{"status":"ok"}` を返すだけ |
| `/webhook` | GET / POST | なし（LINE 側で URL を Webhook として登録） | LINE Messaging API | DRAFT グループからの「公開してOK」メッセージを受け取り、WP の記事を publish |
| `/select-and-draft` | POST | `X-Scheduler-Secret` ヘッダー | Cloud Scheduler `select-draft-friday` | 直近1週の記事を Gemini で選定 → WP 下書き → LINE 通知 |
| `/digest` | POST | `X-Scheduler-Secret` ヘッダー | Cloud Scheduler `digest-wednesday` | 第1・第3水曜のみ、未送信の publish 済み記事を broadcast＋アーカイブ |

### 4.1 POST `/webhook` の発火条件

LINE グループ `記事公開＿NEO商工運営` で、メッセージ本文に **`$` または `＄`（全角）** を含み、かつ **WordPress 編集 URL の `post=<id>` 形式** が入っているとき。

例:
```
$ https://blog.neo-shoko.jp/wp-admin/post.php?post=1234&action=edit
```

`$` が無いメッセージは無視されます（雑談しても何も起きない）。

複数の編集 URL を 1 つのメッセージに含めれば、複数記事を一度に公開できます。

### 4.2 POST `/select-and-draft` の動作

1. `chaen-ai-lab&aismileyのリライト` の `aismiley_rewrite` タブから、B 列（処理日時）が直近 7 日以内かつ F 列（Markdown 本文）が空でない行を取得
2. 各記事の先頭 400 文字をスニペットとして Gemini に渡し、JSON 形式で「row_number」と「reason」を返してもらう
3. 返ってきた行の Markdown を HTML に変換し、WordPress に `status=draft` で投稿
4. 「記事公開＿NEO商工運営」グループに作成済みリストを push 通知

選定基準は [webhook-server/main.py](webhook-server/main.py) の `SELECTION_PROMPT` に記載（OpenAI/Anthropic などの新モデル発表、料金変更、日本企業の AI 活用事例 などを優先）。

### 4.3 POST `/digest` の動作

1. 今日が第1または第3水曜でなければそのまま `ok` を返して終了
2. `ニュースレター記事テスト` タブで E 列（送信済み日時）が空の行を取得
3. それらをまとめて「NEO商工公式アカウント」から **broadcast**（友だち全員に配信）
4. broadcast 成功後、`ニュースレター記事` タブに `[カテゴリ, 送信日, title, link]` で append
5. 元の `ニュースレター記事テスト` の E 列に送信日（YYYY/MM/DD）を書き込み、次回送らないようにする

> **失敗時の挙動**: broadcast は成功したが E 列更新が失敗した場合、HTTP 200 (`status: partial`) を返します。次回の重複送信を防ぐため、シート E 列を手動で埋める必要があります（[15. トラブルシュート](#15-トラブルシュート) 参照）。

---

## 5. 自動実行スケジュール

すべて Cloud Scheduler で管理しています。

| ジョブ名 | cron 式 (タイムゾーン) | 実行頻度 | 呼び出し先 |
|---|---|---|---|
| `select-draft-friday` | `0 12 * * 5` (Asia/Tokyo) | 毎週金曜 12:00 JST | `POST https://line-webhook-156606796405.asia-northeast1.run.app/select-and-draft` |
| `digest-wednesday` | `0 12 * * 3` (Asia/Tokyo) | 毎週水曜 12:00 JST（コード側で第1・第3週のみ実行） | `POST https://line-webhook-156606796405.asia-northeast1.run.app/digest` |

両ジョブとも HTTP ヘッダーに `X-Scheduler-Secret: <SCHEDULER_SECRET の値>` を付与しています。シークレットが一致しない呼び出しは 403 で弾かれます。

> Cloud Scheduler は月3ジョブまで無料枠です。現状2ジョブで完全無料で運用できています。

---

## 6. データの保管場所

このシステムは Google スプレッドシートを 2 つ使っています。**サービスアカウント `sheets-to-wp-bot@sheets-to-wp-draft.iam.gserviceaccount.com` を編集者として共有しておく必要があります。**

### 6.1 ソースシート（GAS が記事を書き込むほう）

| 項目 | 値 |
|---|---|
| シート名 | `chaen-ai-lab&aismileyのリライト` |
| Spreadsheet ID | `1rFhJ7bWNy6a76ecy3LMNmulHE5Iq5OM1L0yQcDNa8K0` |
| 対象タブ | `aismiley_rewrite` |
| 書き込む人 | GAS（自動） |
| 読み込む人 | このサーバー（`/select-and-draft` のみ） |

スキーマ（`/select-and-draft` が見ている列）:

| 列 | 内容 |
|---|---|
| B | 処理日時。`YYYY/MM/DD HH:MM:SS` ほか複数フォーマット許容 |
| F | 記事本文（Markdown）。先頭の `# 見出し` が WordPress 記事のタイトルになる |

A・C・D・E 列は GAS 側の管理列であり、このサーバーは触りません。

### 6.2 配信管理シート（このサーバーが書き込むほう）

| 項目 | 値 |
|---|---|
| Spreadsheet ID | `1GiJN2WryPHUwrcxYOlZbmXE6-KV2EsYYIlbX9THRWW0` |
| タブ`ニュースレター記事テスト` | **配信キュー**。`/webhook` が append、`/digest` が E 列を更新 |
| タブ`ニュースレター記事` | **アーカイブ**。`/digest` が append |

`ニュースレター記事テスト`（配信キュー）:

| 列 | 内容 | 書き込みタイミング |
|---|---|---|
| A | カテゴリ（現状すべて `AI` 固定） | `/webhook` で publish 成功時 |
| B | 公開日（YYYY/MM/DD） | `/webhook` で publish 成功時 |
| C | 記事タイトル | `/webhook` で publish 成功時 |
| D | 記事 URL | `/webhook` で publish 成功時 |
| E | LINE 送信済み日（YYYY/MM/DD） | `/digest` で broadcast 成功時 |

E 列が空 = 「まだ LINE で配信していない」を意味し、`/digest` の対象になります。

`ニュースレター記事`（アーカイブ）:

| 列 | 内容 |
|---|---|
| A | カテゴリ |
| B | 送信日（YYYY/MM/DD） |
| C | 記事タイトル |
| D | 記事 URL |

`/digest` で broadcast したタイミングで append されます。テスト送信や検証で誤って入った行があれば、担当者が手動で削除してください。

---

## 7. LINE 公式アカウント

このシステムは **2 つ別のチャネル** を使っています。トークンを混同しないよう注意。

### 7.1 編集用チャネル（社内向け）

| 項目 | 値 |
|---|---|
| 使う場面 | `/select-and-draft` の通知、`/webhook` の発火元 |
| 送信先 | LINE グループ `記事公開＿NEO商工運営` |
| グループ ID 環境変数 | `LINE_DRAFT_GROUP_ID` |
| チャネルアクセストークン環境変数 | `LINE_DRAFT_TOKEN` |
| 送信種別 | push（特定グループへの送信）／ reply（Webhook の返答） |

### 7.2 公式アカウント（読者向け）

| 項目 | 値 |
|---|---|
| 名称 | `NEO商工公式アカウント` |
| 使う場面 | `/digest` の broadcast |
| 送信先 | 友だち全員 |
| チャネルアクセストークン環境変数 | `LINE_PUBLISH_TOKEN` |
| 送信種別 | broadcast |

> **broadcast は LINE 公式の有料機能枠を消費します。** 第1・第3水曜のみに集約しているのは、配信頻度が増えすぎないためでもあります。

### 7.3 Webhook URL の登録

LINE Developers コンソールの編集用チャネル設定で、Webhook URL に以下を登録しています。

```
https://line-webhook-156606796405.asia-northeast1.run.app/webhook
```

---

## 8. WordPress

| 項目 | 値 |
|---|---|
| サイト | [blog.neo-shoko.jp](https://blog.neo-shoko.jp) |
| ターゲット読者 | 中小企業の社長 |
| 認証方式 | Application Password（共通アカウント） |
| ユーザー名環境変数 | `WP_USERNAME` |
| Application Password 環境変数 | `WP_APPLICATION_PASSWORD` |
| ベース URL 環境変数 | `WP_BASE_URL`（既定 `https://blog.neo-shoko.jp`） |

### 8.1 記事のスラッグ採番

公開時のスラッグは `ai-0001` 形式で連番採番されます。

- `/webhook` 起動時に `GET /wp-json/wp/v2/posts?status=publish&per_page=100` で既存記事を取得
- `ai-(\d+)` パターンに一致するスラッグから最大値を取得し、+1 した値をゼロ埋め 4 桁で採番
- 採番に失敗した場合（API エラーなど）は WP のデフォルトスラッグ（タイトル由来）になります

> **注意**: 既存スラッグを取得する際の `per_page=100` は WP REST API の最大値。記事数が 100 を超えても直近 100 件から最大番号を取れていれば実害はないが、過去のすべての ai-xxxx を完全網羅するわけではない仕様です。

### 8.2 下書き作成

`/select-and-draft` 起動時に `POST /wp-json/wp/v2/posts` を `status=draft` で叩きます。本文は Markdown を `extra` + `nl2br` 拡張で HTML 化したもの。

---

## 9. Google Cloud（GCP）構成

| 項目 | 値 |
|---|---|
| プロジェクト ID | `sheets-to-wp-draft` |
| プロジェクト番号 | `156606796405` |
| Cloud Run リージョン | `asia-northeast1`（東京） |
| Cloud Run サービス名 | `line-webhook` |
| Cloud Run 公開 URL | https://line-webhook-156606796405.asia-northeast1.run.app |
| Cloud Run 認証 | 未認証アクセス許可（LINE / Cloud Scheduler から呼ぶため） |
| サービスアカウント | `sheets-to-wp-bot@sheets-to-wp-draft.iam.gserviceaccount.com` |
| Cloud Scheduler ジョブ | `select-draft-friday`、`digest-wednesday` |
| Gemini モデル | `gemini-2.5-flash` |
| 採用アーキテクチャの理由 | ローカル実行は確実性に欠け、GitHub Actions は秘匿情報の保管先として敬遠、Cloud Run + Cloud Scheduler が最小コストかつ確実に月2回の配信を保証できる |

> Cloud Run は **リクエスト駆動**で動きます。誰も呼んでいない時間帯はインスタンスが寝ている（min instances=0）ので、待機コストはほぼゼロ。`/select-and-draft` や `/digest` が叩かれた瞬間にだけ立ち上がります。

### 9.1 サービスアカウントの権限

`sheets-to-wp-bot@sheets-to-wp-draft.iam.gserviceaccount.com` には以下が必要です。

| 範囲 | 必要な権限 |
|---|---|
| Google Sheets API | 上記 2 つのスプレッドシートへの編集者権限（シート側で共有） |
| Cloud Run | このサービスアカウントを Cloud Run のランタイム ID として設定 |

サーバー側では `google.auth.default()` を使っており、Cloud Run のランタイム ID から自動で資格情報を取得します。サービスアカウント JSON ファイルを Cloud Run にデプロイする必要はありません（ローカル実行時のみ JSON ファイルを使う、[14. ローカル動作確認](#14-ローカル動作確認) 参照）。

---

## 10. 環境変数

すべて Cloud Run のサービス設定に登録済み。**変更は GCP コンソール → Cloud Run → `line-webhook` → 「変数とシークレット」タブから行います。**

| 変数 | 用途 | 既定値 / 設定済みの値の概要 |
|---|---|---|
| `WP_BASE_URL` | WordPress のベース URL | `https://blog.neo-shoko.jp` |
| `WP_USERNAME` | WP ログインユーザー名 | （Cloud Run に設定済み・共通アカウント） |
| `WP_APPLICATION_PASSWORD` | WP Application Password | （Cloud Run に設定済み） |
| `LINE_DRAFT_TOKEN` | 編集用チャネルのアクセストークン | （Cloud Run に設定済み） |
| `LINE_DRAFT_GROUP_ID` | 「記事公開＿NEO商工運営」のグループ ID | （Cloud Run に設定済み） |
| `LINE_PUBLISH_TOKEN` | 公式アカウントのアクセストークン | （Cloud Run に設定済み） |
| `WRITE_SPREADSHEET_ID` | 配信管理シートの ID | `1GiJN2WryPHUwrcxYOlZbmXE6-KV2EsYYIlbX9THRWW0` |
| `WRITE_SHEET_NAME` | 配信キューのタブ名 | `ニュースレター記事テスト` |
| `ARCHIVE_SHEET_NAME` | アーカイブのタブ名 | `ニュースレター記事` |
| `SOURCE_SPREADSHEET_ID` | GAS が書き込むシートの ID | `1rFhJ7bWNy6a76ecy3LMNmulHE5Iq5OM1L0yQcDNa8K0` |
| `SOURCE_SHEET_NAME` | GAS が書き込むタブ名 | `aismiley_rewrite` |
| `GEMINI_API_KEY` | Gemini API キー | （Cloud Run に設定済み） |
| `GEMINI_MODEL` | Gemini モデル名 | `gemini-2.5-flash` |
| `SCHEDULER_SECRET` | Cloud Scheduler 認証用の共有シークレット | （Cloud Run に設定済み・Scheduler ジョブ側にも同値が登録） |

> ローカル開発用の `.env` ファイルはリポジトリには含めません（`.gitignore` 済み）。**本番環境はすべて Cloud Run の環境変数で完結しているため、デプロイ時に `.env` を渡す必要はありません。**

---

## 11. アカウント・認証情報の所有者

このシステムが依存している外部サービスのアカウント所有者一覧。鍵やトークンの再発行、権限委譲時の参照用。

### 11.1 Google アカウント

| 項目 | アカウント | コンソール / リンク | 備考 |
|---|---|---|---|
| GCP プロジェクト `sheets-to-wp-draft` の Owner / 課金管理 | `backoffice@lconsulting.jp` | [GCP Console（プロジェクト sheets-to-wp-draft）](https://console.cloud.google.com/welcome?project=sheets-to-wp-draft) | 全リソースの管理権限 |
| `gcloud auth login` でデプロイに使うアカウント | `backoffice@lconsulting.jp` | — | デプロイ時のローカル CLI ログイン |
| サービスアカウント `sheets-to-wp-bot@sheets-to-wp-draft.iam.gserviceaccount.com` の発行元 | `backoffice@lconsulting.jp` | [Cloud Run: line-webhook サービス画面](https://console.cloud.google.com/run/detail/asia-northeast1/line-webhook/revisions?project=sheets-to-wp-draft) | このサービスアカウントが Cloud Run のランタイム ID として使われている |
| Gemini API キー (`GEMINI_API_KEY`) を Google AI Studio で発行したアカウント | `consultant1@lconsulting.jp` | [Google AI Studio Usage](https://aistudio.google.com/usage?project=gen-lang-client-0698776399) | 内部プロジェクト ID: `gen-lang-client-0698776399`。キー失効時は consultant1 でログインして再発行 → Cloud Run の `GEMINI_API_KEY` を更新 |
| ソースシート `chaen-ai-lab&aismileyのリライト` の Drive オーナー | `backoffice@lconsulting.jp` | [スプレッドシートを開く](https://docs.google.com/spreadsheets/d/1rFhJ7bWNy6a76ecy3LMNmulHE5Iq5OM1L0yQcDNa8K0/edit#gid=572044241) | リンク先のタブ `gid=572044241` が `aismiley_rewrite` |
| 配信管理シート（ID `1GiJN2W...`、タブ「ニュースレター記事テスト」「ニュースレター記事」）の Drive オーナー | `backoffice@lconsulting.jp` | [スプレッドシートを開く](https://docs.google.com/spreadsheets/d/1GiJN2WryPHUwrcxYOlZbmXE6-KV2EsYYIlbX9THRWW0/edit#gid=645550177) | シートを作成したアカウント |
| GAS（記事リライト処理）の所有者・実行アカウント | `backoffice@lconsulting.jp` | [ソースシートを開き 拡張機能 → Apps Script](https://docs.google.com/spreadsheets/d/1rFhJ7bWNy6a76ecy3LMNmulHE5Iq5OM1L0yQcDNa8K0/edit#gid=572044241) | ソースシートに紐づく Container-bound Apps Script |

> **ポイント**: Gemini API キーだけ別アカウント（`consultant1@lconsulting.jp`）で発行されている。それ以外の Google 関連はすべて `backoffice@lconsulting.jp` 配下。

### 11.2 LINE Developers / 公式アカウントマネージャー

| 項目 | 現アカウント | 移行予定 | コンソール / リンク |
|---|---|---|---|
| 編集用 LINE チャネル（`LINE_DRAFT_TOKEN` を発行している側）の管理 | `backoffice@lconsulting.jp` | `soma@lconsulting.jp` | [LINE Developers Console](https://developers.line.biz/console/) |
| 公式アカウントチャネル（`LINE_PUBLISH_TOKEN`、NEO商工公式アカウント）の管理 | `backoffice@lconsulting.jp` | `soma@lconsulting.jp` | [LINE Developers Console](https://developers.line.biz/console/) |
| LINE 公式アカウントマネージャー（NEO商工公式アカウント） | `backoffice@lconsulting.jp` | `soma@lconsulting.jp` | [LINE Official Account Manager](https://manager.line.biz/) |
| LINE グループ「記事公開＿NEO商工運営」の管理 | `backoffice@lconsulting.jp` | `soma@lconsulting.jp` | LINE アプリから参加 |

> **移行時の注意**: アカウント移管後は、上記 4 項目の所有権／管理権を `soma@lconsulting.jp` に譲渡し、必要に応じて Webhook URL の再登録・チャネルアクセストークンの再発行（→ Cloud Run の環境変数 `LINE_DRAFT_TOKEN` / `LINE_PUBLISH_TOKEN` 更新）を行う。

### 11.3 WordPress

`blog.neo-shoko.jp` はセルフホストの WordPress。投稿には **共通の WordPress アカウント** を使っており、Google アカウントとは無関係。`WP_USERNAME` と `WP_APPLICATION_PASSWORD` は WordPress の管理画面（ユーザー → プロフィール → アプリケーションパスワード）で発行する。

---

## 12. ファイル構成

```
neo-shoko-newsletter/
├── README.md                          # このドキュメント
├── webhook-server/                    # Cloud Run で動くサーバー本体
│   ├── main.py                        # Flask アプリ本体（4 つのエンドポイント全部）
│   ├── Procfile                       # gunicorn 起動設定（Cloud Run のコンテナ起動コマンド）
│   ├── requirements.txt               # Python 依存ライブラリ
│   ├── .gitignore                     # 秘密情報除外（.env、サービスアカウント JSON など）
│   ├── test_select_local.py           # /select-and-draft のローカル動作確認（WP・LINE は触らない）
│   ├── test_archive_local.py          # アーカイブシート append のローカル確認（LINE 配信なし）
│   └── test_digest_local.py           # /digest のローカル実行（LINE 実 broadcast されるので注意）
├── scripts/                           # 単発・補助スクリプト群（手元で実行する用）
├── .claude/                           # Claude Code 用の設定・スキル定義
└── sheets-to-wp-draft-XXXXXXXX.json   # サービスアカウント鍵（リポジトリには含めない）
```

`webhook-server/requirements.txt` の依存関係:

| パッケージ | 用途 |
|---|---|
| `flask` | HTTP サーバー |
| `gunicorn` | 本番用 WSGI サーバー（Cloud Run で起動） |
| `requests` | WP API / LINE API へのリクエスト |
| `google-auth` / `google-api-python-client` | Sheets API |
| `google-genai` | Gemini API |
| `markdown` | Markdown → HTML 変換 |

---

## 13. デプロイ手順

ターミナルから手動で `gcloud run deploy` を叩いています。

### 13.1 前提

- gcloud CLI がインストールされ、`gcloud auth login` 済み
- `gcloud config set project sheets-to-wp-draft` 済み

### 13.2 コマンド

```bash
cd webhook-server
gcloud run deploy line-webhook \
  --project sheets-to-wp-draft \
  --region asia-northeast1 \
  --source . \
  --service-account sheets-to-wp-bot@sheets-to-wp-draft.iam.gserviceaccount.com \
  --allow-unauthenticated
```

`--source .` を使うと Cloud Build が自動でコンテナイメージを作って Cloud Run にデプロイします（Procfile が読まれる）。

### 13.3 デプロイ時の注意

- **環境変数はコマンドで上書きしない**こと。Cloud Run コンソール側に既に設定済みの値があります。`--set-env-vars` を付けると差分上書きで一部消える事故が起きやすいので、コードだけ更新する場合は環境変数を一切指定しない上のコマンドが安全です。
- 環境変数を変えたいときは GCP コンソール → Cloud Run → `line-webhook` → 「新しいリビジョンの編集とデプロイ」→「変数とシークレット」から GUI で変更してください。

### 13.4 デプロイ後の確認

ヘルスチェック:

```bash
curl https://line-webhook-156606796405.asia-northeast1.run.app/
# {"status":"ok"}
```

`/select-and-draft` を手動で叩いてみる（実際に WP 下書きが作られ、LINE 通知が飛びます）:

```bash
curl -X POST https://line-webhook-156606796405.asia-northeast1.run.app/select-and-draft \
  -H "X-Scheduler-Secret: <SCHEDULER_SECRET の値>"
```

`/digest` を手動で叩く（**第1・第3水曜以外は何もしないので安全**）:

```bash
curl -X POST https://line-webhook-156606796405.asia-northeast1.run.app/digest \
  -H "X-Scheduler-Secret: <SCHEDULER_SECRET の値>"
```

---

## 14. ローカル動作確認

本番を触らずにロジックだけ確認したいときに使います。

### 14.1 セットアップ

開発者ローカルでのみ必要（社長は通常使いません）。

1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -r webhook-server/requirements.txt`
3. プロジェクトルートに `.env` を置き、Cloud Run と同じ環境変数を定義
4. サービスアカウント JSON ファイル（`sheets-to-wp-draft-XXXXXXXX.json`）を入手し、`.env` に `GOOGLE_SERVICE_ACCOUNT_JSON=/絶対パス/sheets-to-wp-draft-XXXXXXXX.json` を追加
5. `source .env && export GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_SERVICE_ACCOUNT_JSON"`

### 14.2 3 つのテストスクリプト

| スクリプト | 実行されること | 副作用 |
|---|---|---|
| `webhook-server/test_select_local.py` | 直近1週の記事読み込み + Gemini 選定 | **副作用なし**（WP 下書きも LINE 通知も実行しない） |
| `webhook-server/test_archive_local.py` | 配信キューを読んでアーカイブシートに append | アーカイブシート（`ニュースレター記事`）に書き込む。LINE broadcast なし、E 列も触らない |
| `webhook-server/test_digest_local.py` | `/digest` を曜日チェック無効化で実行 | **本番と同じ実コード**。LINE broadcast が実際に送られる |

```bash
.venv/bin/python3 webhook-server/test_select_local.py
.venv/bin/python3 webhook-server/test_archive_local.py
.venv/bin/python3 webhook-server/test_digest_local.py   # 確認後に yes を入力
```

`test_digest_local.py` は実行前に対話プロンプトで「本当に送るか」聞いてくれます。

---

## 15. トラブルシュート

### 15.1 金曜になっても下書きが来ない

確認順序:

1. **Cloud Scheduler のジョブ実行履歴を見る**: GCP コンソール → Cloud Scheduler → `select-draft-friday` → 「ログを表示」。HTTP 200 が返っていれば呼び出しは成功している
2. **Cloud Run のログを見る**: GCP コンソール → Cloud Run → `line-webhook` → 「ログ」タブで `severity>=ERROR` でフィルタ
3. **直近 7 日に GAS が書き込んだ行があるか**: ソースシートの B 列（処理日時）を確認。記事ゼロなら `'no articles in last 7 days'` で正常終了している
4. **Gemini が 0 件しか返さなかった**: ソースに記事はあったが選定基準を満たさなかった可能性。Cloud Run ログに `'no articles passed selection criteria'` が出る

### 15.2 水曜になっても LINE 配信されない

確認順序:

1. **今日が第1または第3水曜か**: それ以外の水曜は仕様としてスキップ。レスポンスに `'not 1st/3rd Wednesday'` が出ている
2. **配信キューの E 列が空の行があるか**: `ニュースレター記事テスト` シート。0 件なら `'no pending rows'` で正常終了
3. **`LINE_PUBLISH_TOKEN` が失効していないか**: LINE Developers コンソールでトークン再発行。Cloud Run の環境変数も更新する
4. **Cloud Run のログ**: `'broadcast failed'` のエラーメッセージ

### 15.3 broadcast は飛んだが、次回も同じ記事が送られそうなとき

`/digest` の途中で E 列更新が失敗したケース。Cloud Run のログに `'broadcast OK but mark_sent failed'` が出ます。

**手動対応**: `ニュースレター記事テスト` シートの該当行（broadcast されたが E 列が空のままの行）を開き、E 列に送信日（例: `2026/05/06`）を手で入力してください。次回の `/digest` から重複しなくなります。

### 15.4 `/webhook` で記事公開がうまくいかない

確認順序:

1. **メッセージに `$` または `＄` が含まれているか**
2. **編集 URL に `?post=<数字>` が含まれているか**（編集画面の URL をそのまま貼ればOK）
3. **WordPress 側で当該 post ID が存在し、削除されていないか**
4. **`WP_APPLICATION_PASSWORD` が失効していないか**（WP 管理画面 → ユーザー → プロフィール → アプリケーションパスワード）

### 15.5 記事のスラッグが想定どおり ai-NNNN にならない

`get_next_slug()` は WP REST API の `?per_page=100` で既存記事を取って最大番号を計算します。WP API 自体が落ちていると `None` を返し、その場合は WP 側のデフォルトスラッグ（タイトル由来の文字列）になります。気になるときは WP 管理画面でスラッグを手動修正してください。

### 15.6 「Cloud Run の URL を変更したい」「Cloud Scheduler の時刻を変えたい」

- URL は Cloud Run サービス名から自動で決まるため、変更したい場合はサービス名を変えるか、カスタムドメインをマップする必要があります（要相談）。
- スケジュール時刻は GCP コンソール → Cloud Scheduler → 該当ジョブ → 「編集」から cron 式を変更可能。タイムゾーンは Asia/Tokyo のまま。
- スケジュール変更後は次回起動から新しい時刻で動きます（Cloud Run 側の再デプロイは不要）。
