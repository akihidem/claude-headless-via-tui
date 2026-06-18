#!/usr/bin/env python3
"""claude-cli-run — `claude -p` の対話TUI(entrypoint=cli)ドロップイン代替。

背景: 2026-06-15 以降、サブスクの `claude -p` / Agent SDK 利用は別建ての
「月次 Agent SDK クレジット」(Max5x=$100/月) から消費され、超過は従量課金。
一方、対話TUI(`claude` を -p 無しで起動 = entrypoint=="cli") は通常のサブスク
利用枠から消費され、クレジットを食わない。これは本番 self-test transcript
581/581 が "cli" であることで実証済み (参照: tools/kokoro-monitor/self-test.py)。

本スクリプトは tmux 専用セッションに対話モード claude を立て、プロンプトを
paste-buffer で流し込み、応答は Claude Code が書き出す transcript jsonl から
直読して stdout に返す。`claude -p "$P"` / `echo "$P" | claude -p` のドロップイン。

Usage:
  claude-cli-run [opts] "PROMPT"
  echo "$PROMPT" | claude-cli-run [opts]
Opts:
  --model M             既定なし(ユーザ設定モデル)。例: claude-haiku-4-5-20251001
  --cwd DIR             claude を起動する cwd。既定: ~/.claude/claude-cli-run-cwd
                        (固定 trust 済み dir。tool でファイル生成させたい時は対象 repo を指定)
  --permission-mode M   既定 bypassPermissions。text 返答なら何でも可。plan で読取専用。
  --timeout SEC         応答完了待ち上限。既定 300。
  --startup-timeout SEC TUI 起動待ち上限。既定 40。
  --no-sentinel         sentinel を付けず「最初の assistant 応答」を返す(自由記述向け)。
終了コード: 成功 0 / 失敗 1 (理由は stderr)。
"""
import argparse
import atexit
import datetime
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_CLI_RUN_BIN") or str(Path.home() / ".npm-global" / "bin" / "claude")
DEFAULT_CWD = Path.home() / ".claude" / "claude-cli-run-cwd"

# 入力欄 ready の安定マーカー（フッター）
READY_MARKERS = ("shift+tab to cycle", "? for shortcuts")
MENU_CONFIRM_MARKER = "Enter to confirm"
# 起動時メニューで安全に accept すべき選択肢のキーワード（優先順）
ACCEPT_KEYWORDS = ("yes, i accept", "continue without", "yes, proceed",
                   "yes, i trust", "trust the files", "got it", "accept")


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


# --- session cleanup (堅牢化) ---------------------------------------------
# tmux セッションは -d (detached) なので親が落ちても生き続ける。通常終了は
# run() の finally で kill するが、SIGINT/SIGKILL 等では finally が走らず孤児が
# 残る。そこで (1) signal/atexit で best-effort に kill、(2) 起動時に「pid が
# 既に死んでいる ccrun-* セッション」を掃除して自己修復する。
_ACTIVE_SESSION = None  # 現在このプロセスが保持しているセッション名


def _kill_active_session():
    global _ACTIVE_SESSION
    if _ACTIVE_SESSION:
        tmux("kill-session", "-t", _ACTIVE_SESSION)
        _ACTIVE_SESSION = None


def _on_signal(signum, _frame):
    _kill_active_session()
    # 既定の終了コード規約 (128 + signum) で抜ける
    sys.exit(128 + signum)


def _install_cleanup_handlers():
    atexit.register(_kill_active_session)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # メインスレッド以外等では無視


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 別ユーザの生存プロセス
    return True


def reap_stale_sessions():
    """所有プロセスが既に死んでいる ccrun-<pid>-<uuid> セッションを掃除する。"""
    out = tmux("list-sessions", "-F", "#{session_name}")
    if out.returncode != 0:
        return 0
    reaped = 0
    for name in out.stdout.splitlines():
        m = re.match(r"^ccrun-(\d+)-[0-9a-f]+$", name.strip())
        if not m:
            continue
        pid = int(m.group(1))
        if pid == os.getpid() or _pid_alive(pid):
            continue  # 自分 / 別の生きてる実行のセッションは触らない
        tmux("kill-session", "-t", name)
        reaped += 1
    return reaped


def transcripts_dir(cwd: Path) -> Path:
    # Claude Code は cwd の "/" と "." を "-" に置換した名前で project dir を作る
    return Path.home() / ".claude" / "projects" / re.sub(r"[/.]", "-", str(cwd))


def list_transcripts(cwd: Path) -> set:
    d = transcripts_dir(cwd)
    return set(d.glob("*.jsonl")) if d.exists() else set()


def wait_for_new_transcript(cwd: Path, pre: set, timeout: int):
    import json  # noqa
    deadline = time.time() + timeout
    while time.time() < deadline:
        diff = list_transcripts(cwd) - pre
        if diff:
            return max(diff, key=lambda p: p.stat().st_mtime)
        time.sleep(1)
    return None


def last_assistant_text(transcript: Path):
    import json
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    return None


def _menu_options(pane: str):
    """pane から '❯ 1. ...' / '  2. ...' 形式の選択肢を順に抽出。"""
    opts = []
    for ln in pane.splitlines():
        m = re.match(r"^\s*(❯)?\s*(\d+)\.\s+(.*)$", ln)
        if m:
            opts.append((m.group(1) == "❯", m.group(3).strip()))
    return opts


