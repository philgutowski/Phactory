# Shared by the deploy/runner scripts (U22, KTD20). Sourced, never run.
#
# Every script works under one root on the runner's host:
#
#   $SLIPWAY_RUNNER_ROOT/
#     repo/                     a bare clone of the runner repository
#     builds/<commit>/          one finished build: bin/ and zig-version
#     instances/<name>/current  a link to the build the supervisor launches next
#     instances/<name>/previous a link to the build current replaced
#
# The root defaults to $HOME/slipway-runner. A build is never edited after
# it is finished; a swap only moves the two links.

set -eu

ROOT=${SLIPWAY_RUNNER_ROOT:-"$HOME/slipway-runner"}

die() {
    echo "$(basename "$0"): $*" >&2
    exit 1
}

# A full forty character commit, so a branch or tag name never reaches git
# or a path.
check_commit() {
    printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$' || die "not a full commit: $1"
}

# An instance name from the operator file, used as a directory name.
check_instance() {
    printf '%s' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]*$' || die "not an instance name: $1"
}

build_dir() {
    printf '%s/builds/%s' "$ROOT" "$1"
}

# Run a shell snippet inside a throwaway sandbox container (a container that
# has no access to this host's files, its Docker socket, or any credential).
# The runner's source arrives on standard input as a tar archive and is
# unpacked under /tmp/src; whatever the snippet leaves in /tmp/out comes
# back on standard output as a tar archive, and everything the snippet
# prints goes to standard error. The root filesystem is read
# only, every capability is dropped, and the user is not root. The network
# stays on so the pinned Zig and the runner's declared packages can be
# fetched.
#
#   RUNNER_IMAGE                     the image built from deploy/runner.Dockerfile
#   SLIPWAY_RUNNER_CONTAINER         the container command, docker by default
#   SLIPWAY_RUNNER_SANDBOX_NETWORK   the network the sandbox joins, bridge by default
in_sandbox() {
    : "${RUNNER_IMAGE:?set RUNNER_IMAGE to the runner image}"
    zig_version=$1
    snippet=$2
    "${SLIPWAY_RUNNER_CONTAINER:-docker}" run --rm -i \
        --network "${SLIPWAY_RUNNER_SANDBOX_NETWORK:-bridge}" \
        --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
        --read-only --tmpfs /tmp:rw,exec,size=4g \
        --env ZIG_VERSION="$zig_version" --env HOME=/tmp \
        --entrypoint /bin/sh "$RUNNER_IMAGE" -c "
set -eu
mkdir -p /tmp/src /tmp/out /tmp/zig
tar -x -C /tmp/src
arch=\$(uname -m)
cd /tmp/zig
for name in zig-\$arch-linux-\$ZIG_VERSION zig-linux-\$arch-\$ZIG_VERSION; do
    if curl -fsSL https://ziglang.org/download/\$ZIG_VERSION/\$name.tar.xz -o zig.tar.xz; then
        tar -xJf zig.tar.xz
        PATH=/tmp/zig/\$name:\$PATH
        break
    fi
done
command -v zig > /dev/null || { echo \"no Zig \$ZIG_VERSION for \$arch\" >&2; exit 1; }
cd /tmp/src
{ $snippet; } >&2
tar -c -C /tmp/out .
"
}

# The source of commit as a tar archive on standard output.
source_archive() {
    git -C "$ROOT/repo" archive --format=tar "$1"
}
