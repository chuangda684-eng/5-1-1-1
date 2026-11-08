"""Google Meet 議事録自動作成ツール。

録音ファイルを文字起こしし、その結果から日本語の議事録を作成します。
作成した議事録は、ログインしたGoogleアカウントのフォルダへ保存できます。
"""

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google.auth import exceptions as google_auth_exceptions
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google_auth_oauthlib.flow import WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

# 25MBを超える音声は、まだ分割せずエラーにする。
MAX_AUDIO_BYTES = 25 * 1024 * 1024
TRANSCRIBE_MODEL = "gpt-transcribe"
MINUTES_MODEL = "gpt-5.4-mini"
OAUTH_CLIENT_PATH = Path(__file__).resolve().parent / "credentials.json"
TOKEN_PATH = Path(__file__).resolve().parent / "token.json"
# drive.file は、このアプリが作ったファイルしか開けない。
# もともとあるフォルダをフォルダIDで指定するには、drive の権限が必要。
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# APIキーは .env から読む。ソースコードには書かない。
# override=True にすると、.env を保存したあとの再実行で新しい値を読み直せる。
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def format_file_size(size_in_bytes):
    """バイト数を、画面で読みやすい大きさの表記に変換する。"""
    if size_in_bytes < 1024:
        return f"{size_in_bytes} バイト"

    size_in_kb = size_in_bytes / 1024
    if size_in_kb < 1024:
        return f"{size_in_kb:.1f} KB（{size_in_bytes} バイト）"

    size_in_mb = size_in_kb / 1024
    return f"{size_in_mb:.1f} MB（{size_in_bytes} バイト）"


def get_openai_api_key():
    """環境変数のAPIキーを返す。値は画面やログに出さない。"""
    return os.getenv("OPENAI_API_KEY", "").strip()


def validate_audio_file(audio_file):
    """文字起こしの前に、ファイルの有無とサイズを確認する。"""
    if audio_file is None:
        return "録音ファイルをアップロードしてから、「文字起こしを実行」を押してください。"

    if audio_file.size == 0:
        return "録音ファイルが空です。音声が入ったファイルをアップロードしてください。"

    if audio_file.size > MAX_AUDIO_BYTES:
        return (
            "録音ファイルが25MBを超えています"
            f"（{format_file_size(audio_file.size)}）。"
            "分割機能はまだないため、25MB以下のファイルを用意してください。"
        )

    return None


def transcribe_audio(audio_file, api_key):
    """アップロード済みの音声をメモリからAPIへ送り、文字起こし文を返す。

    音声ファイルはディスクへ保存しません。
    """
    client = OpenAI(api_key=api_key)
    audio_bytes = audio_file.getvalue()
    content_type = audio_file.type or "application/octet-stream"
    result = client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=(audio_file.name, audio_bytes, content_type),
        language="ja",
    )
    return result.text


def message_for_api_error(error):
    """APIの例外を、キーを含まない説明文に変える。"""
    if isinstance(error, AuthenticationError):
        return (
            "OpenAIがAPIキーを拒否しました。"
            ".env の OPENAI_API_KEY を確認してから、もう一度実行してください。"
        )

    if isinstance(error, RateLimitError):
        return (
            "OpenAIの利用上限に達したか、リクエストが集中しています。"
            "しばらく待ってから、もう一度実行してください。"
        )

    if isinstance(error, APITimeoutError):
        return "OpenAIからの応答が時間内に返りませんでした。もう一度実行してください。"

    if isinstance(error, APIConnectionError):
        return "OpenAIに接続できませんでした。インターネット接続を確認して、もう一度実行してください。"

    if isinstance(error, APIStatusError):
        return (
            "OpenAI APIがエラーを返しました"
            f"（ステータスコード {error.status_code}）。"
            "ファイル形式とサイズを確認し、時間をおいて再度実行してください。"
        )

    return "文字起こし中に予期しないエラーが発生しました。時間をおいて再度実行してください。"


