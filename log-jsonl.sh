#!/usr/bin/env bash
set -u

LOG_DIR="${HOME}/.claude/session-logs"
mkdir -p "$LOG_DIR" 2>/dev/null || exit 0

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || payload='{}'

iso_ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
epoch_s="$(date +"%s")"

# Sanitize a session_id before using it as a filename: keep only
# [A-Za-z0-9._-] and fall back to "unknown-session" if nothing is left.
sanitize_session() {
  local raw="${1:-}"
  local clean
  clean="$(printf '%s' "$raw" | tr -cd 'A-Za-z0-9._-')"
  if [ -z "$clean" ]; then
    printf '%s' "unknown-session"
  else
    printf '%s' "$clean"
  fi
}

# Write one JSONL line per hook call, routed by session_id so each session
# accumulates into its own file: ${LOG_DIR}/${session_id}.jsonl
# Keep full payload for lossless replay; add extracted index fields for
# fast filtering.
event_name="$(printf '%s' "$payload" | jq -r '.hook_event_name // ""' 2>/dev/null || true)"
session_id="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
agent_id="$(printf '%s' "$payload" | jq -r '.agent_id // ""' 2>/dev/null || true)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""' 2>/dev/null || true)"
tool_use_id="$(printf '%s' "$payload" | jq -r '.tool_use_id // ""' 2>/dev/null || true)"

safe_session="$(sanitize_session "$session_id")"
LOG_FILE="${LOG_DIR}/${safe_session}.jsonl"

jq -cn \
  --arg ts "$iso_ts" \
  --arg epoch "$epoch_s" \
  --arg event "$event_name" \
  --arg session "$session_id" \
  --arg agent "$agent_id" \
  --arg tool "$tool_name" \
  --arg tuid "$tool_use_id" \
  --argjson payload "$payload" \
  '{
    ts: $ts,
    epoch_s: ($epoch | tonumber),
    hook_event_name: $event,
    session_id: $session,
    agent_id: (if $agent == "" then null else $agent end),
    tool_name: (if $tool == "" then null else $tool end),
    tool_use_id: (if $tuid == "" then null else $tuid end),
    payload: $payload
  }' >> "$LOG_FILE" 2>/dev/null || true

exit 0
