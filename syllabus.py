from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
import html
import json
import logging
import re
import ssl
import threading
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

from config import (
    ADMIN_ALERT_COOLDOWN_HOURS,
    SYLLABUS_CA_BUNDLE,
    SYLLABUS_PARSE_FAIL_THRESHOLD,
)
import database

log = logging.getLogger(__name__)

# Moodleのコース略称（例: 2026UCS258 や 2026UPC101）から (年度, 科目コード) を抽出
_COURSE_PATTERN = re.compile(r"(?P<year>20\d{2})(?P<code>[A-Za-z0-9]+)")


@dataclass(frozen=True)
class CourseKey:
    year: int
    code: str


def parse_course_key(raw: str) -> Optional[CourseKey]:
    """ICS内の授業表記から (年度, コード) を抽出。解釈不能ならNone。"""
    if not raw:
        return None
    m = _COURSE_PATTERN.search(raw)
    if not m:
        return None
    return CourseKey(int(m["year"]), m["code"].upper())


class SyllabusError(Exception):
    """シラバス通信・解析のエラー基底クラス"""


class SyllabusParseError(SyllabusError):
    """data-page 属性が無い・JSONが壊れている等、構造が想定と異なる場合"""


class SyllabusNetworkError(SyllabusError):
    """通信失敗（タイムアウト・SSLエラー・HTTPエラー）"""


try:
    import certifi

    _DEFAULT_CA = certifi.where()
except ImportError:
    _DEFAULT_CA = None


def _build_ssl_context() -> ssl.SSLContext:
    """ssl.create_default_context(cafile=SYLLABUS_CA_BUNDLE or _DEFAULT_CA)。検証は常に有効"""
    cafile = SYLLABUS_CA_BUNDLE or _DEFAULT_CA
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


class SyllabusHealth:
    """シラバス障害・仕様変更の検知。スレッドセーフ（threading.Lock）。

    - ParseError が連続 SYLLABUS_PARSE_FAIL_THRESHOLD 件に達したら、logging.error を出し、
      alert を1件キューする。成功（授業名取得 or 正常な該当なし）で連続カウントをリセット
    - 管理者通知は ADMIN_ALERT_COOLDOWN_HOURS に1回まで
    """

    def __init__(
        self,
        threshold: int = SYLLABUS_PARSE_FAIL_THRESHOLD,
        cooldown_hours: int = ADMIN_ALERT_COOLDOWN_HOURS,
    ):
        self._lock = threading.Lock()
        self._threshold = threshold
        self._cooldown = timedelta(hours=cooldown_hours)
        self._consecutive_parse_failures = 0
        self._pending_alert: Optional[str] = None
        self._last_alert_at: Optional[datetime] = None

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_parse_failures = 0

    def record_parse_failure(self) -> None:
        with self._lock:
            self._consecutive_parse_failures += 1
            if self._consecutive_parse_failures >= self._threshold:
                log.error(
                    "シラバスの解析失敗が連続 %d 件に達しました（仕様変更の可能性があります）",
                    self._consecutive_parse_failures,
                )
                if not self._pending_alert:
                    self._pending_alert = (
                        f"⚠️ **【管理者アラート】シラバス解析エラー**\n"
                        f"シラバスの解析失敗が連続 {self._consecutive_parse_failures} 件に達しました。\n"
                        f"シラバスサイトのHTML構造や仕様が変更された可能性があります。ログを確認してください。"
                    )

    def record_network_failure(self) -> None:
        # ログのみ。連続カウント対象外
        pass

    def consume_alert(self, now: datetime) -> Optional[str]:
        """未送信のアラート文面を1回だけ返す。クールダウン中・なければ None"""
        with self._lock:
            if not self._pending_alert:
                return None
            if self._last_alert_at is not None and (now - self._last_alert_at) < self._cooldown:
                return None
            alert = self._pending_alert
            self._pending_alert = None
            self._last_alert_at = now
            return alert


syllabus_health = SyllabusHealth()


class SyllabusClient(ABC):
    @abstractmethod
    def fetch_course_name(self, key: CourseKey) -> Optional[str]:
        """日本語授業名を返す。存在しなければNone。通信・解析失敗は例外。"""


