import asyncio
import datetime
import logging
import re
import sys
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import tasks

import database
import moodle
import syllabus
from config import (
    CHECK_INTERVAL_MINUTES,
    COMMANDS_VERSION,
    DIGEST_GRACE_MINUTES,
    DIGEST_MAX_DAYS,
    DIGEST_RETRY_MINUTES,
    DM_RETRY_HOURS,
    MAX_NOTIFY_TIMES,
    MAX_URL_LENGTH,
    MAX_WORD_LENGTH,
    MAX_WORDS_PER_LIST,
    REMINDER_BEFORE_MINUTES,
    STATUS_MESSAGE,
    SYNC_COMMANDS,
    require_allowed_hosts,
    require_token,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# 設定が足りなければ、ここで原因を表示して終了する（DBを触る前に確認）
TOKEN = require_token()
require_allowed_hosts()

# DBから登録データをロード {user_id: ics_url}
user_urls = database.init_db()

# 許可ドメイン外のURLが登録されたままのユーザーを、ターミナルに知らせる（通知は届かなくなる）
for _uid, _url in user_urls.items():
    if moodle.check_url(_url) is not None:
        log.warning(
            "ユーザー %s の登録URLは許可されていない（https以外、または許可ドメイン外）ため、"
            "通知されません。本人に `/url 登録` のやり直しを案内してください。",
            _uid,
        )

# 定刻通知の取得・送信に失敗したユーザーの、次に再試行してよい時刻（メモリ上のみ）
_digest_retry_at: dict = {}


# --------------------------------------------------
# 説明文（/使い方 と、初めてのDMで使う）
# --------------------------------------------------
# URL未登録のユーザーには「URLの登録」だけを案内し、登録できたら他のコマンドを案内する
URL_GUIDE_TEXT = (
    "**【Moodle通知Botの使い方】**\n"
    "まずは、MoodleのカレンダーURLを登録しましょう。\n"
    "\n"
    "**① カレンダーURLを取得する**\n"
    "1. CNSのいずれかの授業から「e-ラーニング」を選び、Moodleへ移動します。\n"
    "2. 右上のアイコンから「カレンダー」を選択します。\n"
    "3. ページ下部にある「カレンダーをインポートまたはエクスポートする」を選択します。\n"
    "4. そのページの「カレンダーをエクスポートする」を選択します。\n"
    "5. 「エクスポートするイベント」は「すべてのイベント」、「期間」は「最近及び次の60日間」を選択します。\n"
    "6. その状態で「カレンダーURLを取得する」を選択し、表示されたURLをコピーします。\n"
    "\n"
    "**② Botに登録する**\n"
    "`/url 登録` を実行し、開いた入力欄にURLを貼り付けて送信します。\n"
    "\n"
    "⚠️ このURLは**あなた専用の鍵**です。知られると、あなたの予定を他の人に見られてしまいます。"
    "他の人には見せないでください。\n"
    "※登録できるのは、大学のMoodleのURL（https://〜）のみです。\n"
    "※登録が終わると、使えるコマンドの説明が表示されます。"
)

# URL登録済みのユーザーには、URL登録以外のコマンドを案内する
COMMANDS_GUIDE_TEXT = (
    "**【Moodle通知Botの使い方】**\n"
    "登録できました！以後は、新しい予定などを自動でお知らせします。\n"
    "手動で確認したいときは `/moodle` を実行してください。\n"
    "\n"
    "**コマンド一覧**\n"
    "・`/moodle` … 今後の予定を今すぐ確認します\n"
    "・`/フィルター 追加 <単語>` … 通知してほしい授業名や「課題」「小テスト」などの単語を登録します"
    "（`/フィルター 一覧` で確認、`/フィルター 削除` で削除）\n"
    "・`/除外 追加 <単語>` … 通知したくない単語を登録します（フィルターより優先されます）\n"
    "・`/期間 設定 <日数>` … 通知する予定を、今後◯日以内に絞ります（`/期間 解除` で解除）\n"
    "・`/通知時間 設定 <時刻>` … 毎日の定刻通知の時刻を変更します"
    "（初期設定は 8:30 と 18:00。`/通知時間 オフ` で停止）\n"
    "\n"
    "**届く通知**\n"
    "・新しい予定が見つかったとき\n"
    "・提出期限の1時間前\n"
    "・毎日の定刻通知（今後1週間の予定のまとめ）\n"
    "※時刻はすべて日本時間です。"
)

NEED_REGISTER_TEXT = (
    "先に `/url 登録` で、MoodleのカレンダーURLを登録してください。\n"
    "取得方法は `/使い方` で確認できます。"
)

LEGACY_TEXT = (
    "コマンドの入力方法が変わりました。「!」ではなく、「/」を入力すると、"
    "使えるコマンドの候補が表示されます。\n例: `/使い方`　`/moodle`"
)


def help_texts(user_id: int) -> list:
    """URL未登録ならURL登録の説明のみ、登録済みならそれ以外のコマンドの説明のみ"""
    return [COMMANDS_GUIDE_TEXT] if user_id in user_urls else [URL_GUIDE_TEXT]


# --------------------------------------------------
# 共通の小道具
# --------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s'\")>]+")
_REQ_URL_RE = re.compile(r"(url: )\S+")  # urllib3の「Max retries exceeded with url: /path?...」
_TOKEN_RE = re.compile(r"(authtoken=)[^&\s'\")]+", re.IGNORECASE)


def redact(text: str) -> str:
    """ログに出す文章から、URLや認証トークンを伏せる"""
    text = _URL_RE.sub("[URL]", text)
    text = _REQ_URL_RE.sub(r"\1[URL]", text)
    return _TOKEN_RE.sub(r"\1[REDACTED]", text)


def log_unexpected(context: str, exc: BaseException):
    """想定外の例外を、URL・トークンを伏せたうえでターミナルに出す"""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("%s で想定外のエラー:\n%s", context, redact(tb))


def chunk_lines(header: str, lines: list, limit: int = 1900) -> list:
    """2000文字制限に収まるよう、行単位でメッセージを分割する。

    通知済みとして記録する以上、途中で切り捨てると予定が届かないまま
    履歴だけ残ってしまうため、切り捨てではなく分割して全件送る。
    """
    chunks, current = [], header + "\n"
    for line in lines:
        line = line[: limit - 1]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    chunks.append(current.rstrip())
    return chunks


def parse_hhmm(text: str) -> Optional[str]:
    """'8:30' '08:30' '８：３０' などを '08:30' に直す。不正ならNone"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", unicodedata.normalize("NFKC", text).strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def validate_word(word: str) -> Optional[str]:
    """フィルター／除外ワードとして登録してよいか検査する。問題なければNone"""
    if not word:
        return "空の単語は登録できません。"
    if len(word) > MAX_WORD_LENGTH:
        return f"単語は{MAX_WORD_LENGTH}文字以内にしてください。"
    # 改行や、目に見えない文字（ゼロ幅スペースなど）は受け付けない
    if any(unicodedata.category(c).startswith("C") for c in word):
        return "使用できない文字（改行や目に見えない文字など）が含まれています。"
    return None


def short(word: str) -> str:
    """返信に入力を含めるとき、長すぎる入力で2000文字を超えないように切り詰める"""
    return word if len(word) <= MAX_WORD_LENGTH else word[:MAX_WORD_LENGTH] + "…"


@dataclass(frozen=True)
class Prefs:
    """ユーザーごとの絞り込み設定"""

    words: list  # フィルター（空なら全予定）
    excludes: list  # 除外ワード
    max_days: Optional[int]  # 期間（今後何日以内か）。Noneなら制限なし


def load_prefs(user_id: int) -> Prefs:
    return Prefs(
        words=database.get_filter_words(user_id),
        excludes=database.get_exclude_words(user_id),
        max_days=database.get_period_days(user_id),
    )


syllabus_client = syllabus.YamanashiSyllabusClient()
course_resolver = syllabus.CourseNameResolver(syllabus_client)


async def fetch_for_user(url: str, prefs: Prefs, now=None) -> moodle.FetchResult:
    """同期関数のfetch_eventsを別スレッドで実行し、Botを止めない"""
    return await asyncio.to_thread(
        moodle.fetch_events,
        url,
        prefs.words,
        now,
        prefs.excludes,
        prefs.max_days,
        course_resolver,
    )


def explain_empty(result: moodle.FetchResult, prefs: Prefs) -> str:
    """予定が0件だったとき、どの段階で0になったかを説明する文を作る"""
    stats = (
        f"（カレンダー内の予定: {result.total}件 / "
        f"これからの予定: {result.upcoming}件 / 表示対象: 0件）"
    )
    if result.total == 0:
        reason = (
            "カレンダーから予定を1件も読み取れませんでした。"
            "MoodleのカレンダーURL（エクスポートする期間の設定など）を確認してください。"
        )
    elif result.upcoming == 0:
        reason = "カレンダー内の予定はすべて終了済みです。"
    else:
        parts = []
        if result.beyond_period:
            parts.append(
                f"期間設定（今後{prefs.max_days}日以内）の範囲外: {result.beyond_period}件"
            )
        if result.excluded:
            parts.append(f"除外ワードで除外: {result.excluded}件")
        unmatched = result.upcoming - result.beyond_period - result.excluded
        if unmatched > 0:
            parts.append(f"フィルターに一致しない: {unmatched}件")
        reason = "これからの予定はありますが、すべて対象外になりました。\n" + "\n".join(
            f"・{p}" for p in parts
        )

    filters = "、".join(prefs.words) if prefs.words else "なし（すべての予定が対象）"
    excludes = "、".join(prefs.excludes) if prefs.excludes else "なし"
    period = f"今後{prefs.max_days}日以内" if prefs.max_days else "制限なし"
    return (
        f"現在、これからの予定はありません。\n{reason}\n{stats}\n"
        f"フィルター: {filters}\n除外ワード: {excludes}\n期間: {period}"
    )


def format_with_remaining(ev: moodle.MoodleEvent, now: datetime.datetime) -> str:
    """期限間近の通知用。予定の表示に「あと◯分」を付ける"""
    due = ev.due_at(now)
    minutes = max(int((due - now).total_seconds() // 60), 0) if due else 0
    return f"{ev.format()}（あと{minutes}分）"


def install_links(app_id: int) -> dict:
    """Botを導入するためのリンク。
    user : 自分のアカウントに追加する（ユーザーインストール。サーバーは不要）
    guild: サーバーに追加する
    """
    base = f"https://discord.com/oauth2/authorize?client_id={app_id}"
    return {
        "user": f"{base}&integration_type=1&scope=applications.commands",
        "guild": f"{base}&integration_type=0&scope=bot+applications.commands",
    }


# --------------------------------------------------
# Discordのクライアント本体
# --------------------------------------------------
class MoodleBot(discord.Client):
    def __init__(self):
        # - メッセージ内容の権限（特権インテント）は不要。スラッシュコマンドを使うため。
        # - allowed_mentions=none: Botの発言に含まれる @everyone などで、実際に通知が飛ばないようにする
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # コマンドの変更があったときだけ、Discordへ再登録（同期）する
        stored = database.get_meta("commands_version")
        if SYNC_COMMANDS or stored != COMMANDS_VERSION:
            synced = await self.tree.sync()
            database.set_meta("commands_version", COMMANDS_VERSION)
            log.info("スラッシュコマンドをDiscordに同期しました（%d件）", len(synced))
        else:
            log.info("スラッシュコマンドは同期済みです（バージョン %s）", COMMANDS_VERSION)

        # 導入リンクをターミナルに表示する
        if self.application_id:
            links = install_links(self.application_id)
            print("【導入リンク】")
            print(f"  自分のアカウントに追加（サーバー不要）: {links['user']}")
            print(f"  サーバーに追加                        : {links['guild']}")


bot = MoodleBot()
tree = bot.tree


def everywhere(obj):
    """コマンドを、次のすべてで使えるようにする。
    ・導入方法: サーバーへの導入 / ユーザーインストール（自分のアカウントへの追加）
    ・使う場所: サーバー / Botとの個人DM / 他の人とのDM・グループDM
    """
    app_commands.allowed_installs(guilds=True, users=True)(obj)
    app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(obj)
    return obj


# --------------------------------------------------
# 返信の共通処理
# --------------------------------------------------
def use_ephemeral(interaction) -> bool:
    """返信を「自分にだけ見える形」にするか。

    Botとの1対1のDMでは、通常のメッセージとして残す。
    サーバーや他の人とのDMでは、予定の内容が他の人に見えないよう、自分にだけ見える形にする。
    判別できないときは、安全のため「自分にだけ見える形」にする。
    """
    ctx = getattr(interaction, "context", None)
    if ctx is not None and getattr(ctx, "dm_channel", False):
        return False
    return True


async def send(interaction, text: str):
    ephemeral = use_ephemeral(interaction)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


async def send_all(interaction, texts: list):
    for text in texts:
        await send(interaction, text)


# --------------------------------------------------
# コマンドの中身（Discordに依存しない部分。文章を返す）
# --------------------------------------------------
def _guard(user_id: int) -> Optional[str]:
    return None if user_id in user_urls else NEED_REGISTER_TEXT


# ---- URL ----
def register_url(user_id: int, url: str) -> tuple:
    """URLを検査して登録する。戻り値: (成功したか, 返信文, 新規ユーザーか)"""
    url = (url or "").strip()
    problem = moodle.check_url(url)
    if problem:
        # 入力されたURL（認証トークン入り）は返信に含めない
        return False, f"❌ {problem}\n取得方法は `/使い方` で確認できます。", False
    user_urls[user_id] = url
    is_new = database.save_user_url(user_id, url)
    database.clear_dm_block(user_id)  # 本人が操作してきたので、DM拒否中の記録は解除する
    message = (
        "✅ MoodleのカレンダーURLを登録しました！"
        if is_new
        else "✅ MoodleのカレンダーURLを更新しました！"
    )
    return True, message, is_new


def url_status_text(user_id: int) -> str:
    if user_id in user_urls:
        return "✅ MoodleのカレンダーURLは登録済みです。\n更新する場合は `/url 登録` を実行してください。"
    return "❌ URLが登録されていません。\n`/url 登録` で登録してください。取得方法は `/使い方` で確認できます。"


def url_delete_text(user_id: int) -> str:
    if user_id not in user_urls:
        return "登録されているURLはありません。"
    del user_urls[user_id]
    database.delete_user_url(user_id)  # フィルター・設定・通知履歴なども削除される
    return "🗑️ 登録されていたURLと各種設定を削除しました。"


# ---- フィルター／除外ワード ----
@dataclass(frozen=True)
class WordKind:
    cmd: str
    header: str
    mark: str
    empty_text: str
    notes: str
    get: Callable
    add: Callable
    delete: Callable
    added: str
    removed: str


FILTER_KIND = WordKind(
    cmd="フィルター",
    header="**【あなたの通知フィルター設定】**",
    mark="✅",
    empty_text=(
        "現在、フィルターは未設定です。**すべての予定**が通知・表示されます。\n"
        "※絞り込む場合: `/フィルター 追加 <単語>`"
    ),
    notes="\n※すべて削除すると、全予定が通知されます",
    get=database.get_filter_words,
    add=database.add_filter_word,
    delete=database.delete_filter_word,
    added="フィルターに「**{word}**」を追加しました！✅",
    removed="フィルターから「**{word}**」を削除しました。❌",
)

EXCLUDE_KIND = WordKind(
    cmd="除外",
    header="**【あなたの除外ワード設定】**",
    mark="🚫",
    empty_text=(
        "現在、除外ワードは設定されていません。\n"
        "※追加する場合: `/除外 追加 <単語>`"
    ),
    notes="\n※除外ワードを含む予定は、フィルターに一致しても通知されません",
    get=database.get_exclude_words,
    add=database.add_exclude_word,
    delete=database.delete_exclude_word,
    added="除外ワードに「**{word}**」を追加しました！🚫",
    removed="除外ワードから「**{word}**」を削除しました。",
)


def words_list_text(kind: WordKind, user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    words = kind.get(user_id)
    if not words:
        return kind.empty_text
    msg = kind.header + "\n" + "".join(f"・{w}：{kind.mark}\n" for w in words)
    msg += "\n※大文字小文字・全角半角は区別しません"
    msg += f"\n※追加: `/{kind.cmd} 追加 <単語>`　削除: `/{kind.cmd} 削除 <単語>`"
    msg += kind.notes
    return msg


def add_word_text(kind: WordKind, user_id: int, word: str) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    word = (word or "").strip()
    problem = validate_word(word)
    if problem:
        return f"❌ {problem}"
    words = kind.get(user_id)
    # 大文字小文字・全角半角の違いだけの重複は登録しない
    if any(moodle.normalize_text(w) == moodle.normalize_text(word) for w in words):
        return f"「{word}」は既に登録されています。"
    if len(words) >= MAX_WORDS_PER_LIST:
        return (
            f"❌ 登録できるのは最大{MAX_WORDS_PER_LIST}個までです。"
            f"不要なものを `/{kind.cmd} 削除` で削除してから追加してください。"
        )
    kind.add(user_id, word)
    return kind.added.format(word=word)


def delete_word_text(kind: WordKind, user_id: int, word: str) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    word = (word or "").strip()
    # 登録時の表記と大文字小文字が違っていても削除できる
    target = next(
        (w for w in kind.get(user_id) if moodle.normalize_text(w) == moodle.normalize_text(word)),
        None,
    )
    if target is None:
        return f"「{short(word)}」は登録されていません。"
    kind.delete(user_id, target)
    return kind.removed.format(word=target)


def word_choices(kind: WordKind, user_id: int, current: str) -> list:
    """削除コマンドの入力候補（登録済みの単語のうち、入力途中の文字を含むもの）"""
    needle = moodle.normalize_text(current or "")
    words = [w for w in kind.get(user_id) if needle in moodle.normalize_text(w)]
    return words[:25]  # Discordの候補は最大25個


# ---- 毎日の通知時刻 ----
def notify_time_text(user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    current = database.get_notify_times(user_id)
    period = database.get_period_days(user_id)
    days = min(period, DIGEST_MAX_DAYS) if period else DIGEST_MAX_DAYS
    if current:
        head = (
            f"**【定刻通知の設定】**\n毎日 {' / '.join(current)} に、"
            f"今後{days}日間の予定一覧をお知らせします。\n"
        )
    else:
        head = "**【定刻通知の設定】**\n現在、定刻通知は**オフ**です。\n"
    return (
        head
        + f"\n※変更: `/通知時間 設定`（最大{MAX_NOTIFY_TIMES}つ。例: 8:30 18:00）"
        + "\n※停止: `/通知時間 オフ`　初期設定に戻す: `/通知時間 初期化`"
        + "\n※時刻は日本時間です。対象の予定が1件もない時は送信されません。"
    )


def set_notify_times_text(user_id: int, raw: str) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    text = unicodedata.normalize("NFKC", raw or "").strip()
    tokens = [t for t in re.split(r"[\s,、，]+", text) if t]
    parsed = [parse_hhmm(t) for t in tokens]
    if not tokens or None in parsed:
        return (
            "時刻は `8:30` のように指定してください（0:00〜23:59）。\n"
            "複数ならスペースで区切ります。例: `8:30 18:00`"
        )
    unique = sorted(set(parsed))
    if len(unique) > MAX_NOTIFY_TIMES:
        return f"登録できる時刻は最大{MAX_NOTIFY_TIMES}つまでです。"
    database.set_notify_times(user_id, unique)
    return f"定刻通知を毎日 {' / '.join(unique)} に設定しました！⏰"


def notify_time_off_text(user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    database.set_notify_times(user_id, [])
    return "定刻通知をオフにしました。再開は `/通知時間 初期化` または `/通知時間 設定` です。"


def notify_time_reset_text(user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    database.set_notify_times(user_id, None)
    return f"定刻通知を初期設定（毎日 {' / '.join(database.get_notify_times(user_id))}）に戻しました。"


# ---- 通知する期間 ----
_PERIOD_NOTE = (
    "\n※期間を広げても、カレンダーURLを発行したときの期間（Moodleのエクスポート範囲）"
    "より先の予定は取得できません。"
)


def period_text(user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    days = database.get_period_days(user_id)
    if days:
        return (
            f"通知する予定の期間: **今後{days}日以内**\n"
            "※変更: `/期間 設定`　解除: `/期間 解除`" + _PERIOD_NOTE
        )
    return (
        "通知する予定の期間: **制限なし**（カレンダーにある今後の予定すべて）\n"
        "※絞り込む場合: `/期間 設定`（例: 7日）" + _PERIOD_NOTE
    )


def set_period_text(user_id: int, days) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        return "日数は1〜365の数字で指定してください。\n例: 7"
    database.set_period_days(user_id, days)
    return f"通知する予定を**今後{days}日以内**に絞りました！📆" + _PERIOD_NOTE


def clear_period_text(user_id: int) -> str:
    guard = _guard(user_id)
    if guard:
        return guard
    database.set_period_days(user_id, None)
    return "期間の制限を解除しました。"


# --------------------------------------------------
# DM通知の送信
# --------------------------------------------------
async def deliver_chunks(user_id: int, chunks: list[str], now: datetime.datetime) -> bool:
    """ユーザーにDMで通知する（複数チャンク対応）。成功したらTrue。

    DMを拒否されている場合は DM_RETRY_HOURS 時間後まで再送を見送る。
    """
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        for chunk in chunks:
            await user.send(chunk)
    except discord.Forbidden:
        retry_after = now + datetime.timedelta(hours=DM_RETRY_HOURS)
        database.block_dm(user_id, retry_after)
        log.warning(
            "ユーザー %s はDMを受け付けていません。%s 以降に再試行します。",
            user_id,
            retry_after.strftime("%Y-%m-%d %H:%M"),
        )
        return False
    except Exception as e:
        log.warning("ユーザー %s への送信失敗: %s", user_id, redact(str(e)))
        return False
    database.clear_dm_block(user_id)
    return True


async def deliver(user_id: int, header: str, lines: list, now: datetime.datetime) -> bool:
    return await deliver_chunks(user_id, chunk_lines(header, lines), now)


async def verify_dm(interaction):
    """URL登録のとき、BotからこのユーザーへDMを送れるかを確認する（通知はDMで届くため）"""
    ctx = getattr(interaction, "context", None)
    if ctx is not None and getattr(ctx, "dm_channel", False):
        return  # Botとの個人DMで操作中なら、DMは届くと分かっている
    try:
        await interaction.user.send("✅ 通知の受信テストです。今後の通知は、このDMに届きます。")
    except discord.Forbidden:
        await send(
            interaction,
            "⚠️ BotからあなたへDMを送れませんでした。通知はDMで届くため、"
            "DMの受信設定（サーバーメンバーからのDMを許可する、など）を確認してから、"
            "もう一度 `/url 登録` を実行してください。",
        )
    except Exception as e:
        log.warning("登録時のDM確認に失敗: %s", redact(str(e)))


# --------------------------------------------------
# 定期実行ループ①：新着・期限間近の通知
# --------------------------------------------------
async def check_user(user_id: int, target_url: str, now: datetime.datetime):
    """1人分の自動チェック"""
    # DM拒否中のユーザーは、再送予定日時まで取得も通知も見送る
    if database.is_dm_blocked(user_id, now):
        return

    # 1. Moodleから取得
    prefs = load_prefs(user_id)
    try:
        result = await fetch_for_user(target_url, prefs, now)
    except moodle.MoodleError as e:
        # 【重要】自動チェックでは、エラーが起きてもユーザーにDMは送らず、
        # ターミナル（ログ）にだけ出力する。Moodleの深夜メンテナンスなどで
        # エラー通知が何通も届くのを防ぐため。復旧後は通常どおり通知される。
        log.warning("ユーザー %s の取得に失敗: %s", user_id, e)
        return

    # 2. 提出期限が近い予定（未リマインドのもの）があれば、この回はそれだけ通知する
    reminded = database.get_reminded_keys(user_id)
    due_soon = [
        ev
        for ev in result.events
        if ev.is_due_soon(now, REMINDER_BEFORE_MINUTES)
        and ev.reminder_key(now) not in reminded
    ]
    if due_soon:
        chunks = moodle.format_events(
            due_soon,
            now,
            header=f"【提出期限が近い予定（{REMINDER_BEFORE_MINUTES}分以内）】",
        )
        ok = await deliver_chunks(user_id, chunks, now)
        if ok:
            database.mark_reminded(user_id, [ev.reminder_key(now) for ev in due_soon])
            # 新着として重複通知しないよう、通知済みにもしておく
            database.mark_notified(user_id, [ev.key for ev in due_soon])
        return  # 新着の通知は次回のチェックに回す

    # 3. 通常の新着通知（通知済みを除外）
    notified = database.get_notified_keys(user_id)
    new_events = [ev for ev in result.events if ev.key not in notified]
    if not new_events:
        return

    # 成功したときだけ通知済みとして記録する
    chunks = moodle.format_events(
        new_events,
        now,
        header="【定期通知：Moodle 新しい予定】",
    )
    if await deliver_chunks(user_id, chunks, now):
        database.mark_notified(user_id, [ev.key for ev in new_events])


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_moodle():
    now = datetime.datetime.now(moodle.JST)
    for user_id, target_url in list(user_urls.items()):
        try:
            await check_user(user_id, target_url, now)
        except Exception as e:
            # 1人分の処理で想定外のエラーが出ても、他のユーザーの処理を止めない。
            # （ループが止まると全員の通知が止まるため）。URL・トークンは伏せてログに出す。
            log_unexpected(f"ユーザー {user_id} の自動チェック", e)


# --------------------------------------------------
# 定期実行ループ②：定刻通知（毎日、設定した時刻に今後の予定一覧を送る）
# 上の新着・期限間近の通知とは独立して動く（通知済み管理も別）。
# --------------------------------------------------
def due_slots(user_id: int, now: datetime.datetime) -> list:
    """今送るべき定刻通知を [(slot, 'HH:MM'), ...] で返す。

    定刻を過ぎても DIGEST_GRACE_MINUTES 分以内なら対象にする（再起動・障害からの復帰用）。
    日付をまたぐ場合（23:50設定で0:10に起動など）も拾えるよう、前日分も見る。
    """
    sent = database.get_digest_sent(user_id)
    grace = datetime.timedelta(minutes=DIGEST_GRACE_MINUTES)
    result = []
    for hhmm in database.get_notify_times(user_id):
        hour, minute = map(int, hhmm.split(":"))
        for day_offset in (0, 1):
            base = now - datetime.timedelta(days=day_offset)
            scheduled = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled <= now < scheduled + grace:
                slot = f"{base:%Y-%m-%d} {hhmm}"
                if slot not in sent:
                    result.append((slot, hhmm))
    return result


async def digest_user(user_id: int, target_url: str, now: datetime.datetime):
    """1人分の定刻通知"""
    if database.is_dm_blocked(user_id, now):
        return

    due = due_slots(user_id, now)
    if not due:
        return
    # 失敗直後は、DIGEST_RETRY_MINUTES 分あけてから再試行する（Moodleを連打しない）
    if now < _digest_retry_at.get(user_id, now):
        return

    prefs = load_prefs(user_id)
    try:
        result = await fetch_for_user(target_url, prefs, now)
    except moodle.MoodleError as e:
        # 自動処理なので、エラーはDMせずターミナルにのみ出力する
        log.warning("ユーザー %s の定刻通知の取得に失敗: %s", user_id, e)
        _digest_retry_at[user_id] = now + datetime.timedelta(minutes=DIGEST_RETRY_MINUTES)
        return

    # 定刻通知に載せるのは「今後 days 日以内」の予定。ユーザーの期間設定が短ければそちらを優先
    days = min(prefs.max_days, DIGEST_MAX_DAYS) if prefs.max_days else DIGEST_MAX_DAYS
    horizon = now + datetime.timedelta(days=days)
    events = [ev for ev in result.events if ev.start <= horizon]

    # 予定が1件もない回は送らない（空の通知で煩わせない）。その回は送信済みとして扱う
    if events:
        latest_hhmm = max(hhmm for _, hhmm in due)
        chunks = moodle.format_events(
            events,
            now,
            header=f"【定刻通知 {latest_hhmm}：今後{days}日間の予定】",
        )
        ok = await deliver_chunks(user_id, chunks, now)
        if not ok:
            if database.is_dm_blocked(user_id, now):
                pass  # DM拒否 → 再送は120時間後。この回は諦める
            else:
                # Discord側の一時的な不調など。少し待って再試行する
                _digest_retry_at[user_id] = now + datetime.timedelta(
                    minutes=DIGEST_RETRY_MINUTES
                )
                return

    database.mark_digest_sent(user_id, [slot for slot, _ in due])
    _digest_retry_at.pop(user_id, None)


@tasks.loop(minutes=1)
async def scheduled_digest():
    now = datetime.datetime.now(moodle.JST)
    for user_id, target_url in list(user_urls.items()):
        try:
            await digest_user(user_id, target_url, now)
        except Exception as e:
            # 1人分の想定外のエラーで、他のユーザーの定刻通知を止めない
            log_unexpected(f"ユーザー {user_id} の定刻通知", e)
            _digest_retry_at[user_id] = now + datetime.timedelta(minutes=DIGEST_RETRY_MINUTES)


# --------------------------------------------------
# イベント
# --------------------------------------------------
@bot.event
async def on_ready():
    print(f"Botが開通しました: {bot.user.name}")
    # Botのステータス欄に「/使い方 で使い方を表示」を常時表示する
    await bot.change_presence(activity=discord.Game(name=STATUS_MESSAGE))
    # Bot起動時に定期タスクを開始
    if not auto_check_moodle.is_running():
        auto_check_moodle.start()
    if not scheduled_digest.is_running():
        scheduled_digest.start()


@bot.event
async def on_message(message):
    """BotへのDMに反応する。

    ・初めてDMを送ってきたユーザーに、使い方を一度だけ自動で送る
    ・昔の「!」から始まるコマンドには、「/」に変わったことを案内する
    （コマンド自体は、スラッシュコマンドで処理される）
    """
    if message.author.bot or not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id
    content = (message.content or "").strip()
    first = not database.has_seen_guide(user_id)
    if first:
        database.mark_guide_shown(user_id)

    texts = []
    if content.startswith("!"):
        texts.append(LEGACY_TEXT)
    if first:
        texts.extend(help_texts(user_id))

    try:
        for text in texts:
            await message.channel.send(text)
    except Exception as e:
        log.warning("ユーザー %s へのDM返信に失敗: %s", user_id, type(e).__name__)


# --------------------------------------------------
# スラッシュコマンド
# --------------------------------------------------
@everywhere
@tree.command(name="使い方", description="Botの使い方を表示します")
async def cmd_help(interaction: discord.Interaction):
    await send_all(interaction, help_texts(interaction.user.id))


@everywhere
@tree.command(name="help", description="Botの使い方を表示します（/使い方 と同じ）")
async def cmd_help_en(interaction: discord.Interaction):
    await send_all(interaction, help_texts(interaction.user.id))


@everywhere
@tree.command(name="moodle", description="今後の予定を今すぐ確認します")
async def cmd_moodle(interaction: discord.Interaction):
    user_id = interaction.user.id
    url = user_urls.get(user_id)
    if not url:
        await send(interaction, NEED_REGISTER_TEXT)
        return

    # 取得に時間がかかることがあるので、いったん「処理中」にしてから返信する
    await interaction.response.defer(ephemeral=use_ephemeral(interaction), thinking=True)

    prefs = load_prefs(user_id)
    try:
        result = await fetch_for_user(url, prefs)
    except moodle.MoodleError as e:
        # 手動コマンドなので、失敗はその場で本人に伝える
        await send(interaction, f"❌ {e}")
        return

    if not result.events:
        await send(interaction, explain_empty(result, prefs))
        return
    now = datetime.datetime.now(moodle.JST)
    chunks = moodle.format_events(result.events, now, header="【Moodle 予定一覧】")
    for chunk in chunks:
        await send(interaction, chunk)


# ---- /url ----
class UrlModal(discord.ui.Modal, title="MoodleカレンダーURLの登録"):
    """URLを入力するポップアップ。入力内容は他の人に見えない。"""

    url = discord.ui.TextInput(
        label="カレンダーURL",
        placeholder="https://… （Moodleで取得したURLを貼り付け）",
        max_length=MAX_URL_LENGTH,
    )

    async def on_submit(self, interaction: discord.Interaction):
        ok, message, is_new = register_url(interaction.user.id, self.url.value)
        await send(interaction, message)
        if not ok:
            return
        await verify_dm(interaction)
        if is_new:
            # URL登録が正しくできたので、使えるコマンドを案内する
            await send(interaction, COMMANDS_GUIDE_TEXT)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log_unexpected("URL登録", error)
        try:
            await send(interaction, "⚠️ 予期しないエラーが発生しました。もう一度お試しください。")
        except Exception:
            pass


url_group = everywhere(
    app_commands.Group(name="url", description="MoodleのカレンダーURLの登録・確認・削除")
)


@url_group.command(name="登録", description="MoodleのカレンダーURLを登録・更新します（入力欄が開きます）")
async def cmd_url_register(interaction: discord.Interaction):
    await interaction.response.send_modal(UrlModal())


@url_group.command(name="確認", description="URLが登録済みかを確認します")
async def cmd_url_status(interaction: discord.Interaction):
    await send(interaction, url_status_text(interaction.user.id))


@url_group.command(name="削除", description="登録したURLと、すべての設定・通知履歴を削除します")
async def cmd_url_delete(interaction: discord.Interaction):
    await send(interaction, url_delete_text(interaction.user.id))


# ---- /フィルター ----
filter_group = everywhere(
    app_commands.Group(name="フィルター", description="通知する予定のキーワード（未設定なら全予定）")
)


@filter_group.command(name="一覧", description="登録中のフィルターを表示します")
async def cmd_filter_list(interaction: discord.Interaction):
    await send(interaction, words_list_text(FILTER_KIND, interaction.user.id))


@filter_group.command(name="追加", description="その単語を含む予定だけを通知します")
@app_commands.describe(単語="授業名や「課題」「小テスト」など（50文字まで）")
async def cmd_filter_add(interaction: discord.Interaction, 単語: str):
    await send(interaction, add_word_text(FILTER_KIND, interaction.user.id, 単語))


@filter_group.command(name="削除", description="フィルターの単語を削除します")
@app_commands.describe(単語="削除する単語")
async def cmd_filter_delete(interaction: discord.Interaction, 単語: str):
    await send(interaction, delete_word_text(FILTER_KIND, interaction.user.id, 単語))


@cmd_filter_delete.autocomplete("単語")
async def ac_filter_delete(interaction: discord.Interaction, current: str):
    words = word_choices(FILTER_KIND, interaction.user.id, current)
    return [app_commands.Choice(name=w, value=w) for w in words]


# ---- /除外 ----
exclude_group = everywhere(
    app_commands.Group(name="除外", description="通知しない予定のキーワード（フィルターより優先）")
)


@exclude_group.command(name="一覧", description="登録中の除外ワードを表示します")
async def cmd_exclude_list(interaction: discord.Interaction):
    await send(interaction, words_list_text(EXCLUDE_KIND, interaction.user.id))


@exclude_group.command(name="追加", description="その単語を含む予定を通知しません")
@app_commands.describe(単語="通知したくない単語（50文字まで）")
async def cmd_exclude_add(interaction: discord.Interaction, 単語: str):
    await send(interaction, add_word_text(EXCLUDE_KIND, interaction.user.id, 単語))


@exclude_group.command(name="削除", description="除外ワードを削除します")
@app_commands.describe(単語="削除する単語")
async def cmd_exclude_delete(interaction: discord.Interaction, 単語: str):
    await send(interaction, delete_word_text(EXCLUDE_KIND, interaction.user.id, 単語))


@cmd_exclude_delete.autocomplete("単語")
async def ac_exclude_delete(interaction: discord.Interaction, current: str):
    words = word_choices(EXCLUDE_KIND, interaction.user.id, current)
    return [app_commands.Choice(name=w, value=w) for w in words]


# ---- /通知時間 ----
notify_group = everywhere(
    app_commands.Group(name="通知時間", description="毎日の定刻通知（予定のまとめ）の時刻")
)


@notify_group.command(name="確認", description="現在の定刻通知の設定を表示します")
async def cmd_notify_show(interaction: discord.Interaction):
    await send(interaction, notify_time_text(interaction.user.id))


@notify_group.command(name="設定", description="定刻通知の時刻を変更します（最大4つ）")
@app_commands.describe(時刻="例: 8:30 18:00 （複数はスペース区切り・日本時間）")
async def cmd_notify_set(interaction: discord.Interaction, 時刻: str):
    await send(interaction, set_notify_times_text(interaction.user.id, 時刻))


@notify_group.command(name="オフ", description="毎日の定刻通知を止めます")
async def cmd_notify_off(interaction: discord.Interaction):
    await send(interaction, notify_time_off_text(interaction.user.id))


@notify_group.command(name="初期化", description="定刻通知を初期設定（8:30 / 18:00）に戻します")
async def cmd_notify_reset(interaction: discord.Interaction):
    await send(interaction, notify_time_reset_text(interaction.user.id))


# ---- /期間 ----
period_group = everywhere(
    app_commands.Group(name="期間", description="通知する予定の期間（今後何日以内か）")
)


@period_group.command(name="確認", description="現在の期間の設定を表示します")
async def cmd_period_show(interaction: discord.Interaction):
    await send(interaction, period_text(interaction.user.id))


@period_group.command(name="設定", description="通知する予定を、今後◯日以内に絞ります")
@app_commands.describe(日数="今後何日以内か（1〜365）")
async def cmd_period_set(interaction: discord.Interaction, 日数: app_commands.Range[int, 1, 365]):
    await send(interaction, set_period_text(interaction.user.id, 日数))


@period_group.command(name="解除", description="期間の制限をなくします")
async def cmd_period_clear(interaction: discord.Interaction):
    await send(interaction, clear_period_text(interaction.user.id))


for _group in (url_group, filter_group, exclude_group, notify_group, period_group):
    tree.add_command(_group)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """コマンド実行中の想定外のエラー。詳細はターミナルにだけ出し（URL・トークンは伏せる）、
    ユーザーには簡単なお知らせだけを返す。"""
    name = getattr(getattr(interaction, "command", None), "qualified_name", "?")
    log_unexpected(f"コマンド /{name}（ユーザー {interaction.user.id}）", error)
    try:
        await send(interaction, "⚠️ 予期しないエラーが発生しました。時間をおいて、もう一度お試しください。")
    except Exception:
        pass


try:
    bot.run(TOKEN)
except discord.LoginFailure:
    sys.exit(
        "【エラー】DISCORD_TOKEN が無効です。\n"
        "Discord Developer Portal でトークンを再発行し、.env を更新してください。"
    )