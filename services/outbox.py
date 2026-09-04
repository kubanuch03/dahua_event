# services/outbox.py
"""
Локальная persistent-очередь на SQLite: гарантирует, что событие проезда
не потеряется, если SmartParking временно недоступен (сеть до archa
подтверждённо нестабильна - наблюдалось многократно на этом объекте, до
нескольких минут простоя). Каждое событие сразу пишется на диск ДО попытки
доставки; удаляется из очереди только после подтверждённой доставки.
Фоновый поток (см. start_drain_thread, запускается из
services/callbacks.py::start_background_drain, вызывается из
traffic_monitor.py до входа в блокирующий цикл SDK) непрерывно добивает
недоставленное.

Почему SQLite, а не Redis: Redis по умолчанию не персистентный (только
ОЗУ - перезапуск контейнера стирает очередь), а даже с AOF+volume
добавляет отдельный процесс без реальной выгоды на этом объёме (один
писатель/один читатель на камеру - ровно то, для чего Redis не нужен).
SQLite - embedded библиотека (в stdlib Python), durable по умолчанию
(WAL), файл лежит в уже примонтированной data/ - переживает и обрыв сети,
и рестарт самого контейнера.
"""
import json
import os
import sqlite3
import threading
import time

from logger import get_logger

logger = get_logger("OUTBOX")