class YamanashiSyllabusClient(SyllabusClient):
    """山梨大学電子シラバスから授業名を取得するクライアント。

    【仕様・制約】
    ・エンドポイント: https://syllabus.yamanashi.ac.jp/{year}/list?kamokuno={code} (GET)
    ・認証: 不要（公開API/Webページ）
    ・レスポンス: Inertia.js形式のHTML。data-page属性にJSONが含まれる
    ・アクセス頻度制限: サーバー負荷軽減のため、リクエスト間に最低1秒の待機を設ける
    """

    BASE_URL = "https://syllabus.yamanashi.ac.jp"
    REQUEST_TIMEOUT = 10
    MIN_REQUEST_INTERVAL = 1.0  # リクエスト間の最小待機秒数

    def __init__(self):
        self._last_request_time = 0.0
        self._ssl_ctx = _build_ssl_context()

    def _wait_for_rate_limit(self):
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def fetch_course_name(self, key: CourseKey) -> Optional[str]:
        self._wait_for_rate_limit()

        encoded_code = urllib.parse.quote(key.code)
        url = f"{self.BASE_URL}/{key.year}/list?kamokuno={encoded_code}"

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT, context=self._ssl_ctx) as res:
                if res.getcode() != 200:
                    raise SyllabusNetworkError(f"HTTPステータス {res.getcode()}")
                html_text = res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise SyllabusNetworkError(f"HTTPエラー {e.code}") from None
        except ssl.SSLError as e:
            raise SyllabusNetworkError(f"SSL検証エラー ({type(e).__name__})") from None
        except Exception as e:
            raise SyllabusNetworkError(f"通信エラー ({type(e).__name__})") from None

        # <div id="app" data-page="..."> からJSONを抽出
        match = re.search(r'data-page="([^"]+)"', html_text)
        if not match:
            # 構造変更またはメンテナンス等でdata-pageがない場合
            raise SyllabusParseError("シラバスのデータ属性（data-page）が見つかりません")

        try:
            raw_json = html.unescape(match.group(1))
            data = json.loads(raw_json)
            jugyo_list = data.get("props", {}).get("data", {}).get("jugyo_list", [])
        except Exception as e:
            raise SyllabusParseError(f"JSON解析エラー ({type(e).__name__})") from None

        if not jugyo_list:
            return None

        first_course = jugyo_list[0]
        course_name = first_course.get("jugyo_nm")
        if course_name:
            course_name = str(course_name).strip()
            return course_name or None

        return None


class CourseNameResolver:
    HIT_TTL_DAYS = database.COURSE_HIT_TTL_DAYS  # 180日
    MISS_TTL_DAYS = database.COURSE_MISS_TTL_DAYS  # 7日

    def __init__(self, client: SyllabusClient):
        self.client = client

    def resolve(self, raw_course: str) -> Optional[str]:
        """
        1. parse_course_key で抽出（失敗→None）
        2. database.get_course_name でキャッシュ参照
           （TTL内ならネガティブキャッシュ中でもNoneを即返す）
        3. 期限切れ/未登録なら client.fetch_course_name
        4. 結果（成功・未発見とも）を database.save_course_name で保存
        5. 通信・解析例外時は保存せずNoneを返す
        """
        key = parse_course_key(raw_course)
        if key is None:
            return None

        # 2. キャッシュ参照
        hit, cached_name = database.get_course_name(key.year, key.code)
        if hit:
            return cached_name

        # 3. 期限切れ/未登録ならシラバス取得
        try:
            fetched_name = self.client.fetch_course_name(key)
        except SyllabusParseError as e:
            log.warning("シラバス解析エラー (%s/%s): %s", key.year, key.code, e)
            syllabus_health.record_parse_failure()
            return None
        except SyllabusNetworkError as e:
            log.warning("シラバス取得失敗 (%s/%s): %s", key.year, key.code, e)
            syllabus_health.record_network_failure()
            return None
        except Exception as e:
            log.warning("シラバス取得中の想定外エラー (%s/%s): %s", key.year, key.code, type(e).__name__)
            return None

        # 成功または正常な該当なし
        syllabus_health.record_success()

        # 4. 結果をキャッシュに保存（成功・未発見Noneとも保存）
        try:
            database.save_course_name(key.year, key.code, fetched_name)
        except Exception as e:
            log.warning("授業名キャッシュ保存失敗: %s", type(e).__name__)

        return fetched_name
