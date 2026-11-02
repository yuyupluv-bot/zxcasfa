# Развёртывание VK Taxi на DockerHosting

Этот комплект запускает в одном Docker Compose-проекте:

- PostgreSQL 16;
- VK-бота;
- веб-админ-панель;
- одноразовый сервис миграции базы данных.

Бот и админ-панель используют одну базу. PostgreSQL не публикуется в интернет. Админ-панель по умолчанию доступна на порту `3000`.

## Что потребуется от владельца

Подготовьте, но не отправляйте в переписку:

1. `VK_TOKEN` — токен сообщества VK.
2. `VK_CHAT_USER_TOKEN` — пользовательский токен, если используется единый водительский чат.
3. `VK_GROUP_ID` — числовой ID сообщества.
4. `ADMIN_VK_IDS` — VK ID администраторов через запятую.
5. Новый логин и пароль веб-админки.
6. Доступ к старой PostgreSQL или выгрузку `.dump`, если нужно сохранить пользователей, заявки, настройки и историю.
7. Необязательно: домен для админ-панели. Без домена панель работает по `http://IP_СЕРВЕРА:3000`.

Секреты хранятся только в серверном файле `.env`. Не добавляйте `.env` в Git и не отправляйте его другим людям.

## 1. Загрузка архива на сервер

Удобнее всего использовать WinSCP, FileZilla/SFTP или `scp`.

Загрузите `vk-taxi-dockerhosting-v96.zip` в `/root/`, затем подключитесь по SSH:

```bash
ssh root@IP_СЕРВЕРА
```

Распакуйте проект:

```bash
mkdir -p /opt/vk-taxi
cd /opt/vk-taxi
unzip /root/vk-taxi-dockerhosting-v96.zip
chmod +x scripts/*.sh
```

Если команды `unzip` нет:

```bash
apt-get update && apt-get install -y unzip
```

Для систем на базе Fedora/Alma/Rocky вместо этого:

```bash
dnf install -y unzip
```

## 2. Подготовка настроек

```bash
cd /opt/vk-taxi
cp .env.example .env
nano .env
```

Сгенерируйте безопасные значения:

```bash
openssl rand -hex 24   # POSTGRES_PASSWORD
openssl rand -hex 32   # SECRET_KEY
```

Вставьте их в `.env`. Пароль PostgreSQL должен состоять только из латинских букв и цифр; результат `openssl rand -hex` подходит.

Обязательно заполните:

```dotenv
POSTGRES_PASSWORD=...
VK_TOKEN=...
VK_GROUP_ID=...
ADMIN_VK_IDS=...
SECRET_KEY=...
ADMIN_LOGIN=...
ADMIN_PASSWORD=...
```

Если водительский чат используется, также заполните:

```dotenv
VK_CHAT_USER_TOKEN=...
DRIVER_FALLBACK_CHAT_PEER_ID=...
DRIVER_CHAT_PEER_ID=...
```

Для первого запуска по IP оставьте:

```dotenv
ADMIN_PORT=3000
ADMIN_BIND_IP=0.0.0.0
FLASK_COOKIE_SECURE=0
```

После подключения домена и HTTPS установите `FLASK_COOKIE_SECURE=1` и перезапустите админ-панель.

## 3А. Запуск с новой пустой базой

```bash
cd /opt/vk-taxi
./scripts/deploy.sh
```

Compose автоматически:

1. запустит PostgreSQL;
2. дождётся готовности базы;
3. применит миграции;
4. запустит бота;
5. запустит админ-панель.

Проверка:

```bash
docker compose ps
docker compose logs --tail=100 migrate
docker compose logs --tail=100 bot
docker compose logs --tail=100 admin
```

Админ-панель:

```text
http://IP_СЕРВЕРА:3000
```

## 3Б. Перенос существующей базы с BotHost

Этот вариант сохраняет пользователей, роли, заявки, настройки, рейтинги, таймеры и историю.

### Вариант 1 — имеется старая `DATABASE_URL`

Сначала остановите старого бота на BotHost, чтобы после создания копии в старую базу не записывались новые события.

На новом сервере:

