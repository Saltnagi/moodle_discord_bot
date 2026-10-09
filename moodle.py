# Moodleサーバとの通信、.icsファイルの解析
import datetime
import logging
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from config import (
    MAX_REDIRECTS,
    MAX_URL_LENGTH,
    MOODLE_ALLOWED_HOSTS,
    REQUEST_TIMEOUT,
)

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

    events: list  # 通知対象の予定（日時順）
    total: int  # カレンダー内の予定の総数
    upcoming: int  # そのうち、これから（または継続中）の予定の数
    excluded: int = 0  # フィルターには一致したが、除外ワードで外れた件数
    beyond_period: int = 0  # 「期間」設定の範囲外で外れた件数


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


def check_url(url) -> Optional[str]:
    """登録・取得してよいURLか検査する。許可なら None、不許可ならユーザーに見せてよい理由を返す。

    SSRF（BotをだましてBotのいるネットワーク内部へアクセスさせる攻撃）の対策として、
    https かつ、MOODLE_ALLOWED_HOSTS に載っているドメインだけを許可する。
    """
    if not isinstance(url, str) or not url:
        return "URLを入力してください。"
    if len(url) > MAX_URL_LENGTH:
        return "URLが長すぎます。"
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return "URLに使用できない文字が含まれています。"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "URLの形式が正しくありません。"
    if parts.scheme != "https":
        return "https:// で始まるURLのみ登録できます。"
    if parts.username is not None or parts.password is not None:
        return "URLにユーザー名やパスワードを含めることはできません。"
    host = (parts.hostname or "").lower()
    if host not in MOODLE_ALLOWED_HOSTS or port not in (None, 443):
        return "登録できるのは、大学のMoodle（許可されたサイト）のカレンダーURLのみです。"
    return None


_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _http_get(ics_url: str):
    """URLを取得する。リダイレクトは自分で追い、移動先も毎回 check_url で検査する。"""
    url = ics_url
    for _ in range(MAX_REDIRECTS + 1):
        if check_url(url) is not None:
            # URL自体（認証トークン入り）はログにも出さない
            log.warning("許可されていないURLへのアクセスをブロックしました")
            raise MoodleError(
                "登録されているURLは許可されていません。"
                "`!url 登録` で、大学のMoodleのカレンダーURLを登録し直してください。"
            )
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except requests.RequestException as e:
            # 例外メッセージにURLが入るため、型名だけをログに残す
            log.warning("Moodle通信エラー: %s", type(e).__name__)
            raise MoodleError(
                "Moodleに接続できませんでした。時間をおいて再度お試しください。"
            ) from None
        if response.status_code in _REDIRECT_CODES and response.headers.get("Location"):
            url = urljoin(url, response.headers["Location"])
            continue
        return response
    log.warning("リダイレクトが多すぎるため中断しました")
    raise MoodleError("Moodleとの通信でリダイレクトが繰り返されました。URLを確認してください。")


def fetch_events(
    ics_url: str,
    filter_words: list,
    now=None,
    exclude_words=None,
    max_days: Optional[int] = None,
) -> FetchResult:
    """Moodleから予定を取得し、条件に合う「これから」の予定を返す。

    - filter_words : どれかを含む予定だけを対象にする（空なら全予定が対象）
    - exclude_words: どれかを含む予定は対象から外す（フィルターより優先）
    - max_days     : 今後この日数以内に始まる予定だけを対象にする（Noneなら制限なし）
    大文字小文字・全角半角は区別しない。

    失敗時は MoodleError を投げる（エラー文を予定として返さない）。
    ネットワーク通信を行う同期関数なので、botからは asyncio.to_thread で呼ぶこと。
    """
    now = now or datetime.datetime.now(JST)

    response = _http_get(ics_url)

    if response.status_code != 200:
        log.warning("Moodleが HTTP %s を返しました", response.status_code)
        raise MoodleError(
            f"Moodleからのデータ取得に失敗しました (HTTP {response.status_code})。"
            "カレンダーURLが正しいか確認してください。"
        )

    # メンテナンス画面やログイン画面が HTTP 200 のHTMLで返ってくることがある。
    # それを「予定0件」と誤認しないよう、ICSかどうかを先に確認する。
    if b"BEGIN:VCALENDAR" not in response.content[:4096].upper():
        log.warning("Moodleの応答がICS形式ではありません（メンテナンス中の可能性）")
        raise MoodleError(
            "Moodleから予定データを受け取れませんでした（メンテナンス中の可能性があります）。"
        )

    try:
        cal = Calendar.from_ical(response.content)
    except Exception as e:
        log.warning("ICSの解析に失敗: %s", type(e).__name__)
        raise MoodleError("カレンダーデータの解析に失敗しました。") from None

    norm_words = [w for w in (normalize_text(w) for w in filter_words) if w]
    norm_excludes = [w for w in (normalize_text(w) for w in (exclude_words or [])) if w]
    horizon = now + datetime.timedelta(days=max_days) if max_days else None

    events = []
    total = 0
    upcoming = 0
    excluded = 0
    beyond_period = 0
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

        # 期間設定の範囲外（開始がhorizonより先）の予定は対象外
        if horizon is not None and start > horizon:
            beyond_period += 1
            continue

        summary = str(component.get("summary", ""))
        norm_summary = normalize_text(summary)

        # フィルターが空なら全予定を対象にする
        if norm_words and not any(w in norm_summary for w in norm_words):
            continue
        # 除外ワードを含む予定は、フィルターに一致していても対象外（除外が優先）
        if any(w in norm_summary for w in norm_excludes):
            excluded += 1
            continue

        uid = str(component.get("uid", "")) or summary
        events.append(
            MoodleEvent(uid=uid, summary=summary, start=start, end=end, all_day=all_day)
        )

    events.sort(key=lambda e: e.start)
    log.info(
        "ICS解析: 全%d件 / これから%d件 / 期間外%d件 / 除外%d件 / 対象%d件",
        total, upcoming, beyond_period, excluded, len(events),
    )
    return FetchResult(
        events=events,
        total=total,
        upcoming=upcoming,
        excluded=excluded,
        beyond_period=beyond_period,
    )