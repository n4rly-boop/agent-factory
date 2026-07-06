#!/usr/bin/env bash
# ai — spawn a REAL interactive `claude` TUI in a visible Terminal.app window,
# then let THIS agent type into it and read its screen.
#
# How: interactive claude runs inside a tmux session (gives it a real TTY); a
# Terminal.app window is opened attached to that session, so you watch the live
# TUI. The controlling agent drives it with `tmux send-keys` and reads with
# `tmux capture-pane` — no FIFOs, no headless mode. The actual interactive app.
#
#   ai up    [name]          launch interactive claude + open Terminal window
#   ai say   [name] <text>   type text into it and submit (Enter)
#   ai keys  [name] <args>   send raw tmux keys (e.g. Escape, C-c, /model)
#   ai screen[name]          print the current TUI screen
#   ai ask   [name] <text>   say, wait for the turn to finish, print its result
#   ai wait  [name]          block until the agent is idle or needs input
#   ai result[name]          print the last completed turn's text (from the log)
#   ai approve[name] [1|2|3] answer a tool-permission prompt (default 2)
#   ai ctx   [name]          print the agent's estimated context size (tokens)
#   ai compact[name]         run /compact in the agent (only when idle — safe point)
#   ai remote[name]          (re)launch with Remote Control so you drive it from the Claude web/app
#   ai revive[name] [id]     relaunch a killed agent with its memory (resume its session)
#   ai revivable             list downed agents (with a surviving log) you can revive by name
#   ai inbox   [--drain]     show blockers spawned agents escalated to you (--drain clears)
#   ai attach[name]          print the command to attach another viewer
#   ai down  [name]          quit claude + kill the session
#   ai list                  list running interactive agents
#
# Compaction is a JUDGMENT call, not an automatic trigger. After a turn, `ask`
# reports the agent's context size; YOU decide to `ai compact <name>` when it's
# grown large (rule of thumb: past ~200k tokens) AND compacting won't drop info
# the agent still needs AND the agent will keep being used. The auto-threshold
# $AI_COMPACT_AT defaults to 1000000 (effectively off) — set it lower only if you
# want a hard safety net regardless of judgment.
set -uo pipefail

S() { echo "ai-${1:-claude}"; }                  # tmux session name
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # this script's dir
CWD="${AF_CWD:-$(pwd)}"                           # where claude runs
FLAGS="${AI_CLAUDE_FLAGS:-}"                      # extra claude flags
STATE="${AF_ROOT:-/tmp/agent-factory}/.ai"        # tracks window ids
INBOX="${AF_ROOT:-/tmp/agent-factory}/inbox.tsv"  # agent→orchestrator escalations
NOTIFY="$HERE/notify.sh"                          # helper a spawned agent runs to escalate
MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"  # registry of spawned agents

# Record a spawned agent so its session log can be filtered/purged later.
# Columns: epoch  tool  name  session_id  cwd
_manifest() {
  mkdir -p "$(dirname "$MANIFEST")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "ai" "$1" "$2" "$CWD" >> "$MANIFEST"
}

# Close the Terminal window previously opened for this name (if any), by killing
# the processes on its tty (no modal prompt), with an AppleScript close fallback.
# Used by both `up` (to avoid orphaning the old window when relaunching) and
# `down`. Killing a window's tmux client also stops it dropping to a bare login
# shell — the empty-window symptom when a session is re-`up`ed underneath it.
_closewin() {
  local name="$1" wf="$STATE/win-$name" tf="$STATE/tty-$name" wid tty
  [ -f "$wf" ] && wid="$(cat "$wf")"
  [ -f "$tf" ] && tty="$(cat "$tf")"
  if [ -n "${tty:-}" ]; then pkill -t "${tty#/dev/}" 2>/dev/null || true; sleep 0.3; fi
  if [ -n "${wid:-}" ]; then
    local n; n="$(osascript -e "tell application \"Terminal\" to count (every window whose id is $wid)" 2>/dev/null)"
    [ "$n" != "0" ] && osascript >/dev/null 2>&1 -e "tell application \"Terminal\" to close (every window whose id is $wid) saving no"
  fi
  rm -f "$wf" "$tf"
}

