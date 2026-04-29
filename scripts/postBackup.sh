#!/bin/bash
set -e
docker compose start hermes-agent hermes-dashboard hermes-webui || docker compose up -d
