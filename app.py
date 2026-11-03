"""Slack to LINE Summary

工程2では、.env の5項目を読み、未記入があれば名前を表示して終了する。
工程3では、Slackの指定チャンネルから最新10件を取得して表示する。
工程4では、表示した投稿を Claude API で日本語要約する。
工程5では、要約文を LINE へ1通送信する。
工程6では、処理済みの投稿を記録し、同じ内容を繰り返して通知しない。
"""

import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from anthropic import Anthropic
from anthropic import APIConnectionError, APIStatusError
from dotenv import load_dotenv
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "processed_messages.json"

REQUIRED_SETTINGS = (
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
    "ANTHROPIC_API_KEY",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_USER_ID",
)


def load_settings() -> dict[str, str]:
    """.env を読み、5項目がすべて入っていれば辞書で返す。"""
    if not ENV_PATH.is_file():
        print(".env ファイルがありません。")
        print("slack-line-summary フォルダで、次のコマンドを実行してください。")
        print("Copy-Item .env.example .env")
        sys.exit(1)

    load_dotenv(ENV_PATH, override=True)

    settings = {}
    missing = []
    for name in REQUIRED_SETTINGS:
        value = os.getenv(name, "").strip()
        if value:
            settings[name] = value
        else:
            missing.append(name)

    if missing:
        print("次の設定が .env に未記入です。")
        for name in missing:
            print(f"- {name}")
        print(".env に値を書いてから、もう一度実行してください。")
        sys.exit(1)

    print("設定を読み込みました。")
    return settings


MESSAGE_LIMIT = 10

SLACK_ERROR_GUIDE = {
    "invalid_auth": "Slack Bot Token が正しくありません。.env の SLACK_BOT_TOKEN を確認してください。",
    "not_authed": "Slack Bot Token が渡せていません。.env の SLACK_BOT_TOKEN を確認してください。",
    "token_revoked": "Slack Bot Token が取り消されています。アプリを再インストールし、新しいトークンを .env に設定してください。",
    "channel_not_found": "チャンネルが見つかりません。.env の SLACK_CHANNEL_ID が、対象チャンネルのID（Cで始まる）か確認してください。",
    "not_in_channel": "Botがチャンネルに参加していません。Slackで対象チャンネルを開き、/invite @アプリ名 を実行してください。",
    "missing_scope": "Botの権限が不足しています。Bot Token Scopes に channels:history と users:read を追加し、アプリを再インストールしてください。",
    "is_archived": "チャンネルがアーカイブされているため、メッセージを取得できません。",
    "account_inactive": "このトークンのアカウントは無効です。Slackアプリを確認してください。",
    "invalid_arguments": "チャンネルIDの形式を確認してください。公開チャンネルのIDは C で始まります。",
}


def select_user_messages(messages: list[dict]) -> list[dict]:
    """subtype がある投稿はシステムメッセージとして除外し、古い順に並べる。"""
    selected = [message for message in messages if not message.get("subtype")]
    selected.reverse()
    return selected


def slack_error_message(error: SlackApiError) -> str:
    """トークンを含めず、エラーコードと対処だけを返す。"""
    response = error.response
    code = ""
    needed = ""
    if response is not None:
        code = response.get("error") or ""
        needed = response.get("needed") or ""

    lines = ["Slack APIでエラーが発生しました。"]
    if code:
        lines.append(f"エラーコード: {code}")
    guide = SLACK_ERROR_GUIDE.get(code)
    if guide:
        lines.append(guide)
    else:
        lines.append("トークン、チャンネルID、Botがそのチャンネルに参加しているかを確認してください。")
    if needed:
        lines.append(f"不足している権限: {needed}")
    return "\n".join(lines)


def fetch_recent_messages(client: WebClient, channel_id: str) -> tuple[list[dict], list[dict]]:
    """最新10件を取得し、システムメッセージを除いた一覧も返す。"""
    response = client.conversations_history(channel=channel_id, limit=MESSAGE_LIMIT)
    messages = response.get("messages") or []
    return messages, select_user_messages(messages)


