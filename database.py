import datetime
import glob
import logging
import os
import shutil
import sqlite3
from contextlib import contextmanager
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from config import (
    BACKUP_KEEP,
    DATA_DIR,
    DB_FILE,
    DEFAULT_FILTER_WORDS,
    DEFAULT_NOTIFY_TIMES,
    RETENTION_DAYS,
)

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
_TS = "%Y-%m-%dT%H:%M:%S"  # 日時を文字列で保存するときの形式（文字列比較で大小が分かる）

# ユーザーを削除したときに、そのユーザーの行を消すテーブル
# （guide_shown は「ガイドを見せたか」の記録なので、URL削除では消さない）
_USER_TABLES = (
    "users",
    "user_filters",
    "user_excludes",
    "user_settings",
    "notified",
    "reminded",
    "dm_blocked",
    "digest_sent",
    "user_health",
)


def get_connection() -> sqlite3.Connection:
    """全DB操作はこの関数経由に統一。
    - PRAGMA busy_timeout = 5000
    - PRAGMA foreign_keys = ON
    """
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def _connect():
    """接続を開き、正常終了ならcommit、最後に必ずcloseする"""
    conn = get_connection()
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


# --- スキーマバージョン管理・マイグレーション ---
def _migration_v1(conn: sqlite3.Connection):
    """初期スキーマのインデックス作成などの調整（ベースライン）"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notified_user_start ON notified(user_id, start);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminded_user_due ON reminded(user_id, due);")


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_v1),
]


def run_migrations(conn: sqlite3.Connection):
    """metaテーブルの schema_version を確認し、未適用のマイグレーションを順次実行する"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    current_version = int(row[0]) if row else 0

    for version, migration_fn in MIGRATIONS:
        if version > current_version:
            log.info("DBマイグレーション適用中: v%d -> v%d", current_version, version)
            migration_fn(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(version),),
            )
            current_version = version


def init_db() -> dict:
    """DB初期化。登録済みユーザーの {user_id: ics_url} を返す"""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                ics_url TEXT NOT NULL
            )
        """)
        # ユーザーごとのフィルターキーワード（これに一致する予定だけ通知。空なら全予定）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id INTEGER NOT NULL,
                word    TEXT    NOT NULL,
                PRIMARY KEY (user_id, word)
            )
        """)
        # ユーザーごとの除外キーワード（ブラックリスト。一致する予定は常に通知しない）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_excludes (
                user_id INTEGER NOT NULL,
                word    TEXT    NOT NULL,
                PRIMARY KEY (user_id, word)
            )
        """)
        # ユーザーごとの設定。NULLは「未設定（既定値を使う）」を表す。
        #   notify_times: 'HH:MM,HH:MM' 形式。空文字は「定刻通知オフ」
        #   period_days : 通知対象にする期間（今後何日以内か）。NULLは制限なし
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id      INTEGER PRIMARY KEY,
                notify_times TEXT,
                period_days  INTEGER
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
        # 送信済みの定刻通知。slot は 'YYYY-MM-DD HH:MM'
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digest_sent (
                user_id INTEGER NOT NULL,
                slot    TEXT    NOT NULL,
                PRIMARY KEY (user_id, slot)
            )
        """)
        # 使い方ガイドを表示済みのユーザー
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guide_shown (
                user_id INTEGER PRIMARY KEY
            )
        """)
        # Bot全体の小さな記録（例: 最後にコマンドを同期したときの COMMANDS_VERSION）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # シラバスの授業名キャッシュ（全ユーザー共有）
        # name が NULL の場合は「シラバスに見つからなかった（ネガティブキャッシュ）」を表す
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                year       INTEGER NOT NULL,
                code       TEXT    NOT NULL,
                name       TEXT,
                fetched_at TEXT    NOT NULL,
                PRIMARY KEY (year, code)
            )
        """)
        # ユーザーのヘルスチェック状態（連続失敗・失効検知）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_health (
                user_id          INTEGER PRIMARY KEY,
                first_failure_at TEXT,
                last_failure_at  TEXT,
                last_kind        TEXT,
                notified_at      TEXT
            )
        """)

        conn.execute("PRAGMA journal_mode = WAL;")

        _migrate_global_filters(conn)
        run_migrations(conn)

        # すでに登録済みのユーザーは、これまでBotとやり取りしているのでガイドは表示済みとする
        conn.execute(
            "INSERT OR IGNORE INTO guide_shown (user_id) SELECT user_id FROM users"
        )

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
    """URLの削除（そのユーザーのフィルター・設定・通知履歴・DM拒否状態も一緒に消す）"""
    with _connect() as conn:
        for table in _USER_TABLES:
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
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_filters (user_id, word) VALUES (?, ?)",
            (user_id, word),
        )


def delete_filter_word(user_id: int, word: str):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_filters WHERE user_id = ? AND word = ?",
            (user_id, word),
        )


# --- 除外キーワード操作（ユーザーごと） ---
def get_exclude_words(user_id: int) -> list:
    """そのユーザーの除外キーワードを登録順で返す"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT word FROM user_excludes WHERE user_id = ? ORDER BY rowid",
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def add_exclude_word(user_id: int, word: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_excludes (user_id, word) VALUES (?, ?)",
            (user_id, word),
        )


