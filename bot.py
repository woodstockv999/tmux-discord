import discord
import subprocess
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = os.getenv("DISCORD_CHANNEL_ID") or None
TMUX_SESSION = os.getenv("TMUX_SESSION", "0")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

active_window = 0
watch_task: asyncio.Task | None = None
watch_channel: discord.TextChannel | None = None


# ── tmux helpers ──────────────────────────────────────────────

def tmux_send(window: int, text: str, enter: bool = True) -> None:
    keys = [text, "Enter"] if enter else [text]
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}"] + keys,
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
         "#{window_index}|#{window_name}|#{window_active}|#{pane_current_command}"],
        capture_output=True, text=True,
    )
    windows = []
    for line in result.stdout.strip().splitlines():
        idx, name, active, cmd = line.split("|")
        windows.append({"index": int(idx), "name": name, "active": active == "1", "command": cmd})
    return windows


# ── output helpers ────────────────────────────────────────────

def truncate(text: str, limit: int = 1800) -> str:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    if len(result) > limit:
        result = "...(省略)...\n" + result[-limit:]
    return result


async def wait_for_stable_output(window: int, timeout: int = 30) -> str:
    """出力が2秒間変化しなくなるまで待つ（Claude Code等の長い応答対応）"""
    prev = ""
    stable_count = 0
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        current = tmux_capture(window)
        if current == prev:
            stable_count += 1
            if stable_count >= 4:  # 2秒間変化なし
                break
        else:
            stable_count = 0
            prev = current
    return prev


# ── watch loop ────────────────────────────────────────────────

async def watch_loop(window: int, interval: int = 2):
    last = tmux_capture(window)
    while True:
        await asyncio.sleep(interval)
        current = tmux_capture(window)
        if current != last and watch_channel:
            diff = truncate(current)
            try:
                await watch_channel.send(f"```\n[w{window}]\n{diff}\n```")
            except Exception:
                pass
            last = current


# ── .env helpers ──────────────────────────────────────────────

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


