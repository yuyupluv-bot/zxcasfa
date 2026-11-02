# БЕЗТАКСЫ — приложение водителя 1.7.1

Нативное Android-приложение для надёжных звуковых уведомлений о заявках. Оно не держит постоянный фоновый процесс: Firebase будит приложение только при поступлении push-события, после чего на ограниченное время запускается foreground service.

## Возможности

- подключение одноразовым шестизначным кодом из VK-бота;
- лёгкий интерфейс с боковым меню «Главная / Настройки»;
- светлая и тёмная темы;
- отдельное включение обычных, параллельных заявок и отмен;
- настройка громкости сигнала и вибрации;
- проверка разрешения уведомлений и ограничений батареи;
- удаление push-токена с сервера при отключении устройства;
- только HTTPS, без доступа к геолокации, контактам, файлам и телефону.

## Сборка debug

Положите настоящий `google-services.json` в `app/`, затем:

```bash
gradle :app:assembleDebug \
  -PDRIVER_APP_SERVER_URL=https://panel.bezrateadminps.ru \
  -PPRIVACY_POLICY_URL=https://panel.bezrateadminps.ru/privacy
```

## Сборка для RuStore

1. Создайте и сохраните release keystore.
2. Скопируйте `keystore.properties.example` как `keystore.properties` и заполните локально.
3. Не добавляйте keystore, пароли и `google-services.json` в Git.
4. Выполните:

```bash
gradle :app:bundleRelease \
  -PDRIVER_APP_SERVER_URL=https://panel.bezrateadminps.ru \
  -PPRIVACY_POLICY_URL=https://panel.bezrateadminps.ru/privacy
```

AAB: `app/build/outputs/bundle/release/app-release.aab`.

Перед публикацией выполните пункты из `../RUSTORE_RELEASE_CHECKLIST_RU.md`.
