#!/usr/bin/env bash
set -o allexport; source .env; set +o allexport

mkdir -p ./workspace ./hermes-home
chown -R 10000:10000 ./workspace ./hermes-home

if ! grep -q '^ADMIN_PASSWORD=' .env || [ -z "${ADMIN_PASSWORD:-}" ]; then
    echo "ADMIN_PASSWORD=$(openssl rand -hex 16)" >> .env
fi
