# The headless CLI builder image (U13, KTD12). One container per card.
#
# Build, with every value from an argument:
#   docker build -f deploy/builder.Dockerfile \
#     --build-arg BASE_IMAGE=<a Node image with the toolchains your projects need> \
#     --build-arg CLAUDE_CODE_VERSION=<a pinned version> \
#     --build-arg PLAYWRIGHT_MCP_VERSION=<a pinned @playwright/mcp version> \
#     --build-arg BUILDER_UID=<the uid that owns the worker's session directories> \
#     --build-arg BUILDER_GID=<its gid> \
#     -t <tag> .
#
# The image holds no secret and no project value. The adapter
# (src/slipway/builders/cli.py) starts it with the KTD12 baseline: a non root
# user, a read only root filesystem with only /workspace and /tmp writable, no
# Docker socket, the isolated builder network, every capability dropped, and
# the account token passed by environment only. The session script, the git
# credential helper, and every path come from the adapter at run time.
#
# The headless browser (U27): the Playwright MCP server and the Chromium it
# drives, so a project's default builder_tools, headless_browser, works in
# the container. The adapter names the server in the session's MCP
# configuration (src/slipway/builders/tools.py) and runs it with no window
# and no Chromium sandbox, since the container itself is the sandbox.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG CLAUDE_CODE_VERSION
ARG PLAYWRIGHT_MCP_VERSION
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

# The Playwright MCP server at a pinned version, and the Chromium build its
# own Playwright expects, with the system libraries Chromium needs. The
# browsers go under /opt, read only at run time, where every user finds them;
# the adapter names the same folder in the server's own environment
# (IMAGE_BROWSERS in src/slipway/builders/tools.py).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# The package has named its binary mcp-server-playwright, mcp, and
# playwright-mcp in different releases, each pointing at its cli.js, so the
# one name the adapter runs is linked to cli.js whatever the pinned version.
RUN test -n "${PLAYWRIGHT_MCP_VERSION}" \
 && npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
 && cd "$(npm root -g)/@playwright/mcp" \
 && test -f cli.js \
 && chmod a+rx cli.js \
 && ln -sf "$PWD/cli.js" /usr/local/bin/playwright-mcp \
 && npx --no-install playwright install --with-deps chromium \
 && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
 && command -v playwright-mcp \
 && rm -rf /var/lib/apt/lists/* \
 && npm cache clean --force

# The non root user. Its home is inside the workspace, the one writable mount,
# so nothing under the read only root needs to change at run time.
RUN (getent group "${BUILDER_GID}" || groupadd --gid "${BUILDER_GID}" builder) \
 && useradd --uid "${BUILDER_UID}" --gid "${BUILDER_GID}" --no-create-home \
      --home-dir /workspace/home --shell /bin/sh builder \
 && mkdir -p /workspace /run/slipway

# No self update and no telemetry: the network allows the code host and the
# package registries only.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER ${BUILDER_UID}:${BUILDER_GID}
WORKDIR /workspace
CMD ["/bin/false"]
