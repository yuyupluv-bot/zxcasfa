#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Создан .env. Заполните его и повторите запуск: nano $ROOT/.env"
  exit 1
fi

env_value() {
  sed -n "s/^${1}=//p" .env | tail -n 1 | tr -d '\r'
}

required=(POSTGRES_PASSWORD VK_TOKEN VK_GROUP_ID SECRET_KEY ADMIN_PASSWORD)
for name in "${required[@]}"; do
  value="$(env_value "$name")"
  if [[ -z "$value" || "$value" == CHANGE_* || "$value" == PASTE_* ]]; then
    echo "Не заполнена обязательная переменная: $name"
    exit 1
  fi
done

postgres_password="$(env_value POSTGRES_PASSWORD)"
if [[ ! "$postgres_password" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "POSTGRES_PASSWORD должен содержать только латинские буквы и цифры."
  exit 1
fi

docker compose config --quiet
docker compose pull db
docker compose up -d --build

admin_port="$(env_value ADMIN_PORT)"
admin_port="${admin_port:-3000}"
echo
echo "Контейнеры запущены:"
docker compose ps
echo
echo "Логи бота: docker compose logs -f --tail=100 bot"
echo "Логи админ-панели: docker compose logs -f --tail=100 admin"
echo "Админ-панель: http://SERVER_IP:${admin_port}"
