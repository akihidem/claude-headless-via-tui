# claude-headless-via-tui

[![CI](https://github.com/akihidem/claude-headless-via-tui/actions/workflows/ci.yml/badge.svg)](https://github.com/akihidem/claude-headless-via-tui/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
![Dependencies](https://img.shields.io/badge/dependencies-none%20(stdlib)-success.svg)
![Requires](https://img.shields.io/badge/requires-Claude%20Code%20%2B%20tmux-orange.svg)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20WSL-lightgrey.svg)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/akihidem/claude-headless-via-tui/pulls)

A drop-in replacement for `claude -p "PROMPT"` (Claude Code headless mode) that
runs the **interactive TUI** under the hood and drives it with tmux.

```bash
# instead of:  claude -p "summarize this repo"
./claude-cli-run.py "summarize this repo"

# pipe also works:
echo "$PROMPT" | ./claude-cli-run.py --model claude-haiku-4-5-20251001
```

It behaves like `claude -p` from the outside — give it a prompt on argv or
stdin, get the assistant's text answer on stdout, exit code `0`/`1` — but the
actual Claude Code process it spawns is the normal interactive session
(`entrypoint == "cli"`), not the headless one.

> 日本語の説明は下のほうにあります。

## Why does this exist?

Since **2026-06-15**, on subscription plans, usage of `claude -p` and the
Claude Agent SDK is metered against a separate **monthly Agent SDK credit**
pool (e.g. Max 5x = \$100/month); going over that pool bills as usage-based
overage.

The plain interactive TUI (`claude` started **without** `-p`, i.e.
`entrypoint == "cli"`) is metered against your normal subscription quota and
does **not** draw from the Agent SDK credit pool.

So if you have automation that calls `claude -p` a lot, every call eats into
that capped credit pool. This script lets you keep the same scripting interface
while the work actually runs through the interactive TUI quota instead.

**This is just a wrapper around the official `claude` CLI. It does not bypass,
crack, or modify any billing — it simply chooses the interactive entrypoint
instead of the headless one. Make sure this is consistent with your plan's
terms of service before relying on it.**

## How it works

It's essentially "a robot that remote-controls the interactive TUI":

```
1. tmux new-session  →  start interactive `claude` inside a dedicated session
     (CLAUDECODE / CLAUDE_CODE_* env vars are unset to dodge the
      claude-in-claude nesting guard)
2. capture-pane poll  →  wait until the TUI input box is ready
     (detect footer markers like "? for shortcuts";
      auto-accept known startup menus: bypass warning / trust prompt)
3. paste-buffer       →  inject the prompt (bracketed paste) + Enter
4. read transcript    →  instead of scraping the screen, read the JSONL
     transcript Claude Code writes under
     ~/.claude/projects/<cwd-as-dashes>/*.jsonl and take the last
     assistant text block
5. sentinel line      →  the prompt is augmented to end the answer with a
     unique CCRUN_DONE_xxxx line, used to detect completion
6. kill-session
```

Input goes in via tmux paste; output comes out by reading the transcript JSONL
(more robust than screen-scraping); completion is detected by the sentinel.

## Requirements

- [Claude Code](https://claude.com/claude-code) installed (the `claude` binary)
- `tmux`
- Python 3.8+ (standard library only — no dependencies)

By default the script looks for the `claude` binary at
`~/.npm-global/bin/claude`. Override with the `CLAUDE_CLI_RUN_BIN` env var.

## Install

It's a single self-contained file — just put it on your `PATH`.

```bash
# 1. get the script
git clone https://github.com/akihidem/claude-headless-via-tui.git
cd claude-headless-via-tui
chmod +x claude-cli-run.py

# 2. put it on PATH as `claude-cli-run` (pick one)
sudo ln -s "$PWD/claude-cli-run.py" /usr/local/bin/claude-cli-run   # system-wide
# or, no sudo:
mkdir -p ~/.local/bin && ln -s "$PWD/claude-cli-run.py" ~/.local/bin/claude-cli-run
#   make sure ~/.local/bin is on your PATH:
#   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# 3. (optional) if `claude` is not at ~/.npm-global/bin/claude, tell the script:
export CLAUDE_CLI_RUN_BIN="$(command -v claude)"

# 4. verify
claude-cli-run "say hi in one word"
```

Or grab just the file without cloning:

```bash
curl -fsSL https://raw.githubusercontent.com/akihidem/claude-headless-via-tui/main/claude-cli-run.py \
  -o ~/.local/bin/claude-cli-run && chmod +x ~/.local/bin/claude-cli-run
```

No `pip install` needed — standard library only.

## Usage

```
claude-cli-run.py [opts] "PROMPT"
echo "$PROMPT" | claude-cli-run.py [opts]
```

| Option | Default | Meaning |
|---|---|---|
| `--model M` | user default | e.g. `claude-haiku-4-5-20251001` |
| `--cwd DIR` | `~/.claude/claude-cli-run-cwd` | cwd to start `claude` in (a fixed, already-trusted dir). Point it at a target repo if you want the tool to create/edit files there. |
| `--permission-mode M` | `bypassPermissions` | `plan` for read-only, etc. |
| `--timeout SEC` | `300` | max wait for the answer to complete |
| `--startup-timeout SEC` | `40` | max wait for the TUI to come up |
| `--no-sentinel` | off | don't append a sentinel; return the first assistant response (good for free-form output) |
| `--reap` | — | kill any stale `ccrun-*` tmux sessions whose owning process is dead, then exit |
| `--no-reap` | off | skip the automatic stale-session sweep done at startup |

Exit code: `0` on success, `1` on failure (reason on stderr).

### Session cleanup

The tmux session is detached (`-d`), so it outlives the script. On normal exit
it's killed in a `finally` block; on `SIGINT`/`SIGTERM`/`SIGHUP` a handler kills
it too. But a hard kill (`SIGKILL`/OOM) skips all of that and leaves an orphan.

To self-heal, every run first sweeps stale `ccrun-<pid>-*` sessions whose owning
process no longer exists (disable with `--no-reap`). It only touches sessions
whose pid is dead — concurrent runs and other tools' sessions are left alone. You
can also sweep manually:

```bash
claude-cli-run --reap
```

## Examples

### Basic — prompt in, answer out

```bash
claude-cli-run "What is the capital of Japan? Answer in one word."
# -> Tokyo
```

### Pipe a prompt from stdin

```bash
echo "Summarize this in 3 bullet points:" | cat - article.txt | claude-cli-run
# or build the prompt in a heredoc:
claude-cli-run "$(cat <<'EOF'
Review the diff below and list any bugs.

$(git diff)
EOF
)"
```

### Pick a cheaper/faster model

```bash
claude-cli-run --model claude-haiku-4-5-20251001 "Classify this sentiment: 'I love it'"
```

### Capture the answer into a shell variable

```bash
TITLE=$(claude-cli-run "Suggest a concise git commit title for: added retry logic")
git commit -m "$TITLE"
```

### Let it create / edit files in a real repo

Point `--cwd` at the target repo (it runs with `bypassPermissions` by default,
so it can use tools without asking):

```bash
claude-cli-run --cwd ~/myproject \
  "Add a README.md with a one-paragraph description of this project."
```

### Read-only investigation (no file changes)

```bash
claude-cli-run --cwd ~/myproject --permission-mode plan \
  "Where is the database connection configured? Just tell me the file and line."
```

### Free-form output (no completion sentinel)

By default the tool asks the model to end with a hidden `CCRUN_DONE_xxxx` marker
to detect completion. For creative / open-ended output where that instruction is
unwanted, use `--no-sentinel` (returns the first assistant response):

```bash
claude-cli-run --no-sentinel "Write a haiku about tmux."
```

### Give a long-running task more time

```bash
claude-cli-run --timeout 900 --cwd ~/bigrepo \
  "Refactor utils.py to remove duplicate code, then summarize what changed."
```

### Use it in a script / cron job

```bash
#!/usr/bin/env bash
set -euo pipefail
DIGEST=$(claude-cli-run --model claude-haiku-4-5-20251001 \
  "In 5 bullets, summarize today's git log:\n$(git log --since=yesterday --oneline)")
printf '%s\n' "$DIGEST" | mail -s "Daily repo digest" me@example.com
```

### Drop-in swap for existing `claude -p` automation

Anywhere your scripts call `claude -p`, replace the command name:

```diff
- claude -p "$PROMPT"
+ claude-cli-run "$PROMPT"
```

## Caveats

- This automates UI flows and relies on TUI footer/menu strings; a future
  Claude Code release can change those and break detection.
- `--permission-mode bypassPermissions` runs tools without prompts. Only point
  `--cwd` at directories you trust.
- Provided as-is. See the billing note above and verify it fits your plan.

---

## 日本語

`claude -p "PROMPT"`（Claude Code の headless モード）の**ドロップイン代替**。
中身は**対話 TUI** を裏で起動し、tmux で操縦する。

### なぜ作ったか

2026-06-15 以降、サブスクの `claude -p` / Agent SDK 利用は別建ての「月次
Agent SDK クレジット」(Max5x=\$100/月) から消費され、超過は従量課金になる。
一方、対話 TUI（`claude` を `-p` 無しで起動＝`entrypoint=="cli"`）は通常の
サブスク枠から消費され、クレジットを食わない。

`claude -p` を多用する自動化があると、毎回その上限付きクレジット枠を削る。
このスクリプトは、外から見たインターフェース（argv/stdin でプロンプト →
stdout に回答）はそのままに、実体を対話 TUI 枠で回す。

**これは公式 `claude` CLI のラッパーにすぎず、課金を回避・改変するものでは
ない**（headless ではなく対話エントリポイントを選ぶだけ）。利用前にプランの
規約と整合するか確認すること。

### 仕組み

「対話 TUI を tmux で遠隔操縦するロボット」。
tmux でセッションを立て → `capture-pane` で入力欄 ready を待ち（起動時の
trust/bypass メニューは安全な選択肢を自動 accept）→ `paste-buffer` で
プロンプト注入＋Enter → 応答は画面スクレイプではなく Claude Code が書く
transcript JSONL を直読 → プロンプト末尾に注入した `CCRUN_DONE_xxxx` 行で
完了検知 → `kill-session`。

### 必要なもの

Claude Code（`claude` バイナリ）/ `tmux` / Python 3.8+（標準ライブラリのみ）。
`claude` のパスは既定 `~/.npm-global/bin/claude`、`CLAUDE_CLI_RUN_BIN` で上書き可。

### インストール

単一ファイルなので `PATH` に置くだけ。

```bash
# 1. 取得
git clone https://github.com/akihidem/claude-headless-via-tui.git
cd claude-headless-via-tui
chmod +x claude-cli-run.py

# 2. PATH に claude-cli-run として置く（どちらか）
sudo ln -s "$PWD/claude-cli-run.py" /usr/local/bin/claude-cli-run   # システム全体
# sudo なしなら:
mkdir -p ~/.local/bin && ln -s "$PWD/claude-cli-run.py" ~/.local/bin/claude-cli-run
#   ~/.local/bin が PATH に入っているか確認:
#   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# 3. （任意）claude が ~/.npm-global/bin/claude に無いなら明示:
export CLAUDE_CLI_RUN_BIN="$(command -v claude)"

# 4. 動作確認
claude-cli-run "一言で挨拶して"
```

clone せずファイルだけ取るなら:

```bash
curl -fsSL https://raw.githubusercontent.com/akihidem/claude-headless-via-tui/main/claude-cli-run.py \
  -o ~/.local/bin/claude-cli-run && chmod +x ~/.local/bin/claude-cli-run
```

`pip install` 不要（標準ライブラリのみ）。

### 使い方の例

```bash
# 基本: プロンプト → 回答
claude-cli-run "日本の首都は? 一言で。"

# stdin から渡す
echo "次を3点で要約して:" | cat - article.txt | claude-cli-run

# 安い/速いモデルを指定
claude-cli-run --model claude-haiku-4-5-20251001 "この感情を分類して: 'I love it'"

# 回答をシェル変数に取り込む
TITLE=$(claude-cli-run "次の変更に合うコミットタイトルを簡潔に: リトライ処理を追加")
git commit -m "$TITLE"

# 実際の repo でファイルを生成・編集させる（既定 bypassPermissions なので確認なしで tool 実行）
claude-cli-run --cwd ~/myproject "このプロジェクトを一段落で説明する README.md を追加して。"

# 読み取り専用で調査（ファイル変更なし）
claude-cli-run --cwd ~/myproject --permission-mode plan \
  "DB接続はどこで設定してる? ファイルと行だけ教えて。"

# 自由記述（完了 sentinel を付けない＝最初の応答を返す）
claude-cli-run --no-sentinel "tmux についての俳句を1つ。"

# 時間のかかるタスクに余裕を与える
claude-cli-run --timeout 900 --cwd ~/bigrepo "utils.py の重複を除去して、変更点を要約して。"

# 既存の `claude -p` 自動化の置き換え（コマンド名を差し替えるだけ）
#   claude -p "$PROMPT"   →   claude-cli-run "$PROMPT"
```

## License

MIT
