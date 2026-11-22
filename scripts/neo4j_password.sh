#!/usr/bin/env bash
# Generate a random Neo4j password into ./.env (gitignored) for `docker compose --profile neo4j`,
# and create the host folders for the Neo4j data, import and logs volumes.
# Idempotent: keeps an existing NEO4J_PASSWORD. Never commit .env.
set -euo pipefail
cd "$(dirname "$0")/.."
touch .env
chmod 600 .env
if grep -q '^NEO4J_PASSWORD=' .env; then
  echo "NEO4J_PASSWORD already set in .env"
else
  pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  printf 'NEO4J_PASSWORD=%s\n' "$pw" >> .env
  echo "NEO4J_PASSWORD written to .env (mode 600)"
fi
base="${HYDRA_HOST_ROOT:-$HOME/HydraNexus}/${HYDRA_NEO4J_SUBDIR:-neo4j}"
mkdir -p "$base/data" "$base/import" "$base/logs"
echo "Neo4j folders ready under $base"