```bash
cd /opt/vk-taxi
mkdir -p backups
export OLD_DATABASE_URL='СТАРАЯ_DATABASE_URL_ЦЕЛИКОМ'
docker run --rm postgres:16-alpine \
  pg_dump "$OLD_DATABASE_URL" \
  --format=custom --no-owner --no-acl \
  > backups/bothost.dump
unset OLD_DATABASE_URL
```

Запустите только новую базу и восстановите копию:

```bash
docker compose up -d db
./scripts/restore.sh backups/bothost.dump
```

Скрипт восстановит данные, применит новую миграцию и запустит бота с админ-панелью.

### Вариант 2 — BotHost выдаёт файл резервной копии

Загрузите файл в `/opt/vk-taxi/backups/`, затем:

```bash
cd /opt/vk-taxi
docker compose up -d db
./scripts/restore.sh backups/ИМЯ_ФАЙЛА.dump
```

Если выгрузка имеет формат `.sql`, восстановите её так:

```bash
docker compose up -d db
docker compose exec -T db psql -U taxi -d taxi < backups/ИМЯ_ФАЙЛА.sql
docker compose run --rm migrate
docker compose up -d bot admin
```

## 4. Правильное переключение с BotHost

Не запускайте старый и новый экземпляры бота одновременно.

Правильный порядок:

1. Создать резервную копию старой базы.
2. Остановить бота на BotHost.
3. Восстановить базу на DockerHosting.
4. Запустить новый Compose-проект.
5. Проверить в логах строку `STARTUP VK OK`.
6. Отправить боту тестовое сообщение.
7. Проверить обычную заявку, выбор водителя и админ-панель.
8. Только после проверки удалить старое приложение.

Админ-панель на старом хостинге также желательно остановить после проверки новой.

## 5. Команды управления

Статус:

```bash
docker compose ps
```

Логи бота:

```bash
docker compose logs -f --tail=100 bot
```

Логи админ-панели:

```bash
docker compose logs -f --tail=100 admin
```

Перезапуск:

```bash
docker compose restart bot admin
```

Остановка без удаления базы:

```bash
docker compose down
```

Запуск:

```bash
docker compose up -d
```

Не выполняйте `docker compose down -v`: параметр `-v` удалит том PostgreSQL.

## 6. Резервные копии

Ручная копия:

```bash
cd /opt/vk-taxi
./scripts/backup.sh
```

Файл появится в `/opt/vk-taxi/backups/`.

Пример ежедневного запуска в 03:20:

```bash
crontab -e
```

Добавьте:

```cron
20 3 * * * cd /opt/vk-taxi && ./scripts/backup.sh >> /var/log/vk-taxi-backup.log 2>&1
```

Копии желательно дополнительно выгружать на другой сервер или облачный диск. Копия на том же VPS не спасёт при потере диска.

## 7. Обновление бота

Перед обновлением:

```bash
cd /opt/vk-taxi
./scripts/backup.sh
```

Замените файлы проекта новой версией, не удаляя `.env` и папку `backups`, затем:

```bash
docker compose up -d --build
```

Бот при старте повторно проверит миграции и восстановит постоянные таймеры.

## 8. Безопасность

- Не публикуйте порт PostgreSQL `5432`; текущий Compose его не открывает.
- Откройте только SSH, порт админ-панели и позднее HTTPS.
- Используйте длинный пароль админ-панели.
- После настройки домена закройте публичный порт `3000` или ограничьте его firewall.
- Защитите Portainer отдельным сложным паролем и по возможности HTTPS.
- Лучше использовать SSH-ключи и отключить вход root по паролю после завершения настройки.
- Никогда не присылайте `VK_TOKEN`, `.env`, пароль PostgreSQL или root-пароль в чат.

## 9. Если бот не запускается

```bash
docker compose ps -a
docker compose logs --tail=200 migrate
docker compose logs --tail=200 bot
docker compose logs --tail=200 db
```

Частые причины:

- неверный `VK_TOKEN` или `VK_GROUP_ID`;
- старый бот ещё запущен;
- пароль PostgreSQL содержит символы, которые требуют URL-кодирования;
- неверная старая `DATABASE_URL`;
- сообщество VK не разрешает сообщения или Long Poll;
- порт `3000` закрыт firewall.

Не удаляйте том базы при диагностике.
