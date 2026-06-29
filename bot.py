import discord
import subprocess
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = os.getenv("DISCORD_CHANNEL_ID")  # 空なら全チャンネル許可
TMUX_SESSION = os.getenv("TMUX_SESSION", "0")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# デフォルトウィンドウ（!sw で切り替え）
active_window = 0


def tmux_send(window: int, cmd: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", cmd, "Enter"],
        check=True,
    )


def tmux_capture(window: int) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{window}", "-p"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def tmux_windows() -> list[dict]:
    result = subprocess.run(
        [
            "tmux",
            "list-windows",
            "-t",
            TMUX_SESSION,
            "-F",
            "#{window_index}|#{window_name}|#{window_active}|#{pane_current_command}",
        ],
        capture_output=True,
        text=True,
    )
    windows = []
    for line in result.stdout.strip().splitlines():
        idx, name, active, cmd = line.split("|")
        windows.append(
            {
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "command": cmd,
            }
        )
    return windows


def is_allowed(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if ALLOWED_CHANNEL and str(message.channel.id) != ALLOWED_CHANNEL:
        return False
    return True


def truncate(text: str, limit: int = 1800) -> str:
    lines = text.splitlines()
    # 空行だらけの末尾を削る
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    if len(result) > limit:
        result = "...(省略)...\n" + result[-limit:]
    return result


@client.event
async def on_ready():
    print(f"[tmux-discord] Logged in as {client.user}")
    if ALLOWED_CHANNEL:
        print(f"[tmux-discord] Restricted to channel {ALLOWED_CHANNEL}")


@client.event
async def on_message(message: discord.Message):
    global active_window

    if not is_allowed(message):
        return

    content = message.content.strip()

    # ── !windows : ウィンドウ一覧 ─────────────────────────────
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

        body = "\n".join(lines)
        await message.reply(f"```\n{body}\n```\n▶=選択中  ★=tmux上でアクティブ")
        return

    # ── !sw <n> : アクティブウィンドウ切り替え ───────────────
    if content.startswith("!sw "):
        try:
            n = int(content[4:].strip())
            active_window = n
            await message.reply(f"ウィンドウ {n} に切り替えました")
        except ValueError:
            await message.reply("使い方: `!sw <番号>`")
        return

    # ── !cap : 現在ウィンドウのスナップショット ───────────────
    if content in ("!cap", "!capture"):
        out = truncate(tmux_capture(active_window))
        await message.reply(f"```\n{out}\n```")
        return

    # ── !cap <n> : 指定ウィンドウのスナップショット ──────────
    if content.startswith("!cap "):
        try:
            n = int(content[5:].strip())
            out = truncate(tmux_capture(n))
            await message.reply(f"```\n[window {n}]\n{out}\n```")
        except (ValueError, subprocess.CalledProcessError) as e:
            await message.reply(f"エラー: {e}")
        return

    # ── !w<n> <cmd> : 指定ウィンドウでコマンド実行 ──────────
    if content.startswith("!w") and len(content) > 2 and content[2].isdigit():
        rest = content[2:]
        space = rest.find(" ")
        if space == -1:
            # コマンドなし → キャプチャだけ
            n = int(rest)
            out = truncate(tmux_capture(n))
            await message.reply(f"```\n[window {n}]\n{out}\n```")
            return
        try:
            n = int(rest[:space])
            cmd = rest[space + 1 :]
        except ValueError:
            return

        try:
            tmux_send(n, cmd)
        except subprocess.CalledProcessError as e:
            await message.reply(f"エラー: {e}")
            return

        await asyncio.sleep(0.8)
        out = truncate(tmux_capture(n))
        await message.reply(f"```\n[window {n}] $ {cmd}\n{out}\n```")
        return

    # ── !<cmd> : アクティブウィンドウでコマンド実行 ──────────
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
        await message.reply(f"```\n[window {active_window}] $ {cmd}\n{out}\n```")
        return


client.run(TOKEN)