def author_name(client: WebClient, user_id: str, cache: dict[str, str], warnings: list[str]) -> str:
    """ユーザーIDを表示名に変換する。同じIDは再取得しない。"""
    if not user_id:
        return "不明"
    if user_id in cache:
        return cache[user_id]

    try:
        response = client.users_info(user=user_id)
    except SlackApiError as error:
        cache[user_id] = user_id
        code = error.response.get("error") if error.response is not None else ""
        if code == "missing_scope":
            warnings.append(
                "投稿者名を取得できません。Bot Token Scopes に users:read を追加し、アプリを再インストールしてください。"
            )
        else:
            warnings.append("一部の投稿者名を取得できなかったため、ユーザーIDを表示します。")
        return user_id
    except SlackClientError:
        cache[user_id] = user_id
        warnings.append("投稿者名の取得中に通信エラーが発生したため、ユーザーIDを表示します。")
        return user_id

    user = response.get("user") or {}
    profile = user.get("profile") or {}
    name = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user_id
    )
    name = str(name).strip() or user_id
    cache[user_id] = name
    return name


def print_messages(client: WebClient, fetched: list[dict], visible: list[dict]) -> list[str]:
    hidden_count = len(fetched) - len(visible)
    print(f"メッセージを取得しました。（取得 {len(fetched)}件、表示 {len(visible)}件、除外 {hidden_count}件）")
    if not visible:
        print("表示できる投稿がありません。参加通知などのシステムメッセージは除外しています。")
        return []

    cache: dict[str, str] = {}
    warnings: list[str] = []
    lines = []
    for index, message in enumerate(visible, start=1):
        name = author_name(client, message.get("user", ""), cache, warnings)
        text = str(message.get("text", "")).strip() or "(本文なし)"
        lines.append(f"[{index}] {name}\n{text}")

    shown = set()
    for warning in warnings:
        if warning not in shown:
            print(warning)
            shown.add(warning)

    for line in lines:
        print()
        print(line)
    return lines


CLAUDE_MODEL = "claude-sonnet-5-5"

SUMMARY_SYSTEM_PROMPT = """あなたはSlackの投稿を日本語で要約します。
次のルールを守ってください。
- 渡された投稿に書かれていることだけを使う。
- 日時、会議、期限、担当者、依頼事項、変更事項を残す。
- 投稿にない情報は推測して書かない。
- 最初の行は「■ Slackメッセージ要約」だけにする。
- その後は「・」で始まる行だけを書く。1つの要点を1行にする。
- 行の途中で改行しない。「会議」「依頼事項」などの分類見出しは付けない。
- 「・」の下に、さらに箇条書きを作らない。
- 次の見本は並び方だけを示す。見本の内容は、投稿に同じ記述があるときだけ使う。

■ Slackメッセージ要約

・明日10時から定例会議を実施
・議題は新商品の進捗確認
"""

CLAUDE_ERROR_GUIDE = {
    "authentication_error": "Claude APIキーが正しくありません。.env の ANTHROPIC_API_KEY を確認してください。",
    "permission_error": "このAPIキーでは要約を実行できません。Claude Consoleの権限を確認してください。",
    "not_found_error": "モデル claude-sonnet-5-5 を利用できません。Claude Consoleで、このモデルが有効か確認してください。",
    "rate_limit_error": "Claude APIの利用制限に達しました。しばらく待ってから、もう一度実行してください。",
    "overloaded_error": "Claude APIが混み合っています。しばらく待ってから、もう一度実行してください。",
    "billing_error": "Claude APIのクレジットが不足しています。Claude Consoleで支払い設定を確認してください。",
    "timeout_error": "Claude APIの応答がタイムアウトしました。もう一度実行してください。",
    "api_error": "Claude API側でエラーが発生しました。しばらく待ってから、もう一度実行してください。",
    "invalid_request_error": "Claude APIがリクエストを拒否しました。時間をおいて、もう一度実行してください。",
}


def redact_secrets(text: str) -> str:
    """画面に出す文からAPIキーとSlackトークンを取り除く。"""
    text = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[api-key]", text)
    text = re.sub(r"xox[baprs]-[A-Za-z0-9\-]+", "[token]", text)
    return text


def claude_error_message(error: APIStatusError) -> str:
    """APIキーを含めず、ステータスと対処を返す。"""
    error_type = error.type or ""
    lines = ["Claude APIでエラーが発生しました。"]
    lines.append(f"HTTPステータス: {error.status_code}")
    if error_type:
        lines.append(f"エラー種別: {error_type}")
    guide = CLAUDE_ERROR_GUIDE.get(error_type)
    if guide:
        lines.append(guide)
    else:
        lines.append("APIキーとネットワーク接続を確認してください。")
        detail = redact_secrets(error.message or "").strip()
        if detail:
            lines.append(detail[:300])
    return "\n".join(lines)


def format_posts_for_summary(lines: list[str]) -> str:
    """表示済みの投稿者名と本文を、要約用の入力文にまとめる。"""
    return "次のSlack投稿を要約してください。\n\n" + "\n\n".join(lines)


