import discord
import subprocess
import asyncio
import os
import json
import re
import time
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = os.getenv("DISCORD_CHANNEL_ID") or None
TMUX_SESSION = os.getenv("TMUX_SESSION", "0")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None

# タイムアウト（秒）
CLAUDE_TIMEOUT = 1800   # Claude Code の1ターン待機上限
SHELL_TIMEOUT = 120     # シェルコマンドの待機上限
STATUS_INTERVAL = 120   # 実行中ステータスの更新間隔

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thread_map.json")
thread_map: dict[str, int] = {}
watch_tasks: dict[int, asyncio.Task] = {}
# ウィンドウごとのメッセージキュー（同一ウィンドウは順番に処理）
window_queues: dict[int, asyncio.Queue] = {}
window_workers: dict[int, asyncio.Task] = {}
# ウィンドウ → バインド済み Claude セッション JSONL パス（Claude再起動まで再利用）
window_jsonl: dict[int, str] = {}

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# Claude Code 実行中サイン: 経過タイマー付きステータス行 "(2m 33s · ..." または "esc to interrupt"。
# スピナーの動詞はランダム語彙（Boogieing 等）なので単語列挙では検出できない。
CLAUDE_BUSY_RE = re.compile(r'\(\s*(?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*·|esc to interrupt')
# Claude Code セッション評価フィードバックプロンプト
FEEDBACK_RE = re.compile(r'How is Claude doing this session|\d+:\s*(?:Bad|Fine|Good|Dismiss)')
# シェルとして扱う pane_current_command
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish", "dash"}


async def safe_reply(message: discord.Message, content: str, mention: bool = False) -> discord.Message:
    """system message への reply は Discord が拒否するので channel.send にフォールバック。
    mention=True で投稿者にプッシュ通知が届く（編集やメンションなし投稿では通知されない）"""
    try:
        return await message.reply(content, mention_author=mention)
    except discord.HTTPException as e:
        if e.code == 50035:
            prefix = f"{message.author.mention} " if mention else ""
            return await message.channel.send(prefix + content)
        raise


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


def _pane_info_sync(window: int) -> tuple[str, str]:
    """(pane_current_command, pane_current_path) を返す。
    display-message は非印字文字を \\037 等にエスケープして出力するため、
    \\x1f 区切りの一括取得は使えない（分割に失敗して ("","") になる）。個別に取得する。"""
    def q(fmt: str) -> str:
        r = subprocess.run(
            ["tmux", "display-message", "-t", f"{TMUX_SESSION}:{window}", "-p", fmt],
            capture_output=True, text=True,
        )
        return r.stdout.strip()
    return q("#{pane_current_command}"), q("#{pane_current_path}")


async def tmux_send(window: int, text: str) -> None:
    await asyncio.to_thread(_tmux_send_sync, window, text)


async def tmux_capture(window: int, scrollback: int = 0) -> str:
    return await asyncio.to_thread(_tmux_capture_sync, window, scrollback)


async def pane_info(window: int) -> tuple[str, str]:
    return await asyncio.to_thread(_pane_info_sync, window)


def tmux_windows() -> list[dict]:
    # 非印字文字の区切りは tmux が "\037" 等にエスケープしてしまうため使えない。
    # index と command は空白を含まないので、空白区切り+名前を最後（maxsplit）にする
    result = subprocess.run(
        ["tmux", "list-windows", "-t", TMUX_SESSION, "-F",
         "#{window_index} #{pane_current_command} #{window_name}"],
        capture_output=True, text=True,
    )
    windows = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        idx, cmd = parts[0], parts[1]
        name = parts[2] if len(parts) > 2 else ""
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
    """テキストを行単位で chunk_size 以下のチャンクに分割する。
    1行が chunk_size を超える場合はその行自体を強制分割する。"""
    lines: list[str] = []
    for line in text.splitlines():
        while len(line) > chunk_size:
            lines.append(line[:chunk_size])
            line = line[chunk_size:]
        lines.append(line)
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


def is_claude_busy(raw: str) -> bool:
    return bool(CLAUDE_BUSY_RE.search(strip_ansi(raw)))


