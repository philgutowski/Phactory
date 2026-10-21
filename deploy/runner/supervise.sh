#!/bin/sh
# supervise.sh
#
# The restart loop of one runner instance (U22, KTD20). The runner is one
# process per job and per repository: it registers a just in time runner (a
# registration good for a single job) under the instance's name prefix,
# runs one job, and exits. So this script keeps one loop per repository the
# instance serves, and each loop launches the binary instances/<name>/current
# points at, waits for it to exit, and launches it again. A swap takes effect
# at the next launch; a process still waiting for a job when the link moves
# is asked to stop, so its next job runs on the new build.
#
# Settings, all from the environment (the VPS runner container's own
# environment file, or the laptop runner user's ~/.slipway-runner.env):
#
#   RUNNER_INSTANCE       the instance's name in the operator file
#   RUNNER_PREFIX         its runner name prefix
#   RUNNER_LABELS         its labels, comma separated
#   RUNNER_REPOSITORIES   space separated owner/name=TOKEN_ENV[:PREVIOUS_ENV]
#                         pairs, naming the variable that holds each
#                         repository's registration token and, during a
#                         rotation, the previous one
#   RUNNER_BINARY         the binary's name under bin/
#   RUNNER_ARGS           arguments for the binary, after the settings below
#   DATABASE_URL          the runner's shared database
#
# Each process receives RUNNER_REPOSITORY, RUNNER_TOKEN, RUNNER_NAME_PREFIX,
# RUNNER_LABELS, and DATABASE_URL, and none of the other repositories'
# tokens.

. "$(dirname "$0")/lib.sh"

: "${RUNNER_INSTANCE:?}" "${RUNNER_PREFIX:?}" "${RUNNER_LABELS:?}"
: "${RUNNER_REPOSITORIES:?}" "${RUNNER_BINARY:?}" "${DATABASE_URL:?}"
check_instance "$RUNNER_INSTANCE"
link="$ROOT/instances/$RUNNER_INSTANCE/current"

value_of() {
    [ -n "$1" ] || return 0
    printf '%s' "$1" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$' || die "not a variable name: $1"
    eval "printf '%s' \"\${$1:-}\""
}

serve() {
    repository=$1 names=$2
    token=$(value_of "${names%%:*}")
    case $names in *:*) previous=$(value_of "${names#*:}") ;; *) previous="" ;; esac
    child=""
    trap 'kill $child 2> /dev/null; exit 0' TERM
    while :; do
        # The build is resolved once per launch, so a swap during this
        # process's life never changes the binary it runs.
        build=$(readlink "$link" 2> /dev/null || true)
        if [ -z "$build" ] || [ ! -x "$build/bin/$RUNNER_BINARY" ]; then
            echo "no build to launch at $link" >&2
            sleep 30
            continue
        fi
        # The current token first; during a rotation, the previous one when
        # the current is not set yet.
        # shellcheck disable=SC2086 # the unset list and RUNNER_ARGS split on purpose
        env $unset RUNNER_REPOSITORY="$repository" RUNNER_TOKEN="${token:-$previous}" \
            RUNNER_NAME_PREFIX="$RUNNER_PREFIX" \
            "$build/bin/$RUNNER_BINARY" ${RUNNER_ARGS:-} < /dev/null &
        child=$!
        # A process launched before a swap would take its next job on the old
        # build while the records attribute that job to the new one. So once
        # the link moves, the process is asked to stop (TERM), once. The cron
        # swaps only when no process under the prefix is busy, and the runner
        # is expected to exit at once when idle and after its job when busy.
        asked=""
        while kill -0 "$child" 2> /dev/null; do
            sleep 5
            if [ -z "$asked" ] && [ "$(readlink "$link" 2> /dev/null || true)" != "$build" ]; then
                kill -TERM "$child" 2> /dev/null || true
                asked=1
            fi
        done
        wait "$child" || sleep 5
        sleep 1
    done
}

# Every token variable is removed from each runner process's environment;
# the process gets its own token as RUNNER_TOKEN only.
unset=""
for pair in $RUNNER_REPOSITORIES; do
    [ "${pair%%=*}" != "$pair" ] || die "no token variable for $pair"
    names=${pair#*=}
    unset="$unset -u ${names%%:*}"
    case $names in *:*) unset="$unset -u ${names#*:}" ;; esac
done

pids=""
for pair in $RUNNER_REPOSITORIES; do
    serve "${pair%%=*}" "${pair#*=}" &
    pids="$pids $!"
done

# When compose or launchd stops the supervisor, each loop stops its runner
# process and exits.
trap 'kill $pids 2> /dev/null; wait; exit 0' TERM INT
wait