def summary_text(content: list) -> str:
    """応答のうち、表示用のテキストだけを取り出す。"""
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = str(getattr(block, "text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def summarize_messages(api_key: str, lines: list[str]) -> str:
    """投稿一覧を Claude に送り、日本語の要約を返す。"""
    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            thinking={"type": "between_tools"},
            output_config={"effort": "medium"},
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": format_posts_for_summary(lines),
                }
            ],
        )
    except APIStatusError as error:
        print(claude_error_message(error))
        sys.exit(1)
    except APIConnectionError:
        print("Claude APIとの通信に失敗しました。")
        print("ネットワーク接続を確認して、もう一度実行してください。")
        sys.exit(1)

    summary = summary_text(response.content)
    if not summary:
        print("Claude APIから要約文を取得できませんでした。")
        sys.exit(1)
    if "■ Slackメッセージ要約" not in summary:
        summary = "■ Slackメッセージ要約\n\n" + summary
    if response.stop_reason == "max_tokens":
        summary += "\n（要約が長すぎたため、途中で切れています。）"
    return summary


LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 5000

LINE_STATUS_GUIDE = {
    400: "送信内容または送信先を確認してください。LINE_USER_ID が U で始まるIDか、公式アカウントを友だち追加しているかを見てください。",
    401: "LINEのチャネルアクセストークンが正しくありません。.env の LINE_CHANNEL_ACCESS_TOKEN を確認してください。",
    403: "このチャネルからはメッセージを送信できません。LINE Official Accountの権限を確認してください。",
    429: "LINEの送信回数の上限に達しました。時間をおいて、もう一度実行してください。",
}


def redact_line_secrets(text: str, access_token: str, user_id: str) -> str:
    """LINEの応答にトークンやユーザーIDが含まれていても、画面には出さない。"""
    text = redact_secrets(text)
    if access_token and len(access_token) >= 8:
        text = text.replace(access_token, "[access-token]")
    if user_id and len(user_id) >= 8:
        text = text.replace(user_id, "[user-id]")
    return re.sub(r"U[0-9a-f]{32}", "[user-id]", text)


def line_error_detail(response: requests.Response, access_token: str, user_id: str) -> str:
    """LINEが返した原因だけを取り出す。"""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""

    messages = []
    if body.get("message"):
        messages.append(str(body["message"]))
    for detail in body.get("details") or []:
        if isinstance(detail, dict) and detail.get("message"):
            messages.append(str(detail["message"]))
    return redact_line_secrets(" ".join(messages), access_token, user_id)


def send_line_message(access_token: str, user_id: str, text: str) -> None:
    """要約文を Push Message API で1通送る。成功時はHTTP 200を確認する。"""
    if len(text) > LINE_TEXT_LIMIT:
        print("LINEへの送信に失敗しました。")
        print("要約が5000文字を超えているため、送信できません。")
        sys.exit(1)

    try:
        response = requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "to": user_id,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30,
        )
    except requests.RequestException:
        print("LINEへの送信に失敗しました。")
        print("LINE APIとの通信に失敗しました。ネットワーク接続を確認して、もう一度実行してください。")
        sys.exit(1)

    if response.status_code == 200:
        print(f"LINEへ要約を送信しました。（HTTPステータス: {response.status_code}）")
        return

    print("LINEへの送信に失敗しました。")
    print(f"HTTPステータス: {response.status_code}")
    detail = line_error_detail(response, access_token, user_id)
    if detail:
        print(f"LINEの応答: {detail}")
    guide = LINE_STATUS_GUIDE.get(response.status_code)
    if guide:
        print(guide)
    else:
        print("LINE Developersのチャネル設定と、友だち追加の状態を確認してください。")
    sys.exit(1)


def message_timestamp(message: dict) -> Decimal | None:
    """Slackの ts を比較できる数値にする。形式が不正なら None。"""
    raw = message.get("ts")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def newest_ts(messages: list[dict]) -> str | None:
    """一覧の中で最も新しい ts を返す。"""
    newest: tuple[Decimal, str] | None = None
    for message in messages:
        timestamp = message_timestamp(message)
        raw = message.get("ts")
        if timestamp is None or not isinstance(raw, str):
            continue
        if newest is None or timestamp > newest[0]:
            newest = (timestamp, raw)
    if newest is None:
        return None
    return newest[1]


