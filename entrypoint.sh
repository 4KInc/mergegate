#!/bin/bash
set -euo pipefail

# The Circle CLI authenticates from a local session directory rather than an
# API key. Cloud Run receives that session as a base64 tarball from Secret
# Manager and unpacks it before the server starts.
#
# The session is a BEARER CREDENTIAL: anyone holding it can move USDC from the
# agent wallets. It comes from Secret Manager, never a plain env var, and is
# never logged.
if [ -n "${CIRCLE_SESSION_B64:-}" ]; then
  mkdir -p /root/.circle-cli
  echo "$CIRCLE_SESSION_B64" | base64 -d | tar xz -C /root/.circle-cli
  echo "circle session unpacked"
else
  # Not fatal: the service still authenticates webhooks and serves status.
  # It simply cannot settle, and saying so at boot beats discovering it at
  # settlement time.
  echo "WARNING: no CIRCLE_SESSION_B64 — settlement will fail until one is provided" >&2
fi

exec python -m uvicorn mergegate.app:app --host 0.0.0.0 --port "${PORT:-8080}"
