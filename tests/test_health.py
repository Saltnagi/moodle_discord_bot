import datetime
from datetime import timezone
import pytest
import database
import health
from errors import FailureKind

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_health.db")
    monkeypatch.setattr(database, "DB_FILE", test_db)
    database.init_db()

def test_health_success_flow():
    user_id = 99991
    now = datetime.datetime.now(timezone.utc)

    # 初期状態
    assert health.should_fetch(user_id, now) is True

    # 成功記録
    action = health.record_success(user_id, now)
    assert action == health.HealthAction.NONE
    assert database.get_user_health(user_id) is None

def test_health_invalid_state_transition():
    user_id = 99992
    t0 = datetime.datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)

    # 最初のINVALID失敗
    action = health.record_failure(user_id, FailureKind.INVALID, t0)
    assert action == health.HealthAction.NONE

    # まだ180分経っていない
    t1 = t0 + datetime.timedelta(minutes=100)
    action = health.record_failure(user_id, FailureKind.INVALID, t1)
    assert action == health.HealthAction.NONE

    # 180分経過
    t2 = t0 + datetime.timedelta(minutes=181)
    action = health.record_failure(user_id, FailureKind.INVALID, t2)
    assert action == health.HealthAction.NOTIFY_INVALID

    # 通知直後は再試行見送り (HEALTH_PROBE_INTERVAL_MIN=360分)
    t3 = t2 + datetime.timedelta(minutes=30)
    assert health.should_fetch(user_id, t3) is False

    # 360分経過後は再試行を許可
    t4 = t2 + datetime.timedelta(minutes=361)
    assert health.should_fetch(user_id, t4) is True

    # 復旧時の通知
    rec_action = health.record_success(user_id, t4)
    assert rec_action == health.HealthAction.NOTIFY_RECOVERED
    assert database.get_user_health(user_id) is None
