# 設定を管理するファイル
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")


def _parse_hosts(raw) -> list:
    """'a.example.jp, B.example.jp' → ['a.example.jp', 'b.example.jp']"""
    return [h.strip().lower() for h in (raw or "").split(",") if h.strip()]


# Botがアクセスしてよいドメイン（.env の MOODLE_ALLOWED_HOSTS で設定。カンマ区切りで複数可）。
# ここに無いサイトのURLは、登録も取得もできない（SSRF対策）。
MOODLE_ALLOWED_HOSTS = frozenset(_parse_hosts(os.environ.get("MOODLE_ALLOWED_HOSTS")))

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

# --- 入力・通信の上限（悪用や誤入力の対策） ---
MAX_WORD_LENGTH = 50  # フィルター／除外ワード1つあたりの最大文字数
MAX_WORDS_PER_LIST = 30  # フィルター／除外ワードそれぞれの最大登録数
MAX_URL_LENGTH = 2000  # 登録できるURLの最大文字数
MAX_REDIRECTS = 3  # Moodleからのリダイレクトを追う最大回数（毎回、許可ドメインか検査する）

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


def require_allowed_hosts() -> frozenset:
    """許可ドメインが未設定・不正なら、原因と対処をコンソールに出力して終了する"""
    hosts = _parse_hosts(os.environ.get("MOODLE_ALLOWED_HOSTS"))
    if not hosts:
        sys.exit(
            "【エラー】MOODLE_ALLOWED_HOSTS が設定されていません。\n"
            "  ・.env ファイルに  MOODLE_ALLOWED_HOSTS=大学のMoodleのドメイン  を1行追加してください。\n"
            "    （例: MOODLE_ALLOWED_HOSTS=moodle.example.ac.jp）\n"
            "  ・ドメインは、MoodleのカレンダーURL（https://◯◯◯/…）の ◯◯◯ の部分です。\n"
            "Botは、ここに書いたサイト以外のURLにはアクセスしません（セキュリティ対策）。"
        )
    bad = [h for h in hosts if any(c in h for c in "/:@ ?#")]
    if bad:
        sys.exit(
            "【エラー】MOODLE_ALLOWED_HOSTS には、https:// やパスを付けず、ドメイン名だけを書いてください。\n"
            "  誤: https://moodle.example.ac.jp/\n"
            "  正: moodle.example.ac.jp\n"
            "  問題のある値: " + ", ".join(bad)
        )
    return frozenset(hosts)