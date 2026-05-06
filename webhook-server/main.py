#!/usr/bin/env python3
"""
LINE Webhook + 定期実行サーバー

エンドポイント:
- POST /webhook            : LINE からのメッセージ受信。draft → publish に変更し、結果をスプレッドシートに蓄積
- POST /select-and-draft   : Cloud Scheduler から週1で起動。直近1週間の記事を Gemini で選定し draft 作成 + LINE 通知
- POST /digest             : Cloud Scheduler から週1で起動。第1・第3水曜のみ、蓄積分を broadcast し E列に送信日時を記録
- GET  /                   : ヘルスチェック
"""

import json
import os
import re
from datetime import datetime, timezone, timedelta
from functools import wraps

import google.auth
from google import genai
import markdown as md_lib
from googleapiclient.discovery import build
import requests
from flask import Flask, abort, jsonify, request

app = Flask(__name__)

# === 環境変数 ===
WP_BASE_URL = os.environ.get('WP_BASE_URL', 'https://blog.neo-shoko.jp').rstrip('/')
WP_USERNAME = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APPLICATION_PASSWORD', '')
LINE_DRAFT_TOKEN = os.environ.get('LINE_DRAFT_TOKEN', '')
LINE_DRAFT_GROUP_ID = os.environ.get('LINE_DRAFT_GROUP_ID', '')
LINE_PUBLISH_TOKEN = os.environ.get('LINE_PUBLISH_TOKEN', '')
WRITE_SPREADSHEET_ID = os.environ.get('WRITE_SPREADSHEET_ID', '1GiJN2WryPHUwrcxYOlZbmXE6-KV2EsYYIlbX9THRWW0')
WRITE_SHEET_NAME = os.environ.get('WRITE_SHEET_NAME', 'ニュースレター記事テスト')
ARCHIVE_SHEET_NAME = os.environ.get('ARCHIVE_SHEET_NAME', 'ニュースレター記事')

# 記事選定 (/select-and-draft 用)
SOURCE_SPREADSHEET_ID = os.environ.get('SOURCE_SPREADSHEET_ID', '1rFhJ7bWNy6a76ecy3LMNmulHE5Iq5OM1L0yQcDNa8K0')
SOURCE_SHEET_NAME = os.environ.get('SOURCE_SHEET_NAME', 'aismiley_rewrite')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

# Cloud Scheduler 認証用
SCHEDULER_SECRET = os.environ.get('SCHEDULER_SECRET', '')

JST = timezone(timedelta(hours=9))

SELECTION_PROMPT = """あなたは、日本の中小企業向けにAI関連ニュース・ノウハウ記事を選定する編集者です。

以下のスプレッドシートに蓄積された記事一覧から、LINE公式アカウントで配信する価値が高い記事を選んでください。

# 目的
日本の中小企業の経営者・担当者にとって、
「AI活用」「業務効率化」「コスト削減」「売上向上」「新しいツール理解」「競合や他社事例の把握」に役立つ記事を選ぶこと。

# 最優先で必ず選ぶ記事
以下に該当する記事がある場合は、通常のランキング評価より優先して必ず選定してください。

- OpenAI / Anthropic / Google / Meta / Microsoft / xAI など主要AI企業の新しいモデル発表
- ChatGPT / Claude / Gemini / Copilot などの新機能発表
- 一般ユーザー向けに公開された新モデル・新機能に関する記事
- 新しい料金プラン、値段、利用制限、無料枠、有料プラン変更に関する記事
- API料金、モデル利用料金、サブスクリプション価格に関する記事

特に「新しいモデル」「一般ユーザーへの公開」「料金・値段」が書かれている記事は、必ず拾ってください。

# 選定対象にしたい記事
以下のような記事を優先してください。

- AIツールの具体的な使い方が分かる記事
- 日本企業、特に中小企業や自治体、店舗、士業、製造業、営業・事務部門などでのAI活用事例
- 日本国内の企業がAIを導入したニュース
- AIによる業務効率化、問い合わせ対応、資料作成、営業支援、採用、経理、マーケティングなどの実例
- AI業界の重要ニュース
- 中小企業が今後対応すべき変化が分かる記事
- ノウハウだけでなく、ニュース性のある記事

# 選ばなくてよい記事
以下は優先度を下げてください。

- 大企業向けすぎて中小企業に応用しづらい記事
- 研究論文寄りで実務利用が見えにくい記事
- 海外ニュースのみで日本企業への示唆が薄い記事
- 既に広く知られていて新規性が低い記事
- 抽象的なAI論・ポエム・未来予測だけの記事
- 宣伝色が強く、具体的な学びが少ない記事

# 有用性の評価基準
各記事を以下の観点で評価してください。

1. 日本の中小企業にとって実務に役立つか
2. 今すぐ知っておくべきニュース性があるか
3. AIツール活用の具体的なヒントがあるか
4. コスト・料金・導入判断に関わる情報があるか
5. 他社事例として参考になるか
6. 経営者や現場担当者が読んで行動につながるか
7. 新規性・話題性があるか
"""


