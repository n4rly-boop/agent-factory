#!/usr/bin/env bash
# afctl — manage/clean up session logs of agents spawned by the factory.
#
# Every agent launched by ai.sh / af.sh runs with a known --session-id (so its
# log file is <uuid>.jsonl) recorded in the manifest. This tool uses the
# manifest to list and purge ONLY factory logs, never your manual sessions.
#
#   afctl list             show the manifest (every spawned agent)
#   afctl sessions         locate each manifest agent's .jsonl (present/missing)
#   afctl purge            delete every log listed in the manifest, then clear it
#   afctl purge --dry      show what purge WOULD delete, change nothing
set -uo pipefail

MANIFEST="$HOME/.claude/agent-factory/manifest.tsv"
PROJECTS="$HOME/.claude/projects"

_find_log() { find "$PROJECTS" -type f -name "$1.jsonl" 2>/dev/null | head -1; }

list() {
  [ -f "$MANIFEST" ] || { echo "[afctl] no manifest yet ($MANIFEST)"; return; }
  printf 'EPOCH\tTOOL\tNAME\tSESSION_ID\tCWD\n'; cat "$MANIFEST"
}

sessions() {
  [ -f "$MANIFEST" ] || { echo "[afctl] no manifest"; return; }
  while IFS=$'\t' read -r epoch tool name id cwd; do
    [ -z "$id" ] && continue
    if [ -n "$(_find_log "$id")" ]; then echo "PRESENT  $tool/$name  $id"
    else echo "missing  $tool/$name  $id"; fi
  done < "$MANIFEST"
}

purge() {
  local dry=0; [ "${1:-}" = "--dry" ] && dry=1
  [ -f "$MANIFEST" ] || { echo "[afctl] nothing to purge (no manifest)"; return; }
  local n=0 f
  while IFS=$'\t' read -r epoch tool name id cwd; do
    [ -z "$id" ] && continue
    f="$(_find_log "$id")"; [ -z "$f" ] && continue
    if [ "$dry" = 1 ]; then echo "WOULD delete  $tool/$name  $id  ($f)"
    else rm -f "$f" && { echo "deleted  $tool/$name  $id"; n=$((n+1)); }; fi
  done < "$MANIFEST"
  if [ "$dry" = 1 ]; then echo "[afctl] dry run — nothing changed."
  else echo "[afctl] purged $n log(s)."; : > "$MANIFEST"; echo "[afctl] manifest cleared."; fi
}

cmd="${1:-list}"; shift || true
case "$cmd" in
  list) list ;;  sessions) sessions ;;  purge) purge "$@" ;;
  *) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
