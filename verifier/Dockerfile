# The sealed verifier image.
#
# Contracts pin this image *by digest*, so every property below is part of what
# the buyer agreed to and what the receipt binds. Rebuilding changes the digest,
# which means an existing contract keeps running against the image it was funded
# against — image drift cannot silently change a graded result.
#
# Deliberately absent: any cloud SDK, any credential helper, any network tool.
# The job runs with default-deny egress, so a package installer inside the
# graded run would fail anyway; not shipping one removes the temptation and the
# attack surface both.

FROM python:3.12-slim-bookworm@sha256:034724ef64585eeb0e82385e9aabcbeabfe5f7cae2c2dcedb1da95114372b6d7

# git is needed to materialize the pinned base tree; nothing else is.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.* \
    && rm -rf /var/lib/apt/lists/*

# pytest is the graded runner, version-pinned so a rebuild cannot change how an
# existing contract grades.
RUN pip install --no-cache-dir pytest==8.3.4

# Unprivileged. The graded run never needs root, and Cloud Run enforces the
# non-root requirement for gVisor sandboxed workloads.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin verifier

# Workspace is the only writable location the run gets.
RUN mkdir -p /workspace && chown verifier:verifier /workspace

USER verifier
WORKDIR /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONNOUSERSITE=1 \
    PIP_NO_INDEX=1

# No ENTRYPOINT that runs anything on its own. The job is handed the pinned
# argv from the contract; there is no default command a submission could
# influence into running.
ENTRYPOINT []
CMD ["/bin/false"]
