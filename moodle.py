import datetime
import logging
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from config import (
    FETCH_TOTAL_TIMEOUT_SEC,
    MAX_ICS_BYTES,
    MAX_REDIRECTS,
    MAX_URL_LENGTH,
    MOODLE_ALLOWED_HOSTS,
    REQUEST_TIMEOUT,
)
from errors import FailureKind, MoodleError
from syllabus import CourseNameResolver, parse_course_key

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")


def format_event_line(
    start_str: str,
    summary: str,
    course_raw: str,
    resolver: Optional[CourseNameResolver] = None,
) -> str:
    """解決できた場合: ・[日時] 【情報工学基礎】課題名
    解決できない場合: ・[日時] 課題名 （コードは載せない）"""
    resolved = resolver.resolve(course_raw) if (resolver and course_raw) else None
    if not resolved and course_raw:
        resolved = display_course(course_raw)
    prefix = f"【{resolved}】" if resolved else ""
    return f"・[{start_str}] {prefix}{summary}"


WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def underline(text: str) -> str:
    """数値をDiscordの下線装飾で囲む"""
    return f"__{text}__"


def urgency_mark(remaining: datetime.timedelta) -> str:
    """緊急度マーク（🔴 24時間以内 / 🟡 72時間以内 / ⚪ それ以降）"""
    secs = remaining.total_seconds()
    if secs <= 24 * 3600:
        return "🔴"
    elif secs <= 72 * 3600:
        return "🟡"
    return "⚪"


