# claude-headless-via-tui

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

Exit code: `0` on success, `1` on failure (reason on stderr).

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

## License

MIT
