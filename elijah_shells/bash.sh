# Elijah Investigator bash hook
ELIJAH_CHECK_IT="__CHECK_IT__"
__elijah_command=""
__elijah_started=0
__elijah_preexec() { __elijah_command="$BASH_COMMAND"; __elijah_started=$(date +%s%3N); }
__elijah_precmd() {
  local exit_code=$? now duration_ms=0
  [[ -z "$__elijah_command" || "${ELIJAH_CAPTURE:-fail}" == off ]] && { __elijah_command=""; return; }
  now=$(date +%s%3N); duration_ms=$((now-__elijah_started))
  [[ "${ELIJAH_CAPTURE:-fail}" == all || $exit_code -ne 0 ]] && "$ELIJAH_CHECK_IT" hook --command "$__elijah_command" --cwd "$PWD" --exit-code "$exit_code" --duration-ms "$duration_ms" --capture all >/dev/null 2>&1 &
  __elijah_command=""
}
trap '__elijah_preexec' DEBUG
PROMPT_COMMAND="__elijah_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