def delete_exclude_word(user_id: int, word: str):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_excludes WHERE user_id = ? AND word = ?",
            (user_id, word),
        )


# --- ユーザー設定（定刻通知の時刻・通知する期間） ---
def _ensure_settings_row(conn, user_id: int):
    conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))


def get_notify_times(user_id: int) -> list:
    """定刻通知の時刻（'HH:MM' のリスト）。未設定なら既定値、オフなら空リスト"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT notify_times FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None or row[0] is None:
        return list(DEFAULT_NOTIFY_TIMES)
    return [t for t in row[0].split(",") if t]


def set_notify_times(user_id: int, times: Optional[list]):
    """定刻通知の時刻を保存する。None=既定値に戻す、[]=オフ"""
    value = None if times is None else ",".join(times)
    with _connect() as conn:
        _ensure_settings_row(conn, user_id)
        conn.execute(
            "UPDATE user_settings SET notify_times = ? WHERE user_id = ?",
            (value, user_id),
        )


def get_period_days(user_id: int) -> Optional[int]:
    """通知対象の期間（今後何日以内か）。Noneなら制限なし"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT period_days FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def set_period_days(user_id: int, days: Optional[int]):
    with _connect() as conn:
        _ensure_settings_row(conn, user_id)
        conn.execute(
            "UPDATE user_settings SET period_days = ? WHERE user_id = ?",
            (days, user_id),
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
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO reminded (user_id, uid, due) VALUES (?, ?, ?)",
            [(user_id, uid, due) for uid, due in keys],
        )


# --- 定刻通知の送信済み管理 ---
def get_digest_sent(user_id: int) -> set:
    """送信済みの定刻通知のslot（'YYYY-MM-DD HH:MM'）の集合"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slot FROM digest_sent WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row[0] for row in rows}


def mark_digest_sent(user_id: int, slots):
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO digest_sent (user_id, slot) VALUES (?, ?)",
            [(user_id, slot) for slot in slots],
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


# --- 使い方ガイド ---
def has_seen_guide(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM guide_shown WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def mark_guide_shown(user_id: int):
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO guide_shown (user_id) VALUES (?)", (user_id,))


# --- Bot全体の記録 ---
def get_meta(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str):
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# --- シラバスの授業名キャッシュ操作 ---
COURSE_HIT_TTL_DAYS = 180
COURSE_MISS_TTL_DAYS = 7


def get_course_name(year: int, code: str) -> tuple[bool, Optional[str]]:
    """(有効なキャッシュがあるか, 授業名 or None)。TTL判定はここで統一"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, fetched_at FROM courses WHERE year = ? AND code = ?",
            (year, code),
        ).fetchone()
    if not row:
        return False, None

    name, fetched_at_str = row[0], row[1]
    try:
        fetched_at = datetime.datetime.strptime(fetched_at_str, _TS).replace(tzinfo=JST)
    except Exception:
        return False, None

    now = datetime.datetime.now(JST)
    ttl_days = COURSE_HIT_TTL_DAYS if name is not None else COURSE_MISS_TTL_DAYS
    if now - fetched_at > datetime.timedelta(days=ttl_days):
        return False, None

    return True, name


