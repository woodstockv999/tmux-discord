import discord
import subprocess
import asyncio
import os
import json
import re
import glob
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
# ウィンドウごとのメッセージキュー（同一ウィンドウは順番に処理）
window_queues: dict[int, asyncio.Queue] = {}
window_workers: dict[int, asyncio.Task] = {}

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# Claude Code のツール呼び出し行にマッチ（⏺ 等のアイコン、もしくは "ToolName(" で始まる行）
TOOL_RE = re.compile(r'[⏺✓✗⚡◆▶]|^\s*(?:Bash|Read|Edit|Write|Search|Glob|Task|Agent|WebFetch|WebSearch)\(')
# シェルプロンプト行（Claude Code の ❯ および bash の user@host:path$ 形式）
PROMPT_RE = re.compile(r'^\s*[>$#%❯]\s*$|.+[#$]\s*$')
# Claude Code が処理中であることを示すパターン
CLAUDE_BUSY_RE = re.compile(r'Undulating|Working|Running|Thinking|\d+s\s*·|⎿\s*\$')
# Claude Code UI の装飾要素（区切り線・ステータスバー）
CHROME_RE = re.compile(r'^[─━═╌╍┈┉\s]+$|⏵⏵|⏺⏺')
# Claude Code セッション評価フィードバックプロンプト
FEEDBACK_RE = re.compile(r'How is Claude doing this session|\d+:\s*(?:Bad|Fine|Good|Dismiss)')


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

def _tmux_send_sync(window: int, text: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "--", text],
        check=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"],
        check=True,
    )


