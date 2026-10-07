import datetime
import sqlite3
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from config import DB_FILE, DEFAULT_FILTER_WORDS

JST = ZoneInfo("Asia/Tokyo")
_TS = "%Y-%m-%dT%H:%M:%S"  # 日時を文字列で保存するときの形式（文字列比較で大小が分かる）


@contextmanager
def _connect():
    """接続を開き、正常終了ならcommit、最後に必ずcloseする"""
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_global_filters(conn):
    """旧バージョンの全ユーザー共通フィルター(filtersテーブル)を、
    登録済みの各ユーザーにコピーしてから廃止する。2回目以降は何もしない。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='filters'"
    ).fetchone()
    if not exists:
        return
    conn.execute("""
        INSERT OR IGNORE INTO user_filters (user_id, word)
        SELECT u.user_id, f.word FROM users u CROSS JOIN filters f
    """)
    conn.execute("DROP TABLE filters")


def init_db() -> dict:
    """DB初期化。登録済みユーザーの {user_id: ics_url} を返す"""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                ics_url TEXT NOT NULL
            )
        """)
        # ユーザーごとのフィルターキーワード
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id INTEGER NOT NULL,
                word    TEXT    NOT NULL,
                PRIMARY KEY (user_id, word)
            )
        """)
        # 新着として通知済みの予定。uid + 開始日時で同一の予定とみなす。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notified (
                user_id INTEGER NOT NULL,
                uid     TEXT    NOT NULL,
                start   TEXT    NOT NULL,
                PRIMARY KEY (user_id, uid, start)
            )
        """)
        # 期限間近としてリマインド済みの予定。uid + 期限日時で同一とみなす。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminded (
                user_id INTEGER NOT NULL,
                uid     TEXT    NOT NULL,
                due     TEXT    NOT NULL,
                PRIMARY KEY (user_id, uid, due)
            )
        """)
        # DM拒否中のユーザーと、再送を試みる日時
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_blocked (
                user_id     INTEGER PRIMARY KEY,
                retry_after TEXT NOT NULL
            )
        """)

        _migrate_global_filters(conn)

        # 30日以上前の予定の通知履歴は不要なので掃除する
        cutoff = (
            datetime.datetime.now(JST) - datetime.timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M")
        conn.execute("DELETE FROM notified WHERE start < ?", (cutoff,))
        conn.execute("DELETE FROM reminded WHERE due < ?", (cutoff,))

        rows = conn.execute("SELECT user_id, ics_url FROM users").fetchall()

    return {user_id: ics_url for user_id, ics_url in rows}


# --- ユーザーURL操作 ---
def save_user_url(user_id: int, ics_url: str) -> bool:
    """URLの保存・更新。新規ユーザーならデフォルトのフィルターも登録し、Trueを返す"""
    with _connect() as conn:
        is_new = (
            conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            is None
        )
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, ics_url) VALUES (?, ?)",
            (user_id, ics_url),
        )
        if is_new:
            conn.executemany(
                "INSERT OR IGNORE INTO user_filters (user_id, word) VALUES (?, ?)",
                [(user_id, w) for w in DEFAULT_FILTER_WORDS],
            )
    return is_new


def delete_user_url(user_id: int):
    """URLの削除（そのユーザーのフィルター・通知履歴・DM拒否状態も一緒に消す）"""
    with _connect() as conn:
        for table in ("users", "user_filters", "notified", "reminded", "dm_blocked"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


# --- フィルターキーワード操作（ユーザーごと） ---
def get_filter_words(user_id: int) -> list:
    """そのユーザーのフィルターキーワードを登録順で返す"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT word FROM user_filters WHERE user_id = ? ORDER BY rowid",
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def add_filter_word(user_id: int, word: str):
    """フィルターキーワードを追加"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_filters (user_id, word) VALUES (?, ?)",
            (user_id, word),
        )


def delete_filter_word(user_id: int, word: str):
    """フィルターキーワードを削除"""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_filters WHERE user_id = ? AND word = ?",
            (user_id, word),
        )


# --- 新着の通知済み管理 ---
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


# --- 期限間近のリマインド済み管理 ---
def get_reminded_keys(user_id: int) -> set:
    """そのユーザーにリマインド済みの (uid, due) の集合を返す"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT uid, due FROM reminded WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {(uid, due) for uid, due in rows}


def mark_reminded(user_id: int, keys):
    """(uid, due) のリストをリマインド済みとして記録する"""
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO reminded (user_id, uid, due) VALUES (?, ?, ?)",
            [(user_id, uid, due) for uid, due in keys],
        )


# --- DM拒否の管理 ---
def block_dm(user_id: int, retry_after: datetime.datetime):
    """DMを拒否されたユーザーを記録し、retry_after までは送信を試みないようにする"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dm_blocked (user_id, retry_after) VALUES (?, ?)",
            (user_id, retry_after.strftime(_TS)),
        )


def is_dm_blocked(user_id: int, now: datetime.datetime) -> bool:
    """再送予定日時になるまではTrue（送信を見送る）"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT retry_after FROM dm_blocked WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None and row[0] > now.strftime(_TS)


def clear_dm_block(user_id: int):
    """DM拒否の記録を消す（DMが届いたとき、または本人が操作してきたとき）"""
    with _connect() as conn:
        conn.execute("DELETE FROM dm_blocked WHERE user_id = ?", (user_id,))