MINUTES_INSTRUCTIONS = """あなたは会議の議事録作成担当です。
渡された文字起こしだけを根拠に、日本語の議事録を書いてください。

守ること:
- 文字起こしにない事実、数値、期限、担当者、出席者を推測して足さない。
- 判断できない項目は「未確認」と書く。
- 会議名は、入力欄の値があるときだけその値を使う。空なら「未確認」とする。
- 開催日時は、渡された日時をそのまま書く。
- 重複した発言や、はい、ええ、などの相づちは省いて整理する。
- パーセント、日付、人数、期限は文字起こしの表現のまま書く。数値を別の表現に変えない。
- 前置きは書かず、次の見出しと項目だけを出力する。

# 会議議事録
- 会議名
- 開催日時
- 出席者（文字起こしから特定できる場合）
- 会議の概要
- 議題
- 各参加者の進捗報告
- 課題・懸念事項
- 決定事項
- アクションアイテム（担当者・対応内容・期限）
- 次回会議予定

各項目の見出しは変えず、その内容を見出しの次の行に書いてください。
複数ある内容は箇条書きにしてください。
アクションアイテムは「担当者」「対応内容」「期限」が分かるように書いてください。分からない部分は「未確認」としてください。
"""


def message_for_minutes_error(error):
    """議事録作成時のAPI例外を、キーを含まない説明文に変える。"""
    if isinstance(error, AuthenticationError):
        return (
            "OpenAIがAPIキーを拒否しました。"
            ".env の OPENAI_API_KEY を確認してから、もう一度実行してください。"
        )

    if isinstance(error, RateLimitError):
        return (
            "OpenAIの利用上限に達したか、リクエストが集中しています。"
            "しばらく待ってから、もう一度議事録を作成してください。"
        )

    if isinstance(error, APITimeoutError):
        return "OpenAIからの応答が時間内に返りませんでした。もう一度議事録を作成してください。"

    if isinstance(error, APIConnectionError):
        return "OpenAIに接続できませんでした。インターネット接続を確認して、もう一度実行してください。"

    if isinstance(error, APIStatusError):
        return (
            "議事録の作成中にOpenAI APIがエラーを返しました"
            f"（ステータスコード {error.status_code}）。"
            "時間をおいて再度実行してください。"
        )

    return "議事録の作成中に予期しないエラーが発生しました。時間をおいて再度実行してください。"


def extract_message_text(message):
    """API応答から本文だけを取り出す。"""
    content = message.content
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        texts = []
        for part in content:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    return ""


def create_minutes(transcript, meeting_name, meeting_datetime, api_key):
    """表示中の文字起こしをAPIへ送り、議事録の本文を返す。"""
    name = meeting_name.strip() or "未確認"
    held_at = meeting_datetime.strip() or "未確認"
    user_prompt = (
        "次の情報から議事録を作成してください。\n\n"
        f"入力された会議名: {name}\n"
        f"入力された開催日時: {held_at}\n\n"
        "文字起こし:\n"
        f"{transcript.strip()}"
    )
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=MINUTES_MODEL,
        messages=[
            {"role": "system", "content": MINUTES_INSTRUCTIONS},
            {"role": "user", "content": user_prompt},
        ],
    )
    return extract_message_text(response.choices[0].message)


