import asyncio
import datetime
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands, tasks

import database
import moodle
from config import (
    CHECK_INTERVAL_MINUTES,
    DIGEST_GRACE_MINUTES,
    DIGEST_MAX_DAYS,
    DIGEST_RETRY_MINUTES,
    DM_RETRY_HOURS,
    MAX_NOTIFY_TIMES,
    REMINDER_BEFORE_MINUTES,
    STATUS_MESSAGE,
    require_token,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# トークン未設定ならここで原因を表示して終了する（DBを触る前に確認）
TOKEN = require_token()

# DBから登録データをロード {user_id: ics_url}
user_urls = database.init_db()

intents = discord.Intents.default()
intents.message_content = True
# 標準の help コマンドは使わず、下の「!使い方 / !help」を自前で用意する
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# 定刻通知の取得・送信に失敗したユーザーの、次に再試行してよい時刻（メモリ上のみ）
_digest_retry_at: dict = {}

GUIDE_TEXT = (
    "**【Moodle通知Botの使い方】**\n"
    "1. Moodleの「カレンダーをエクスポート」からURLを取得します。\n"
    "2. `!url 登録 <あなたのURL>` を送信して登録します。\n"
    "3. `!フィルター 追加 <キーワード>` で、通知してほしい授業名や「課題」「小テスト」などの単語を登録します。\n"
    "※以後は自動で予定をお知らせします！手動で確認したい時は `!moodle` と送信してください。\n"
    "\n"
    "**【その他のコマンド】**\n"
    "・`!除外 追加 <キーワード>` … 通知したくない単語を登録します（フィルターより優先）\n"
    "・`!通知時間 設定 8:30 18:00` … 毎日の定刻通知の時刻を変更します（`!通知時間` で確認、`オフ` で停止）\n"
    "・`!期間 7` … 通知する予定を今後7日以内に絞ります（`!期間 解除` で解除）\n"
    "・`!フィルター` / `!除外` … 現在の設定を表示します\n"
    "※URLの登録・確認は、このBotとのDMで行ってください。"
)


# --------------------------------------------------
# 共通の小道具
# --------------------------------------------------
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


def parse_days(text: str) -> Optional[int]:
    """'7' '7日' '７' などを日数(1〜365)に直す。不正ならNone"""
    m = re.fullmatch(r"(\d{1,3})日?", unicodedata.normalize("NFKC", text).strip())
    if not m:
        return None
    days = int(m.group(1))
    return days if 1 <= days <= 365 else None


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


async def fetch_for_user(url: str, prefs: Prefs, now=None) -> moodle.FetchResult:
    """同期関数のfetch_eventsを別スレッドで実行し、Botを止めない"""
    return await asyncio.to_thread(
        moodle.fetch_events, url, prefs.words, now, prefs.excludes, prefs.max_days
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


async def deliver(user_id: int, header: str, lines: list, now: datetime.datetime) -> bool:
    """ユーザーにDMで通知する。成功したらTrue。

    DMを拒否されている場合は DM_RETRY_HOURS 時間後まで再送を見送る。
    """
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        for chunk in chunk_lines(header, lines):
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
        log.warning("ユーザー %s への送信失敗: %s", user_id, e)
        return False
    database.clear_dm_block(user_id)
    return True


async def reply_dm(ctx, text: str) -> bool:
    """コマンドを送った本人のDMにだけ返信する。

    DMを送れなかった場合は、その旨だけをチャンネルに出してFalseを返す
    （予定の内容はチャンネルに出さない）。
    """
    try:
        await ctx.author.send(text)
        return True
    except discord.Forbidden:
        await ctx.send(
            f"{ctx.author.mention} DMを送信できませんでした。"
            "DMの受信設定（サーバーメンバーからのDMを許可）を確認してください。"
        )
        return False


# --------------------------------------------------
# 定期実行ループ①：新着・期限間近の通知
# --------------------------------------------------
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_moodle():
    now = datetime.datetime.now(moodle.JST)

    for user_id, target_url in list(user_urls.items()):
        # DM拒否中のユーザーは、再送予定日時まで取得も通知も見送る
        if database.is_dm_blocked(user_id, now):
            continue

        # 1. Moodleから取得
        prefs = load_prefs(user_id)
        try:
            result = await fetch_for_user(target_url, prefs, now)
        except moodle.MoodleError as e:
            # 【重要】自動チェックでは、エラーが起きてもユーザーにDMは送らず、
            # ターミナル（ログ）にだけ出力する。Moodleの深夜メンテナンスなどで
            # エラー通知が何通も届くのを防ぐため。復旧後は通常どおり通知される。
            log.warning("ユーザー %s の取得に失敗: %s", user_id, e)
            continue

        # 2. 提出期限が近い予定（未リマインドのもの）があれば、この回はそれだけ通知する
        reminded = database.get_reminded_keys(user_id)
        due_soon = [
            ev
            for ev in result.events
            if ev.is_due_soon(now, REMINDER_BEFORE_MINUTES)
            and ev.reminder_key(now) not in reminded
        ]
        if due_soon:
            ok = await deliver(
                user_id,
                f"**【⏰ 提出期限が近い予定（{REMINDER_BEFORE_MINUTES}分以内）】**",
                [format_with_remaining(ev, now) for ev in due_soon],
                now,
            )
            if ok:
                database.mark_reminded(user_id, [ev.reminder_key(now) for ev in due_soon])
                # 新着として重複通知しないよう、通知済みにもしておく
                database.mark_notified(user_id, [ev.key for ev in due_soon])
            continue  # 新着の通知は次回のチェックに回す

        # 3. 通常の新着通知（通知済みを除外）
        notified = database.get_notified_keys(user_id)
        new_events = [ev for ev in result.events if ev.key not in notified]
        if not new_events:
            continue

        # 成功したときだけ通知済みとして記録する
        if await deliver(
            user_id,
            "**【定期通知：Moodle 新しい予定】**",
            [ev.format() for ev in new_events],
            now,
        ):
            database.mark_notified(user_id, [ev.key for ev in new_events])


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


@tasks.loop(minutes=1)
async def scheduled_digest():
    now = datetime.datetime.now(moodle.JST)

    for user_id, target_url in list(user_urls.items()):
        if database.is_dm_blocked(user_id, now):
            continue

        due = due_slots(user_id, now)
        if not due:
            continue
        # 失敗直後は、DIGEST_RETRY_MINUTES 分あけてから再試行する（Moodleを連打しない）
        if now < _digest_retry_at.get(user_id, now):
            continue

        prefs = load_prefs(user_id)
        try:
            result = await fetch_for_user(target_url, prefs, now)
        except moodle.MoodleError as e:
            # 自動処理なので、エラーはDMせずターミナルにのみ出力する
            log.warning("ユーザー %s の定刻通知の取得に失敗: %s", user_id, e)
            _digest_retry_at[user_id] = now + datetime.timedelta(minutes=DIGEST_RETRY_MINUTES)
            continue

        # 定刻通知に載せるのは「今後 days 日以内」の予定。ユーザーの期間設定が短ければそちらを優先
        days = min(prefs.max_days, DIGEST_MAX_DAYS) if prefs.max_days else DIGEST_MAX_DAYS
        horizon = now + datetime.timedelta(days=days)
        events = [ev for ev in result.events if ev.start <= horizon]

        # 予定が1件もない回は送らない（空の通知で煩わせない）。その回は送信済みとして扱う
        if events:
            latest_hhmm = max(hhmm for _, hhmm in due)
            ok = await deliver(
                user_id,
                f"**【📅 定刻通知 {latest_hhmm}：今後{days}日間の予定】**",
                [ev.format() for ev in events],
                now,
            )
            if not ok:
                if database.is_dm_blocked(user_id, now):
                    pass  # DM拒否 → 再送は120時間後。この回は諦める
                else:
                    # Discord側の一時的な不調など。少し待って再試行する
                    _digest_retry_at[user_id] = now + datetime.timedelta(
                        minutes=DIGEST_RETRY_MINUTES
                    )
                    continue

        database.mark_digest_sent(user_id, [slot for slot, _ in due])
        _digest_retry_at.pop(user_id, None)


# --------------------------------------------------
# イベント
# --------------------------------------------------
@bot.event
async def on_ready():
    print(f"Botが開通しました: {bot.user.name}")
    # Botのステータス欄に「!help で使い方を表示」を常時表示する
    await bot.change_presence(activity=discord.Game(name=STATUS_MESSAGE))
    # Bot起動時に定期タスクを開始
    if not auto_check_moodle.is_running():
        auto_check_moodle.start()
    if not scheduled_digest.is_running():
        scheduled_digest.start()


@bot.event
async def on_message(message):
    """初めてDMを送ってきたユーザーに、使い方ガイドを一度だけ自動で送る"""
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        if not database.has_seen_guide(user_id):
            database.mark_guide_shown(user_id)
            # 最初のメッセージが「!使い方」「!help」なら、コマンド側がガイドを送るので重複させない
            ctx = await bot.get_context(message)
            if ctx.command is None or ctx.command.name != "使い方":
                try:
                    await message.channel.send(GUIDE_TEXT)
                except Exception as e:
                    log.warning("ユーザー %s へのガイド送信失敗: %s", user_id, e)

    await bot.process_commands(message)


# --------------------------------------------------
# コマンド各種
# --------------------------------------------------

# 使い方ガイド
@bot.command(name="使い方", aliases=["help"])
async def show_help(ctx):
    await ctx.send(GUIDE_TEXT)


# 手動でMoodleの予定を取得するコマンド
# （通知済みかどうかに関係なく今後の予定をすべて表示。結果は送信者本人のDMにのみ届く）
@bot.command(name="moodle")
async def send_moodle_schedule(ctx):
    user_id = ctx.author.id
    in_guild = not isinstance(ctx.channel, discord.DMChannel)
    ics_url = user_urls.get(user_id)

    # 最初のDMで、DMが送れるかも同時に確認する
    first = (
        "Moodleから最新の予定を取得中..."
        if ics_url
        else "❌ MoodleのカレンダーURLが登録されていません。\nこのBotとのDMで `!url 登録 <URL>` を送信してください。"
    )
    if not await reply_dm(ctx, first):
        return
    database.clear_dm_block(user_id)  # DMが送れたので、拒否中の記録は解除する
    if in_guild:
        await ctx.send(f"{ctx.author.mention} 結果をDMでお送りします📩")
    if not ics_url:
        return

    prefs = load_prefs(user_id)
    try:
        result = await fetch_for_user(ics_url, prefs)
    except moodle.MoodleError as e:
        # 手動コマンドなので、失敗はその場で本人に伝える
        await reply_dm(ctx, f"❌ {e}")
        return

    if not result.events:
        await reply_dm(ctx, explain_empty(result, prefs))
        return

    for chunk in chunk_lines(
        "**【Moodle 予定一覧】**", [ev.format() for ev in result.events]
    ):
        if not await reply_dm(ctx, chunk):
            return


async def manage_word_list(
    ctx,
    action,
    word,
    *,
    cmd,
    header,
    mark,
    empty_text,
    notes,
    get_words,
    add_word,
    delete_word,
    added_text,
    removed_text,
):
    """フィルター／除外ワードの共通処理（どちらもユーザーごとに保存される）"""
    user_id = ctx.author.id

    # 設定はURL登録時に作られるため、先に登録してもらう
    if user_id not in user_urls:
        await ctx.send(
            "先にBotとのDMで `!url 登録 <URL>` を行ってください。\n"
            "設定はユーザーごとに保存されます。"
        )
        return

    words = get_words(user_id)

    # 1. 引数なし：現在の一覧を表示
    if action is None:
        if not words:
            await ctx.send(empty_text)
            return
        msg = header + "\n" + "".join(f"・{w}：{mark}\n" for w in words)
        msg += "\n※大文字小文字・全角半角は区別しません"
        msg += f"\n※追加する場合: `!{cmd} 追加 <単語>`\n※削除する場合: `!{cmd} 削除 <単語>`"
        msg += notes
        await ctx.send(msg)

    # 2. 追加
    elif action in ["登録", "追加"]:
        if word is None:
            await ctx.send(f"追加する単語を指定してください。\n例: `!{cmd} 追加 レポート`")
            return
        # 大文字小文字・全角半角の違いだけの重複は登録しない
        if any(moodle.normalize_text(w) == moodle.normalize_text(word) for w in words):
            await ctx.send(f"「{word}」は既に登録されています。")
        else:
            add_word(user_id, word)
            await ctx.send(added_text.format(word=word))

    # 3. 削除
    elif action == "削除":
        if word is None:
            await ctx.send(f"削除する単語を指定してください。\n例: `!{cmd} 削除 テスト`")
            return
        # 登録時の表記と大文字小文字が違っていても削除できる
        target = next(
            (w for w in words if moodle.normalize_text(w) == moodle.normalize_text(word)),
            None,
        )
        if target is not None:
            delete_word(user_id, target)
            await ctx.send(removed_text.format(word=target))
        else:
            await ctx.send(f"「{word}」は登録されていません。")

    else:
        await ctx.send(
            f"使い方: `!{cmd}`（一覧） / `!{cmd} 追加 <単語>` / `!{cmd} 削除 <単語>`"
        )


# フィルター管理コマンド（一致する予定だけ通知。空なら全予定）
@bot.command(name="フィルター")
async def manage_filter(ctx, action: str = None, word: str = None):
    await manage_word_list(
        ctx,
        action,
        word,
        cmd="フィルター",
        header="**【あなたの通知フィルター設定】**",
        mark="✅",
        empty_text=(
            "現在、フィルターは未設定です。**すべての予定**が通知・表示されます。\n"
            "※絞り込む場合: `!フィルター 追加 <単語>`"
        ),
        notes="\n※すべて削除すると、全予定が通知されます",
        get_words=database.get_filter_words,
        add_word=database.add_filter_word,
        delete_word=database.delete_filter_word,
        added_text="フィルターに「**{word}**」を追加しました！✅",
        removed_text="フィルターから「**{word}**」を削除しました。❌",
    )


# 除外ワード管理コマンド（ブラックリスト。一致する予定は常に通知しない）
@bot.command(name="除外", aliases=["ブラックリスト"])
async def manage_exclude(ctx, action: str = None, word: str = None):
    await manage_word_list(
        ctx,
        action,
        word,
        cmd="除外",
        header="**【あなたの除外ワード設定】**",
        mark="🚫",
        empty_text=(
            "現在、除外ワードは設定されていません。\n"
            "※追加する場合: `!除外 追加 <単語>`"
        ),
        notes="\n※除外ワードを含む予定は、フィルターに一致しても通知されません",
        get_words=database.get_exclude_words,
        add_word=database.add_exclude_word,
        delete_word=database.delete_exclude_word,
        added_text="除外ワードに「**{word}**」を追加しました！🚫",
        removed_text="除外ワードから「**{word}**」を削除しました。",
    )


# 定刻通知の時刻管理コマンド
@bot.command(name="通知時間")
async def manage_notify_time(ctx, action: str = None, *times: str):
    user_id = ctx.author.id
    if user_id not in user_urls:
        await ctx.send(
            "先にBotとのDMで `!url 登録 <URL>` を行ってください。\n"
            "設定はユーザーごとに保存されます。"
        )
        return

    # 「!通知時間 8:30 18:00」のように、「設定」を省略した形も受け付ける
    if action is not None and parse_hhmm(action) is not None:
        action, times = "設定", (action, *times)

    # 1. 引数なし：現在の設定を表示
    if action is None:
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
        await ctx.send(
            head
            + f"\n※変更: `!通知時間 設定 8:30 18:00`（最大{MAX_NOTIFY_TIMES}つ）"
            + "\n※停止: `!通知時間 オフ`　※初期設定に戻す: `!通知時間 初期化`"
            + "\n※時刻は日本時間です。対象の予定が1件もない時は送信されません。"
        )

    # 2. 設定
    elif action == "設定":
        parsed = [parse_hhmm(t) for t in times]
        if not times or None in parsed:
            await ctx.send(
                "時刻は `8:30` のように指定してください（0:00〜23:59）。\n"
                "例: `!通知時間 設定 8:30 18:00`"
            )
            return
        unique = sorted(set(parsed))
        if len(unique) > MAX_NOTIFY_TIMES:
            await ctx.send(f"登録できる時刻は最大{MAX_NOTIFY_TIMES}つまでです。")
            return
        database.set_notify_times(user_id, unique)
        await ctx.send(f"定刻通知を毎日 {' / '.join(unique)} に設定しました！⏰")

    # 3. オフ
    elif action in ["オフ", "off", "OFF", "停止"]:
        database.set_notify_times(user_id, [])
        await ctx.send("定刻通知をオフにしました。再開は `!通知時間 初期化` または `!通知時間 設定 ...` です。")

    # 4. 初期化
    elif action == "初期化":
        database.set_notify_times(user_id, None)
        await ctx.send(
            f"定刻通知を初期設定（毎日 {' / '.join(database.get_notify_times(user_id))}）に戻しました。"
        )

    else:
        await ctx.send(
            "使い方: `!通知時間`（確認） / `!通知時間 設定 8:30 18:00` / `!通知時間 オフ` / `!通知時間 初期化`"
        )


# 通知する期間の管理コマンド
@bot.command(name="期間")
async def manage_period(ctx, value: str = None):
    user_id = ctx.author.id
    if user_id not in user_urls:
        await ctx.send(
            "先にBotとのDMで `!url 登録 <URL>` を行ってください。\n"
            "設定はユーザーごとに保存されます。"
        )
        return

    note = (
        "\n※期間を広げても、カレンダーURLを発行したときの期間（Moodleのエクスポート範囲）"
        "より先の予定は取得できません。"
    )

    # 1. 引数なし：現在の設定を表示
    if value is None:
        days = database.get_period_days(user_id)
        if days:
            await ctx.send(
                f"通知する予定の期間: **今後{days}日以内**\n"
                "※変更: `!期間 14`　※解除: `!期間 解除`" + note
            )
        else:
            await ctx.send(
                "通知する予定の期間: **制限なし**（カレンダーにある今後の予定すべて）\n"
                "※絞り込む場合: `!期間 7`（今後7日以内）" + note
            )
        return

    # 2. 解除
    if value in ["解除", "なし", "off", "OFF"]:
        database.set_period_days(user_id, None)
        await ctx.send("期間の制限を解除しました。")
        return

    # 3. 設定
    days = parse_days(value)
    if days is None:
        await ctx.send("日数は1〜365の数字で指定してください。\n例: `!期間 7`")
        return
    database.set_period_days(user_id, days)
    await ctx.send(f"通知する予定を**今後{days}日以内**に絞りました！📆" + note)


# 4.URL登録コマンド
@bot.command(name="url")
async def manage_url(ctx, action: str = None, url: str = None):
    user_id = ctx.author.id

    # コマンドの送信先がDM（プライベートチャット）かチェック
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send(
            f"{ctx.author.mention} セキュリティのため、URLの登録・確認はBotとの**DM（ダイレクトメッセージ）**で行ってください！"
        )
        return

    # 1. 引数なし：登録状況の確認
    if action is None:
        if user_id in user_urls:
            await ctx.send(
                "✅ MoodleのカレンダーURLは登録済みです。\n更新する場合は `!url 登録 <新しいURL>` と送信してください。"
            )
        else:
            await ctx.send(
                "❌ URLが登録されていません。\n`!url 登録 <Moodleの.ics URL>` と送信して登録してください。"
            )

    # 2. URLの登録
    elif action == "登録":
        if url is None or not (
            url.startswith("http://") or url.startswith("https://")
        ):
            await ctx.send("有効なURLを入力してください。\n例: `!url 登録 https://...`")
            return

        # URLをDBに保存（新規ユーザーならデフォルトのフィルターも登録される）
        user_urls[user_id] = url
        is_new = database.save_user_url(user_id, url)
        database.clear_dm_block(user_id)  # DMで操作してきたので、拒否中の記録は解除する
        msg = "✅ MoodleのカレンダーURLを登録しました！"
        if is_new:
            msg += "\n通知フィルターは初期設定になっています。`!フィルター` で確認・変更できます。"
        await ctx.send(msg)

    # 3. URLの削除
    elif action == "削除":
        if user_id in user_urls:
            del user_urls[user_id]
            database.delete_user_url(user_id)  # フィルター・設定・通知履歴なども削除される
            await ctx.send("🗑️ 登録されていたURLと各種設定を削除しました。")
        else:
            await ctx.send("登録されているURLはありません。")


try:
    bot.run(TOKEN)
except discord.LoginFailure:
    sys.exit(
        "【エラー】DISCORD_TOKEN が無効です。\n"
        "Discord Developer Portal でトークンを再発行し、.env を更新してください。"
    )
except discord.PrivilegedIntentsRequired:
    sys.exit(
        "【エラー】MESSAGE CONTENT INTENT が有効になっていません。\n"
        "Discord Developer Portal の Bot 設定で「Message Content Intent」をオンにしてください。"
    )