def format_remaining(remaining: datetime.timedelta) -> str:
    """残り時間を 'あと__5__時間' の形式で返す。
    1時間未満=分 / 24時間未満=時間 / それ以上=日"""
    secs = max(0, int(remaining.total_seconds()))
    if secs < 3600:
        minutes = max(1, secs // 60)
        return f"あと{underline(str(minutes))}分"
    elif secs < 86400:
        hours = max(1, secs // 3600)
        return f"あと{underline(str(hours))}時間"
    else:
        days = max(1, secs // 86400)
        return f"あと{underline(str(days))}日"


def format_date_heading(dt: datetime.datetime, now: datetime.datetime) -> str:
    """日付見出し行（例: '__10/20__(火)'、年が異なる場合は '__2027/1/5__(火)'）"""
    wd = WEEKDAYS[dt.weekday()]
    if dt.year == now.year:
        date_str = f"{dt.month}/{dt.day}"
    else:
        date_str = f"{dt.year}/{dt.month}/{dt.day}"
    return f"{underline(date_str)}({wd})"


def format_single_event(ev: "MoodleEvent", now: datetime.datetime) -> str:
    """1件の予定の表示文字列を作る。
    例:
    🔴 __23:59__（あと__5__時間）
    【情報工学基礎】
    　課題1 提出
    """
    due = ev.due_at(now)
    lines = []

    if ev.all_day:
        # 終日予定: 日付差から緊急度判定。時刻と「あと◯◯」は省略
        days_diff = (ev.start.date() - now.date()).days
        mark = urgency_mark(datetime.timedelta(days=days_diff))
        lines.append(mark)
    else:
        # 通常予定: 期限日時から緊急度・残り時間を判定
        target_dt = due if due is not None else ev.start
        rem = target_dt - now
        mark = urgency_mark(rem)
        hhmm = target_dt.strftime("%H:%M")
        rem_str = format_remaining(rem)
        lines.append(f"{mark} {underline(hhmm)}（{rem_str}）")

    # 授業名（取得できている場合のみ表示、コードは載せない）
    if ev.course:
        lines.append(f"【{ev.course}】")

    # 課題名（全角スペース1つでインデント）
    lines.append(f"　{ev.summary}")

    return "\n".join(lines)


def format_events(
    events: list["MoodleEvent"],
    now: datetime.datetime,
    header: Optional[str] = None,
    limit: int = 1950,
) -> list[str]:
    """予定一覧を新フォーマットで整形し、2000文字制限に収まるよう分割したリストを返す。"""
    if not events:
        return []

    from collections import defaultdict

    # 期限日時（なければ開始日時）でソート
    sorted_events = sorted(events, key=lambda e: e.due_at(now) or e.start)

    # 日付ごとにグループ化
    groups = defaultdict(list)
    for ev in sorted_events:
        target_dt = ev.due_at(now) or ev.start
        groups[target_dt.date()].append(ev)

    chunks: list[str] = []
    current_lines: list[str] = []

    if header:
        current_lines.append(header)

    for date_key, ev_list in groups.items():
        rep_dt = ev_list[0].due_at(now) or ev_list[0].start
        date_heading = format_date_heading(rep_dt, now)

        event_blocks = [format_single_event(ev, now) for ev in ev_list]
        group_body = "\n\n".join(event_blocks)
        full_group_str = f"{date_heading}\n{group_body}"

        # 現在のチャンクにグループ全体が追加できるか試す
        prefix = "\n\n" if current_lines else ""
        candidate = "\n".join(current_lines) + prefix + full_group_str
        if len(candidate) <= limit:
            if current_lines:
                current_lines.append("")
            current_lines.append(full_group_str)
        else:
            # 入らない場合: 現在のチャンクを確定
            if current_lines:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []

            # 新規チャンクでグループ全体が入るか？
            if len(full_group_str) <= limit:
                current_lines.append(full_group_str)
            else:
                # 1つの日付グループだけでlimitを超える場合: 予定単位で分割
                current_lines.append(date_heading)
                for ev_block in event_blocks:
                    candidate_single = "\n".join(current_lines) + "\n\n" + ev_block
                    if len(candidate_single) <= limit:
                        current_lines.append("")
                        current_lines.append(ev_block)
                    else:
                        chunks.append("\n".join(current_lines).strip())
                        # 次のチャンクにも同じ日付見出しを再掲して継続
                        current_lines = [date_heading, "", ev_block]

    if current_lines:
        final_text = "\n".join(current_lines).strip()
        if final_text:
            chunks.append(final_text)

    return chunks


def normalize_text(text: str) -> str:
    """フィルター判定用の正規化。大文字小文字と、全角/半角の違いを無視できるようにする。
    （例: "Report" / "report" / "ＲＥＰＯＲＴ" はすべて同じ扱い）"""
    return unicodedata.normalize("NFKC", text).casefold()



@dataclass(frozen=True)
class MoodleEvent:
    uid: str
    summary: str
    start: datetime.datetime  # JST (aware)
    end: Optional[datetime.datetime]  # JST (aware)。終了時刻がなければNone
    all_day: bool
    course: str = ""  # 通知に表示する授業名（日本語のもののみ）。取得できなければ空
    course_code: str = ""  # Moodleのコース略称（例: 2026UCS258）。表示せず、絞り込みにだけ使う

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
        prefix = f"【{self.course}】" if self.course else ""
        return f"・[{text}] {prefix}{self.summary}"


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


_MAX_COURSE_LENGTH = 40


def _get_course(component) -> str:
    """予定の「授業名」を取り出す。

    MoodleのカレンダーはコースのイベントにCATEGORIES（コース略称）を付けて出力する。
    CATEGORIESが無い予定（個人の予定など）は空文字を返す。
    ライブラリのバージョンにより値の型が違うため、いくつかの形に対応している。
    """
    cats = component.get("categories")
    if cats is None:
        return ""
    names = []
    for item in cats if isinstance(cats, list) else [cats]:
        values = getattr(item, "cats", None)  # icalendar の vCategory
        for v in values if values is not None else [item]:
            text = " ".join(str(v).split())  # 改行などを空白1つにまとめる
            text = "".join(ch for ch in text if ch.isprintable())
            if text and text not in names:
                names.append(text)
    course = " / ".join(names)
    if len(course) > _MAX_COURSE_LENGTH:
        course = course[:_MAX_COURSE_LENGTH] + "…"
    return course


def _has_japanese(text: str) -> bool:
    """ひらがな・カタカナ・漢字（半角カタカナを含む）が1文字でもあればTrue"""
    return any(
        "\u3040" <= ch <= "\u30ff"  # ひらがな・カタカナ
        or "\u3400" <= ch <= "\u4dbf"  # 漢字（拡張A）
        or "\u4e00" <= ch <= "\u9fff"  # 漢字
        or "\uff66" <= ch <= "\uff9f"  # 半角カタカナ
        for ch in text
    )


def display_course(course_raw: str) -> str:
    """通知に表示する授業名を選ぶ。

    Moodleのカレンダーに入っているのは「2026UCS258」のような科目コードのことが多く、
    それを表示しても何の授業か分からない。そのため、日本語を含むものだけを表示し、
    英数字だけのコードは表示しない（取得できなければ空文字）。
    """
    names = [n for n in course_raw.split(" / ") if _has_japanese(n)]
    return " / ".join(names)


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


def fetch_ics_limited(
    url: str,
    *,
    max_bytes: int = MAX_ICS_BYTES,
    total_timeout: float = FETCH_TOTAL_TIMEOUT_SEC,
) -> bytes:
    """
    - 既存の check_url / リダイレクト再検証（最大3回）を維持
    - requests.get(..., stream=True)
    - Content-Length が max_bytes 超なら読まずに中断
    - iter_content(8192) で累積サイズを監視し、超過で中断
    - 開始からの経過が total_timeout 超なら中断（遅延送信対策。read timeoutとは別に全体で見る）
    - HTTP 401/403/404/410 → MoodleError(kind=INVALID)
    - 5xx/429/timeout/接続失敗/サイズ超過 → MoodleError(kind=TRANSIENT)
    - 本文の先頭が b"BEGIN:VCALENDAR" でない → MoodleError(kind=INVALID)
    - 例外メッセージ・ログにURL（トークン）を含めない（既存のマスキング方針を踏襲）
    """
    start_time = time.monotonic()
    current_url = url

    for _ in range(MAX_REDIRECTS + 1):
        if check_url(current_url) is not None:
            log.warning("許可されていないURLへのアクセスをブロックしました")
            raise MoodleError(
                "登録されているURLは許可されていません。"
                "`/url 登録` で、大学のMoodleのカレンダーURLを登録し直してください。",
                FailureKind.INVALID,
            )

        elapsed = time.monotonic() - start_time
        remaining_time = total_timeout - elapsed
        if remaining_time <= 0:
            log.warning("Moodle通信全体タイムアウト")
            raise MoodleError("Moodleからのデータ取得がタイムアウトしました。", FailureKind.TRANSIENT)

        request_timeout = min(float(REQUEST_TIMEOUT), remaining_time)

        try:
            response = requests.get(
                current_url,
                timeout=request_timeout,
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            log.warning("Moodle通信タイムアウト")
            raise MoodleError(
                "Moodleへの接続がタイムアウトしました。時間をおいて再度お試しください。",
                FailureKind.TRANSIENT,
            ) from None
        except requests.RequestException as e:
            log.warning("Moodle通信エラー: %s", type(e).__name__)
            raise MoodleError(
                "Moodleに接続できませんでした。時間をおいて再度お試しください。",
                FailureKind.TRANSIENT,
            ) from None

        if response.status_code in _REDIRECT_CODES and response.headers.get("Location"):
            response.close()
            current_url = urljoin(current_url, response.headers["Location"])
            continue

        status = response.status_code
        if status in (401, 403, 404, 410):
            response.close()
            log.warning("Moodleが HTTP %s を返しました (無効なURL/トークン)", status)
            raise MoodleError(
                f"カレンダーURLが無効か、アクセス権がありません (HTTP {status})。"
                "`/url 登録` で再登録してください。",
                FailureKind.INVALID,
            )
        elif status == 429 or 500 <= status <= 599:
            response.close()
            log.warning("Moodleが HTTP %s を返しました (一時的エラー)", status)
            raise MoodleError(
                f"Moodleサーバーで一時的なエラーが発生しました (HTTP {status})。",
                FailureKind.TRANSIENT,
            )
        elif status != 200:
            response.close()
            log.warning("Moodleが HTTP %s を返しました", status)
            raise MoodleError(
                f"Moodleからのデータ取得に失敗しました (HTTP {status})。",
                FailureKind.TRANSIENT,
            )

        cl_header = response.headers.get("Content-Length")
        if cl_header:
            try:
                cl_val = int(cl_header)
                if cl_val > max_bytes:
                    response.close()
                    log.warning("Content-Length が上限を超過: %d > %d", cl_val, max_bytes)
                    raise MoodleError(
                        "カレンダーデータのサイズが上限（5MB）を超えています。",
                        FailureKind.TRANSIENT,
                    )
            except ValueError:
                pass

        chunks = []
        total_downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic() - start_time > total_timeout:
                    response.close()
                    log.warning("Moodle通信全体タイムアウト（受信中）")
                    raise MoodleError("Moodleからのデータ取得がタイムアウトしました。", FailureKind.TRANSIENT)

                if chunk:
                    total_downloaded += len(chunk)
                    if total_downloaded > max_bytes:
                        response.close()
                        log.warning("受信サイズが上限を超過: %d > %d", total_downloaded, max_bytes)
                        raise MoodleError(
                            "カレンダーデータのサイズが上限（5MB）を超えています。",
                            FailureKind.TRANSIENT,
                        )
                    chunks.append(chunk)
        except requests.RequestException as e:
            response.close()
            log.warning("Moodleデータ受信中エラー: %s", type(e).__name__)
            raise MoodleError(
                "Moodleからのデータ受信中に通信エラーが発生しました。",
                FailureKind.TRANSIENT,
            ) from None
        finally:
            response.close()

        content = b"".join(chunks)

        if b"BEGIN:VCALENDAR" not in content[:4096].upper():
            log.warning("Moodleの応答がICS形式ではありません（メンテナンス中またはログイン画面の可能性）")
            raise MoodleError(
                "Moodleからカレンダーデータを受け取れませんでした（メンテナンス中等の可能性があります）。",
                FailureKind.INVALID,
            )

        return content

    log.warning("リダイレクトが多すぎるため中断しました")
    raise MoodleError(
        "Moodleとの通信でリダイレクトが繰り返されました。URLを確認してください。",
        FailureKind.INVALID,
    )


def fetch_events(
    ics_url: str,
    filter_words: list,
    now=None,
    exclude_words=None,
    max_days: Optional[int] = None,
    resolver: Optional[CourseNameResolver] = None,
) -> FetchResult:
    """Moodleから予定を取得し、条件に合う「これから」の予定を返す。

    - filter_words : どれかを含む予定だけを対象にする（空なら全予定が対象）
    - exclude_words: どれかを含む予定は対象から外す（フィルターより優先）
    - max_days     : 今後この日数以内に始まる予定だけを対象にする（Noneなら制限なし）
    - resolver     : シラバスから日本語授業名を引くリゾルバ
    大文字小文字・全角半角は区別しない。

    失敗時は MoodleError を投げる（エラー文を予定として返さない）。
    ネットワーク通信を行う同期関数なので、botからは asyncio.to_thread で呼ぶこと。
    """
    now = now or datetime.datetime.now(JST)

    content = fetch_ics_limited(ics_url)

    try:
        cal = Calendar.from_ical(content)
    except Exception as e:
        log.warning("ICSの解析に失敗: %s", type(e).__name__)
        raise MoodleError("カレンダーデータの解析に失敗しました。", FailureKind.INVALID) from None

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
        course_raw = _get_course(component)  # コース略称（例: 2026UCS258）
        if not course_raw:
            # categoriesに無い場合、descriptionやsummaryから探す
            desc = str(component.get("description", ""))
            m = parse_course_key(desc) or parse_course_key(summary)
            if m:
                course_raw = f"{m.year}{m.code}"

        # シラバスから日本語授業名を解決。解決できなければICS内の日本語判定を試みる
        resolved_name = resolver.resolve(course_raw) if (resolver and course_raw) else None
        if not resolved_name and course_raw:
            resolved_name = display_course(course_raw)

        # 解決できなければ空文字（授業コードは絶対に表示しない）
        course = resolved_name or ""

        # フィルター・除外は「予定名」「授業名」「コース略称（コード）」が対象
        norm_summary = normalize_text(f"{summary} {course_raw} {course}")

        # フィルターが空なら全予定を対象にする
        if norm_words and not any(w in norm_summary for w in norm_words):
            continue
        # 除外ワードを含む予定は、フィルターに一致していても対象外（除外が優先）
        if any(w in norm_summary for w in norm_excludes):
            excluded += 1
            continue

        uid = str(component.get("uid", "")) or summary
        events.append(
            MoodleEvent(
                uid=uid,
                summary=summary,
                start=start,
                end=end,
                all_day=all_day,
                course=course,
                course_code=course_raw,
            )
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