# 設定を管理するファイル
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# 起動時のカレントディレクトリに依存しないよう、このファイルの隣にDBを置く
DB_FILE = str(Path(__file__).parent / "moodle_bot.db")

# 初回起動時（新規ユーザーのURL登録時）に登録されるフィルターキーワード
DEFAULT_FILTER_WORDS = ["期限", "試験"]

# 定期チェックの間隔（分）。「提出期限の1時間前」を捉えるため短めにしている。
CHECK_INTERVAL_MINUTES = 10

# 提出期限の何分前から「期限間近」として通知するか
REMINDER_BEFORE_MINUTES = 60

# DMを拒否しているユーザーに再送を試みる間隔（時間）
DM_RETRY_HOURS = 120

# Moodleへのリクエストのタイムアウト（秒）
REQUEST_TIMEOUT = 10


def require_token() -> str:
    """トークンが未設定なら、原因と対処をコンソールに出力して終了する"""
    token = (DISCORD_TOKEN or "").strip()
    if not token:
        sys.exit(
            "【エラー】DISCORD_TOKEN が設定されていません。\n"
            "  ・プロジェクト直下の .env ファイルに  DISCORD_TOKEN=あなたのトークン  と記述する\n"
            "  ・または環境変数 DISCORD_TOKEN を設定する\n"
            "のいずれかを行ってから、もう一度起動してください。"
        )
    return token