# When resuming, claude pauses on a chooser for large/old sessions:
#   ❯ 1. Resume from summary   2. Resume full session as-is   3. Don't ask again
# Auto-answer it so revive lands on a ready agent, not a stuck prompt. Default 2
# (full session — revive means "bring back the whole memory"); AI_RESUME_MODE=1
# for the cheaper summary. No-op (after a short watch) if no chooser appears.
_answer_resume() {
  local name="$1" mode="${AI_RESUME_MODE:-2}" i
  for ((i=0; i<24; i++)); do
    if tmux capture-pane -t "$(S "$name")" -p 2>/dev/null | grep -qE 'Resume full session as-is|Resume from summary'; then
      tmux send-keys -t "$(S "$name")" -l "$mode"; tmux send-keys -t "$(S "$name")" Enter
      echo "[ai] '$name' resume chooser → option $mode (set AI_RESUME_MODE to change)"
      return 0
    fi
    sleep 0.5
  done
}

up() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux kill-session -t "$s" 2>/dev/null || true
  _closewin "$name"   # close any prior window for this name so we don't orphan it as a bare shell
  # Give the agent a known identity so its session log is filterable later:
  # --session-id <uuid> sets the log filename (<uuid>.jsonl); we record that
  # uuid in the manifest. (Note: --append-system-prompt is NOT written to the
  # transcript, so the manifest — not an in-log marker — is the durable link.)
  # If resuming, reuse the existing id instead of minting a new one.
  local id launchflags
  if [[ "$FLAGS" == *"--resume"* ]]; then
    id="$(printf '%s' "$FLAGS" | grep -oiE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
    launchflags="$FLAGS"
  else
    id="$(uuidgen | tr 'A-Z' 'a-z')"
    launchflags="--session-id $id $FLAGS"
  fi
  # Spawned agents run unattended, so by default skip permission prompts — else
  # they'd silently stall on the first tool gate with no one to approve. Opt out
  # with AI_SKIP_PERMS=0; suppressed if the caller already set a permission flag.
  if [ "${AI_SKIP_PERMS:-1}" != 0 ] \
     && [[ "$launchflags" != *"--dangerously-skip-permissions"* ]] \
     && [[ "$launchflags" != *"--permission-mode"* ]]; then
    launchflags="$launchflags --dangerously-skip-permissions"
  fi
  _manifest "$name" "$id"
  # Give the agent an IDENTITY and a back-channel to its orchestrator: env vars
  # (AF_AGENT/AF_INBOX/AF_NOTIFY) so notify.sh knows who it is + where to post,
  # plus an appended system prompt telling it to escalate real blockers instead
  # of stalling silently. Opt out with AI_NOTIFY_OFF=1. printf %q keeps the whole
  # thing safe through the sh -c that tmux runs the command under.
  local full envpfx sysprompt
  envpfx="$(printf 'AF_AGENT=%q AF_INBOX=%q AF_NOTIFY=%q AF_ROOT=%q' \
            "$name" "$INBOX" "$NOTIFY" "${AF_ROOT:-/tmp/agent-factory}")"
  if [ "${AI_NOTIFY_OFF:-0}" != 1 ]; then
    sysprompt="You are a spawned peer agent named '$name', launched by an orchestrator (another Claude) via the agent-factory skill. You run unattended with permissions skipped and no human necessarily watching. When you hit a real blocker you cannot resolve on your own — a decision only the orchestrator or a human can make, a missing secret or access you lack, an irreversible or destructive action you should not take alone, or repeated failure on the same step — do NOT stall silently. Escalate to your orchestrator by running: bash '$NOTIFY' 'one line: what you need'. That delivers the message tagged with your name ('$name'); then keep doing any work that does not depend on the answer. Escalate only genuine blockers, not routine progress. You may also send bash '$NOTIFY' --kind done 'summary' when you finish a long task."
    full="$(printf '%s claude %s --append-system-prompt %q' "$envpfx" "$launchflags" "$sysprompt")"
  else
    full="$(printf '%s claude %s' "$envpfx" "$launchflags")"
  fi
  # Big virtual size so the TUI has room; start interactive claude as the cmd.
  tmux new-session -d -s "$s" -x 220 -y 50 -c "$CWD" "$full"
  echo "[ai] interactive claude launched (session=$s id=$id cwd=$CWD)"
  # Open a visible Terminal.app window attached to it. Capture both the window
  # id AND its tty — `down` kills the tty's process to close the window cleanly
  # (AppleScript `close` pops a modal "terminate?" sheet that can't be dismissed
  # headlessly; killing the backing process closes the window with no prompt).
  mkdir -p "$STATE"
  printf '%s' "$id" > "$STATE/sid-$name"   # always: jsonl-based completion tracking needs this
  local meta err winid tty tmpf; tmpf="$(mktemp)"
  meta=$(osascript 2>"$tmpf" <<OSA
tell application "Terminal"
  activate
  set tb to do script "tmux attach -t $s"
  delay 0.2
  return (id of front window as text) & "|" & (tty of tb)
end tell
OSA
)
  err="$(cat "$tmpf" 2>/dev/null)"; rm -f "$tmpf"
  if [ -n "$meta" ] && [[ "$meta" == *"|"* ]] && [ -n "${meta%%|*}" ]; then
    winid="${meta%%|*}"; tty="${meta##*|}"
    printf '%s' "$winid" > "$STATE/win-$name"
    printf '%s' "$tty"   > "$STATE/tty-$name"
    echo "[ai] opened a Terminal.app window (id=$winid, $tty) showing the live TUI."
  else
    # osascript blocked (e.g. Apple Events not authorized, -1743) or no window —
    # agent is alive in tmux regardless; tell the human how to watch it.
    rm -f "$STATE/win-$name" "$STATE/tty-$name"
    echo "[ai] ⚠ couldn't open a Terminal window${err:+ ($err)}."
    echo "[ai]   likely macOS Automation permission: System Settings ▸ Privacy & Security ▸"
    echo "[ai]   Automation ▸ allow your terminal app to control \"Terminal\". Until then it's headless."
    echo "[ai]   watch it live:  tmux attach -t $s"
  fi
  # If resuming, clear the "summary vs full" chooser so the agent is ready to drive.
  [[ "$launchflags" == *"--resume"* ]] && _answer_resume "$name"
  echo "[ai] drive it:  ai say $name \"hello\"   |   watch screen:  ai screen $name"
}

