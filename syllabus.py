import html
import json
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

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


class SyllabusNetworkError(SyllabusError):
    """通信失敗（タイムアウト・HTTPエラー等。再試行対象）"""


class SyllabusClient(ABC):
    @abstractmethod
    def fetch_course_name(self, key: CourseKey) -> Optional[str]:
        """日本語授業名を返す。存在しなければNone。通信失敗は例外。"""


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
        self._ssl_ctx = ssl._create_unverified_context()

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
            # 404などシラバス年度自体が存在しないケースも通信エラーとして扱う
            raise SyllabusNetworkError(f"HTTPエラー {e.code}") from None
        except Exception as e:
            # URL等の機密情報漏洩を防ぎ、型名のみを記録
            raise SyllabusNetworkError(f"通信エラー ({type(e).__name__})") from None

        # <div id="app" data-page="..."> からJSONを抽出
        match = re.search(r'data-page="([^"]+)"', html_text)
        if not match:
            # メンテナンス中等でdata-pageがない場合
            raise SyllabusNetworkError("シラバスのデータ属性が見つかりません")

        try:
            raw_json = html.unescape(match.group(1))
            data = json.loads(raw_json)
            jugyo_list = data.get("props", {}).get("data", {}).get("jugyo_list", [])
        except Exception as e:
            raise SyllabusNetworkError(f"JSON解析エラー ({type(e).__name__})") from None

        if not jugyo_list:
            return None

        # 先頭の授業の日本語名を取得（クラス分けなどで複数あっても授業名は共通）
        first_course = jugyo_list[0]
        course_name = first_course.get("jugyo_nm")
        if course_name:
            # 前後の空白を除去
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
        5. 通信例外時は保存せずNoneを返す
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
        except SyllabusNetworkError as e:
            # 5. 通信例外時はキャッシュせずNoneを返す（次回再試行）
            log.warning("シラバス取得失敗 (%s/%s): %s", key.year, key.code, e)
            return None
        except Exception as e:
            log.warning("シラバス取得中の想定外エラー (%s/%s): %s", key.year, key.code, type(e).__name__)
            return None

        # 4. 結果をキャッシュに保存（成功・未発見Noneとも保存）
        try:
            database.save_course_name(key.year, key.code, fetched_name)
        except Exception as e:
            log.warning("授業名キャッシュ保存失敗: %s", type(e).__name__)

        return fetched_name
