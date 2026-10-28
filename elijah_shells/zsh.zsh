# Elijah Investigator zsh hook
zmodload zsh/datetime 2>/dev/null
autoload -Uz add-zsh-hook
typeset -g ELIJAH_CURRENT_COMMAND=''
typeset -g ELIJAH_COMMAND_STARTED=0
_elijah_preexec() { ELIJAH_CURRENT_COMMAND="$1"; ELIJAH_COMMAND_STARTED=$EPOCHREALTIME; }
_elijah_precmd() {
  local exit_code=$? command_lc
  integer duration_ms=0
  [[ -z "$ELIJAH_CURRENT_COMMAND" ]] && return
  (( duration_ms = (EPOCHREALTIME - ELIJAH_COMMAND_STARTED) * 1000 ))
  command_lc=${ELIJAH_CURRENT_COMMAND:l}
  [[ "${ELIJAH_CAPTURE:-fail}" == off ]] && { ELIJAH_CURRENT_COMMAND=''; return; }
  [[ -z "${ELIJAH_CAPTURE_SECRETS:-}" && ( "$command_lc" == *bearer* || "$command_lc" == *api_key* || "$command_lc" == *apikey* || "$command_lc" == *password* || "$command_lc" == *secret* ) ]] && { ELIJAH_CURRENT_COMMAND=''; return; }
  [[ "$ELIJAH_CURRENT_COMMAND" != check-it* && "$ELIJAH_CURRENT_COMMAND" != elijah* ]] && command "__CHECK_IT__" hook --command "$ELIJAH_CURRENT_COMMAND" --cwd "$PWD" --exit-code "$exit_code" --duration-ms "$duration_ms" --capture "${ELIJAH_CAPTURE:-fail}" >/dev/null 2>&1 &!
  ELIJAH_CURRENT_COMMAND=''
}
add-zsh-hook preexec _elijah_preexec
add-zsh-hook precmd _elijah_precmd
