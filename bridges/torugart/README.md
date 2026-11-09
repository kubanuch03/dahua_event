# Мост камер Торугарта

Один образ на все мосты объекта: событие Dahua (NetSDK) → выбор номера (камера или модель) → журнал → получатели (Dolibarr). Мост — один процесс на камеру.

## Состав
| Путь | Назначение |
|---|---|
| `main.py`, `services/` | вход, NetSDK, разбор события, выбор номера (`autovision.py`, `av_nm.py`), Dolibarr |
| `services/config.py` | настройки: умолчания → `/app/config/bridge.json` → env |
| `bridge/journal.py` | журнал `data/journal/YYYYMMDD.jsonl`, кадры, строки доставки |
| `bridge/pipeline.py`, `capture.py` | выбор номера и запись в журнал; перехват решения (`av_*`, ответ читалки) |
| `bridge/recipients.py` | получатели с курсором по журналу; Dolibarr |
| `bridge/watchdog.py`, `health.py`, `core.py` | тишина подписки, `/health` и `/metrics`, сборка |
| `env/*.env.example` | настройки мостов (секреты заполняются при развёртывании) |
| `tools/` | поиск секретов, сборка ветки для публикации |

## Настройки моста (env)
| Переменная | Значение |
|---|---|
| `BRIDGE_CAMERA_CODE`, `BRIDGE_CAMERA_IP`, `BRIDGE_CAMERA_PASSWORD`, `DOLIBARR_URL`, `DOLIBARR_API_KEY` | обязательны |
| `BRIDGE_CAMERA_PORT` (37777), `BRIDGE_CAMERA_USER` (admin) | необязательны |
| `BRIDGE_REQUIRE_DOLIBARR_FIELDS` (0) | 1 — пропускать камеру, если в записи Dolibarr нет полей подключения (Lane1, Lane2, Lane4) |
| `BRIDGE_CREDS_FROM_DOLIBARR` (0) | 1 — логин и пароль камеры брать из записи Dolibarr, env — запасной (Lane3) |
| `BRIDGE_RECIPIENTS` (`dolibarr`), `BRIDGE_DOLIBARR_MODE` (`on`) | получатели; режимы `on`, `shadow` (тело пишется в `data/recipients/`, сеть не используется), `off` |
| `BRIDGE_HEALTH_PORT` (8099), `BRIDGE_JOURNAL_KEEP_DAYS` (14) | `/health` (0 — выключить), хранение журнала |
| `WATCHDOG_ENABLED` (1), `WATCHDOG_MIN_SILENCE_S` (900), `WATCHDOG_MAX_SILENCE_S` (7200), `WATCHDOG_MULT` (10) | порог тишины подписки |
| `AV_*`, флаги `data/av_*` | выбор номера, как раньше |

## Журнал
Одна строка на событие (`type=event`): `ts` (UTC), `camera`, `run`, `n`, `event_id`, `cam_plate`, `cam_conf`, `cam_time`, `attrs`, `av`, `av_extra`, `model`, `outcome`, `sent` (тело запроса в Dolibarr без фото), `country`, `frame` (копия кадра в `journal/YYYYMMDD/`). Строки `type=av2` — итог второго номера, `type=delivery` — итог доставки (`outcome`: `sent`, `dedup`, `push_error…`, `dead…`, и ответ Dolibarr в `dolibarr`). `outcome` события: `queued` (ждёт получателей), `not_sent`, `short`, `model_error…`. Строка пишется до отправки; сборщик центра (`live_pull.py`) читает `event` и `av2`, остальные типы пропускает.

## Получатель Dolibarr
- Тело запроса — `sent` из журнала плюс base64 кадра из журнала.
- Повтор только при отказе соединения; таймаут и коды ответа не повторяются (запрос мог дойти). Паузы 5…300 с, не более 20 попыток, затем `dead`.
- Отсев повторов номера (флаг `data/av_dedup`, окно 300 с, I/O = 1/0).
- Курсор и мёртвые письма: `data/state/recipients.sqlite`. Первый запуск при существующем журнале историю не пересылает.

## Watchdog и /health
Порог тишины для часа суток: `WATCHDOG_MULT` × p95 интервалов между событиями за 7 суток, в пределах `[MIN, MAX]`. Ступени от последнего события: порог — переподписка, 2×порог — перелогин, 4×порог — выход процесса. При разрыве соединения watchdog молчит (перелогин по разрыву делает `traffic_monitor`). `GET :8099/health` — состояние SDK, возраст последнего события, отставание получателей, конфигурация без секретов (503 при потере сессии дольше 120 с); `GET /metrics` — Prometheus.

## Сборка и проверка
```
docker build -f bridges/torugart/Dockerfile -t autovision-bridge:<тег> .     # из корня репозитория
cd bridges/torugart && python3 -m unittest discover tests
```
Развёртывание: `docker-compose.yml` (мосты `*_v2`, переменные `BRIDGE_TAG`, `BRIDGE_NETWORK`, `BRIDGE_DATA_ROOT`; данные — тот же каталог, что у старого моста, журнал читает сборщик центра). Старый контейнер останавливается, не удаляется; откат — остановить новый и запустить старый.

## Отличия от живого кода
- Отправка в Dolibarr из читателя журнала, не из потока SDK; таймаут 15 с вместо 60; повторы при отказе соединения.
- Строка журнала пишется до отправки; итог и ответ Dolibarr приходят отдельной строкой `delivery`.
- Журнал включён всегда (раньше по флагу `data/av_journal`); имя кадра и поля строки прежние.
- Один код на все мосты. Lane4 раньше работал на более старых `autovision.py` и `callbacks.py`; теперь на общих.
- Не реализовано: получатель SmartParking, горячая перечитка флагов `av_*` из одного файла, генерация compose по описанию объекта.

## Публикация
Ветки от `torugart-bridge` содержат в истории литералы ключа и пароля, их публиковать нельзя. Ветка для публикации — `tools/make_clean_branch.sh` (`--dry-run` сканирует дерево без создания ветки); проверка истории и дерева — `tools/scan_secrets.py`.