def is_allowed(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if ALLOWED_CHANNEL and str(message.channel.id) != ALLOWED_CHANNEL:
        return False
    return True


# ── events ───────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"[tmux-discord] Logged in as {client.user}", flush=True)
    if ALLOWED_CHANNEL:
        print(f"[tmux-discord] Restricted to channel {ALLOWED_CHANNEL}", flush=True)


@client.event
async def on_message(message: discord.Message):
    global active_window, watch_task, watch_channel, ALLOWED_CHANNEL

    print(f"[msg] {message.author} ch={message.channel.id} content={repr(message.content[:80])}", flush=True)

    if not is_allowed(message):
        return

    content = message.content.strip()

    # ── >テキスト : アクティブウィンドウに直接送信（Claude Code等） ──
    if content.startswith(">") and not content.startswith(">>"):
        text = content[1:].strip()
        if not text:
            return
        try:
            tmux_send(active_window, text)
        except subprocess.CalledProcessError as e:
            await message.reply(f"エラー: {e}")
            return
        # 出力が安定するまで最大30秒待つ
        thinking = await message.reply("⏳ 応答待ち...")
        out = await wait_for_stable_output(active_window)
        await thinking.edit(content=f"```\n[w{active_window}]\n{truncate(out)}\n```")
        return

    # ── !setchannel ───────────────────────────────────────────
    if content == "!setchannel":
        ALLOWED_CHANNEL = str(message.channel.id)
        save_env("DISCORD_CHANNEL_ID", ALLOWED_CHANNEL)
        await message.reply(f"このチャンネル（{message.channel.id}）に固定しました。")
        return

    # ── !windows / !w ─────────────────────────────────────────
    if content in ("!windows", "!w"):
        try:
            wins = tmux_windows()
        except Exception as e:
            await message.reply(f"エラー: {e}")
            return
        lines = []
        for w in wins:
            mark = "▶" if w["index"] == active_window else " "
            star = "★" if w["active"] else " "
            lines.append(f"{mark}{star} [{w['index']}] {w['name']}  ({w['command']})")
        await message.reply(f"```\n{chr(10).join(lines)}\n```\n▶=選択中  ★=tmuxアクティブ")
        return

    # ── !sw <n> : ウィンドウ切り替え ─────────────────────────
    if content.startswith("!sw "):
        try:
            n = int(content[4:].strip())
            active_window = n
            wins = tmux_windows()
            w = next((x for x in wins if x["index"] == n), None)
            label = f"  ({w['command']})" if w else ""
            await message.reply(f"ウィンドウ {n}{label} に切り替えました")
        except ValueError:
            await message.reply("使い方: `!sw <番号>`")
        return

    # ── !watch [n] : 自動監視開始 ─────────────────────────────
    if content.startswith("!watch"):
        if watch_task and not watch_task.done():
            await message.reply("すでに監視中です。`!unwatch` で止められます。")
            return
        try:
            n = int(content[6:].strip()) if content[6:].strip() else active_window
        except ValueError:
            n = active_window
        watch_channel = message.channel
        watch_task = asyncio.create_task(watch_loop(n))
        await message.reply(f"ウィンドウ {n} の監視を開始しました。変化があると自動投稿します。`!unwatch` で停止。")
        return

    # ── !unwatch : 監視停止 ───────────────────────────────────
    if content == "!unwatch":
        if watch_task and not watch_task.done():
            watch_task.cancel()
            watch_task = None
            await message.reply("監視を停止しました。")
        else:
            await message.reply("監視は動いていません。")
        return

    # ── !cap / !cap <n> ───────────────────────────────────────
    if content.startswith("!cap"):
        rest = content[4:].strip()
        n = int(rest) if rest.isdigit() else active_window
        out = truncate(tmux_capture(n))
        await message.reply(f"```\n[w{n}]\n{out}\n```")
        return

    # ── !ai <テキスト> : Claude APIに質問 ────────────────────
    if content.startswith("!ai "):
        if not ANTHROPIC_API_KEY:
            await message.reply("ANTHROPIC_API_KEY が .env に設定されていません。")
            return
        prompt = content[4:].strip()
        thinking = await message.reply("⏳ 考え中...")
        try:
            import anthropic
            ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = ac.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = resp.content[0].text
            # 2000字制限対応
            for i in range(0, len(answer), 1900):
                chunk = answer[i:i+1900]
                if i == 0:
                    await thinking.edit(content=chunk)
                else:
                    await message.channel.send(chunk)
        except Exception as e:
            await thinking.edit(content=f"エラー: {e}")
        return

    # ── !w<n> <cmd> : 指定ウィンドウで実行 ───────────────────
    if content.startswith("!w") and len(content) > 2 and content[2].isdigit():
        rest = content[2:]
        space = rest.find(" ")
        if space == -1:
            n = int(rest)
            out = truncate(tmux_capture(n))
            await message.reply(f"```\n[w{n}]\n{out}\n```")
            return
        try:
            n = int(rest[:space])
            cmd = rest[space + 1:]
        except ValueError:
            return
        try:
            tmux_send(n, cmd)
        except subprocess.CalledProcessError as e:
            await message.reply(f"エラー: {e}")
            return
        await asyncio.sleep(0.8)
        out = truncate(tmux_capture(n))
        await message.reply(f"```\n[w{n}] $ {cmd}\n{out}\n```")
        return

    # ── !<cmd> : アクティブウィンドウで実行 ──────────────────
    if content.startswith("!") and not content.startswith("!!"):
        cmd = content[1:].strip()
        if not cmd:
            return
        try:
            tmux_send(active_window, cmd)
        except subprocess.CalledProcessError as e:
            await message.reply(f"エラー: {e}")
            return
        await asyncio.sleep(0.8)
        out = truncate(tmux_capture(active_window))
        await message.reply(f"```\n[w{active_window}] $ {cmd}\n{out}\n```")
        return


@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[error] event={event}", flush=True)
    traceback.print_exc()


client.run(TOKEN)
