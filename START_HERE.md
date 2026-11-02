# Начните здесь

Для **BotHost: бот + админка + Firebase** читайте `README_BOTHOST_RU.md`.

- Точка входа: `bothost_main.py`.
- Использовать собственный Dockerfile и домен.
- Порт: `3000` в панели и `PORT=3000`.
- Настройки: `.env.bothost.example` (не старый `.env.example` для Compose).
- Обязательно: доступная внешняя PostgreSQL.
- Для уведомлений: передать Firebase service-account через `FIREBASE_SERVICE_ACCOUNT_B64`.
- APK из исходного архива уже в `downloads/BEZTAKSY.apk`; он использует старый домен. Если домен меняется, нужна пересборка APK.

`README_DOCKERHOSTING.md`, `docker-compose.yml` и старые инструкции оставлены для отдельного VPS/Compose. Для BotHost их выполнять не нужно.

Сборка подготовлена и проверена локально, но не развернута в вашем аккаунте. Реальные VK и FCM требуют проверки с вашими настройками.
