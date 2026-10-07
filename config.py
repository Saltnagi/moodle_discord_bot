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

# 定期チェック（新着通知・期限間近の通知）の間隔（分）
CHECK_INTERVAL_MINUTES = 10

# 提出期限の何分前から「期限間近」として通知するか
REMINDER_BEFORE_MINUTES = 60

# DMを拒否しているユーザーに再送を試みる間隔（時間）
DM_RETRY_HOURS = 120

# Moodleへのリクエストのタイムアウト（秒）
REQUEST_TIMEOUT = 10

# --- 定刻通知（毎日この時刻に、今後の予定一覧を送る） ---
# 初期値。ユーザーごとに !通知時間 で変更できる（時刻は日本時間）
DEFAULT_NOTIFY_TIMES = ["08:30", "18:00"]
# 1人が登録できる定刻の数
MAX_NOTIFY_TIMES = 4
# 定刻を過ぎても、この時間内なら送る（再起動やMoodle障害からの復帰用）
DIGEST_GRACE_MINUTES = 30
# 定刻通知の取得・送信に失敗したときの再試行間隔（分）
DIGEST_RETRY_MINUTES = 5
# 定刻通知に載せる予定の範囲（今後何日分か）。ユーザーの !期間 設定がこれより短ければそちらを優先
DIGEST_MAX_DAYS = 7

# Botのステータス欄に常時表示するメッセージ
STATUS_MESSAGE = "!help で使い方を表示"


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