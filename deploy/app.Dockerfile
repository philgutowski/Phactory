# One image for the app and the worker (U29, KTD28). Compose runs it twice,
# with a different command and a different environment for each:
#
#   app     python -m slipway.app               the page and the webhooks
#   worker  python -m slipway.workflow.worker   the card workflow
#
# Build from the repository root:
#   docker build -f deploy/app.Dockerfile \
#     --build-arg SLIPWAY_UID=<the uid that owns $SLIPWAY_SESSIONS on the VPS> \
#     --build-arg SLIPWAY_GID=<its gid> \
#     -t <tag> .
#
# The image holds no secret and no project value: every value arrives by
# environment or by mount when compose starts a container.

ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm
ARG DOCKER_CLI_IMAGE=docker:27.3.1-cli

# The docker command line alone, for the worker to start builder containers
# (builders/cli.py). The app never uses it.
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE}

ARG SLIPWAY_UID=10001
ARG SLIPWAY_GID=10001

# Git for the worker's read only cache (gitread.py), certificates for TLS to
# the hosts, trackers, and identity provider.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /opt/slipway
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
 && rm -rf /opt/slipway/src /root/.cache

# The non root user both processes run as. Its home holds nothing: the
# configuration root is mounted at SLIPWAY_HOME.
RUN test "${SLIPWAY_UID}" != 0 \
 && groupadd --gid "${SLIPWAY_GID}" slipway \
 && useradd --uid "${SLIPWAY_UID}" --gid "${SLIPWAY_GID}" --create-home \
      --shell /usr/sbin/nologin slipway

ENV PYTHONUNBUFFERED=1 \
    SLIPWAY_HOME=/srv/slipway/home

USER ${SLIPWAY_UID}:${SLIPWAY_GID}
WORKDIR /home/slipway
CMD ["python", "-m", "slipway.app"]
