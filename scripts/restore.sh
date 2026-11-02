#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Использование: $0 /путь/к/backup.dump"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP="$(realpath "$1")"
cd "$ROOT"
[[ -f .env ]] || { echo "Файл .env не найден"; exit 1; }

env_value() {
  sed -n "s/^${1}=//p" .env | tail -n 1 | tr -d '\r'
}

user="$(env_value POSTGRES_USER)"; user="${user:-taxi}"
db="$(env_value POSTGRES_DB)"; db="${db:-taxi}"

echo "Останавливаю бот и админ-панель..."
docker compose stop bot admin || true

echo "Восстанавливаю PostgreSQL из $BACKUP..."
docker compose exec -T db pg_restore \
  -U "$user" -d "$db" \
  --clean --if-exists --no-owner --no-acl < "$BACKUP"

echo "Применяю миграции и запускаю сервисы..."
docker compose run --rm migrate
docker compose up -d bot admin
docker compose ps
