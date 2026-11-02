#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -f .env ]] || { echo "Файл .env не найден"; exit 1; }

env_value() {
  sed -n "s/^${1}=//p" .env | tail -n 1 | tr -d '\r'
}

user="$(env_value POSTGRES_USER)"; user="${user:-taxi}"
db="$(env_value POSTGRES_DB)"; db="${db:-taxi}"
mkdir -p backups
file="backups/taxi_$(date +%Y-%m-%d_%H-%M-%S).dump"
docker compose exec -T db pg_dump \
  -U "$user" -d "$db" \
  --format=custom --no-owner --no-acl > "$file"
chmod 600 "$file"
echo "Резервная копия создана: $file"
