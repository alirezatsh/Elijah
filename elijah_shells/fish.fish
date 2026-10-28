# Elijah Investigator fish hook
set -g ELIJAH_CHECK_IT "__CHECK_IT__"
function __elijah_preexec --on-event fish_preexec
  set -g ELIJAH_COMMAND "$argv"
end
function __elijah_postexec --on-event fish_postexec
  set -l exit_code $status
  test -z "$ELIJAH_COMMAND"; and return
  test "$ELIJAH_CAPTURE" = off; and return
  if test "$ELIJAH_CAPTURE" = all; or test $exit_code -ne 0
    command $ELIJAH_CHECK_IT hook --command "$ELIJAH_COMMAND" --cwd "$PWD" --exit-code $exit_code --duration-ms 0 --capture all >/dev/null 2>&1 &
  end
  set -e ELIJAH_COMMAND
end
