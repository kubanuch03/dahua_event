# ТЗ: консолидация dahua_event (2 роли вместо 4 директорий)

## 1. Текущее состояние

4 директории (`Camera_Entrance`, `Camera_Exit`, `Camera_White_Entrance`, `Camera_White_Exit`), по факту 2 уникальные роли — `Camera_White_*` не про SmartParking whitelist, это дубль ролей entrance/exit под вторые физические камеры (единственная разница — строка `"camera"` в payload и IP). `requirements.txt`/`Dockerfile` идентичны байт-в-байт между всеми копиями (проверено `diff`), `logger.py`/`traffic_monitor.py`/`traffic_info.py`/`smart_parking.py`/`dolibarr.py`/`autovision.py` — тоже идентичны. NetSDK wheel (37MB) продублирован 4 раза.

Уже сделано (эта сессия, только Camera_Entrance/Camera_Exit):
- `main.py` не хардкодит IP — тянет камеры у `GET /parking/api/cameras/config/` (Django), фильтрует по `CAMERA_ROLE`+`CAMERA_SLOT`.
- Починен баг с перепутанными `camera_id`/`camera_name` в вызове `start_traffic_monitor` (именованные аргументы).
- `push_parking_event` — ретраи с backoff вместо одной попытки без повтора.
- Debug-снимки в `data/Global/` — выключены по умолчанию (`SAVE_DEBUG_SNAPSHOTS`), раньше росли без ограничения (на Balykchy — 23-24GB/год на роль).

## 2. КРИТИЧНАЯ находка (меняет план) — resolve камеры по `alias`

`DataProcessor.data_event_processing()` (`parking/services/parking_logic/parking_service.py:24-30`):
```python
camera_name = data.get('camera')
camera_instance = Camera.objects.filter(alias=camera_name).first()
if not camera_instance:
    return format_response(success=False, description=f"Камера '{camera_name}' не найдена.")
```
Поле `"camera"` в payload, который dahua_event шлёт в `/parking/data_process/` — это **не свободный ярлык**, а точное совпадение с `Camera.alias` в БД. Сейчас в `callbacks.py` каждой из 4 копий это захардкожено буквально ("Entry"/"Exit"/"Entry2"/"Exit2") — значения, подобранные под Balykchy. **Совпадают ли они с реальными `alias` у 4 камер archa — неизвестно, archa недоступна для проверки.** Если не совпадают (или `alias` вообще не заполнен — поле `null=True, blank=True`), каждое событие проезда будет молча отклоняться с "Камера не найдена".

Это не косметика, а блокер: без решения этого пункта dahua_event физически не сможет передать ни одного события на archa, даже если всё остальное развёрнуто идеально.

### Правильное решение
Не хардкодить label в dahua_event вообще. `Camera.alias` уже есть в БД — API-эндпоинт должен его отдавать, а dahua_event — использовать значение, которое реально получил от Django для конкретной камеры, а не выводить его из `CAMERA_ROLE`.

Изменения:
1. `CameraConfigAPIView` (`parking/api/camera_views.py`) — добавить `alias` в ответ (сейчас отдаёт `id/ip_address/port/username/password/action/lane_type`, `alias` не хватает).
2. `main.py` — передавать `camera["alias"]` в `Callbacks.set_camera_info(...)` дополнительным полем (сейчас туда идут только `camera_code/camera_id/camera_name`, `camera_name` используется только для имени debug-файла, не для payload).
3. `callbacks.py` — заменить хардкод `"camera": "Entry"` на `"camera": Callbacks.camera_alias` (читать из класса, как уже делается для `camera_code`/`camera_id`).
4. **Отдельная задача не для dahua_event, а для настройки archa**: убедиться, что все 4 `Camera` в БД archa имеют осмысленный `alias` (сейчас не проверял — нужен доступ к archa). Если `alias` пуст — заполнить до первого реального теста.

## 3. Целевая архитектура

Один Docker-образ вместо четырёх, роль и слот — только через env при запуске контейнера, ничего не хардкодится в коде:

```
dahua_event/
├── Dockerfile              # один, сейчас идентичны у всех копий — просто взять любой
├── requirements.txt        # один, идентичны у всех копий
├── main.py                 # общий (уже готов у Entrance/Exit, слить в один файл)
├── logger.py
├── services/
│   ├── traffic_monitor.py  # идентичен, взять как есть
│   ├── traffic_info.py     # идентичен, взять как есть
│   ├── smart_parking.py    # идентичен (с ретраями), взять как есть
│   └── callbacks.py        # правка из п.2: "camera" из Callbacks.camera_alias, не хардкод
├── dist/
│   └── NetSDK-2.0.0.1-py3-none-linux_x86_64.whl   # одна копия вместо четырёх
├── docker-compose.yml      # 4 сервиса из ОДНОГО образа (build: единый context), разные env
└── .env                    # SMARTPARKING_API_URL / _TOKEN / _DATA_PROCESS_URL (общие для всех 4)
```

