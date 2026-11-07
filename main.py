"""EC商品価格・在庫管理ツールの入口。

products.txt のASINをReefAPIで取得し、スプレッドシートの末尾に履歴として追記する。
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from check_products import load_asins
from reefapi_test import fetch_product, load_api_key

REQUIRED_SETTINGS = (
    "SERVICE_ACCOUNT_FILE",
    "SPREADSHEET_ID",
    "WORKSHEET_NAME",
)
EXPECTED_HEADERS = [
    "ASIN",
    "商品名",
    "Amazon価格",
    "在庫状況",
    "新品出荷数",
    "商品URL",
    "取得日時",
    "取得結果",
    "エラー理由",
]
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


def confirm_project_venv():
    project_dir = Path(__file__).resolve().parent
    venv_python = (project_dir / ".venv" / "Scripts" / "python.exe").resolve()
    executable = Path(sys.executable).resolve()
    print(f"使用中のPython: {executable}")
    if executable != venv_python:
        print("課題フォルダの .venv が使われていません。")
        print(r"次を実行してから、もう一度起動してください: .\.venv\Scripts\Activate.ps1")
        sys.exit(1)
    print("仮想環境: 課題フォルダの .venv")


def japan_now_text():
    try:
        import tzdata  # Windowsで Asia/Tokyo を解決するために必要
    except ModuleNotFoundError:
        print("日本時間のデータが仮想環境にありません。")
        print("python -m pip install -r requirements.txt を実行してください。")
        sys.exit(1)

    del tzdata
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")


def load_settings():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        print(".env が見つかりません。課題フォルダに .env を置いてください。")
        sys.exit(1)

    load_dotenv(env_path)

    missing = [
        name for name in REQUIRED_SETTINGS if not os.getenv(name, "").strip()
    ]
    if missing:
        print("設定が不足しています: " + ", ".join(missing))
        sys.exit(1)

    key_path = Path(os.environ["SERVICE_ACCOUNT_FILE"])
    if not key_path.is_file():
        print("JSON鍵のファイルが見つかりません。")
        print("SERVICE_ACCOUNT_FILE の場所を確認してください。")
        sys.exit(1)

    return {
        "key_path": key_path,
        "spreadsheet_id": os.environ["SPREADSHEET_ID"],
        "worksheet_name": os.environ["WORKSHEET_NAME"],
    }


def open_worksheet(settings):
    credentials = Credentials.from_service_account_file(
        settings["key_path"],
        scopes=SHEETS_SCOPE,
    )
    client = gspread.authorize(credentials)
    try:
        spreadsheet = client.open_by_key(settings["spreadsheet_id"])
        worksheet = spreadsheet.worksheet(settings["worksheet_name"])
    except SpreadsheetNotFound:
        print("スプレッドシートを開けませんでした。")
        print("IDと、サービスアカウントへの編集者の共有を確認してください。")
        sys.exit(1)
    except WorksheetNotFound:
        print("指定したシートが見つかりません。")
        print(f"タブ名: {settings['worksheet_name']}")
        sys.exit(1)
    except APIError as error:
        print(f"スプレッドシートの操作に失敗しました。コード: {error.code}")
        if error.code == 403:
            print("サービスアカウントが編集者として共有されているか確認してください。")
        sys.exit(1)
    return worksheet


def require_headers(worksheet):
    headers = worksheet.row_values(1)
    if headers != EXPECTED_HEADERS:
        print("1行目の列名が予定と違うため、書き込みを中止しました。")
        print("現在の1行目: " + ", ".join(headers))
        sys.exit(1)


def product_url(asin):
    return f"https://www.amazon.co.jp/dp/{asin}"


def history_row(asin, timestamp, result):
    if result["ok"]:
        values = {
            "ASIN": asin,
            "商品名": result["title"],
            "Amazon価格": result["price"],
            "在庫状況": result["stock"],
            "新品出荷数": "",
            "商品URL": product_url(asin),
            "取得日時": timestamp,
            "取得結果": "成功",
            "エラー理由": "",
        }
    else:
        reason = result["error"] or "商品情報を取得できませんでした。"
        if result.get("status_code") is not None:
            reason = f"HTTP {result['status_code']}: {reason}"
        values = {
            "ASIN": asin,
            "商品名": "",
            "Amazon価格": "",
            "在庫状況": "",
            "新品出荷数": "",
            "商品URL": product_url(asin),
            "取得日時": timestamp,
            "取得結果": "失敗",
            "エラー理由": reason,
        }
    return [values[header] for header in EXPECTED_HEADERS]


def append_history(worksheet, row):
    try:
        worksheet.append_row(row, value_input_option="RAW")
    except APIError as error:
        print(f"スプレッドシートへの追記に失敗しました。コード: {error.code}")
        return False
    return True


def main():
    confirm_project_venv()
    settings = load_settings()
    api_key = load_api_key()
    asins = load_asins()
    print("設定を読み込みました")
    print(f"シート名: {settings['worksheet_name']}")
    print(f"ASIN: {len(asins)}件")

    worksheet = open_worksheet(settings)
    require_headers(worksheet)

    success_count = 0
    failure_count = 0
    for asin in asins:
        result = fetch_product(api_key, asin)
        timestamp = japan_now_text()
        recorded = append_history(worksheet, history_row(asin, timestamp, result))
        if not recorded:
            failure_count += 1
            print(f"{asin}: シートへ記録できませんでした")
            continue
        if result["ok"]:
            success_count += 1
            print(f"{asin}: 成功")
        else:
            failure_count += 1
            print(f"{asin}: 失敗")

    print(f"記録しました: 成功 {success_count}件、失敗 {failure_count}件")
    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
