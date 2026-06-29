import discord
import subprocess
import asyncio
import os
import json
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = os.getenv("DISCORD_CHANNEL_ID") or None
TMUX_SESSION = os.getenv("TMUX_SESSION", "0")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# thread_id(str) → window_index(int)
MAPPING_FILE = os.path.join(os.path.dirname(__file__), "thread_map.json")
thread_map: dict[str, int] = {}

watch_tasks: dict[int, asyncio.Task] = {}  # window_index → Task


# ── 永続化 ────────────────────────────────────────────────────

def load_map():
    global thread_map
    try:
        with open(MAPPING_FILE) as f:
            thread_map = {k: int(v) for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        thread_map = {}


def save_map():
    with open(MAPPING_FILE, "w") as f:
        json.dump(thread_map, f)


# ── tmux helpers ──────────────────────────────────────────────

def tmux_send(window: int, text: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", text, "Enter"],
        check=True,
    )


def tmux_capture(window: int) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{window}", "-p"],
        capture_output=True, text=True,
    )
    return result.stdout


def tmux_windows() -> list[dict]:
    result = subprocess.run(
        ["tmux", "list-windows", "-t", TMUX_SESSION, "-F",
         "#{window_index}|#{window_name}|#{pane_current_command}"],
        capture_output=True, text=True,
    )
    windows = []
    for line in result.stdout.strip().splitlines():
        idx, name, cmd = line.split("|")
        windows.append({"index": int(idx), "name": name, "command": cmd})
    return windows


def truncate(text: str, limit: int = 1800) -> str:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    if len(result) > limit:
        result = "...(省略)...\n" + result[-limit:]
    return result


async def wait_stable(window: int, timeout: int = 30) -> str:
    """出力が2秒変化しなくなるまで待つ"""
    prev = ""
    stable = 0
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        cur = tmux_capture(window)
        if cur == prev:
            stable += 1
            if stable >= 4:
                break
        else:
            stable = 0
            prev = cur
    return prev


# ── watch loop ────────────────────────────────────────────────

async def watch_loop(window: int, thread: discord.Thread):
    last = tmux_capture(window)
    while True:
        await asyncio.sleep(2)
        cur = tmux_capture(window)
        if cur != last:
            try:
                await thread.send(f"```\n{truncate(cur)}\n```")
            except Exception:
                pass
            last = cur


# ── .env save ─────────────────────────────────────────────────

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def save_env(key: str, value: str) -> None:
    lines = []
    found = False
    try:
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)


def is_main_channel(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if ALLOWED_CHANNEL and str(message.channel.id) != ALLOWED_CHANNEL:
        return False
    return True


def get_thread_window(message: discord.Message) -> int | None:
    """メッセージがスレッド内なら対応ウィンドウ番号を返す"""
    if not isinstance(message.channel, discord.Thread):
        return None
    return thread_map.get(str(message.channel.id))


# ── events ───────────────────────────────────────────────────

@client.event
async def on_ready():
    load_map()
    print(f"[tmux-discord] Logged in as {client.user} | {len(thread_map)} thread(s) mapped", flush=True)


@client.event
async def on_message(message: discord.Message):
    global ALLOWED_CHANNEL

    if message.author.bot:
        return

    # ── スレッド内メッセージ ──────────────────────────────────
    window = get_thread_window(message)
    if window is not None:
        content = message.content.strip()

        if content == "!cap":
            out = truncate(tmux_capture(window))
            await message.reply(f"```\n{out}\n```")
            return

        if content == "!watch":
            if window in watch_tasks and not watch_tasks[window].done():
                await message.reply("すでに監視中です。`!unwatch` で停止。")
                return
            watch_tasks[window] = asyncio.create_task(
                watch_loop(window, message.channel)
            )
            await message.reply(f"ウィンドウ {window} の監視を開始しました。")
            return

        if content == "!unwatch":
            t = watch_tasks.pop(window, None)
            if t:
                t.cancel()
                await message.reply("監視停止しました。")
            else:
                await message.reply("監視は動いていません。")
            return

        if content.startswith("!"):
            return  # 未知のコマンドは無視

        # 通常テキスト → tmuxウィンドウへ送信
        try:
            tmux_send(window, content)
        except subprocess.CalledProcessError as e:
            await message.reply(f"エラー: {e}")
            return
        thinking = await message.reply("⏳")
        out = await wait_stable(window)
        await thinking.edit(content=f"```\n{truncate(out)}\n```")
        return

    # ── メインチャンネル内メッセージ ─────────────────────────
    if not is_main_channel(message):
        return

    content = message.content.strip()

    # !setchannel
    if content == "!setchannel":
        ALLOWED_CHANNEL = str(message.channel.id)
        save_env("DISCORD_CHANNEL_ID", ALLOWED_CHANNEL)
        await message.reply(f"このチャンネルに固定しました。")
        return

    # !init : tmuxウィンドウごとにスレッドを作成
    if content == "!init":
        try:
            wins = tmux_windows()
        except Exception as e:
            await message.reply(f"tmuxエラー: {e}")
            return

        created = []
        for w in wins:
            name = f"w{w['index']} • {w['name']} [{w['command']}]"
            # 既存スレッドがあればスキップ
            existing = next(
                (tid for tid, idx in thread_map.items() if idx == w["index"]), None
            )
            if existing:
                created.append(f"w{w['index']}: 既存スレッドあり")
                continue
            thread = await message.channel.create_thread(
                name=name[:100],
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
            )
            thread_map[str(thread.id)] = w["index"]
            await thread.send(
                f"**ウィンドウ {w['index']} • `{w['command']}`** に接続しました。\n"
                f"このスレッドに書くとウィンドウに送信されます。\n"
                f"`!cap` = 現在画面  `!watch` / `!unwatch` = 自動監視"
            )
            created.append(f"w{w['index']}: #{thread.name}")

        save_map()
        await message.reply("スレッド作成完了:\n" + "\n".join(created))
        return

    # !refresh : ウィンドウ一覧表示
    if content in ("!windows", "!refresh"):
        try:
            wins = tmux_windows()
        except Exception as e:
            await message.reply(f"エラー: {e}")
            return
        lines = []
        for w in wins:
            tid = next((t for t, i in thread_map.items() if i == w["index"]), None)
            link = f"<#{tid}>" if tid else "（スレッドなし）"
            lines.append(f"[{w['index']}] {w['name']} ({w['command']}) → {link}")
        await message.reply("\n".join(lines))
        return

    # !ai
    if content.startswith("!ai "):
        if not ANTHROPIC_API_KEY:
            await message.reply("ANTHROPIC_API_KEY が未設定です。")
            return
        prompt = content[4:].strip()
        thinking = await message.reply("⏳")
        try:
            import anthropic
            ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = ac.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = resp.content[0].text
            for i in range(0, len(answer), 1900):
                chunk = answer[i:i+1900]
                if i == 0:
                    await thinking.edit(content=chunk)
                else:
                    await message.channel.send(chunk)
        except Exception as e:
            await thinking.edit(content=f"エラー: {e}")
        return

    # メインチャンネルでの !<cmd>（後方互換）
    if content.startswith("!") and not content.startswith("!!"):
        cmd = content[1:].strip()
        if not cmd:
            return
        await message.reply(
            "`!init` でウィンドウごとのスレッドを作ってください。\n"
            "スレッド内でコマンドを入力するとtmuxに送られます。"
        )
        return


@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[error] {event}", flush=True)
    traceback.print_exc()


client.run(TOKEN)