def is_shell_idle(raw: str) -> bool:
    """シェルペインがプロンプトで止まっているか（末尾の非空行がプロンプト形状）"""
    lines = [l.rstrip() for l in strip_ansi(raw).splitlines() if l.strip()]
    if not lines:
        return False
    # % は zsh プロンプト用だが "45%" 等の進捗表示と衝突するため直前に空白を要求
    return bool(re.search(r'[$#]\s*$|\s%\s*$', lines[-1]))


def is_waiting_for_input(raw: str) -> bool:
    """Claude Code がユーザー入力待ちのインタラクティブダイアログを表示中か判定。"""
    clean = strip_ansi(raw)
    return bool(re.search(r'Enter to confirm|Esc to cancel|Do you want to proceed', clean))


def line_diff(before: str, after: str) -> str:
    """スクロールバッファの追記分を取り出す。before と共通する行プレフィックス以降を返す。"""
    b = before.splitlines()
    a = after.splitlines()
    # before 末尾の空行はプロンプト再描画で変わるため無視して比較
    while b and not b[-1].strip():
        b.pop()
    common = 0
    for x, y in zip(b, a):
        if x != y:
            break
        common += 1
    return "\n".join(a[common:]).strip("\n")


# ── Claude JSONL 読み取り ──────────────────────────────────────

def _jsonl_dir_for_cwd(cwd: str) -> str:
    project = cwd.replace('/', '-')
    return os.path.expanduser(f"~/.claude/projects/{project}")


def _scan_jsonl_dir_sync(directory: str) -> dict[str, int]:
    """JSONL ディレクトリ内の全ファイルとそのサイズを返す（送信前スナップショット用）。"""
    try:
        import glob as _glob
        files = _glob.glob(os.path.join(directory, "*.jsonl"))
        return {path: os.path.getsize(path) for path in files}
    except Exception:
        return {}


_jsonl_claimed_size: dict[str, int] = {}


def _entry_matches_user_text(obj: dict, user_text: str, send_time: float) -> bool:
    """JSONL の user エントリが、送信したテキストに対応するか判定"""
    from datetime import datetime

    if obj.get('type') != 'user':
        return False
    ts_str = obj.get('timestamp', '')
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
            if ts < send_time - 5.0:
                return False
        except Exception:
            pass
    match_text = user_text.strip()[:200]
    content_list = obj.get('message', {}).get('content', []) or []
    items = [content_list] if isinstance(content_list, str) else content_list
    for block in items:
        entry_text = (
            block if isinstance(block, str)
            else (block.get('text', '') if isinstance(block, dict) and block.get('type') == 'text' else '')
        ).strip()
        if not entry_text:
            continue
        if (match_text and match_text in entry_text) or (entry_text and entry_text in user_text):
            return True
    return False


