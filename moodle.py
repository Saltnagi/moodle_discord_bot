# Moodleサーバとの通信、.icsファイルの解析
import datetime
import logging
import unicodedata
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from config import REQUEST_TIMEOUT

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")


def normalize_text(text: str) -> str:
    """フィルター判定用の正規化。大文字小文字と、全角/半角の違いを無視できるようにする。
    （例: "Report" / "report" / "ＲＥＰＯＲＴ" はすべて同じ扱い）"""
    return unicodedata.normalize("NFKC", text).casefold()


class MoodleError(Exception):
    """取得・解析の失敗。メッセージはユーザーに見せても安全な固定文のみ。

    MoodleのカレンダーURLには認証トークンが含まれるため、
    requestsの例外メッセージ（URL入り）をそのまま表示してはいけない。
    """


@dataclass(frozen=True)
class MoodleEvent:
    uid: str
    summary: str
    start: datetime.datetime  # JST (aware)
    end: Optional[datetime.datetime]  # JST (aware)。終了時刻がなければNone
    all_day: bool

    @property
    def key(self) -> tuple:
        """通知済み管理用のキー。日時が変わった予定は別物として扱われる。"""
        return (self.uid, self.start.strftime("%Y-%m-%dT%H:%M"))

    def due_at(self, now: datetime.datetime) -> Optional[datetime.datetime]:
        """「次に迫っている期限」の時刻。これから始まる予定は開始時刻、
        すでに始まっている継続中の予定は終了時刻。終日予定や終了済みはNone。"""
        if self.all_day:
            return None
        if self.start >= now:
            return self.start
        if self.end is not None and self.end >= now:
            return self.end
        return None

    def is_due_soon(self, now: datetime.datetime, minutes: int) -> bool:
        """期限まで0〜minutes分の間ならTrue"""
        due = self.due_at(now)
        return due is not None and datetime.timedelta(0) <= due - now <= datetime.timedelta(minutes=minutes)

    def reminder_key(self, now: datetime.datetime) -> tuple:
        """リマインド済み管理用のキー（uid + 期限日時）"""
        due = self.due_at(now)
        return (self.uid, due.strftime("%Y-%m-%dT%H:%M") if due else "")

    def format(self) -> str:
        if self.all_day:
            text = self.start.strftime("%Y-%m-%d")
            if self.end is not None:
                # 終日予定のDTENDは「最終日の翌日」を指す
                last = (self.end - datetime.timedelta(days=1)).date()
                if last > self.start.date():
                    text += f" 〜 {last:%Y-%m-%d}"
        else:
            text = self.start.strftime("%Y-%m-%d %H:%M")
            if self.end is not None and self.end > self.start:
                if self.end.date() == self.start.date():
                    text += f"〜{self.end:%H:%M}"
                else:
                    text += f" 〜 {self.end:%Y-%m-%d %H:%M}"
        return f"・[{text}] {self.summary}"


@dataclass(frozen=True)
class FetchResult:
    """取得結果。0件のとき原因を説明できるよう、各段階の件数も持つ。"""

    events: list  # フィルター一致 かつ これからの予定（日時順）
    total: int  # カレンダー内の予定の総数
    upcoming: int  # そのうち、これから（または継続中）の予定の数


def _to_jst(dt) -> tuple:
    """日時の値をJSTのaware datetimeに揃える。戻り値: (datetime, all_day)"""
    if isinstance(dt, datetime.datetime):  # datetimeはdateのサブクラスなので先に判定
        if dt.tzinfo is None:
            # タイムゾーンなし（floating）はローカル時刻として扱う
            return dt.replace(tzinfo=JST), False
        return dt.astimezone(JST), False
    # 日付のみ（終日予定）。00:00 JST として保持する
    return datetime.datetime.combine(dt, datetime.time.min, tzinfo=JST), True


def _get_end(component, start: datetime.datetime) -> Optional[datetime.datetime]:
    """DTEND、なければ DURATION から終了時刻を求める"""
    dtend = component.get("dtend")
    if dtend is not None:
        end, _ = _to_jst(dtend.dt)
        return end
    duration = component.get("duration")
    if duration is not None:
        return start + duration.dt
    return None


def _is_upcoming(
    start: datetime.datetime,
    end: Optional[datetime.datetime],
    all_day: bool,
    now: datetime.datetime,
) -> bool:
    """これから始まる、または今まさに継続中の予定ならTrue"""
    if all_day:
        last_day = start.date()
        if end is not None:
            last_day = max(last_day, (end - datetime.timedelta(days=1)).date())
        return last_day >= now.date()
    # 開始が過去でも、終了が未来なら継続中（例: 開いている小テスト）
    return (max(start, end) if end is not None else start) >= now


def fetch_events(ics_url: str, filter_words: list, now=None) -> FetchResult:
    """Moodleから予定を取得し、フィルターに一致する「これから」の予定を返す。

    失敗時は MoodleError を投げる（エラー文を予定として返さない）。
    ネットワーク通信を行う同期関数なので、botからは asyncio.to_thread で呼ぶこと。
    """
    now = now or datetime.datetime.now(JST)

    try:
        response = requests.get(ics_url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        # 例外メッセージにURLが入るため、型名だけをログに残す
        log.warning("Moodle通信エラー: %s", type(e).__name__)
        raise MoodleError(
            "Moodleに接続できませんでした。時間をおいて再度お試しください。"
        ) from None

    if response.status_code != 200:
        log.warning("Moodleが HTTP %s を返しました", response.status_code)
        raise MoodleError(
            f"Moodleからのデータ取得に失敗しました (HTTP {response.status_code})。"
            "カレンダーURLが正しいか確認してください。"
        )

    try:
        cal = Calendar.from_ical(response.content)
    except Exception as e:
        log.warning("ICSの解析に失敗: %s", type(e).__name__)
        raise MoodleError("カレンダーデータの解析に失敗しました。") from None

    norm_words = [w for w in (normalize_text(w) for w in filter_words) if w]
    events = []
    total = 0
    upcoming = 0
    for component in cal.walk("VEVENT"):
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        total += 1

        start, all_day = _to_jst(dtstart.dt)
        end = _get_end(component, start)
        if not _is_upcoming(start, end, all_day, now):
            continue
        upcoming += 1

        summary = str(component.get("summary", ""))
        # フィルターが空なら全予定を対象にする。大文字小文字・全角半角は区別しない。
        if norm_words and not any(w in normalize_text(summary) for w in norm_words):
            continue

        uid = str(component.get("uid", "")) or summary
        events.append(
            MoodleEvent(uid=uid, summary=summary, start=start, end=end, all_day=all_day)
        )

    events.sort(key=lambda e: e.start)
    log.info(
        "ICS解析: 全%d件 / これから%d件 / フィルター一致%d件",
        total, upcoming, len(events),
    )
    return FetchResult(events=events, total=total, upcoming=upcoming)