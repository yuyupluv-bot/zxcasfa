HOTFIX: MIGRATE EXIT 1 / ix_users_vk_id
======================================

Исправлены две стартовые команды пересчёта рейтингов, которые при каждом
запуске обновляли всю таблицу users и на существующей базе могли падать с:

  duplicate key value violates unique constraint "ix_users_vk_id"

Команды удалены из стартового schema guard. Структурные миграции и данные
не удаляются. Основной docker-compose.yml, volumes и настройки не изменены.

Обновление в Portainer:
1. Загрузить содержимое папки в корень production-репозитория с заменой.
2. Основной Stack: Compose path docker-compose.yml.
3. Не менять production Environment variables.
4. Нажать Pull and redeploy / Update stack с Re-pull image / Rebuild.
5. migrate должен завершиться с exit code 0.

Никогда не удаляйте volume vk_taxi_postgres_data.