def select_new_messages(messages: list[dict], latest_ts: str | None) -> list[dict]:
    """記録済みの ts より新しい投稿だけを、古い順のまま返す。"""
    if latest_ts is None:
        return [message for message in messages if message_timestamp(message) is not None]
    try:
        baseline = Decimal(latest_ts)
    except InvalidOperation:
        return []
    selected = []
    for message in messages:
        timestamp = message_timestamp(message)
        if timestamp is not None and timestamp > baseline:
            selected.append(message)
    return selected


def load_processed_state(path: Path = STATE_PATH) -> dict | None:
    """処理済み記録を読む。ファイルが無い場合だけ初回起動とみなす。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("processed_messages.json を読み込めません。")
        print("ファイルが壊れている可能性があるため、処理を中断しました。")
        print("通知は送っていません。ファイルは変更していません。")
        sys.exit(1)
    if not isinstance(data, dict) or data.get("initialized") is not True:
        print("processed_messages.json の内容を確認できません。")
        print("通知は送っていません。ファイルは変更していません。")
        sys.exit(1)
    latest_ts = data.get("latest_ts")
    if latest_ts is not None:
        if not isinstance(latest_ts, str):
            print("processed_messages.json に記録された時刻を確認できません。")
            print("通知は送っていません。ファイルは変更していません。")
            sys.exit(1)
        try:
            Decimal(latest_ts)
        except InvalidOperation:
            print("processed_messages.json に記録された時刻を確認できません。")
            print("通知は送っていません。ファイルは変更していません。")
            sys.exit(1)
    channel_id = data.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        print("processed_messages.json の内容を確認できません。")
        print("通知は送っていません。ファイルは変更していません。")
        sys.exit(1)
    return data


def save_processed_state(channel_id: str, latest_ts: str | None, path: Path = STATE_PATH) -> None:
    """一時ファイルへ書いてから置き換え、途中で壊れないようにする。"""
    payload = {
        "initialized": True,
        "channel_id": channel_id,
        "latest_ts": latest_ts,
    }
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        print("処理済みメッセージを保存できませんでした。")
        print("通知済みの記録は更新していません。")
        sys.exit(1)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def handle_messages(
    settings: dict[str, str],
    client: WebClient,
    fetched: list[dict],
    visible: list[dict],
    path: Path = STATE_PATH,
) -> None:
    """新しい投稿だけを要約して送信し、成功したときだけ記録する。"""
    channel_id = settings["SLACK_CHANNEL_ID"]
    state = load_processed_state(path)
    if state is None or state.get("channel_id") != channel_id:
        baseline = newest_ts(fetched)
        save_processed_state(channel_id, baseline, path)
        print("初回起動のため、過去の投稿は通知しません。")
        if baseline is None:
            print("取得できた投稿がなかったため、現在の状態を基準として記録しました。")
        else:
            print("いま取得できた最新の投稿を基準として記録しました。")
        print("このあと新しく投稿されたメッセージだけを通知します。")
        print("Claude APIとLINE APIは呼び出していません。")
        return

    pending = select_new_messages(visible, state.get("latest_ts"))
    if not pending:
        print("新しい投稿はないため、Claude APIとLINE APIは呼び出していません。")
        return

    lines = print_messages(client, pending, pending)
    if not lines:
        print()
        print("要約できる投稿がないため、Claude APIは呼び出していません。")
        return

    print()
    print("Claude APIで要約しています。")
    summary = summarize_messages(settings["ANTHROPIC_API_KEY"], lines)
    print()
    print(summary)
    print()
    send_line_message(
        settings["LINE_CHANNEL_ACCESS_TOKEN"],
        settings["LINE_USER_ID"],
        summary,
    )
    recorded = newest_ts(pending)
    if recorded is None:
        print("投稿の時刻を記録できなかったため、処理済みにはしていません。")
        return
    save_processed_state(channel_id, recorded, path)
    print("LINEへの送信に成功したため、処理済みとして記録しました。")


def show_slack_messages(settings: dict[str, str]) -> None:
    client = WebClient(token=settings["SLACK_BOT_TOKEN"])
    try:
        fetched, visible = fetch_recent_messages(client, settings["SLACK_CHANNEL_ID"])
    except SlackApiError as error:
        print(slack_error_message(error))
        sys.exit(1)
    except SlackClientError:
        print("Slack APIとの通信に失敗しました。")
        print("ネットワーク接続を確認して、もう一度実行してください。")
        sys.exit(1)

    handle_messages(settings, client, fetched, visible)


def main() -> None:
    settings = load_settings()
    show_slack_messages(settings)


if __name__ == "__main__":
    main()
