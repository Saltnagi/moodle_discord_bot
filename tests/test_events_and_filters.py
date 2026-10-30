import datetime
import pytest
import moodle

def test_normalize_text():
    # 全角半角・大文字小文字の正規化
    assert moodle.normalize_text("Ｔｅｓｔ １２３") == "test 123"
    assert moodle.normalize_text("テスト　課題") == "テスト 課題"

def test_due_soon_and_keys():
    now = datetime.datetime(2026, 10, 30, 10, 0, tzinfo=moodle.JST)
    due_in_30m = now + datetime.timedelta(minutes=30)
    due_in_90m = now + datetime.timedelta(minutes=90)

    ev_soon = moodle.MoodleEvent(
        uid="event1",
        summary="レポート提出",
        start=due_in_30m,
        end=None,
        all_day=False,
        course="プログラミング演習",
    )
    ev_later = moodle.MoodleEvent(
        uid="event2",
        summary="期末試験",
        start=due_in_90m,
        end=None,
        all_day=False,
        course="数学",
    )

    assert ev_soon.is_due_soon(now, minutes=60) is True
    assert ev_later.is_due_soon(now, minutes=60) is False

    assert ev_soon.key == ("event1", due_in_30m.strftime("%Y-%m-%dT%H:%M"))
