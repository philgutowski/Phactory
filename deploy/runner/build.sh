#!/bin/sh
# build.sh <repository url> <commit> <version file>
#
# Build one runner commit (U22, KTD20). Fetch the commit into the bare clone,
# read the Zig version the repository pins in <version file>, and build in a
# sandbox container, the source going in and the build coming out as tar
# archives. The result lands in builds/<commit>, renamed into place only
# once complete, so a half build never exists under that name. A commit
# already built is left as it is.
#
#   SLIPWAY_RUNNER_TARGET  a Zig target such as aarch64-macos, for a host
#                          whose binary the Linux sandbox cross compiles;
#                          unset builds for the sandbox's own platform

. "$(dirname "$0")/lib.sh"

[ $# -eq 3 ] || die "usage: build.sh <repository url> <commit> <version file>"
url=$1 commit=$2 version_file=$3
check_commit "$commit"

out=$(build_dir "$commit")
if [ -d "$out" ]; then
    echo "$commit is already built"
    exit 0
fi

mkdir -p "$ROOT/builds"
[ -d "$ROOT/repo" ] || git init -q --bare "$ROOT/repo"
git -C "$ROOT/repo" fetch -q "$url" "$commit"

zig_version=$(git -C "$ROOT/repo" show "$commit:$version_file" | tr -d ' \t\r\n')
printf '%s' "$zig_version" | grep -Eq '^[0-9A-Za-z.+-]+$' \
    || die "$version_file at $commit does not hold a Zig version"

target=""
if [ -n "${SLIPWAY_RUNNER_TARGET:-}" ]; then
    printf '%s' "$SLIPWAY_RUNNER_TARGET" | grep -Eq '^[a-z0-9_.-]+$' \
        || die "not a Zig target: $SLIPWAY_RUNNER_TARGET"
    target="-Dtarget=$SLIPWAY_RUNNER_TARGET"
fi

partial="$out.partial.$$"
trap 'rm -rf "$partial"' EXIT
mkdir -p "$partial"
source_archive "$commit" \
    | in_sandbox "$zig_version" "zig build $target -Doptimize=ReleaseSafe --prefix /tmp/out \
        --cache-dir /tmp/zig-cache --global-cache-dir /tmp/zig-global" \
    | tar -x -C "$partial"
ls "$partial"/bin/* > /dev/null 2>&1 || die "the build of $commit produced no bin/"
printf '%s\n' "$zig_version" > "$partial/zig-version"
mv "$partial" "$out"
trap - EXIT
echo "built $commit with Zig $zig_version"
