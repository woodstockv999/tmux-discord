import discord
import subprocess
import asyncio
import os
import json
import re
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = os.getenv("DISCORD_CHANNEL_ID") or None
TMUX_SESSION = os.getenv("TMUX_SESSION", "0")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thread_map.json")
thread_map: dict[str, int] = {}
watch_tasks: dict[int, asyncio.Task] = {}

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# Claude Code のツール呼び出し行にマッチ（⏺ 等のアイコン、もしくは "ToolName(" で始まる行）
TOOL_RE = re.compile(r'[⏺✓✗⚡◆▶]|^\s*(?:Bash|Read|Edit|Write|Search|Glob|Task|Agent|WebFetch|WebSearch)\(')
# シェル/Claude Code プロンプト行
PROMPT_RE = re.compile(r'^\s*[>$#%❯]\s*$')


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
    """テキストをtmuxペインに送信し、Enterを確実に押す"""
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "--", text],
        check=True,
    )
    # Enterを別コマンドで送ることで送信を確実にする
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"],
        check=True,
    )


def tmux_capture(window: int, scrollback: int = 0) -> str:
    cmd = ["tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{window}", "-p"]
    if scrollback:
        cmd += ["-S", f"-{scrollback}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


# ── テキスト処理 ──────────────────────────────────────────────

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


def truncate(text: str, limit: int = 1800) -> str:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    if len(result) > limit:
        result = "...(省略)...\n" + result[-limit:]
    return result


def ends_with_prompt(raw: str) -> bool:
    """ANSIを除去後、末尾の非空行がプロンプト行か判定"""
    lines = strip_ansi(raw).splitlines()
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return False
    last = non_empty[-1].strip()
    return bool(PROMPT_RE.match(last))


def extract_final_result(raw: str) -> str:
    """
    tmuxキャプチャからClaude Codeの最終応答テキストのみを抽出。
    - ANSI除去
    - 末尾プロンプト行を除去
    - 下から走査し、ツール呼び出し行に当たったらそこで打ち切り
    """
    clean = strip_ansi(raw)
    lines = clean.splitlines()

    # 末尾のプロンプト行・空行を除去
    while lines and (not lines[-1].strip() or PROMPT_RE.match(lines[-1].strip())):
        lines.pop()

    if not lines:
        return ""

    # 下から走査して最後の「ツール非呼び出し」ブロックを取得
    result_lines: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if TOOL_RE.search(stripped) or PROMPT_RE.match(stripped):
            if result_lines:
                break  # ツール行を超えた → ここまでが最終ブロック
        else:
            result_lines.insert(0, line)

    # 先頭の空行を除去
    while result_lines and not result_lines[0].strip():
        result_lines.pop(0)

    result = "\n".join(result_lines).strip()

    # 抽出できなければ全体をフォールバック
    if not result:
        fallback = "\n".join(lines).strip()
        return truncate(fallback)

    if len(result) > 1800:
        result = "...(省略)...\n" + result[-1800:]
    return result


# ── 完了待機 ──────────────────────────────────────────────────

async def wait_for_completion(window: int, timeout: int = 300) -> str:
    """
    tmuxペインの出力が安定 かつ プロンプト行で終わるまで待機。
    timeout秒経過したら最後のキャプチャを返す。
    """
    # コマンドが処理開始するまで少し待つ
    await asyncio.sleep(1.0)

    prev = ""
    stable = 0

    for _ in range(timeout * 2):  # 0.5秒ごとにポーリング
        await asyncio.sleep(0.5)
        cur = tmux_capture(window, scrollback=100)

        if cur == prev:
            stable += 1
        else:
            stable = 0
            prev = cur

        # 3秒間変化なし + プロンプト行で終わっていれば完了
        if stable >= 6 and ends_with_prompt(cur):
            return cur

    return prev


# ── watch loop ────────────────────────────────────────────────

async def watch_loop(window: int, thread: discord.Thread):
    last = tmux_capture(window)
    while True:
        await asyncio.sleep(2)
        cur = tmux_capture(window)
        if cur != last:
            try:
                await thread.send(f"```\n{truncate(strip_ansi(cur))}\n```")
            except Exception:
                pass
            last = cur


# ── .env save ─────────────────────────────────────────────────

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


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


# ── helpers ───────────────────────────────────────────────────

def is_main_channel(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if ALLOWED_CHANNEL and str(message.channel.id) != ALLOWED_CHANNEL:
        return False
    return True


def get_thread_window(message: discord.Message) -> int | None:
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

    print(f"[msg] {message.author} ch={message.channel.id} type={type(message.channel).__name__} content={repr(message.content[:60])}", flush=True)

    # ── スレッド内メッセージ ──────────────────────────────────
    window = get_thread_window(message)
    if window is not None:
        content = message.content.strip()

        if content == "!enter":
            subprocess.run(["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"])
            await asyncio.sleep(0.5)
            out = truncate(strip_ansi(tmux_capture(window)))
            await message.reply(f"```\n{out}\n```")
            return

        if content.startswith("!key "):
            key = content[5:].strip()
            subprocess.run(["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", key])
            await asyncio.sleep(0.5)
            out = truncate(strip_ansi(tmux_capture(window)))
            await message.reply(f"```\n{out}\n```")
            return

        if content == "!cap":
            out = truncate(strip_ansi(tmux_capture(window, scrollback=50)))
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
            return

        # ── 通常テキスト: tmuxへ送信 ──────────────────────────
        try:
            tmux_send(window, content)
        except subprocess.CalledProcessError as e:
            await message.reply(f"tmux送信エラー: {e}")
            return

        # tmuxが受信確認できたら ⌛️ を新規送信
        await message.channel.send("⌛️")

        # 処理完了まで待機
        raw = await wait_for_completion(window)

        # 最終結果のみ抽出して返信
        result = extract_final_result(raw)
        if result:
            await message.reply(f"```\n{result}\n```")
        else:
            await message.reply("（出力なし）")
        return

    # ── メインチャンネル内メッセージ ─────────────────────────
    if not is_main_channel(message):
        return

    content = message.content.strip()

    if content == "!setchannel":
        ALLOWED_CHANNEL = str(message.channel.id)
        save_env("DISCORD_CHANNEL_ID", ALLOWED_CHANNEL)
        await message.reply("このチャンネルに固定しました。")
        return

    if content == "!init":
        try:
            wins = tmux_windows()
        except Exception as e:
            await message.reply(f"tmuxエラー: {e}")
            return

        created = []
        for w in wins:
            name = f"w{w['index']} • {w['name']} [{w['command']}]"
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
            await thinking.edit(content=answer[:1900])
            for i in range(1900, len(answer), 1900):
                await message.channel.send(answer[i:i + 1900])
        except Exception as e:
            await thinking.edit(content=f"エラー: {e}")
        return

    if content.startswith("!") and not content.startswith("!!"):
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
