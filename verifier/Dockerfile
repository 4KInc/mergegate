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

# cryptography is the only library the grading code needs beyond the standard
# library: the job imports the contract, hashing and workspace modules, and
# nothing that reaches a network. Verified by importing the job entry point and
# checking that fastapi, web3, google-cloud and friends stay unloaded.
RUN pip install --no-cache-dir cryptography==44.0.0

# The evaluator itself. Until this was here the image held python, git and
# pytest and no MergeGate code at all, which is why the sealed job was
# specified but never the thing that graded: there was nothing in it to run.
#
# Only what the job imports. Shipping the API service, the payment rails or the
# Gemini modules into a sealed grader would put code that talks to networks and
# holds credentials inside the one container built to do neither.
COPY mergegate/__init__.py            /opt/mergegate/mergegate/__init__.py
COPY mergegate/contract.py            /opt/mergegate/mergegate/contract.py
COPY mergegate/hashing.py             /opt/mergegate/mergegate/hashing.py
COPY mergegate/engine.py              /opt/mergegate/mergegate/engine.py
COPY mergegate/paths.py               /opt/mergegate/mergegate/paths.py
COPY mergegate/submission.py          /opt/mergegate/mergegate/submission.py
COPY mergegate/verifier/              /opt/mergegate/mergegate/verifier/
COPY engine/gateway/                  /opt/mergegate/gateway/

ENV PYTHONPATH=/opt/mergegate

# Workspace is the only writable location the run gets. /mnt/eval is where the
# platform mounts the evaluation volume; the job reads its inputs and writes its
# manifest there, which is how anything crosses a boundary with no egress.
RUN mkdir -p /workspace /mnt/eval && chown verifier:verifier /workspace /mnt/eval

USER verifier
WORKDIR /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONNOUSERSITE=1 \
    PIP_NO_INDEX=1

# The job entry point. Note what it is not: the *pinned commands* still come
# from the contract and are run by the evaluator inside this process, so a
# submission cannot influence what executes by changing the image's command.
#
# CMD stays /bin/false so an image started without an explicit command does
# nothing rather than grading something by accident.
ENTRYPOINT []
CMD ["/bin/false"]
