import sqlite3
from config import DB_FILE, DEFAULT_FILTER_WORDS

def init_db():
    """DB初期化と全データ取得"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # usersテーブルが存在しない場合は作成
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            ics_url TEXT NOT NULL
        )
    """)
    # filters テーブルの作成 (word)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            word TEXT PRIMARY KEY
        )
    """)
    # 初回起動時にデフォルトのフィルターキーワードを挿入
    cursor.execute("SELECT COUNT(*) FROM filters")
    if cursor.fetchone()[0] == 0:
        # config.pyのリストから動的に挿入データを作成
        default_words = [(word,) for word in DEFAULT_FILTER_WORDS]
        cursor.executemany(
            "INSERT INTO filters (word) VALUES (?)", default_words
        )

    conn.commit()

    # URLデータの読み込み
    cursor.execute("SELECT user_id, ics_url FROM users")
    user_data = {row[0]: row[1] for row in cursor.fetchall()}

    # フィルターキーワードの読み込み
    cursor.execute("SELECT word FROM filters")
    filter_data = [row[0] for row in cursor.fetchall()]

    conn.close()

    return user_data, filter_data


# --- ユーザーURL操作 ---
def save_user_url(user_id: int, ics_url: str):
    """URLの保存・更新"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO users (user_id, ics_url)
        VALUES (?, ?)
    """,
        (user_id, ics_url),
    )
    conn.commit()
    conn.close()


def delete_user_url(user_id: int):
    """URLの削除"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# --- フィルターキーワード操作 ---
def add_filter_word(word: str):
    """フィルターキーワードをDBに追加"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO filters (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()


def delete_filter_word(word: str):
    """フィルターキーワードをDBから削除"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM filters WHERE word = ?", (word,))
    conn.commit()
    conn.close()