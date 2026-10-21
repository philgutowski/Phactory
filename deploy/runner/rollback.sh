#!/bin/sh
# rollback.sh <instance> [commit]
#
# Restore an instance's previous build (U22, KTD20). With a commit, the
# instance goes back to that build, which is how the rollback evaluator
# names the build its records say is previous; without one, to whatever the
# link previous holds. Either way the build being left becomes previous, so
# a mistaken rollback is undone by running this again.

. "$(dirname "$0")/lib.sh"

[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: rollback.sh <instance> [commit]"
instance=$1
check_instance "$instance"
dir="$ROOT/instances/$instance"

if [ $# -eq 2 ]; then
    commit=$2
else
    previous=$(readlink "$dir/previous" 2> /dev/null) || die "$instance has no previous build"
    commit=$(basename "$previous")
fi
exec "$(dirname "$0")/swap.sh" "$instance" "$commit"