def _handle_startup_menu(session: str, pane: str) -> bool:
    """起動時の確認メニュー(bypass警告/settings error/trust)を安全な選択肢で accept。
    未知のメニューは触らない(False)。"""
    opts = _menu_options(pane)
    if not opts:
        return False
    cur = next((i for i, (c, _) in enumerate(opts) if c), 0)
    tgt = next((i for i, (_, t) in enumerate(opts)
                if any(k in t.lower() for k in ACCEPT_KEYWORDS)), None)
    if tgt is None:
        return False
    delta = tgt - cur
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        tmux("send-keys", "-t", session, key)
        time.sleep(0.15)
    tmux("send-keys", "-t", session, "Enter")
    time.sleep(2.5)
    return True


def wait_for_tui_ready(session: str, timeout: int):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = tmux("capture-pane", "-t", session, "-p").stdout or ""
        if MENU_CONFIRM_MARKER in last:
            if not _handle_startup_menu(session, last):
                time.sleep(1)  # 未知メニュー: 触らず待つ
            continue
        if any(m in last for m in READY_MARKERS):
            return True, last
        time.sleep(1)
    return False, last


def paste_prompt(session: str, text: str):
    """multi-line prompt を bracketed paste で流し込み、Enter で送信。"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        path = tf.name
    try:
        tmux("load-buffer", "-b", "ccrun", path)
        # -p: bracketed paste (改行が即送信されない) / -r: LF をそのまま
        tmux("paste-buffer", "-p", "-r", "-d", "-b", "ccrun", "-t", session)
        time.sleep(0.6)
        tmux("send-keys", "-t", session, "Enter")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run(prompt: str, model, cwd: Path, perm: str, timeout: int,
        startup: int, use_sentinel: bool):
    cwd.mkdir(parents=True, exist_ok=True)
    session = f"ccrun-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    sentinel = f"CCRUN_DONE_{uuid.uuid4().hex[:8]}"
    if use_sentinel:
        prompt = (prompt.rstrip()
                  + f"\n\n---\n(指示: 上の依頼に回答してください。回答の一番最後に、"
                    f"{sentinel} という文字列だけの行を必ず付けて終了してください。)")

    # CLAUDECODE 等を unset して claude-in-claude のネスト検知を回避
    inner = (f"env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID "
             f"-u CLAUDE_CODE_CHILD_SESSION {CLAUDE_BIN} --permission-mode {perm}")
    if model:
        inner += f" --model {model}"

    pre = list_transcripts(cwd)
    new = tmux("new-session", "-d", "-s", session, "-x", "220", "-y", "50",
               "-c", str(cwd), "bash", "-lc", inner)
    if new.returncode != 0:
        return None, f"tmux new-session 失敗: {new.stderr.strip()[:200]}"
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = session  # signal/atexit ハンドラの掃除対象に登録

    try:
        ready, pane = wait_for_tui_ready(session, startup)
        if not ready:
            return None, f"TUI が {startup}s 以内に起動しない\n--- pane ---\n{pane[-400:]}"

        paste_prompt(session, prompt)

        tx = wait_for_new_transcript(cwd, pre, 30)
        if tx is None:
            return None, "30s 以内に新規 transcript が出現しない"

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            txt = last_assistant_text(tx)
            if txt:
                last = txt
                if not use_sentinel:
                    return txt, None
                if sentinel in txt:
                    cleaned = re.sub(rf"\n?^{re.escape(sentinel)}\s*$", "",
                                     txt, flags=re.MULTILINE).rstrip()
                    return cleaned, None
            time.sleep(1)
        # timeout: sentinel 未確認でも応答があれば best-effort で返す
        if last:
            cleaned = (re.sub(rf"\n?^{re.escape(sentinel)}\s*$", "", last,
                              flags=re.MULTILINE).rstrip() if use_sentinel else last)
            return cleaned, f"WARN: {timeout}s 内に完了マーカー未検出。途中応答を返す"
        return None, f"{timeout}s 内に assistant 応答なし"
    finally:
        tmux("kill-session", "-t", session)
        _ACTIVE_SESSION = None


def main():
    ap = argparse.ArgumentParser(description="claude -p の対話TUI(cli枠)代替")
    ap.add_argument("prompt", nargs="?", help="プロンプト (無ければ stdin)")
    ap.add_argument("--model")
    ap.add_argument("--cwd", default=str(DEFAULT_CWD))
    ap.add_argument("--permission-mode", default="bypassPermissions")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--startup-timeout", type=int, default=40)
    ap.add_argument("--no-sentinel", action="store_true")
    ap.add_argument("--reap", action="store_true",
                    help="所有プロセスが死んだ ccrun-* セッションを掃除して終了")
    ap.add_argument("--no-reap", action="store_true",
                    help="起動時の自動掃除(stale 回収)を無効化")
    a = ap.parse_args()

    if a.reap:
        n = reap_stale_sessions()
        print(f"[claude-cli-run] reaped {n} stale session(s)", file=sys.stderr)
        sys.exit(0)

    _install_cleanup_handlers()
    if not a.no_reap:
        reap_stale_sessions()  # 過去の強制終了で残った孤児を自己修復

    prompt = a.prompt if a.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        print("ERROR: 空プロンプト", file=sys.stderr)
        sys.exit(1)

    t0 = time.time()
    out, err = run(prompt, a.model, Path(a.cwd).expanduser(), a.permission_mode,
                   a.timeout, a.startup_timeout, not a.no_sentinel)
    if err:
        print(f"[claude-cli-run] {err}", file=sys.stderr)
    if out is None:
        sys.exit(1)
    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    print(f"[claude-cli-run] ok ({time.time()-t0:.0f}s, entrypoint=cli)", file=sys.stderr)


if __name__ == "__main__":
    main()
