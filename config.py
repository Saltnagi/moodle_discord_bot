# 設定を管理するファイル
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# 起動時のカレントディレクトリに依存しないよう、このファイルの隣にDBを置く
DB_FILE = str(Path(__file__).parent / "moodle_bot.db")

# 初回起動時に登録されるフィルターキーワード（重複させないこと）
DEFAULT_FILTER_WORDS = ["期限", "試験", "締切", "提出", "課題", "レポート", "小テスト", "テスト"]

# 定期チェックの間隔（分）
CHECK_INTERVAL_MINUTES = 60

# Moodleへのリクエストのタイムアウト（秒）
REQUEST_TIMEOUT = 10