Удаляется целиком: `Camera_Entrance/`, `Camera_Exit/`, `Camera_White_Entrance/`, `Camera_White_Exit/` как отдельные директории (их актуальный код уже смёржен в файлы выше), `main_frigate.py` (нигде не вызывается), `dolibarr.py`/`autovision.py` (мёртвый код, не используется в реальном пути выполнения).

`docker-compose.yml` — 4 сервиса (`camera_entrance_1/2`, `camera_exit_1/2`), у каждого `build: context: .` (один и тот же для всех четырёх — Docker переиспользует кэш слоёв, включая слой с 37MB wheel, реально скачает/распакует один раз), различаются только `environment: CAMERA_ROLE / CAMERA_SLOT`.

## 4. Порядок работ

1. **[БЛОКЕР — archa должна быть доступна]** Проверить `Camera.alias` для всех 4 камер в БД archa — если пусто/не совпадает с ожидаемым, договориться о значениях и проставить. Не сделано — archa была недоступна (DHCP churn на собственном IP сервера) на момент выполнения остальных шагов.
   - **Заодно проверить `Camera.password`** — найден реальный баг локально (2026-09-02): `EncryptedCharField.from_db_value` при несовпадении текущего `FIELD_ENCRYPTION_KEY`/`SECRET_KEY` с тем, каким значение было зашифровано, НЕ кидает ошибку, а молча возвращает сырой Fernet-шифротекст как есть (см. докстринг `common/fields.py`). После ротации секретов в этой же сессии 2 локальные камеры (`Entry`/`Exit`, созданы 2026-08-10, до ротации) отдавали именно шифротекст вместо пароля — `dahua_event` получил бы это как "пароль" и SDK-логин тихо проваливался бы с неверными кредами, без явной ошибки на стороне dahua_event. Старый ключ утерян (пользователь подтвердил), локально почин: пароль просто переустановлен под текущим ключом (`admin123` - заглушка, не настоящий пароль камеры). **На archa эту же проверку нужно сделать до реального теста** — просто дёрнуть `camera.password` через `manage.py shell` для всех 4 камер и убедиться, что это выглядит как реальный пароль, а не `gAAAAA...` шифротекст.
2. ✅ Добавлен `alias` в `CameraConfigAPIView` (`src/parking/api/camera_views.py`) + тест `test_camera_config_api.py` проверяет `data[0]["alias"]`. Коммиты `1dfca8c`, `1f6cd05`, запушены в `arch/rework` и `main`.
3. ✅ Файлы слиты в единую структуру п.3 в `/home/pc-kaltynbek-uulu/Dmain/PB/dahua_event/` (один `Dockerfile`/`requirements.txt`/`main.py`/`services/`/`dist/`), старые 4 директории (`Camera_Entrance`, `Camera_Exit`, `Camera_White_Entrance`, `Camera_White_Exit`) удалены. `CAMERA_ROLE` теперь обязателен (`os.environ["CAMERA_ROLE"]`, без дефолта под конкретную роль — раньше каждая копия дефолтилась на свою роль, что после слияния было бы вводящим в заблуждение поведением).
4. ✅ `services/callbacks.py`: `"camera"` берётся из `Callbacks.camera_alias` (пробрасывается через `main.py` → `start_traffic_monitor(camera_alias=...)` → `Callbacks.set_camera_info(...)`). Если у камеры `alias` пуст — `main.py` пишет `logger.error` при старте (не блокирует запуск, просто предупреждает заранее, а не постфактум через молчаливый reject на стороне Django).
5. ✅ `python -m py_compile` на все файлы — чисто. `docker build .` — успешно (образ собирается, `dist/NetSDK...whl` ставится). Дополнительно проверено `docker run --entrypoint python ... -c "import main; import services.callbacks; ...; from NetSDK.NetSDK import NetClient"` — все импорты резолвятся, включая vendor-wheel NetSDK.
6. ⏳ Не сделано — разворачивание на archa (`docker compose up --build -d`, сеть `prod_db_network`) ждёт шага 1 (archa должна быть доступна и `Camera.alias` заполнен).
7. ⏳ Не сделано — живой тест с камерой, зависит от шагов 1 и 6.

## 5. Вне рамок этого ТЗ (осознанно не трогаем)

- Настоящая логика "белый список" (`lane_type`/`barrier_settings`, уже есть в `CameraConfigAPIView`) — использование этого поля в dahua_event (например, отдельная обработка проезда по льготе) не требуется прямо сейчас, значения совпадений между 4 камерами archa и лентами not yet известны.
- Watchdog на зависший SDK-коннект без событий (низкий приоритет по предыдущему отчёту).
- Сопоставление конкретных IP → физический гейт (Ala-Archa camera identification, отдельная задача с фотографированием камер на месте).

## 6. Открытые вопросы

Закрыт: `Camera_White_Entrance/Camera_White_Exit` удалены при слиянии (подтверждённый дубль, см. п.1) — референс не нужен, актуальный код и так в git-истории SmartParking/этого файла.

Открытых вопросов не осталось — весь оставшийся объём работы упирается в единственный внешний блокер (доступность archa, п.4 шаг 1).