say() {
  local name="${1:-claude}"; shift || true
  local s; s="$(S "$name")"; local msg="$*"
  [ -z "$msg" ] && { echo "[ai] usage: ai say $name <text>"; return 1; }
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name' — ai up $name"; return 1; }
  local try pending
  for try in 1 2; do
    tmux send-keys -t "$s" Escape        # close any autocomplete/file popup + clear line
    sleep 0.2
    tmux send-keys -t "$s" -l "$msg"     # literal text (no key interpretation)
    sleep 0.2
    tmux send-keys -t "$s" Enter         # submit
    sleep 0.5
    # Verify against the LIVE input box only — the last `❯` line in the capture.
    # Success = the box no longer holds OUR message. An empty box OR a greyed
    # autosuggestion (which differs from our text) both count as submitted; only
    # our exact message still sitting there means a popup ate the Enter.
    # (Submitted prompts also appear as `❯ ...` in scrollback history; the
    # tail -1 isolates the live box, ignoring those.)
    pending="$(tmux capture-pane -t "$s" -p | grep '❯' | tail -1 | sed 's/.*❯[[:space:]]*//')"
    [ "$pending" != "$msg" ] && { echo "[ai] sent to '$name': $msg"; return 0; }
    echo "[ai] input box still holds our text (popup?), retrying…"
  done
  echo "[ai] WARN: '$name' may not have submitted — check: ai screen $name"; return 1
}

keys() {
  local name="${1:-claude}"; shift || true
  tmux send-keys -t "$(S "$name")" "$@"
  echo "[ai] keys -> '$name': $*"
}

screen() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  tmux capture-pane -t "$s" -p
}

# --- State signals --------------------------------------------------------
# Completion is read from the agent's STRUCTURED LOG, not the screen: the screen
# is a viewport snapshot (scrollback-limited, racy between tool calls), whereas
# the jsonl is the authoritative append-only event stream. A finished turn lands
# an assistant record with `stop_reason: end_turn`.
#
# Permission pauses, however, leave NO trace in the jsonl (the pending tool_use
# isn't even flushed while it waits), so "needs input" can ONLY be read from the
# TUI. Hence the split below: log for done, screen for needs-input.