def _find_session_jsonl_sync(
    directory: str,
    user_text: str,
    known_sizes: dict[str, int],
    send_time: float,
    timeout: float = 15.0,
    only_path: str | None = None,
) -> tuple[str, int] | None:
    """
    メッセージ送信後、どのJSONLファイルがそのメッセージを受け取ったかを特定する。
    only_path 指定時はそのファイルだけを確認する（バインドキャッシュの検証用）。
    戻り値: (path, offset) — マッチしたユーザーエントリの「直後」のバイト位置。
    送信前サイズではなくエントリ直後にすることで、送信先 Claude が前ターン実行中で
    メッセージがキューされた場合に前ターンの応答を誤って拾わない。
    """
    import glob as _glob

    deadline = time.monotonic() + timeout
    while True:
        if only_path:
            files = [only_path]
        else:
            files = _glob.glob(os.path.join(directory, "*.jsonl"))
        for path in files:
            try:
                known = known_sizes.get(path, 0)
                # 同じ cwd を共有する他ウィンドウが既にマッチ済みの範囲は
                # 再走査対象から除外し、同一エントリの二重マッチを防ぐ
                scan_from = max(known, _jsonl_claimed_size.get(path, 0))
                current_size = os.path.getsize(path)
                if current_size <= scan_from:
                    continue
                with open(path, 'rb') as f:
                    f.seek(scan_from)
                    raw = f.read()
                pos = scan_from
                for bline in raw.split(b'\n'):
                    line_len = len(bline) + 1
                    try:
                        obj = json.loads(bline.decode('utf-8', errors='replace'))
                        if _entry_matches_user_text(obj, user_text, send_time):
                            _jsonl_claimed_size[path] = current_size
                            print(f"[jsonl] bound {os.path.basename(path)} @ {pos + line_len} for text={repr(user_text.strip()[:30])}", flush=True)
                            return (path, min(pos + line_len, current_size))
                    except Exception:
                        pass
                    pos += line_len
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _read_turn_result_sync(jsonl_path: str, offset: int) -> str:
    """
    JSONL の offset 以降を読み、「最後のツールイベント以降のテキスト」を結合して返す。
    assistant エントリはブロックごとに分割記録されるため、途中テキストは後続の
    tool_use / tool_result 出現時にリセットされ、ターン末尾のテキストだけが残る。
    """
    try:
        with open(jsonl_path, 'rb') as f:
            f.seek(offset)
            new_content = f.read().decode('utf-8', errors='replace')
        texts: list[str] = []
        for line in new_content.splitlines():
            try:
                obj = json.loads(line)
                otype = obj.get('type')
                if otype not in ('assistant', 'user'):
                    continue
                content = obj.get('message', {}).get('content', [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get('type')
                    if btype in ('tool_use', 'tool_result'):
                        texts = []  # ツールイベント → ここまでのテキストは途中経過
                    elif btype == 'text' and otype == 'assistant':
                        t = block.get('text', '').strip()
                        if t and not FEEDBACK_RE.search(t):
                            texts.append(t)
            except Exception:
                pass
        return "\n\n".join(texts).strip()
    except Exception:
        return ""


# ── 実行パイプライン ──────────────────────────────────────────

async def update_status(status_msg: discord.Message | None, text: str) -> None:
    if status_msg is None:
        return
    try:
        await status_msg.edit(content=text)
    except Exception:
        pass


async def send_result(message: discord.Message, text: str) -> None:
    """最終結果の返信。メンション付き reply でプッシュ通知を発火させる"""
    chunks = split_chunks(text)
    print(f"[worker] sending {len(chunks)} chunk(s), {len(text)} chars", flush=True)
    await safe_reply(message, f"```\n{chunks[0]}\n```", mention=True)
    for chunk in chunks[1:]:
        await message.channel.send(f"```\n{chunk}\n```")


async def run_shell_command(window: int, message: discord.Message,
                            status_msg: discord.Message | None, content: str) -> None:
    """シェルペイン: 送信 → プロンプト復帰まで待機 → スクロールバッファ差分を返信"""
    before = await tmux_capture(window, scrollback=2000)
    await tmux_send(window, content)

    prev = ""
    stable = 0
    start = time.monotonic()
    completed = False
    while time.monotonic() - start < SHELL_TIMEOUT:
        await asyncio.sleep(0.5)
        cur = await tmux_capture(window)
        if cur == prev:
            stable += 1
        else:
            stable = 0
            prev = cur
        if stable >= 3 and is_shell_idle(cur):
            completed = True
            break

    after = await tmux_capture(window, scrollback=2000)
    out = line_diff(before, after)
    if not completed:
        out = (out or "") + f"\n…（{SHELL_TIMEOUT}秒経過してもプロンプトに戻りません。実行中の可能性があります）"
    await update_status(status_msg, "✅" if completed else "⚠️ 継続中")
    if out.strip():
        await send_result(message, out)
    else:
        await safe_reply(message, "（出力なし）", mention=True)


async def run_claude_message(window: int, message: discord.Message,
                             status_msg: discord.Message | None, content: str) -> None:
    """
    Claude ペイン: 送信 → セッション JSONL をバインド（キャッシュ優先）→
    「画面が busy でない ∧ JSONL 末尾にターン完了テキストがある」まで待機 → 全文返信。
    途中経過は Discord に送らない（ステータス編集のみ）。
    """
    cmd, cwd = await pane_info(window)
    jsonl_dir = _jsonl_dir_for_cwd(cwd) if cwd else None
    known_sizes: dict[str, int] = {}
    if jsonl_dir:
        known_sizes = await asyncio.to_thread(_scan_jsonl_dir_sync, jsonl_dir)

    send_time = time.time()
    await tmux_send(window, content)

    # JSONL バインド: 前回のファイルを最優先で確認（Claude 再起動まで同じファイル）。
    # 送信先が前ターン実行中だとメッセージはキューされ、JSONL への書き込みが
    # ターン完了後になるため、ここで見つからなくても完了待ちループ内で再試行する。
    jsonl_path = None
    jsonl_offset = 0
    cached = window_jsonl.get(window)
    if jsonl_dir:
        if cached and os.path.exists(cached):
            bind = await asyncio.to_thread(
                _find_session_jsonl_sync, jsonl_dir, content, known_sizes,
                send_time, 5.0, cached
            )
            if bind:
                jsonl_path, jsonl_offset = bind
        if not jsonl_path:
            bind = await asyncio.to_thread(
                _find_session_jsonl_sync, jsonl_dir, content, known_sizes, send_time, 5.0
            )
            if bind:
                jsonl_path, jsonl_offset = bind
                window_jsonl[window] = jsonl_path
    if not jsonl_path:
        print(f"[worker] win={window} jsonl not bound yet, retrying lazily", flush=True)

    start = time.monotonic()
    last_status = start
    dialog_stable = 0
    idle_stable = 0

    while time.monotonic() - start < CLAUDE_TIMEOUT:
        await asyncio.sleep(1.0)
        pane = await tmux_capture(window)

        # 未バインドなら1パスだけ再走査（キュー済みメッセージは書き込まれ次第拾える）
        if not jsonl_path and jsonl_dir:
            bind = await asyncio.to_thread(
                _find_session_jsonl_sync, jsonl_dir, content, known_sizes, send_time, 0.0
            )
            if bind:
                jsonl_path, jsonl_offset = bind
                window_jsonl[window] = jsonl_path

        # 確認ダイアログ（ツール実行許可など）→ 画面を通知して人の入力を待つ
        if is_waiting_for_input(pane):
            dialog_stable += 1
            if dialog_stable >= 2:
                await update_status(status_msg, "⏸️ 入力待ち")
                await send_result(message, truncate(strip_ansi(pane)) +
                                  "\n\n（入力待ちです。`!key` / `!enter` で応答できます）")
                return
            continue
        dialog_stable = 0

        if is_claude_busy(pane):
            idle_stable = 0
            now = time.monotonic()
            if now - last_status >= STATUS_INTERVAL:
                await update_status(status_msg, f"⏳ 実行中… {int((now - start) // 60)}分経過")
                last_status = now
            continue

        # busy でない → JSONL のターン完了テキストを確認
        idle_stable += 1
        if jsonl_path:
            text = await asyncio.to_thread(_read_turn_result_sync, jsonl_path, jsonl_offset)
            if text:
                # 直後にツール実行へ進む一瞬の non-busy を誤検出しないよう再確認
                await asyncio.sleep(1.5)
                pane2 = await tmux_capture(window)
                if is_claude_busy(pane2):
                    idle_stable = 0
                    continue
                text2 = await asyncio.to_thread(_read_turn_result_sync, jsonl_path, jsonl_offset)
                await update_status(status_msg, f"✅ 完了（{int(time.monotonic() - start)}秒）")
                await send_result(message, text2 or text)
                return
            # busy でもなくテキストも来ない状態が続く → /コマンド等の画面内完結操作
            if idle_stable >= 15:
                out = truncate(strip_ansi(await tmux_capture(window, scrollback=50)))
                await update_status(status_msg, "✅")
                await send_result(message, out if out.strip() else "（出力なし）")
                return
        else:
            # バインド失敗時: busy→idle の遷移を10秒安定で確認して画面末尾を返す
            if idle_stable >= 10:
                out = truncate(strip_ansi(await tmux_capture(window, scrollback=200)), 3600)
                await update_status(status_msg, "✅（画面キャプチャ）")
                await send_result(message, out if out.strip() else "（出力なし）")
                return

    await update_status(status_msg, "⏰ タイムアウト")
    await safe_reply(
        message,
        f"{CLAUDE_TIMEOUT // 60}分経過しても完了を検出できませんでした。"
        "まだ実行中の可能性があります。`!cap` で現在の画面を確認してください。",
        mention=True,
    )


# ── window worker（1ウィンドウ1件ずつ順番に処理）─────────────

async def _window_worker(window: int) -> None:
    q = window_queues[window]
    while not q.empty():
        message, kind, content, status_msg = await q.get()
        print(f"[worker] win={window} dequeue kind={kind}: {repr(content[:40])}", flush=True)

        try:
            if kind == "enter":
                # !enter: 通常メッセージと同じキューに乗せることで、直前の送信中テキストの
                # Enter より先にこの Enter が tmux に届いてしまう競合を防ぐ
                await asyncio.to_thread(
                    subprocess.run, ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", "Enter"]
                )
                await asyncio.sleep(2.5)
                out = truncate(strip_ansi(await tmux_capture(window, scrollback=50)))
                await safe_reply(message, f"```\n{out}\n```")
                continue

            if kind == "key":
                await asyncio.to_thread(
                    subprocess.run, ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window}", content]
                )
                await asyncio.sleep(0.5)
                out = truncate(strip_ansi(await tmux_capture(window)))
                await safe_reply(message, f"```\n{out}\n```")
                continue

            # 通常テキスト: ペインの実行コマンドでモードを自動判定
            cmd, _cwd = await pane_info(window)
            if cmd in SHELL_COMMANDS:
                await run_shell_command(window, message, status_msg, content)
            else:
                await run_claude_message(window, message, status_msg, content)
        except Exception as e:
            print(f"[worker] win={window} exception: {e}", flush=True)
            import traceback; traceback.print_exc()
            try:
                await safe_reply(message, f"エラー: {e}", mention=True)
            except Exception:
                pass


# ── watch loop ────────────────────────────────────────────────

async def watch_loop(window: int, thread: discord.Thread):
    # Claude Code のペインはスピナーと経過タイマー("(2m 33s · …")で2秒ごとに必ず変わるため、
    # 「前回と違えば送る」だと実行中ずっと2秒に1通投稿し続ける（2026-07-11 の通知連発の原因）。
    # 実行中(CLAUDE_BUSY_RE)は送らず、出力が落ち着いてから(2回連続で同一)1通だけ送る。
    last_sent = strip_ansi(await tmux_capture(window))
    prev = last_sent
    while True:
        await asyncio.sleep(2)
        cur = strip_ansi(await tmux_capture(window))
        if CLAUDE_BUSY_RE.search(cur):
            prev = None   # 実行中は静観し、落ち着いてから安定判定をやり直す
            continue
        settled, prev = cur == prev, cur
        if settled and cur != last_sent:
            try:
                await thread.send(f"```\n{truncate(cur)}\n```")
            except Exception:
                pass
            last_sent = cur


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
    if message.author.bot:
        return None
    # スレッドは public_thread で作成されるため、チャンネルを見られる者は誰でも参加・投稿できる。
    # 親チャンネルが ALLOWED_CHANNEL と一致することを確認しないと、認可チェックを完全に迂回して
    # tmux（実シェル）へ任意コマンドを送信できてしまう。
    if ALLOWED_CHANNEL and str(message.channel.parent_id) != ALLOWED_CHANNEL:
        return None
    return thread_map.get(str(message.channel.id))


def enqueue(window: int, item: tuple) -> None:
    q = window_queues.setdefault(window, asyncio.Queue())
    q.put_nowait(item)
    if window not in window_workers or window_workers[window].done():
        window_workers[window] = asyncio.create_task(_window_worker(window))


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
            enqueue(window, (message, "enter", "", None))
            return

        if content.startswith("!key "):
            enqueue(window, (message, "key", content[5:].strip(), None))
            return

        if content == "!cap":
            out = truncate(strip_ansi(await tmux_capture(window, scrollback=50)))
            await safe_reply(message, f"```\n{out}\n```")
            return

        if content == "!watch":
            if window in watch_tasks and not watch_tasks[window].done():
                await safe_reply(message, "すでに監視中です。`!unwatch` で停止。")
                return
            watch_tasks[window] = asyncio.create_task(
                watch_loop(window, message.channel)
            )
            await safe_reply(message, f"ウィンドウ {window} の監視を開始しました。")
            return

        if content == "!unwatch":
            t = watch_tasks.pop(window, None)
            if t:
                t.cancel()
                await safe_reply(message, "監視停止しました。")
            else:
                await safe_reply(message, "監視は動いていません。")
            return

        if content.startswith("!"):
            await safe_reply(message,
                "このコマンドはスレッド内では使えません。\n"
                "スレッドで使えるのは `!enter` `!key` `!cap` `!watch` `!unwatch` です。\n"
                "`!init` `!windows` `!resync` `!setchannel` `!ai` はメインチャンネルで実行してください。"
            )
            return

        # ── 通常テキスト: キューに追加して順番に処理 ──────────
        status_msg = await message.channel.send("⌛️")
        enqueue(window, (message, "text", content, status_msg))
        return

    # ── メインチャンネル内メッセージ ─────────────────────────
    if not is_main_channel(message):
        return

    content = message.content.strip()

    if content == "!setchannel":
        ALLOWED_CHANNEL = str(message.channel.id)
        save_env("DISCORD_CHANNEL_ID", ALLOWED_CHANNEL)
        await safe_reply(message, "このチャンネルに固定しました。")
        return

    if content == "!init":
        try:
            wins = tmux_windows()
        except Exception as e:
            await safe_reply(message, f"tmuxエラー: {e}")
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
        await safe_reply(message, "スレッド作成完了:\n" + "\n".join(created))
        return

    if content == "!windows":
        try:
            wins = tmux_windows()
        except Exception as e:
            await safe_reply(message, f"エラー: {e}", mention=True)
            return
        lines = []
        for w in wins:
            tid = next((t for t, i in thread_map.items() if i == w["index"]), None)
            link = f"<#{tid}>" if tid else "（スレッドなし）"
            lines.append(f"[{w['index']}] {w['name']} ({w['command']}) → {link}")
        await safe_reply(message, "\n".join(lines))
        return

    if content == "!resync":
        try:
            wins = sorted(tmux_windows(), key=lambda w: w["index"])
        except Exception as e:
            await safe_reply(message, f"tmuxエラー: {e}")
            return

        # 現在マッピング済みのスレッドを取得（削除済み等はスキップ）
        threads: list[discord.Thread] = []
        for tid in thread_map:
            try:
                th = client.get_channel(int(tid)) or await client.fetch_channel(int(tid))
                threads.append(th)
            except (discord.NotFound, discord.Forbidden):
                continue

        # スレッドID（作成順=若い順）昇順で、ウィンドウ番号昇順に1対1で振り直す
        threads.sort(key=lambda t: t.id)

        new_map: dict[str, int] = {}
        report = []
        for i, w in enumerate(wins):
            name = f"w{w['index']} • {w['name']} [{w['command']}]"
            if i < len(threads):
                thread = threads[i]
                old_window = thread_map.get(str(thread.id))
                await thread.edit(name=name[:100])
                new_map[str(thread.id)] = w["index"]
                report.append(f"w{w['index']}: <#{thread.id}>（旧 w{old_window}）")
            else:
                thread = await message.channel.create_thread(
                    name=name[:100],
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=10080,
                )
                new_map[str(thread.id)] = w["index"]
                await thread.send(
                    f"**ウィンドウ {w['index']} • `{w['command']}`** に接続しました。\n"
                    f"このスレッドに書くとウィンドウに送信されます。\n"
                    f"`!cap` = 現在画面  `!watch` / `!unwatch` = 自動監視"
                )
                report.append(f"w{w['index']}: <#{thread.id}>（新規作成）")

        orphaned = threads[len(wins):]
        for thread in orphaned:
            report.append(f"（対応ウィンドウなし・紐付け解除）: <#{thread.id}>")

        thread_map.clear()
        thread_map.update(new_map)
        save_map()
        await safe_reply(message, "スレッド↔ウィンドウの紐付けを若い順に振り直しました:\n" + "\n".join(report))
        return

    if content.startswith("!ai "):
        if not ANTHROPIC_API_KEY:
            await safe_reply(message, "ANTHROPIC_API_KEY が未設定です。")
            return
        prompt = content[4:].strip()
        thinking = await safe_reply(message, "⏳")
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
        await safe_reply(message,
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
