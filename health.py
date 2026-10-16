from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import database
from config import (
    HEALTH_ANY_AFTER_HOURS,
    HEALTH_INVALID_AFTER_MIN,
    HEALTH_PROBE_INTERVAL_MIN,
)
from errors import FailureKind


class HealthAction(Enum):
    NONE = "none"
    NOTIFY_INVALID = "notify_invalid"       # 「URLが無効の可能性」DMを送る
    NOTIFY_RECOVERED = "notify_recovered"   # 「復旧しました」DMを送る


def _ensure_dt(val) -> datetime:
    """文字列またはdatetimeをUTC aware datetimeに揃える"""
    if isinstance(val, str):
        dt = datetime.fromisoformat(val)
    elif isinstance(val, datetime):
        dt = val
    else:
        raise ValueError(f"Invalid datetime value: {val}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_str(dt: datetime) -> str:
    """UTCのISO8601文字列に変換"""
    return _ensure_dt(dt).isoformat()


def record_success(user_id: int, now: datetime) -> HealthAction:
    """
    user_health に notified_at がある場合のみ NOTIFY_RECOVERED を返す。
    いずれの場合も行を削除（状態リセット）。
    """
    row = database.get_user_health(user_id)
    if not row:
        return HealthAction.NONE

    action = HealthAction.NOTIFY_RECOVERED if row.get("notified_at") else HealthAction.NONE
    database.delete_user_health(user_id)
    return action


def record_failure(user_id: int, kind: FailureKind, now: datetime) -> HealthAction:
    """
    - 行がなければ first_failure_at=now で作成。あれば first_failure_at は維持
    - last_failure_at=now, last_kind=kind を更新
    - notified_at が NULL で、かつ次のいずれかなら NOTIFY_INVALID を返し notified_at=now を保存:
        (a) kind==INVALID かつ now-first_failure_at >= HEALTH_INVALID_AFTER_MIN
        (b) now-first_failure_at >= HEALTH_ANY_AFTER_HOURS
    - それ以外は NONE
    """
    now_dt = _ensure_dt(now)
    now_str = _to_str(now_dt)

    row = database.get_user_health(user_id)
    if row:
        first_failure_dt = _ensure_dt(row["first_failure_at"])
        first_failure_str = row["first_failure_at"]
        notified_at_str = row.get("notified_at")
    else:
        first_failure_dt = now_dt
        first_failure_str = now_str
        notified_at_str = None

    action = HealthAction.NONE
    if notified_at_str is None:
        elapsed = now_dt - first_failure_dt
        cond_a = (
            kind == FailureKind.INVALID
            and elapsed >= timedelta(minutes=HEALTH_INVALID_AFTER_MIN)
        )
        cond_b = elapsed >= timedelta(hours=HEALTH_ANY_AFTER_HOURS)

        if cond_a or cond_b:
            action = HealthAction.NOTIFY_INVALID
            notified_at_str = now_str

    database.save_user_health(
        user_id=user_id,
        first_failure_at=first_failure_str,
        last_failure_at=now_str,
        last_kind=kind.value,
        notified_at=notified_at_str,
    )
    return action


def should_fetch(user_id: int, now: datetime) -> bool:
    """
    - 行がない、または notified_at が NULL → True
    - notified_at がある場合 → now - last_failure_at >= HEALTH_PROBE_INTERVAL_MIN のときのみ True
    """
    row = database.get_user_health(user_id)
    if not row or not row.get("notified_at"):
        return True

    now_dt = _ensure_dt(now)
    last_failure_dt = _ensure_dt(row["last_failure_at"])
    return (now_dt - last_failure_dt) >= timedelta(minutes=HEALTH_PROBE_INTERVAL_MIN)


def reset(user_id: int) -> None:
    """user_health の行を削除（/url 登録・削除時）"""
    database.delete_user_health(user_id)
