# Проверки V179

- Python 3.11.17, установлены точные зависимости requirements.txt.
- Полный прогон: `python -m unittest discover -s tests -p 'test_*.py'`.
- Результат: 844 теста, 52.178 секунды, OK (без пропущенных тестов).
- Включены реальные ORM-сценарии на SQLite в изолированных процессах,
  конкурентный тест освобождения слота при зависшей отправке,
  тест аварийного завершения отдельного процесса watchdog с кодом 70.
- Дополнительно проверен реальный vk_api 11.9.9 с подменённым транспортом:
  API принимает переданную BoundedSession, запрос получает timeout=(5,15).
  Проверены наличие Long Poll check() и Firebase UnregisteredError/httpTimeout.
- Синтаксис 250 Python-файлов разобран успешно.
- Сравнение байтов подтверждает неизменность web/, android-driver-app/,
  migrations/, common/models.py, bot/handlers.py, queue_service.py,
  order_service.py, parallel_orders.py, keyboards.py, admin_main.py,
  common/admin_supervisor.py.
- Docker/Portainer, реальный PostgreSQL, реальные VK/FCM и телефоны не запускались.
  Блокировки PostgreSQL, реальная производительность и поведение на сервере
  не подтверждены этими локальными тестами. Нужна проверка после установки.
- Предупреждения ResourceWarning в старых тестах относятся к незакрытым
  файлам тестового кода, не к работе бота. Их проверки не отключались.