def _tmux_capture_sync(window: int, scrollback: int = 0) -> str:
    cmd = ["tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{window}", "-p"]
    if scrollback:
        cmd += ["-S", f"-{scrollback}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


async def tmux_send(window: int, text: str) -> None:
    """イベントループをブロックしない tmux 送信"""
    await asyncio.to_thread(_tmux_send_sync, window, text)


async def tmux_capture(window: int, scrollback: int = 0) -> str:
    """イベントループをブロックしない tmux キャプチャ"""
    return await asyncio.to_thread(_tmux_capture_sync, window, scrollback)


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


def split_chunks(text: str, chunk_size: int = 1800) -> list[str]:
    """テキストを行単位で chunk_size 以下のチャンクに分割する"""
    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > chunk_size and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def is_idle(raw: str) -> bool:
    """
    Claude Code / シェルがアイドル状態か判定。
    処理中サイン（タイマー等）がなく、末尾10行以内に ❯ 単独行または
    bash プロンプト行があればアイドルとみなす。
    ステータスバー（⏵⏵ bypass...）が最終行に来るため末尾N行で判定する。
    """
    clean = strip_ansi(raw)
    if CLAUDE_BUSY_RE.search(clean):
        return False
    lines = [l for l in clean.splitlines() if l.strip()]
    if not lines:
        return False
    for line in lines[-10:]:
        if re.search(r'^\s*[❯>$#%]\s*$|[#$]\s*$', line):
            return True
    return False


def extract_final_result(raw: str) -> str:
    """
    tmuxキャプチャからClaude Codeの最終応答テキストのみを抽出。
    ● で始まる応答ブロックを前向きスキャンし、最後のブロックを返す。
    ● がなければ bash 出力として旧ロジックにフォールバック。
    """
    clean = strip_ansi(raw)
    lines = clean.splitlines()

    # Claude Code モード: ● 応答ブロックを全収集し、フィードバックプロンプトを除外して最後を返す
    blocks: list[list[str]] = []
    current_block: list[str] = []
    in_response = False
    for line in lines:
        s = line.strip()
        if s.startswith('●'):
            if current_block:
                blocks.append(current_block)
            current_block = [s[1:].strip()]
            in_response = True
        elif in_response:
            if s.startswith(('✻', '⏺', '❯', '⎿')) or CHROME_RE.search(line):
                in_response = False
            else:
                current_block.append(line.rstrip())
    if current_block:
        blocks.append(current_block)

    # フィードバックプロンプトブロックを除外
    blocks = [b for b in blocks if not FEEDBACK_RE.search('\n'.join(b))]

    if blocks:
        result_lines = blocks[-1]
        while result_lines and not result_lines[-1].strip():
            result_lines.pop()
        result = '\n'.join(result_lines).strip()
        if result:
            return result  # 呼び出し側で split_chunks するので truncate しない

    # ● がスクロールアウトしている場合: 末尾から ✻/❯ で区切られた
    # 直近の応答テキストブロックを逆走査で取得
    filtered = []
    for line in lines:
        s = line.strip()
        if CHROME_RE.search(line) or PROMPT_RE.match(s) or FEEDBACK_RE.search(s):
            continue
        if s.startswith(('✻', '⏺', '❯', '⎿', '●')):
            filtered.append(None)  # ブロック区切りマーカー
        else:
            filtered.append(line)

    # 末尾の空行・マーカーを除去
    while filtered and (filtered[-1] is None or not (filtered[-1] or '').strip()):
        filtered.pop()

    # マーカーで分割し最後のブロックを取得
    last_block: list[str] = []
    for item in reversed(filtered):
        if item is None:
            if last_block:
                break
        else:
            last_block.insert(0, item)

    while last_block and not last_block[0].strip():
        last_block.pop(0)

    result = "\n".join(last_block).strip()
    return result if result else ""


# ── Claude JSONL 読み取り ──────────────────────────────────────

def _get_pane_cwd_sync(window: int) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-t", f"{TMUX_SESSION}:{window}", "-p", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _jsonl_dir_for_cwd(cwd: str) -> str:
    project = cwd.replace('/', '-')  # /home/w00dst0ck → -home-w00dst0ck
    return os.path.expanduser(f"~/.claude/projects/{project}")


def _latest_jsonl_state(directory: str) -> tuple[str, int] | None:
    """最近変更された JSONL ファイルのパスと現在サイズを返す。"""
    try:
        files = glob.glob(os.path.join(directory, "*.jsonl"))
        if not files:
            return None
        path = max(files, key=os.path.getmtime)
        return (path, os.path.getsize(path))
    except Exception:
        return None


def _read_assistant_text_since(jsonl_path: str, offset: int) -> str:
    """JSONL の offset 以降から最後の assistant テキストブロックを返す。"""
    try:
        with open(jsonl_path, 'rb') as f:
            f.seek(offset)
            new_content = f.read().decode('utf-8', errors='replace')
        last_text = ""
        for line in new_content.splitlines():
            try:
                obj = json.loads(line)
                if obj.get('type') == 'assistant':
                    for block in obj.get('message', {}).get('content', []):
                        if isinstance(block, dict) and block.get('type') == 'text':
                            text = block.get('text', '').strip()
                            if text and not FEEDBACK_RE.search(text):
                                last_text = text
            except Exception:
                pass
        return last_text
    except Exception:
        return ""


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
        cur = await tmux_capture(window, scrollback=100)

        if cur == prev:
            stable += 1
        else:
            stable = 0
            prev = cur

        # アイドル判定は現在画面のみ（scrollback の古い ⎿  $ を除外するため）
        if stable >= 6 and is_idle(await tmux_capture(window)):
            return cur

    return prev


# ── window worker（1ウィンドウ1件ずつ順番に処理）─────────────

async def _window_worker(window: int) -> None:
    q = window_queues[window]
    while not q.empty():
        message, content = await q.get()
        print(f"[worker] win={window} dequeue: {repr(content[:40])}", flush=True)
        try:
            await tmux_send(window, content)
        except subprocess.CalledProcessError as e:
            print(f"[worker] win={window} send_error: {e}", flush=True)
            try:
                await message.reply(f"tmux送信エラー: {e}")
            except Exception:
                pass
            continue

        try:
            # 送信前に JSONL ファイルの状態を記録
            cwd = await asyncio.to_thread(_get_pane_cwd_sync, window)
            jsonl_dir = _jsonl_dir_for_cwd(cwd)
            jsonl_state = await asyncio.to_thread(_latest_jsonl_state, jsonl_dir)
            print(f"[worker] win={window} jsonl_dir={jsonl_dir} state={jsonl_state}", flush=True)

            await wait_for_completion(window)

            # まず JSONL から応答を取得（capture-pane より確実）
            result = ""
            if jsonl_state:
                jsonl_path, offset = jsonl_state
                # セッションが変わった場合は最新の JSONL を探す
                new_state = await asyncio.to_thread(_latest_jsonl_state, jsonl_dir)
                if new_state:
                    new_path, new_size = new_state
                    read_path = new_path
                    read_offset = offset if new_path == jsonl_path else 0
                    if new_size > read_offset:
                        result = await asyncio.to_thread(_read_assistant_text_since, read_path, read_offset)
                        print(f"[worker] win={window} jsonl result {len(result)} chars", flush=True)

            # JSONL で取れなければ capture-pane フォールバック
            if not result:
                raw = await tmux_capture(window, scrollback=1000)
                print(f"[worker] win={window} captured {len(raw)} chars (fallback)", flush=True)
                result = extract_final_result(raw)
                print(f"[worker] win={window} fallback result {len(result)} chars: {repr(result[:60])}", flush=True)

            if result:
                chunks = split_chunks(result)
                print(f"[worker] win={window} sending {len(chunks)} chunk(s)", flush=True)
                await message.reply(f"```\n{chunks[0]}\n```")
                for chunk in chunks[1:]:
                    await message.channel.send(f"```\n{chunk}\n```")
            else:
                await message.reply("（出力なし）")
        except Exception as e:
            print(f"[worker] win={window} exception: {e}", flush=True)
            import traceback; traceback.print_exc()
            try:
                await message.reply(f"エラー: {e}")
            except Exception:
                pass


# ── watch loop ────────────────────────────────────────────────

async def watch_loop(window: int, thread: discord.Thread):
    last = await tmux_capture(window)
    while True:
        await asyncio.sleep(2)
        cur = await tmux_capture(window)
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
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"])
            await asyncio.sleep(0.5)
            out = truncate(strip_ansi(await tmux_capture(window)))
            await message.reply(f"```\n{out}\n```")
            return

        if content.startswith("!key "):
            key = content[5:].strip()
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", key])
            await asyncio.sleep(0.5)
            out = truncate(strip_ansi(await tmux_capture(window)))
            await message.reply(f"```\n{out}\n```")
            return

        if content == "!cap":
            out = truncate(strip_ansi(await tmux_capture(window, scrollback=50)))
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

        # ── 通常テキスト: キューに追加して順番に処理 ──────────
        await message.channel.send("⌛️")
        q = window_queues.setdefault(window, asyncio.Queue())
        await q.put((message, content))

        # ワーカーがなければ起動
        if window not in window_workers or window_workers[window].done():
            window_workers[window] = asyncio.create_task(_window_worker(window))
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
