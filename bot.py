import discord
from discord.ext import commands, tasks
from config import DISCORD_TOKEN
import database
import moodle

# DBから登録データをロード
user_urls, filter_words = database.init_db()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------
# 定期実行ループタスク（例: 60分ごとに自動チェック）
# --------------------------------------------------
@tasks.loop(minutes=3)
async def auto_check_moodle():
    for user_id, target_url in list(user_urls.items()):
        try:
            # ユーザーオブジェクトを取得してDMを送信
            user = await bot.fetch_user(user_id)
            if user:
                events = moodle.get_moodle_events(target_url, filter_words)
                message = "**【定期通知：Moodle 最新予定】**\n" + "\n".join(events)
                
                # 2000文字制限対策
                if len(message) > 2000:
                    await user.send(
                        "予定が多すぎるため、一部のみ表示します:\n"
                        + message[:1900]
                    )
                else:
                    await user.send(message)
                
        # DM受信拒否やBotがブロックされている場合のエラー
        except discord.Forbidden:
            print(
                f"ユーザー {user_id} はDM受信を許可していないか、Botをブロックしています。"
            )
        except Exception as e:
            print(f"ユーザー {user_id} への送信失敗: {e}")


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
@bot.command(name="moodle")
async def send_moodle_schedule(ctx):
    await ctx.send("Moodleから最新の予定を取得中...")
    # ユーザーIDを取得
    user_id = ctx.author.id
    # 登録されたURLを取得
    ics_url = user_urls.get(user_id)
    
    if not ics_url:
        await ctx.send("❌ MoodleのカレンダーURLが登録されていません。")
        return

    events = moodle.get_moodle_events(ics_url, filter_words)
    message = "**【Moodle 予定一覧】**\n" + "\n".join(events)
    
    # 2000文字制限対策を追加
    if len(message) > 2000:
        await ctx.send(
            "予定が多すぎるため、一部のみ表示します:\n" + message[:1900]
        )
    else:
        await ctx.send(message)

# フィルター管理コマンド
@bot.command(name="フィルター")
async def manage_filter(ctx, action: str = None, word: str = None):
    global filter_words

    # 1. 引数なしの場合：現在のフィルター一覧を表示
    if action is None:
        if not filter_words:
            await ctx.send("現在設定されているフィルターキーワードはありません。")
            return

        msg = "**【現在の通知フィルター設定】**\n"
        for w in filter_words:
            msg += f"・{w}：✅\n"
        msg += "\n※追加する場合: `!フィルター 追加 <単語>`\n※削除する場合: `!フィルター 削除 <単語>`"
        await ctx.send(msg)

    # 2. キーワード追加の場合
    elif action in ["登録", "追加"]:
        if word is None:
            await ctx.send("追加する単語を指定してください。\n例: `!フィルター 追加 レポート`")
            return
        
        if word in filter_words:
            await ctx.send(f"「{word}」は既に登録されています。")
        else:
            filter_words.append(word)
            database.add_filter_word(word)  # DBに追加
            await ctx.send(f"フィルターに「**{word}**」を追加しました！✅")

    # 3. キーワード削除の場合
    elif action == "削除":
        if word is None:
            await ctx.send("削除する単語を指定してください。\n例: `!フィルター 削除 テスト`")
            return
        
        if word in filter_words:
            filter_words.remove(word)
            database.delete_filter_word(word)  # DBから削除
            await ctx.send(f"フィルターから「**{word}**」を削除しました。❌")
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

        # URLをDBに保存
        user_urls[user_id] = url
        database.save_user_url(user_id, url)
        await ctx.send("✅ MoodleのカレンダーURLを登録しました！")

    # 3. URLの削除
    elif action == "削除":
        if user_id in user_urls:
            del user_urls[user_id]
            database.delete_user_url(user_id)
            await ctx.send("🗑️ 登録されていたURLを削除しました。")
        else:
            await ctx.send("登録されているURLはありません。")


bot.run(DISCORD_TOKEN)