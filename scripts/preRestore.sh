#!/bin/bash
set -e
docker compose stop hermes-agent hermes-dashboard hermes-webui || true
