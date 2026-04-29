#!/usr/bin/env bash
set -o allexport; source .env; set +o allexport

mkdir -p ./workspace ./hermes-home
chown -R 10000:10000 ./workspace ./hermes-home

if ! grep -q '^HERMES_WEBUI_PASSWORD=' .env || [ -z "${HERMES_WEBUI_PASSWORD:-}" ]; then
    echo "HERMES_WEBUI_PASSWORD=$(openssl rand -hex 16)" >> .env
fi
