#!/bin/sh
# swap.sh <instance> <commit>
#
# Point an instance at a finished build (U22, KTD20). The link current moves
# to builds/<commit> and previous keeps the build it replaced. A runner
# process already running keeps its binary; supervise.sh launches the new
# one after the current job ends, which is why the cron swaps only an
# instance with no busy process. Swapping to the build already current
# changes nothing.

. "$(dirname "$0")/lib.sh"

[ $# -eq 2 ] || die "usage: swap.sh <instance> <commit>"
instance=$1 commit=$2
check_instance "$instance"
check_commit "$commit"

target=$(build_dir "$commit")
[ -d "$target/bin" ] || die "$commit is not built"

dir="$ROOT/instances/$instance"
mkdir -p "$dir"
current=$(readlink "$dir/current" 2> /dev/null || true)
if [ "$current" = "$target" ]; then
    echo "$instance is already on $commit"
    exit 0
fi
if [ -n "$current" ]; then
    ln -sfn "$current" "$dir/previous"
fi
ln -sfn "$target" "$dir/current"
echo "$instance now launches $commit"
