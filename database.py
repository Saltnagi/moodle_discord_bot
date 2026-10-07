import datetime
import sqlite3
from contextlib import contextmanager
from zoneinfo import ZoneInfo
 
from config import DB_FILE, DEFAULT_FILTER_WORDS
 
JST = ZoneInfo("Asia/Tokyo")
 
 
@contextmanager
def _connect():
    """接続を開き、正常終了ならcommit、最後に必ずcloseする"""
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
 
 
def init_db():
    """DB初期化と全データ取得"""
    with _connect() as conn:
        cursor = conn.cursor()
 
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                ics_url TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                word TEXT PRIMARY KEY
            )
        """)
        # 通知済みの予定（ユーザーごと）。uid + 開始日時で同一の予定とみなす。
        # 予定の日時が変更された場合は別の予定として再通知される。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notified (
                user_id INTEGER NOT NULL,
                uid     TEXT    NOT NULL,
                start   TEXT    NOT NULL,
                PRIMARY KEY (user_id, uid, start)
            )
        """)
 
        # 初回起動時にデフォルトのフィルターキーワードを挿入
        cursor.execute("SELECT COUNT(*) FROM filters")
        if cursor.fetchone()[0] == 0:
            cursor.executemany(
                "INSERT OR IGNORE INTO filters (word) VALUES (?)",
                [(word,) for word in DEFAULT_FILTER_WORDS],
            )
 
        # 30日以上前に開始した予定の通知履歴は不要なので掃除する
        cutoff = (
            datetime.datetime.now(JST) - datetime.timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M")
        cursor.execute("DELETE FROM notified WHERE start < ?", (cutoff,))
 
        cursor.execute("SELECT user_id, ics_url FROM users")
        user_data = {row[0]: row[1] for row in cursor.fetchall()}
 
        cursor.execute("SELECT word FROM filters")
        filter_data = [row[0] for row in cursor.fetchall()]
 
    return user_data, filter_data
 
 
# --- ユーザーURL操作 ---
def save_user_url(user_id: int, ics_url: str):
    """URLの保存・更新"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, ics_url) VALUES (?, ?)",
            (user_id, ics_url),
        )
 
 
def delete_user_url(user_id: int):
    """URLの削除（そのユーザーの通知履歴も一緒に消す）"""
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM notified WHERE user_id = ?", (user_id,))
 
 
# --- フィルターキーワード操作 ---
def add_filter_word(word: str):
    """フィルターキーワードをDBに追加"""
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO filters (word) VALUES (?)", (word,))
 
 
def delete_filter_word(word: str):
    """フィルターキーワードをDBから削除"""
    with _connect() as conn:
        conn.execute("DELETE FROM filters WHERE word = ?", (word,))
 
 
# --- 通知済み管理 ---
def get_notified_keys(user_id: int) -> set:
    """そのユーザーに通知済みの (uid, start) の集合を返す"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT uid, start FROM notified WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {(uid, start) for uid, start in rows}
 
 
def mark_notified(user_id: int, keys):
    """(uid, start) のリストを通知済みとして記録する"""
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO notified (user_id, uid, start) VALUES (?, ?, ?)",
            [(user_id, uid, start) for uid, start in keys],
        )