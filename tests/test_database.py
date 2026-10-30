import datetime
import pytest
import database

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_db.db")
    monkeypatch.setattr(database, "DB_FILE", test_db)
    monkeypatch.setattr(database, "DATA_DIR", str(tmp_path))
    database.init_db()

def test_user_url_and_delete():
    user_id = 12345
    url = "https://moodle.example.ac.jp/export.php"
    assert database.save_user_url(user_id, url) is True
    # 更新
    assert database.save_user_url(user_id, url) is False

    database.delete_user_url(user_id)
    with database._connect() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None

def test_cleanup_and_backup(tmp_path):
    now = datetime.datetime.now(database.JST)
    old = (now - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M")

    with database._connect() as conn:
        conn.execute("INSERT INTO notified (user_id, uid, start) VALUES (1, 'u1', ?)", (old,))
        conn.execute("INSERT INTO reminded (user_id, uid, due) VALUES (1, 'u1', ?)", (old,))

    database.cleanup_old_records(now, retention_days=30)

    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notified").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reminded").fetchone()[0] == 0

    backup_path = database.backup_db(keep=2)
    assert backup_path is not None
    assert (tmp_path / "backups").exists()