_log() {  # path to this agent's session jsonl, via the id we recorded at `up`
  local sid; sid="$(cat "$STATE/sid-$1" 2>/dev/null)"; [ -z "$sid" ] && return 1
  find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1
}
# Count completed turns. grep (not jq) so a half-written final line can't break
# it. Capture via $() because `grep -c` EXITS 1 on zero matches — an &&/|| chain
# would then emit a second "0" and yield the multiline "0\n0" that breaks -gt.
_endturns() {
  local f n; f="$(_log "$1")" || { echo 0; return; }
  [ -f "$f" ] || { echo 0; return; }
  n="$(grep -c '"stop_reason":"end_turn"' "$f" 2>/dev/null)"
  echo "${n:-0}"
}
# Estimate current context size in tokens: the full prompt the model last saw =
# input + cache-read + cache-creation tokens of the most recent assistant record.
# (output tokens aren't part of the next prompt; the cached buckets are.) Echoes 0
# if no usage yet. This is what we threshold for auto-compaction.
_ctx() {
  local f; f="$(_log "$1")" && [ -f "$f" ] || { echo 0; return; }
  jq -sRr 'split("\n") | map(fromjson? // empty)
           | map(select(.type=="assistant" and .message.usage)) | (last.message.usage // {})
           | ((.input_tokens//0)+(.cache_read_input_tokens//0)+(.cache_creation_input_tokens//0))' \
     "$f" 2>/dev/null || echo 0
}
# Actively generating: a live "(Ns · …)" timer on screen.
_busy()      { tmux capture-pane -t "$(S "$1")" -p | grep -qE '\([0-9]+s · '; }
# Paused on a tool-permission decision.
_permission(){ tmux capture-pane -t "$(S "$1")" -p | grep -qE 'Do you want to proceed\?|❯ 1\. Yes'; }

# Block until the in-flight turn ends, the agent pauses for input, or timeout.
# DONE requires BOTH a new end_turn in the log AND the timer gone — so a spurious
# early end_turn (e.g. a thinking block) followed by more tool calls won't fool
# it. Echoes DONE | NEEDS_INPUT | TIMEOUT.
_wait_turn() {
  local name="$1" base="$2" to="${3:-300}" i
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if [ "$(_endturns "$name")" -gt "$base" ] && ! _busy "$name"; then echo DONE; return; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# Print the text of the most recent completed turn, straight from the log — no
# scraping, no scrollback limit. `fromjson?` tolerates a partial trailing line.
result() {
  local f; f="$(_log "$1")" && [ -f "$f" ] || { echo "[ai] no log for '$1' (not spawned by this skill?)"; return 1; }
  jq -sRr 'split("\n") | map(fromjson? // empty)
           | map(select(.type=="assistant" and .message.stop_reason=="end_turn")
                 | [.message.content[]? | select(.type=="text") | .text] | join("\n"))
           | map(select(length>0)) | (last // "(no text in last turn)")' "$f" 2>/dev/null
}

# Block until the agent is idle/paused now (ad-hoc, when you don't have a
# baseline). Echoes DONE | NEEDS_INPUT | TIMEOUT.
wait_() {
  local name="${1:-claude}" to="${2:-300}" i settle=0
  tmux has-session -t "$(S "$name")" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  for ((i=0; i<to*2; i++)); do
    _permission "$name" && { echo NEEDS_INPUT; return; }
    if _busy "$name"; then settle=0; else settle=$((settle+1)); [ "$settle" -ge 4 ] && { echo DONE; return; }; fi
    sleep 0.5
  done
  echo TIMEOUT
}

# say, then wait for THIS turn to finish, then print its result from the log.
# Surfaces a permission pause instead of hanging or guessing "done".
ask() {
  local name="${1:-claude}"; shift || true
  local base; base="$(_endturns "$name")"
  say "$name" "$@" || return 1
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name"; _maybe_autocompact "$name"; _inbox_hint ;;
    NEEDS_INPUT) echo "[ai] ⚠ '$name' paused on a permission prompt — approve: ai approve $name [1|2|3]"
                 tmux capture-pane -t "$(S "$name")" -p | grep -vE '^[[:space:]]*$' | tail -8 ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working past ${AI_TIMEOUT:-300}s — check: ai result $name / ai screen $name" ;;
  esac
}

# Print the agent's estimated context size in tokens.
ctx() { echo "[ai] '$1' context ≈ $(_ctx "${1:-claude}") tokens"; }

# Run /compact in the agent to shrink its context. SAFE ONLY between turns: it
# refuses while the agent is generating (would interrupt) or paused on a
# permission prompt (the keystrokes would answer the prompt, not compact).
# /compact preserves a summary, so no task context is lost — only raw history.
compact() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  _busy "$name"       && { echo "[ai] '$name' is mid-turn — refusing to compact (would interrupt). retry when idle."; return 1; }
  _permission "$name" && { echo "[ai] '$name' is on a permission prompt — answer it first (ai approve $name)."; return 1; }
  local before; before="$(_ctx "$name")"
  echo "[ai] compacting '$name' (ctx ≈ ${before} tok)…"
  say "$name" "/compact" || return 1
  wait_ "$name" "${AI_TIMEOUT:-300}" >/dev/null   # compaction runs the busy timer; wait it out
  echo "[ai] '$name' compacted (was ≈ ${before} tok, now ≈ $(_ctx "$name") tok)."
}

# Called after a turn finishes (the natural safe point). Reports context size so
# the driver can DECIDE whether to compact (the judgment call). Only auto-runs
# /compact if context passes the hard net $AI_COMPACT_AT (default 1000000 =
# effectively off; lower it for an unconditional safety net). We only get here
# when the agent is idle, so neither the report nor a compact interrupts work.
_maybe_autocompact() {
  local c; c="$(_ctx "$1")"; [ "${c:-0}" -gt 0 ] || return
  if [ "$c" -gt 200000 ]; then
    echo "[ai] context ≈ ${c} tok (>200k) — consider 'ai compact $1' if it won't drop needed info and '$1' will keep being used."
  fi
  local at="${AI_COMPACT_AT:-1000000}"; [ "$at" = 0 ] && return
  if [ "$c" -gt "$at" ]; then
    echo "[ai] context ≈ ${c} tok > hard net ${at} — auto-compacting '$1'…"
    compact "$1"
  fi
}

# (Re)launch the named agent with Claude Code's Remote Control enabled, so the
# human can monitor and drive it from the Claude web app / phone. Reuses the
# agent's recorded session (so memory survives the relaunch) when its log still
# exists; otherwise starts fresh. Requires being signed in to claude.ai.
remote() {
  local name="${1:-claude}" sid="${2:-}"
  [ -z "$sid" ] && sid="$(cat "$STATE/sid-$name" 2>/dev/null)"
  [ -z "$sid" ] && sid="$(grep -P "\t$name\t" "$MANIFEST" 2>/dev/null | cut -f4 | tail -1)"
  tmux has-session -t "$(S "$name")" 2>/dev/null && down "$name"   # close the old window/session first
  if [ -n "$sid" ] && [ -n "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ]; then
    echo "[ai] launching '$name' with Remote Control, resuming session $sid"
    FLAGS="--resume $sid --remote-control $name $FLAGS"
  else
    echo "[ai] launching fresh '$name' with Remote Control"
    FLAGS="--remote-control $name $FLAGS"
  fi
  up "$name"
  echo "[ai] Remote Control on — open the Claude web app / phone to drive '$name' (sign-in required)."
}

# Answer a permission prompt (default 2 = allow & don't ask again), then wait for
# the resumed turn to finish and print its result.
approve() {
  local name="${1:-claude}" choice="${2:-2}" s; s="$(S "$name")"
  tmux has-session -t "$s" 2>/dev/null || { echo "[ai] no agent '$name'"; return 1; }
  local base; base="$(_endturns "$name")"
  tmux send-keys -t "$s" -l "$choice"; tmux send-keys -t "$s" Enter
  sleep 1   # let the prompt dismiss before we re-check state (else we'd read the stale prompt as NEEDS_INPUT)
  case "$(_wait_turn "$name" "$base" "${AI_TIMEOUT:-300}")" in
    DONE)        result "$name" ;;
    NEEDS_INPUT) echo "[ai] ⚠ another permission prompt for '$name' — ai approve $name" ;;
    TIMEOUT)     echo "[ai] ⚠ '$name' still working — ai result $name" ;;
  esac
}

attach() { echo "tmux attach -t $(S "${1:-claude}")"; }

# Relaunch a previously-`down`ed agent WITH its full memory, by resuming its
# recorded session. `down` keeps the log; only `afctl purge` deletes it — so
# revive works any time the log still exists. Resolves the session id from (in
# order) an explicit arg, the sid state file, or the last manifest entry.
revive() {
  local name="${1:-claude}" sid="${2:-}"
  [ -z "$sid" ] && sid="$(cat "$STATE/sid-$name" 2>/dev/null)"
  [ -z "$sid" ] && sid="$(grep -P "\t$name\t" "$MANIFEST" 2>/dev/null | cut -f4 | tail -1)"
  [ -z "$sid" ] && { echo "[ai] no recorded session for '$name' — see: ai revivable"; return 1; }
  [ -z "$(find "$HOME/.claude/projects" -type f -name "$sid.jsonl" 2>/dev/null | head -1)" ] \
    && { echo "[ai] session $sid log is gone (purged?) — can't revive '$name'"; return 1; }
  echo "[ai] reviving '$name' from session $sid"
  FLAGS="--resume $sid $FLAGS"   # up() detects --resume and reuses this id
  up "$name"
}

# List agents that CAN be revived by name: every ai-spawned agent in the manifest
# whose session log still exists (not purged) and which isn't currently running.
# `list` shows only live sessions, so this is how you find a downed agent's name.
revivable() {
  [ -f "$MANIFEST" ] || { echo "[ai] no manifest — nothing spawned yet"; return; }
  local out
  # latest manifest row per ai/<name>, then keep only downed-with-surviving-log
  out="$(awk -F'\t' '$2=="ai"{seen[$3]=$4} END{for(n in seen) print n"\t"seen[n]}' "$MANIFEST" \
        | while IFS=$'\t' read -r name id; do
            [ -z "$id" ] && continue
            tmux has-session -t "$(S "$name")" 2>/dev/null && continue   # running → not "revivable"
            [ -n "$(find "$HOME/.claude/projects" -type f -name "$id.jsonl" 2>/dev/null | head -1)" ] || continue
            printf '  %s\t(%s)\n' "$name" "$id"
          done)"
  if [ -z "$out" ]; then echo "[ai] no revivable agents (none downed with a surviving log)"
  else echo "[ai] revivable (run: ai revive <name>):"; printf '%s\n' "$out"; fi
}