def save_course_name(year: int, code: str, name: Optional[str]) -> None:
    """nameがNoneならネガティブキャッシュとして保存"""
    now_str = datetime.datetime.now(JST).strftime(_TS)
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courses (year, code, name, fetched_at) VALUES (?, ?, ?, ?)",
            (year, code, name, now_str),
        )


# --- ユーザーヘルス状態操作 ---
def get_user_health(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT first_failure_at, last_failure_at, last_kind, notified_at FROM user_health WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": user_id,
        "first_failure_at": row[0],
        "last_failure_at": row[1],
        "last_kind": row[2],
        "notified_at": row[3],
    }


def save_user_health(
    user_id: int,
    first_failure_at: str,
    last_failure_at: str,
    last_kind: str,
    notified_at: Optional[str],
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_health
                (user_id, first_failure_at, last_failure_at, last_kind, notified_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, first_failure_at, last_failure_at, last_kind, notified_at),
        )


def delete_user_health(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM user_health WHERE user_id = ?", (user_id,))


# --- クリーンアップ・バックアップ ---
def cleanup_old_records(now: Optional[datetime.datetime] = None, retention_days: int = RETENTION_DAYS) -> None:
    """
    - notified: start が retention_days 日以上前のレコードを削除
    - reminded: due が retention_days 日以上前のレコードを削除
    - digest_sent: slot（日付部分）が retention_days 日以上前のレコードを削除
    - courses: fetched_at が 365日以上前のレコードを削除
    """
    if now is None:
        now = datetime.datetime.now(JST)

    cutoff = (now - datetime.timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M")
    slot_cutoff = (now - datetime.timedelta(days=retention_days)).strftime("%Y-%m-%d 00:00")
    course_cutoff = (now - datetime.timedelta(days=365)).strftime(_TS)

    with _connect() as conn:
        conn.execute("DELETE FROM notified WHERE start < ?", (cutoff,))
        conn.execute("DELETE FROM reminded WHERE due < ?", (cutoff,))
        conn.execute("DELETE FROM digest_sent WHERE slot < ?", (slot_cutoff,))
        conn.execute("DELETE FROM courses WHERE fetched_at < ?", (course_cutoff,))


def backup_db(keep: int = BACKUP_KEEP) -> Optional[str]:
    """
    - backups/ ディレクトリを DATA_DIR 配下に作成
    - sqlite3 の backup API を使用（稼働中のロック競合を回避）
    - ファイル名: backups/moodle_bot_YYYYMMDD_HHMMSS.db
    - 成功後、古いバックアップを keep 世代残して削除
    """
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"moodle_bot_{timestamp}.db")

    try:
        src = get_connection()
        dst = sqlite3.connect(dest_path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        log.info("DBバックアップを作成しました: %s", dest_path)
    except Exception as e:
        log.error("DBバックアップの作成に失敗しました: %s", e)
        return None

    # 古いバックアップのローテーション
    pattern = os.path.join(backup_dir, "moodle_bot_*.db")
    backups = sorted(glob.glob(pattern))
    if len(backups) > keep:
        for old_file in backups[:-keep]:
            try:
                os.remove(old_file)
                log.info("古いDBバックアップを削除しました: %s", old_file)
            except OSError as e:
                log.warning("古いDBバックアップの削除に失敗 (%s): %s", old_file, e)

    return dest_path

