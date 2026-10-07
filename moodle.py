# Moodleサーバとの通信、.icsファイルの解析
import datetime
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo
 
import requests
from icalendar import Calendar
 
from config import REQUEST_TIMEOUT
 
log = logging.getLogger(__name__)
 
JST = ZoneInfo("Asia/Tokyo")
 
 
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
    all_day: bool
 
    @property
    def key(self) -> tuple:
        """通知済み管理用のキー。日時が変わった予定は別物として扱われる。"""
        return (self.uid, self.start.strftime("%Y-%m-%dT%H:%M"))
 
    def format(self) -> str:
        fmt = "%Y-%m-%d" if self.all_day else "%Y-%m-%d %H:%M"
        return f"・[{self.start.strftime(fmt)}] {self.summary}"
 
 
def _to_jst(dt) -> tuple:
    """dtstartの値をJSTのaware datetimeに揃える。戻り値: (datetime, all_day)"""
    if isinstance(dt, datetime.datetime):  # datetimeはdateのサブクラスなので先に判定
        if dt.tzinfo is None:
            # タイムゾーンなし（floating）はローカル時刻として扱う
            return dt.replace(tzinfo=JST), False
        return dt.astimezone(JST), False
    # 日付のみ（終日予定）。00:00 JST として保持する
    return datetime.datetime.combine(dt, datetime.time.min, tzinfo=JST), True
 
 
def _is_upcoming(start: datetime.datetime, all_day: bool, now: datetime.datetime) -> bool:
    if all_day:
        # 終日予定は当日いっぱい「これから」の予定として扱う
        return start.date() >= now.date()
    return start >= now
 
 
def fetch_events(ics_url: str, filter_words: list, now=None) -> list:
    """Moodleから予定を取得し、フィルターに一致する「これから」の予定を日時順で返す。
 
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
 
    events = []
    for component in cal.walk("VEVENT"):
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
 
        start, all_day = _to_jst(dtstart.dt)
        if not _is_upcoming(start, all_day, now):
            continue
 
        summary = str(component.get("summary", ""))
        if not any(word in summary for word in filter_words):
            continue
 
        uid = str(component.get("uid", "")) or summary
        events.append(MoodleEvent(uid=uid, summary=summary, start=start, all_day=all_day))
 
    events.sort(key=lambda e: e.start)
    return events