down() {
  local name="${1:-claude}" s; s="$(S "$name")"
  tmux kill-session -t "$s" 2>/dev/null || true
  _closewin "$name"
  echo "[ai] '$name' down — session killed, window closed."
}

list() { tmux ls 2>/dev/null | grep '^ai-' || echo "[ai] none"; _inbox_hint; }

# --- Agent→orchestrator escalations ---------------------------------------
# Spawned agents post blockers to $INBOX via notify.sh (see `up`). The
# orchestrator (this session) reads them here. `inbox --drain` prints then
# clears so the same escalation isn't re-reported next turn.
inbox() {
  local f="$INBOX" drain=0 ts name kind msg hhmm
  [ "${1:-}" = "--drain" ] && drain=1
  [ -s "$f" ] || { echo "[ai] no notifications from spawned agents"; return; }
  echo "[ai] notifications from spawned agents:"
  while IFS=$'\t' read -r ts name kind msg; do
    # BSD (macOS) `date -r`, GNU `date -d @`; fall back to the raw epoch.
    hhmm="$(date -r "$ts" +%H:%M:%S 2>/dev/null || date -d "@$ts" +%H:%M:%S 2>/dev/null || echo "$ts")"
    printf '  %s  %-10s [%s]  %s\n' "$hhmm" "$name" "$kind" "$msg"
  done < "$f"
  [ "$drain" = 1 ] && { : > "$f"; echo "[ai] (inbox drained)"; }
}
# One-line nudge appended to ask/list output when escalations are waiting, so
# the orchestrator notices an agent that blocked while it was doing other work.
_inbox_hint() {
  [ -s "$INBOX" ] || return 0
  local n; n="$(grep -c '' "$INBOX" 2>/dev/null)"; [ "${n:-0}" -gt 0 ] || return 0
  echo "[ai] ⚠ $n pending escalation(s) from spawned agents — ai inbox"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;  say) say "$@" ;;  keys) keys "$@" ;;
  screen) screen "$@" ;;  ask) ask "$@" ;;  approve) approve "$@" ;;  attach) attach "$@" ;;
  ctx) ctx "$@" ;;  compact) compact "$@" ;;  remote) remote "$@" ;;
  wait) wait_ "$@" ;;  result) result "$@" ;;  inbox) inbox "$@" ;;
  revive) revive "$@" ;;  revivable) revivable ;;
  down) down "$@" ;;  list) list ;;
  *) sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