class GoogleDocsSaveError(Exception):
    """画面に出してよい、認証情報を含まない保存エラー。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def format_meeting_datetime(meeting_date, meeting_time):
    """カレンダーの日付と開始時刻を、議事録とタイトル用の文字列にする。"""
    return f"{meeting_date.isoformat()} {meeting_time.strftime('%H:%M')}"


def build_document_title(meeting_name, meeting_datetime):
    """会議名と開催日時から、ドキュメントのタイトルを作る。"""
    name = meeting_name.strip()
    held_at = meeting_datetime.strip()
    if name and held_at:
        return f"{name} {held_at}"
    if name:
        return name
    if held_at:
        return f"議事録 {held_at}"
    return "議事録"


def validate_google_save_settings():
    """APIを呼ぶ前に、フォルダIDとOAuthクライアントファイルの有無だけを確認する。"""
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        return "保存先フォルダが未設定です。.env の GOOGLE_DRIVE_FOLDER_ID にフォルダIDを記入してください。"

    if not OAUTH_CLIENT_PATH.is_file():
        return (
            "credentials.json が見つかりません。"
            "OAuthクライアントのJSONを、プロジェクトのルートに credentials.json として置いてください。"
        )

    return None


def google_error_reason(error):
    """Googleのエラー理由だけを取り出す。メッセージ本文は使わない。"""
    try:
        payload = json.loads(error.content.decode("utf-8"))
        errors = payload.get("error", {}).get("errors", [])
        if errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason") or "")
    except (UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
        return ""
    return ""


def message_for_google_http_error(error):
    """HTTPステータスを、秘密情報を含まない説明に変える。"""
    status = getattr(error, "status_code", None)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    reason = google_error_reason(error)

    if status_code == 401 or reason == "authError":
        return (
            "Googleアカウントの認証が無効です。"
            "もう一度「Googleドキュメントに保存」を押し、ブラウザでログインしてください。"
        )

    if reason == "storageQuotaExceeded":
        return "Googleドライブの保存容量が不足しているため、ドキュメントを作成できません。"

    if status_code in (403, 404):
        return (
            "保存先フォルダにアクセスできません。"
            "ログインしたGoogleアカウントで、そのフォルダを開けるか確認してください。"
        )

    if status_code >= 500:
        return "Google APIとの通信に失敗しました。時間をおいて再度保存してください。"

    return "Googleドキュメントの作成に失敗しました。時間をおいて再度保存してください。"


def _token_has_required_scopes(credentials):
    """保存済みトークンが、今回必要な権限を含んでいるか確認する。"""
    granted_scopes = set(credentials.scopes or [])
    return set(GOOGLE_SCOPES).issubset(granted_scopes)


def _save_token(credentials):
    """更新したトークンを token.json に保存する。中身は画面に出さない。"""
    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")


def _is_access_denied(error):
    """許可拒否かどうかを判定する。判定に使った文面は表示しない。"""
    text = str(error).lower()
    return "access_denied" in text or "access denied" in text


def load_user_credentials():
    """保存済みトークンを使い、必要ならブラウザでログインし直す。"""
    credentials = None
    if TOKEN_PATH.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), GOOGLE_SCOPES)
        except (ValueError, OSError):
            credentials = None
        if credentials is not None and not _token_has_required_scopes(credentials):
            credentials = None

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_token(credentials)
            return credentials
        except GoogleAuthError:
            credentials = None

    if not OAUTH_CLIENT_PATH.is_file():
        raise GoogleDocsSaveError(
            "credentials.json が見つかりません。"
            "OAuthクライアントのJSONを、プロジェクトのルートに置いてください。"
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(OAUTH_CLIENT_PATH),
            GOOGLE_SCOPES,
        )
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            authorization_prompt_message="",
            prompt="consent",
            timeout_seconds=180,
        )
    except WSGITimeoutError:
        raise GoogleDocsSaveError(
            "ログインが完了していません。"
            "もう一度「Googleドキュメントに保存」を押し、ブラウザでGoogleアカウントにログインしてください。"
        ) from None
    except GoogleDocsSaveError:
        raise
    except Exception as error:
        if _is_access_denied(error):
            raise GoogleDocsSaveError(
                "Googleアカウントのアクセス許可が拒否されました。"
                "もう一度保存ボタンを押し、アクセスを許可してください。"
            ) from None
        raise GoogleDocsSaveError(
            "OAuth認証が完了していません。"
            "もう一度「Googleドキュメントに保存」を押し、ブラウザでログインしてください。"
        ) from None

    if credentials is None or not credentials.valid:
        raise GoogleDocsSaveError(
            "OAuth認証が完了していません。"
            "もう一度「Googleドキュメントに保存」を押し、ブラウザでログインしてください。"
        )

    _save_token(credentials)
    return credentials


def save_minutes_to_google_doc(minutes_text, meeting_name, meeting_datetime):
    """表示中の議事録を、指定フォルダに新しいGoogleドキュメントとして保存する。"""
    settings_error = validate_google_save_settings()
    if settings_error:
        raise GoogleDocsSaveError(settings_error)

    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    title = build_document_title(meeting_name, meeting_datetime)
    document_id = None
    drive = None
    try:
        credentials = load_user_credentials()
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
        created = drive.files().create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [folder_id],
            },
            fields="id",
            supportsAllDrives=True,
        ).execute()
        document_id = created["id"]
        docs.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": minutes_text,
                        }
                    }
                ]
            },
        ).execute()
    except GoogleDocsSaveError:
        raise
    except HttpError as error:
        _delete_incomplete_document(drive, document_id)
        raise GoogleDocsSaveError(message_for_google_http_error(error)) from None
    except (google_auth_exceptions.TransportError, google_auth_exceptions.TimeoutError):
        raise GoogleDocsSaveError(
            "Google APIとの通信に失敗しました。インターネット接続を確認して、もう一度保存してください。"
        ) from None
    except (TimeoutError, ConnectionError, OSError):
        raise GoogleDocsSaveError(
            "Google APIとの通信に失敗しました。インターネット接続を確認して、もう一度保存してください。"
        ) from None
    except Exception:
        _delete_incomplete_document(drive, document_id)
        raise GoogleDocsSaveError(
            "Googleドキュメントの保存に失敗しました。時間をおいて再度保存してください。"
        ) from None

    return f"https://docs.google.com/document/d/{document_id}/edit"


def _delete_incomplete_document(drive, document_id):
    """本文の書き込みに失敗した空のドキュメントを、可能なら削除する。"""
    if drive is None or not document_id:
        return
    try:
        drive.files().delete(fileId=document_id, supportsAllDrives=True).execute()
    except Exception:
        return


# ブラウザのタブに表示するタイトル
st.set_page_config(page_title="Google Meet 議事録自動作成ツール")

st.title("Google Meet 議事録自動作成ツール")
st.write(
    "Google Meetの録音データをアップロードし、"
    "AIで議事録を作成してGoogleドキュメントに保存するツールです。"
)

# 会議名が空のときは議事録で「未確認」にする。開催日時は選択した日付と時刻を使う。
meeting_name = st.text_input("会議名", placeholder="例: 週次定例")
meeting_date = st.date_input("会議日")
meeting_start_time = st.time_input("開始時刻")
meeting_datetime = format_meeting_datetime(meeting_date, meeting_start_time)

audio_file = st.file_uploader(
    "録音ファイル",
    type=["mp3", "mp4", "m4a", "wav", "webm"],
    help="対応形式: mp3, mp4, m4a, wav, webm",
)
st.caption("25MBを超える録音ファイルは、まだ文字起こしできません。")

if audio_file is not None:
    st.write(f"ファイル名: {audio_file.name}")
    st.write(f"ファイルサイズ: {format_file_size(audio_file.size)}")

if "transcript" not in st.session_state:
    st.session_state.transcript = ""
if "minutes" not in st.session_state:
    st.session_state.minutes = ""
if "google_doc_url" not in st.session_state:
    st.session_state.google_doc_url = ""

api_key = get_openai_api_key()
if not api_key:
    st.warning(
        "OPENAI_API_KEY が未設定です。"
        ".env にAPIキーを記入してから、もう一度実行してください。"
    )

if st.button("文字起こしを実行"):
    if not api_key:
        st.error(
            "OPENAI_API_KEY が未設定です。"
            ".env にAPIキーを記入してから、もう一度実行してください。"
        )
    else:
        validation_error = validate_audio_file(audio_file)
        if validation_error:
            st.error(validation_error)
        else:
            try:
                with st.spinner("音声を送信して、文字起こししています..."):
                    transcript_text = transcribe_audio(audio_file, api_key)
            except (
                AuthenticationError,
                RateLimitError,
                APITimeoutError,
                APIConnectionError,
                APIStatusError,
            ) as error:
                st.error(message_for_api_error(error))
            except Exception:
                st.error(
                    "文字起こし中に予期しないエラーが発生しました。"
                    "時間をおいて再度実行してください。"
                )
            else:
                if not transcript_text.strip():
                    st.warning(
                        "文字起こし結果が空でした。"
                        "音声が含まれているファイルか確認してください。"
                    )
                else:
                    st.session_state.transcript = transcript_text
                    st.session_state.minutes = ""
                    st.session_state.google_doc_url = ""
                    st.success("文字起こしが完了しました。")

if st.session_state.transcript:
    shown_transcript = st.text_area(
        "文字起こし結果",
        value=st.session_state.transcript,
        height=240,
    )
    st.download_button(
        "文字起こし結果をダウンロード",
        data=shown_transcript.encode("utf-8-sig"),
        file_name="transcription.txt",
        mime="text/plain",
    )

    if st.button("議事録を作成"):
        if not api_key:
            st.error(
                "OPENAI_API_KEY が未設定です。"
                ".env にAPIキーを記入してから、もう一度実行してください。"
            )
        elif not shown_transcript.strip():
            st.error(
                "文字起こし結果が空です。"
                "文字起こしを実行してから、議事録を作成してください。"
            )
        else:
            try:
                with st.spinner("文字起こし結果から議事録を作成しています..."):
                    minutes_text = create_minutes(
                        shown_transcript,
                        meeting_name,
                        meeting_datetime,
                        api_key,
                    )
            except (
                AuthenticationError,
                RateLimitError,
                APITimeoutError,
                APIConnectionError,
                APIStatusError,
            ) as error:
                st.error(message_for_minutes_error(error))
            except Exception:
                st.error(
                    "議事録の作成中に予期しないエラーが発生しました。"
                    "時間をおいて再度実行してください。"
                )
            else:
                if not minutes_text:
                    st.warning("議事録の本文が空でした。もう一度実行してください。")
                else:
                    st.session_state.minutes = minutes_text
                    st.session_state.google_doc_url = ""
                    st.success("議事録を作成しました。")

    if st.session_state.minutes:
        shown_minutes = st.text_area(
            "議事録",
            value=st.session_state.minutes,
            height=360,
        )
        st.download_button(
            "議事録をダウンロード",
            data=shown_minutes.encode("utf-8-sig"),
            file_name="minutes.txt",
            mime="text/plain",
        )

        st.caption(
            "初回はブラウザが開きます。自分のGoogleアカウントでログインし、アクセスを許可してください。"
        )
        if st.button("Googleドキュメントに保存"):
            if not shown_minutes.strip():
                st.error(
                    "保存する議事録が空です。"
                    "議事録を作成してから保存してください。"
                )
            else:
                try:
                    with st.spinner("Googleドキュメントに保存しています..."):
                        doc_url = save_minutes_to_google_doc(
                            shown_minutes,
                            meeting_name,
                            meeting_datetime,
                        )
                except GoogleDocsSaveError as error:
                    st.error(error.message)
                else:
                    st.session_state.google_doc_url = doc_url
                    st.success("Googleドキュメントに保存しました。")

        if st.session_state.google_doc_url:
            st.link_button("Googleドキュメントを開く", st.session_state.google_doc_url)
            st.write(st.session_state.google_doc_url)
