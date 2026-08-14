# The MergeGate API service.
#
# Distinct from verifier/Dockerfile in one important way: this container is
# ALLOWED outbound network access, because it must reach Circle to settle and
# GitHub to read submissions. The verifier runs sealed on a VPC with no TCP
# egress. Sealing this one would silently break settlement.

FROM python:3.12-slim-bookworm

# node is here only to run the Circle CLI, which is how MergeGate settles:
# Circle agent wallets are driven by the CLI, not the REST API.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @circle-fin/cli@0.0.6 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mergegate ./mergegate
COPY engine ./engine
# The dashboard renders from real receipts; they are evidence, so they ship.
COPY demo ./demo
RUN pip install --no-cache-dir .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    CIRCLE_ACCEPT_TERMS=1 \
    MERGEGATE_EAGER_APP=1

ENTRYPOINT ["/entrypoint.sh"]