# ============================================================
# 認証ヘルパー
# ============================================================

def require_scheduler_auth(f):
    """Cloud Scheduler 用の共有シークレット検証デコレーター。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not SCHEDULER_SECRET:
            print('ERROR: SCHEDULER_SECRET not configured', flush=True)
            abort(500)
        provided = request.headers.get('X-Scheduler-Secret', '')
        if provided != SCHEDULER_SECRET:
            print('WARN: invalid scheduler secret', flush=True)
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# 既存: /webhook 関連
# ============================================================

def extract_post_ids(text):
    matches = re.findall(r'post=(\d+)', text)
    return [int(m) for m in matches]


def is_publish_request(text):
    return '$' in text or '＄' in text


def get_next_slug():
    endpoint = f'{WP_BASE_URL}/wp-json/wp/v2/posts'
    response = requests.get(
        endpoint,
        params={'status': 'publish', 'per_page': 100, 'orderby': 'date', 'order': 'desc'},
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        timeout=30,
    )

    if response.status_code != 200:
        return None

    max_num = 0
    for post in response.json():
        match = re.match(r'^ai-(\d+)$', post.get('slug', ''))
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num

    next_num = max_num + 1
    return f'ai-{next_num:04d}'


def publish_post(post_id):
    endpoint = f'{WP_BASE_URL}/wp-json/wp/v2/posts/{post_id}'

    next_slug = get_next_slug()
    payload = {'status': 'publish'}
    if next_slug:
        payload['slug'] = next_slug

    response = requests.post(
        endpoint,
        json=payload,
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        timeout=30,
    )

    if response.status_code not in (200, 201):
        return None, f'HTTP {response.status_code}'

    data = response.json()
    title = data.get('title', {}).get('rendered', '(no title)')
    link = data.get('link', '')
    slug = data.get('slug', '')
    return {'title': title, 'link': link, 'post_id': post_id, 'slug': slug}, None


def write_to_spreadsheet(results):
    """publish した記事をスプレッドシートに蓄積（digest の送信キュー）。"""
    try:
        sheets = get_sheets_service()
        today = datetime.now(JST).strftime('%Y/%m/%d')

        rows = []
        for r in results:
            # A=カテゴリ, B=日付, C=title, D=link, E=送信済み日時(空)
            rows.append(['AI', today, r['title'], r['link'], ''])

        sheets.values().append(
            spreadsheetId=WRITE_SPREADSHEET_ID,
            range=f'{WRITE_SHEET_NAME}!A:E',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows},
        ).execute()
    except Exception as e:
        print(f'WARNING: Failed to write to spreadsheet. {e}', flush=True)


def send_line_reply(reply_token, text):
    requests.post(
        'https://api.line.me/v2/bot/message/reply',
        json={
            'replyToken': reply_token,
            'messages': [{'type': 'text', 'text': text}],
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {LINE_DRAFT_TOKEN}',
        },
        timeout=10,
    )


def send_publish_broadcast(text):
    """公開アカウントの友だち全員に Broadcast。"""
    if not LINE_PUBLISH_TOKEN:
        return None, 'LINE_PUBLISH_TOKEN not configured'
    response = requests.post(
        'https://api.line.me/v2/bot/message/broadcast',
        json={'messages': [{'type': 'text', 'text': text}]},
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {LINE_PUBLISH_TOKEN}',
        },
        timeout=30,
    )
    if response.status_code != 200:
        return None, f'HTTP {response.status_code} {response.text}'
    return response, None


# ============================================================
# Sheets 共通
# ============================================================

def get_sheets_service():
    credentials, _ = google.auth.default(
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    service = build('sheets', 'v4', credentials=credentials)
    return service.spreadsheets()


# ============================================================
# /select-and-draft 関連
# ============================================================

def parse_processed_at(value):
    """B列の処理日時（複数フォーマットを許容）。"""
    if not value:
        return None
    value = value.strip()
    formats = [
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M',
        '%Y/%m/%d',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def read_recent_articles(days=7):
    """直近 N 日の記事を取得 (B=処理日時, F=Markdown)。"""
    sheets = get_sheets_service()
    range_str = f'{SOURCE_SHEET_NAME}!A2:F'
    result = sheets.values().get(
        spreadsheetId=SOURCE_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    values = result.get('values', [])

    cutoff = datetime.now(JST) - timedelta(days=days)
    articles = []
    for i, row in enumerate(values):
        row = row + [''] * (6 - len(row))
        b_value = row[1]
        f_value = row[5]
        if not f_value or not f_value.strip():
            continue
        processed_at = parse_processed_at(b_value)
        if not processed_at or processed_at < cutoff:
            continue
        articles.append({
            'row_number': i + 2,
            'processed_at': processed_at,
            'markdown': f_value,
        })
    return articles


def parse_markdown_for_draft(md_text):
    """先頭の見出しをタイトルにし、残りを HTML 化する。(title, body_md, body_html)"""
    lines = md_text.split('\n')
    title = None
    title_idx = None
    for i, line in enumerate(lines):
        m = re.match(r'^#{1,6}\s+(.+)$', line.strip())
        if m:
            title = m.group(1).strip()
            title_idx = i
            break
    if title is None:
        return None, None, None
    body_lines = lines[:title_idx] + lines[title_idx + 1:]
    body_md = '\n'.join(body_lines).strip()
    if not body_md:
        return None, None, None
    body_html = md_lib.markdown(body_md, extensions=['extra', 'nl2br'])
    return title, body_md, body_html


def article_summary_for_gemini(article, max_chars=400):
    """Gemini に渡す要約（タイトル + 本文先頭）。"""
    title, body_md, _ = parse_markdown_for_draft(article['markdown'])
    title = title or '(no title)'
    snippet = (body_md or '')[:max_chars]
    return {
        'row_number': article['row_number'],
        'title': title,
        'snippet': snippet,
    }


def select_with_gemini(articles, max_pick=5):
    """Gemini で条件を満たす記事を最大 N 件選定。"""
    if not GEMINI_API_KEY:
        raise RuntimeError('GEMINI_API_KEY not set')
    if not articles:
        return []

    summaries = [article_summary_for_gemini(a) for a in articles]

    prompt = SELECTION_PROMPT
    prompt += "\n\n# 候補記事\n"
    prompt += f"以下から条件を満たす記事を最大{max_pick}件、優先度順に選んでください。"
    prompt += "条件に該当するものが少なければ満たすぶんだけ返してください。0件なら空配列でOKです。\n\n"
    prompt += "出力は以下の JSON 形式のみ:\n"
    prompt += '{"selected": [{"row_number": <int>, "reason": "<short reason>"}, ...]}\n\n'
    prompt += "候補一覧:\n"
    for s in summaries:
        prompt += f"\n--- row_number: {s['row_number']} ---\n"
        prompt += f"タイトル: {s['title']}\n"
        prompt += f"本文先頭:\n{s['snippet']}\n"

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={'response_mime_type': 'application/json'},
    )

    try:
        data = json.loads(response.text)
        selected = data.get('selected', [])
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        raise RuntimeError(f'Gemini returned invalid JSON: {e}; raw={getattr(response, "text", "")[:500]}')

    by_row = {a['row_number']: a for a in articles}
    picked = []
    for item in selected[:max_pick]:
        rn = item.get('row_number')
        if isinstance(rn, str) and rn.isdigit():
            rn = int(rn)
        if rn in by_row:
            picked.append({**by_row[rn], 'reason': item.get('reason', '')})
    return picked


def create_wordpress_draft(title, content_html):
    endpoint = f'{WP_BASE_URL}/wp-json/wp/v2/posts'
    payload = {'title': title, 'content': content_html, 'status': 'draft'}
    response = requests.post(
        endpoint,
        json=payload,
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        timeout=30,
    )
    if response.status_code not in (200, 201):
        return None, f'HTTP {response.status_code}'
    data = response.json()
    post_id = data.get('id')
    if not post_id:
        return None, 'no post id in response'
    return {
        'post_id': post_id,
        'title': title,
        'edit_url': f'{WP_BASE_URL}/wp-admin/post.php?post={post_id}&action=edit',
    }, None


def send_line_draft_notification(drafts):
    """ドラフトグループに作成完了を Push 送信。"""
    if not LINE_DRAFT_TOKEN or not LINE_DRAFT_GROUP_ID:
        print('WARNING: LINE_DRAFT_TOKEN or LINE_DRAFT_GROUP_ID not set', flush=True)
        return
    lines = [f'WordPress 下書きを{len(drafts)}件作成しました\n']
    for d in drafts:
        lines.append(f'{d["title"]}\n{d["edit_url"]}')
    message = '\n'.join(lines)
    requests.post(
        'https://api.line.me/v2/bot/message/push',
        json={'to': LINE_DRAFT_GROUP_ID, 'messages': [{'type': 'text', 'text': message}]},
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {LINE_DRAFT_TOKEN}',
        },
        timeout=10,
    )


# ============================================================
# /digest 関連
# ============================================================

def is_first_or_third_wednesday(today=None):
    """1〜7日 or 15〜21日 の水曜なら True。"""
    today = today or datetime.now(JST).date()
    if today.weekday() != 2:  # Wednesday = 2
        return False
    return 1 <= today.day <= 7 or 15 <= today.day <= 21


def read_pending_digest_rows():
    """E列が空の行（未送信）を返す。"""
    sheets = get_sheets_service()
    range_str = f'{WRITE_SHEET_NAME}!A2:E'
    result = sheets.values().get(
        spreadsheetId=WRITE_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    values = result.get('values', [])
    pending = []
    for i, row in enumerate(values):
        row = row + [''] * (5 - len(row))
        # A=カテゴリ, B=日付, C=title, D=link, E=送信日時
        if not row[3]:
            continue
        if row[4] and row[4].strip():
            continue
        pending.append({
            'row_number': i + 2,
            'category': row[0],
            'date': row[1],
            'title': row[2],
            'link': row[3],
        })
    return pending


def append_to_archive_sheet(rows, sent_date):
    """broadcast 済みの記事を本番アーカイブシートに append。B列は送信日（YYYY/MM/DD）。"""
    if not rows:
        return
    sheets = get_sheets_service()
    # A=カテゴリ, B=送信日, C=title, D=link
    values = [[r['category'], sent_date, r['title'], r['link']] for r in rows]
    sheets.values().append(
        spreadsheetId=WRITE_SPREADSHEET_ID,
        range=f'{ARCHIVE_SHEET_NAME}!A:D',
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body={'values': values},
    ).execute()


def mark_digest_rows_sent(row_numbers, sent_date):
    """E列に送信日（YYYY/MM/DD）を書き込む。"""
    if not row_numbers:
        return
    sheets = get_sheets_service()
    data = [
        {'range': f'{WRITE_SHEET_NAME}!E{rn}', 'values': [[sent_date]]}
        for rn in row_numbers
    ]
    sheets.values().batchUpdate(
        spreadsheetId=WRITE_SPREADSHEET_ID,
        body={'valueInputOption': 'USER_ENTERED', 'data': data},
    ).execute()


# ============================================================
# エンドポイント
# ============================================================

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """LINE Webhook。draft → publish に変更し、結果をスプレッドシートに蓄積。"""
    if request.method == 'GET':
        return jsonify({'status': 'ok'})

    body = request.get_json(silent=True)
    if not body or 'events' not in body:
        return jsonify({'status': 'ok'})

    for event in body['events']:
        if event.get('type') != 'message':
            continue
        if event.get('message', {}).get('type') != 'text':
            continue

        text = event['message']['text']
        reply_token = event.get('replyToken', '')

        if not is_publish_request(text):
            continue

        post_ids = extract_post_ids(text)
        if not post_ids:
            send_line_reply(reply_token, '公開する記事の URL が見つかりませんでした。\nWordPress の編集 URL を含めて送信してください。')
            continue

        successes = []
        errors = []
        for post_id in post_ids:
            result, error = publish_post(post_id)
            if error:
                errors.append(f'Post ID {post_id}: {error}')
            else:
                successes.append(result)

        # 成功分はスプレッドシートに蓄積するのみ。即時 broadcast は廃止し /digest に集約。
        if successes:
            write_to_spreadsheet(successes)

        reply_lines = []
        if successes:
            reply_lines.append('記事の公開とLINE配信予約が完了しました')
            for s in successes:
                reply_lines.append('')
                reply_lines.append(s['title'])
                reply_lines.append(s['link'])
        if errors:
            if reply_lines:
                reply_lines.append('')
            reply_lines.append('記事の公開に失敗しました')
            for e in errors:
                reply_lines.append(e)
        if reply_lines:
            send_line_reply(reply_token, '\n'.join(reply_lines))

    return jsonify({'status': 'ok'})


@app.route('/select-and-draft', methods=['POST'])
@require_scheduler_auth
def select_and_draft():
    """直近1週間の記事を Gemini で選定し、最大5件の draft を作成して LINE 通知。"""
    try:
        articles = read_recent_articles(days=7)
    except Exception as e:
        print(f'ERROR: read_recent_articles failed: {e}', flush=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

    if not articles:
        return jsonify({'status': 'ok', 'message': 'no articles in last 7 days', 'created': 0})

    try:
        selected = select_with_gemini(articles, max_pick=5)
    except Exception as e:
        print(f'ERROR: select_with_gemini failed: {e}', flush=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

    if not selected:
        return jsonify({'status': 'ok', 'message': 'no articles passed selection criteria', 'created': 0})

    drafts = []
    failures = []
    for art in selected:
        title, _, body_html = parse_markdown_for_draft(art['markdown'])
        if not title:
            failures.append(f'row {art["row_number"]}: no H1 title')
            continue
        result, err = create_wordpress_draft(title, body_html)
        if err:
            failures.append(f'row {art["row_number"]}: {err}')
            continue
        drafts.append(result)
        print(f'Created draft: row={art["row_number"]} post_id={result["post_id"]} title={title}', flush=True)

    if drafts:
        send_line_draft_notification(drafts)

    return jsonify({
        'status': 'ok',
        'created': len(drafts),
        'failed': len(failures),
        'drafts': drafts,
        'failures': failures,
    })


@app.route('/digest', methods=['POST'])
@require_scheduler_auth
def digest():
    """第1・第3水曜のみ、未送信の publish 済み記事をまとめて broadcast し、E列に送信日時を記録。"""
    today = datetime.now(JST).date()
    if not is_first_or_third_wednesday(today):
        return jsonify({
            'status': 'ok',
            'message': f'not 1st/3rd Wednesday ({today.isoformat()}), skipping',
        })

    try:
        pending = read_pending_digest_rows()
    except Exception as e:
        print(f'ERROR: read_pending_digest_rows failed: {e}', flush=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

    if not pending:
        return jsonify({'status': 'ok', 'message': 'no pending rows', 'sent': 0})

    lines = [f'記事を{len(pending)}件公開しました\n']
    for r in pending:
        lines.append(f'{r["title"]}\n{r["link"]}')
    message = '\n'.join(lines)

    _, err = send_publish_broadcast(message)
    if err:
        print(f'ERROR: broadcast failed: {err}', flush=True)
        return jsonify({'status': 'error', 'message': err}), 500

    sent_date = datetime.now(JST).strftime('%Y/%m/%d')

    try:
        append_to_archive_sheet(pending, sent_date)
    except Exception as e:
        # broadcast は成功している。アーカイブ失敗は次回 /digest に影響しないので警告のみ
        print(f'WARNING: broadcast OK but append_to_archive_sheet failed: {e}', flush=True)

    try:
        mark_digest_rows_sent([r['row_number'] for r in pending], sent_date)
    except Exception as e:
        # broadcast は成功している。次回重複しないよう手動で E列を埋める必要あり
        print(f'WARNING: broadcast OK but mark_sent failed (manual fix needed): {e}', flush=True)
        return jsonify({
            'status': 'partial',
            'message': f'broadcast sent but failed to mark rows: {e}',
            'sent': len(pending),
        }), 200

    return jsonify({'status': 'ok', 'sent': len(pending)})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
