# The headless CLI builder image (U13, KTD12). One container per card.
#
# Build, with every value from an argument:
#   docker build -f deploy/builder.Dockerfile \
#     --build-arg BASE_IMAGE=<a Node image with the toolchains your projects need> \
#     --build-arg CLAUDE_CODE_VERSION=<a pinned version> \
#     --build-arg BUILDER_UID=<the uid that owns the worker's session directories> \
#     --build-arg BUILDER_GID=<its gid> \
#     -t <tag> .
#
# The image holds no secret and no project value. The adapter
# (src/phactory/builders/cli.py) starts it with the KTD12 baseline: a non root
# user, a read only root filesystem with only /workspace and /tmp writable, no
# Docker socket, the isolated builder network, every capability dropped, and
# the account token passed by environment only. The session script, the git
# credential helper, and every path come from the adapter at run time.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG CLAUDE_CODE_VERSION
ARG BUILDER_UID
ARG BUILDER_GID

# Git for the clone and the push, certificates for TLS to the code host.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The CLI at a pinned version. An empty version fails the build rather than
# installing whatever is newest.
RUN test -n "${CLAUDE_CODE_VERSION}" \
 && test -n "${BUILDER_UID}" && test "${BUILDER_UID}" != 0 \
 && test -n "${BUILDER_GID}" \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# The non root user. Its home is inside the workspace, the one writable mount,
# so nothing under the read only root needs to change at run time.
RUN (getent group "${BUILDER_GID}" || groupadd --gid "${BUILDER_GID}" builder) \
 && useradd --uid "${BUILDER_UID}" --gid "${BUILDER_GID}" --no-create-home \
      --home-dir /workspace/home --shell /bin/sh builder \
 && mkdir -p /workspace /run/phactory

# No self update and no telemetry: the network allows the code host and the
# package registries only.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER ${BUILDER_UID}:${BUILDER_GID}
WORKDIR /workspace
CMD ["/bin/false"]
