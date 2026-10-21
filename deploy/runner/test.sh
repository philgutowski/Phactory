#!/bin/sh
# test.sh <commit>
#
# Run the runner repository's own tests, zig build test, for a commit
# build.sh has built (U22, KTD20), in the same kind of sandbox container and
# with the same Zig version. Exits non zero when a test fails.

. "$(dirname "$0")/lib.sh"

[ $# -eq 1 ] || die "usage: test.sh <commit>"
commit=$1
check_commit "$commit"

out=$(build_dir "$commit")
[ -f "$out/zig-version" ] || die "$commit is not built"
zig_version=$(cat "$out/zig-version")

source_archive "$commit" \
    | in_sandbox "$zig_version" "zig build test --cache-dir /tmp/zig-cache \
        --global-cache-dir /tmp/zig-global" \
    > /dev/null
echo "tests passed at $commit"
