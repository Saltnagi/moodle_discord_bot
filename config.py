# 設定を管理するファイル
import os
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DB_FILE = "moodle_bot.db"
DEFAULT_FILTER_WORDS = ["期限", "試験", "期限"]