# Порог, после которого при каждом цикле добивки громко предупреждаем в
# лог - не отбрасываем ни одного события ни при каком размере очереди, но
# затяжной простой SmartParking должен быть заметен в docker logs, а не
# тихо копиться месяцами.
_WARN_QUEUE_SIZE = 10_000


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Outbox:
    """
    Одна SQLite-БД на контейнер (одна камера = один процесс = один файл).
    Каждый метод открывает и закрывает своё короткоживущее соединение -
    sqlite3-соединения не потокобезопасны для расшаривания между потоками
    без явных костылей (check_same_thread=False + дисциплина), а нам и не
    нужно шарить одно соединение: WAL спокойно тянет конкурентные короткие
    транзакции от разных потоков (поток захвата событий и фоновый
    drain-поток), каждая операция - отдельное соединение открывается и
    закрывается почти мгновенно.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Будит drain-поток немедленно на новое событие вместо ожидания до
        # idle_sleep секунд - set() в enqueue(), wait()+clear() в drain-цикле
        # (см. wait_for_work). Порядок wait -> clear -> заново читаем
        # очередь - если между clear() и следующим чтением придёт ещё одно
        # enqueue(), событие снова взведёт флаг и следующий wait() не
        # заблокируется - пропущенных пробуждений нет.
        self._has_work = threading.Event()
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = _connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_data TEXT NOT NULL,
                    photo BLOB,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, event_data: dict, photo: bytes = None) -> int:
        conn = _connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO events (event_data, photo, created_at) VALUES (?, ?, ?)",
                (json.dumps(event_data), photo, time.time()),
            )
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
        self._has_work.set()
        return row_id

    def wait_for_work(self, timeout: float) -> None:
        """Блокируется до enqueue() (или до timeout) и сразу сбрасывает
        флаг - следующий цикл drain-потока сам перечитает очередь."""
        self._has_work.wait(timeout=timeout)
        self._has_work.clear()

    def delete(self, row_id: int) -> None:
        conn = _connect(self.db_path)
        try:
            conn.execute("DELETE FROM events WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    def bump_attempts(self, row_id: int) -> None:
        conn = _connect(self.db_path)
        try:
            conn.execute("UPDATE events SET attempts = attempts + 1 WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    def oldest_pending(self):
        """(id, event_data_json_str, photo_bytes, created_at) самой старой
        строки (FIFO по id), или None, если очередь пуста. Отдаёт СЫРУЮ
        JSON-строку не распарсенной - разбор нарочно вынесен в вызывающий
        код (drain-цикл), чтобы битая строка (повреждённый JSON) не роняла
        это чтение с исключением до того, как вызывающий код успеет узнать
        row_id и решить, что с ней делать (см. start_drain_thread)."""
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id, event_data, photo, created_at FROM events ORDER BY id ASC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return row

    def pending_count(self) -> int:
        conn = _connect(self.db_path)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        finally:
            conn.close()
        return count


def start_drain_thread(outbox: Outbox, sender, idle_sleep=10.0, min_backoff=10.0, max_backoff=120.0):
    """
    Фоновый демон-поток: единственный потребитель очереди, непрерывно
    добивает недоставленное. Раньше поток захвата (callbacks.py) ещё и сам
    пытался отправить событие немедленно сразу после enqueue() - это была
    гонка: drain-поток мог забрать ту же самую только что записанную
    строку и отправить её ВТОРОЙ раз параллельно с этой немедленной
    попыткой. Теперь callbacks.py только кладёт событие в очередь, доставка
    - целиком здесь.

    - Очередь пуста -> ждём событие enqueue() (или idle_sleep) через
      Outbox.wait_for_work - без опроса и без задержки на здоровом пути
      (было: слепой time.sleep(idle_sleep) даже когда событие только что
      пришло).
    - Доставка успешна -> сразу следующая строка без паузы (дренируем весь
      бэклог рывком), backoff сбрасывается на min_backoff.
    - Доставка неуспешна -> экспоненциальный backoff до max_backoff перед
      следующей попыткой ТОЙ ЖЕ строки (FIFO - не перескакиваем через
      недоставленное).
    - Строка с повреждённым JSON (не должно происходить, но диск/процесс
      могут упасть посреди записи) - после нескольких подряд неудачных
      попыток разобрать эту же строку считаем её "отравленной", удаляем из
      очереди (иначе она блокирует FIFO навсегда) и пишем CRITICAL с сырым
      содержимым - потеря данных, но видимая в логах, а не тихая заморозка
      всей доставки.
    - Любое другое неожиданное исключение (SQLite залочен, sender() кинул
      что-то помимо своего штатного bool) - логируется и цикл продолжает
      жить, а не падает демоном навсегда без присмотра.

    `sender(event_data: dict, photo: bytes) -> bool` - True, если событие
    можно считать переданным (успех ИЛИ осознанный бизнес-отказ сервера -
    и то, и другое означает "сервер жив, повторять этот конкретный запрос
    бессмысленно"), False - на сетевую проблему/таймаут/временную (5xx)
    ошибку сервера.
    """

    def _loop():
        backoff = min_backoff
        poison_attempts = {}

        while True:
            try:
                item = outbox.oldest_pending()
            except Exception:
                logger.exception("Outbox: не удалось прочитать очередь, повтор через паузу")
                time.sleep(min_backoff)
                continue

            if item is None:
                outbox.wait_for_work(idle_sleep)
                continue

            row_id, raw_event_data, photo, created_at = item

            try:
                event_data = json.loads(raw_event_data)
            except (TypeError, ValueError) as parse_err:
                count = poison_attempts.get(row_id, 0) + 1
                poison_attempts[row_id] = count
                logger.error(
                    f"Outbox: событие id={row_id} не распарсилось ({count}-я подряд "
                    f"попытка): {parse_err}. raw={raw_event_data!r}"
                )
                if count >= 3:
                    logger.critical(
                        f"Outbox: событие id={row_id} повреждено {count} раза подряд - "
                        f"удаляю из очереди, чтобы не блокировать доставку остальных. "
                        f"Данные теряются, сохранены здесь для ручного восстановления: "
                        f"raw={raw_event_data!r}"
                    )
                    try:
                        outbox.delete(row_id)
                    except Exception:
                        logger.exception(f"Outbox: не удалось удалить повреждённую строку id={row_id}")
                    poison_attempts.pop(row_id, None)
                else:
                    time.sleep(min_backoff)
                continue

            try:
                age_seconds = time.time() - created_at
                delivered = sender(event_data, photo)
            except Exception:
                logger.exception(f"Outbox: неожиданная ошибка при попытке доставки id={row_id}")
                delivered = False

            if delivered:
                poison_attempts.pop(row_id, None)
                try:
                    outbox.delete(row_id)
                except Exception:
                    logger.exception(f"Outbox: не удалось удалить доставленную строку id={row_id}")
                backoff = min_backoff
                logger.info(
                    f"Outbox: доставлено отложенное событие id={row_id} "
                    f"(ждало в очереди {age_seconds:.0f}с): "
                    f"plate={event_data.get('license_plate')!r} camera={event_data.get('camera')!r}"
                )
                continue

            try:
                outbox.bump_attempts(row_id)
                pending = outbox.pending_count()
            except Exception:
                logger.exception("Outbox: не удалось обновить счётчик попыток/размер очереди")
                pending = None

            if pending is not None and pending >= _WARN_QUEUE_SIZE:
                logger.warning(
                    f"Outbox: {pending} событий в очереди (порог {_WARN_QUEUE_SIZE}) - "
                    f"похоже на длительный простой SmartParking. Ничего не отбрасывается, "
                    f"но стоит разобраться, почему связь не восстанавливается."
                )
            logger.debug(
                f"Outbox: доставка id={row_id} (в очереди {age_seconds:.0f}с) не удалась, "
                f"следующая попытка через {backoff:.0f}с (всего в очереди: {pending})"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    thread = threading.Thread(target=_loop, name="outbox-drain", daemon=True)
    thread.start()
    return thread
