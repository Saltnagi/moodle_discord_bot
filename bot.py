import asyncio
import datetime
import logging
import sys

import discord
from discord.ext import commands, tasks

import database
import moodle
from config import (
    CHECK_INTERVAL_MINUTES,
    DM_RETRY_HOURS,
    REMINDER_BEFORE_MINUTES,
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
bot = commands.Bot(command_prefix="!", intents=intents)


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


def explain_empty(result: moodle.FetchResult, words: list) -> str:
    """予定が0件だったとき、どの段階で0になったかを説明する文を作る"""
    stats = (
        f"（カレンダー内の予定: {result.total}件 / "
        f"これからの予定: {result.upcoming}件 / フィルター一致: 0件）"
    )
    if result.total == 0:
        reason = (
            "カレンダーから予定を1件も読み取れませんでした。"
            "MoodleのカレンダーURL（エクスポートする期間の設定など）を確認してください。"
        )
    elif result.upcoming == 0:
        reason = "カレンダー内の予定はすべて終了済みです。"
    else:
        reason = (
            "これからの予定はありますが、フィルターに一致するものがありません。"
            "`!フィルター` で確認・追加できます。"
        )
    shown = "、".join(words) if words else "なし（すべての予定が対象）"
    return f"現在、これからの予定はありません。\n{reason}\n{stats}\n現在のフィルター: {shown}"


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
# 定期実行ループタスク
# --------------------------------------------------
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_moodle():
    now = datetime.datetime.now(moodle.JST)

    for user_id, target_url in list(user_urls.items()):
        # DM拒否中のユーザーは、再送予定日時まで取得も通知も見送る
        if database.is_dm_blocked(user_id, now):
            continue

        # 1. Moodleから取得（同期関数なので別スレッドで実行しBotを止めない）
        words = database.get_filter_words(user_id)
        try:
            result = await asyncio.to_thread(
                moodle.fetch_events, target_url, words, now
            )
        except moodle.MoodleError as e:
            # 定期通知ではエラーをDMしない（毎回届くとスパムになるため）
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


@bot.event
async def on_ready():
    print(f"Botが開通しました: {bot.user.name}")
    # Bot起動時に定期タスクを開始
    if not auto_check_moodle.is_running():
        auto_check_moodle.start()


# --------------------------------------------------
# コマンド各種
# --------------------------------------------------

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

    words = database.get_filter_words(user_id)
    try:
        result = await asyncio.to_thread(moodle.fetch_events, ics_url, words)
    except moodle.MoodleError as e:
        await reply_dm(ctx, f"❌ {e}")
        return

    if not result.events:
        await reply_dm(ctx, explain_empty(result, words))
        return

    for chunk in chunk_lines(
        "**【Moodle 予定一覧】**", [ev.format() for ev in result.events]
    ):
        if not await reply_dm(ctx, chunk):
            return


# フィルター管理コマンド（フィルターはユーザーごとに保存される）
@bot.command(name="フィルター")
async def manage_filter(ctx, action: str = None, word: str = None):
    user_id = ctx.author.id

    # フィルターはURL登録時に作られるため、先に登録してもらう
    if user_id not in user_urls:
        await ctx.send(
            "先にBotとのDMで `!url 登録 <URL>` を行ってください。\n"
            "フィルターはユーザーごとに保存されます。"
        )
        return

    words = database.get_filter_words(user_id)

    # 1. 引数なしの場合：現在のフィルター一覧を表示
    if action is None:
        if not words:
            await ctx.send(
                "現在、フィルターは未設定です。**すべての予定**が通知・表示されます。\n"
                "※絞り込む場合: `!フィルター 追加 <単語>`"
            )
            return

        msg = "**【あなたの通知フィルター設定】**\n"
        for w in words:
            msg += f"・{w}：✅\n"
        msg += "\n※大文字小文字・全角半角は区別しません"
        msg += "\n※追加する場合: `!フィルター 追加 <単語>`\n※削除する場合: `!フィルター 削除 <単語>`"
        msg += "\n※すべて削除すると、全予定が通知されます"
        await ctx.send(msg)

    # 2. キーワード追加の場合
    elif action in ["登録", "追加"]:
        if word is None:
            await ctx.send("追加する単語を指定してください。\n例: `!フィルター 追加 レポート`")
            return

        # 大文字小文字・全角半角の違いだけの重複は登録しない
        if any(moodle.normalize_text(w) == moodle.normalize_text(word) for w in words):
            await ctx.send(f"「{word}」は既に登録されています。")
        else:
            database.add_filter_word(user_id, word)
            await ctx.send(f"フィルターに「**{word}**」を追加しました！✅")

    # 3. キーワード削除の場合
    elif action == "削除":
        if word is None:
            await ctx.send("削除する単語を指定してください。\n例: `!フィルター 削除 テスト`")
            return

        # 登録時の表記と大文字小文字が違っていても削除できる
        target = next(
            (w for w in words if moodle.normalize_text(w) == moodle.normalize_text(word)),
            None,
        )
        if target is not None:
            database.delete_filter_word(user_id, target)
            await ctx.send(f"フィルターから「**{target}**」を削除しました。❌")
        else:
            await ctx.send(f"「{word}」は登録されていません。")


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
            database.delete_user_url(user_id)  # フィルター・通知履歴なども削除される
            await ctx.send("🗑️ 登録されていたURLとフィルター設定を削除しました。")
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