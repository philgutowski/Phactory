# The Zig actions runner image (U22, KTD20, KTD25). One container per VPS
# runner instance, and the sandbox deploy/runner/build.sh and test.sh build
# and test each runner commit in.
#
# Build, with every value from an argument:
#   docker build -f deploy/runner.Dockerfile \
#     --build-arg BASE_IMAGE=<a Debian or Ubuntu image with the toolchains the jobs need> \
#     --build-arg RUNNER_UID=<a uid other than 0> \
#     -t <tag> .
#
# The image holds no runner binary, no secret, and no project value. The
# binary comes from the read only builds volume, where swap.sh points each
# instance's current link, and supervise.sh launches it. The registration
# tokens and the runner's database URL arrive by the runner's own
# environment file, which no other service receives (KTD28). The container
# runs without the Docker socket on the isolated builder network (KTD25).
#
# Jobs written inside the runner's subset install their own packages with
# sudo apt-get (Slipway's ci.yml does), so the runner user may run apt-get
# as root. apt-get can run any command through its options, so a job is in
# effect root inside the container; the boundary is the container itself,
# with no socket, no other service's secrets, and only the builder network.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG RUNNER_UID

# Git for checkouts, certificates for TLS, curl and xz for fetching the
# pinned Zig in the build sandbox, sudo for the jobs' apt-get.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl xz-utils tar sudo \
 && rm -rf /var/lib/apt/lists/*

RUN test -n "${RUNNER_UID}" && test "${RUNNER_UID}" != 0 \
 && useradd --uid "${RUNNER_UID}" --create-home --shell /bin/sh runner \
 && echo 'runner ALL=(root) NOPASSWD: /usr/bin/apt-get' > /etc/sudoers.d/runner-apt \
 && chmod 0440 /etc/sudoers.d/runner-apt

COPY deploy/runner/lib.sh deploy/runner/supervise.sh /opt/slipway-runner/

USER runner
WORKDIR /home/runner
ENV SLIPWAY_RUNNER_ROOT=/srv/slipway-runner
CMD ["/opt/slipway-runner/supervise.sh"]
