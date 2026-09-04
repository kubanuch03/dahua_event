# services/smart_parking.py
import time

import requests
from logger import get_logger

logger = get_logger("SMART_PARKING")


class SmartParking(object):
    def __init__(self, api_url, api_key=None):
        self.api_url = api_url
        self.api_key = api_key
        self.headers = {}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

    def _send_once(self, event_data: dict, image_data: bytes = None, timeout: float = 25.0):
        """
        Одна попытка доставки. Возвращает (delivered: bool, result).

        delivered=True означает "сервер получил и обработал запрос" - это
        касается и успеха (200), и осознанного бизнес-отказа сервера
        (4xx, напр. "камера не найдена" или "нет открытой сессии") -
        повтор ТОГО ЖЕ запроса ничего не изменит в обоих случаях, значит
        это не повод держать событие в outbox. delivered=False - только
        для сетевых проблем/таймаутов, где повтор имеет смысл.
        """
        files_payload = None
        if image_data:
            files_payload = {
                'photo': ('event_image.jpg', image_data, 'image/jpeg')
            }
        try:
            response = requests.post(
                self.api_url,
                data=event_data,
                files=files_payload,
                headers=self.headers,
                # 10с не хватало на реальный снимок с камеры (полное
                # разрешение, не крошечный тестовый файл) + обработку
                # на бэкенде - все реальные события падали по таймауту
                # 3/3 попытки подряд, хотя тот же эндпоинт с маленьким
                # тестовым фото отвечал за <100мс.
                timeout=timeout,
            )
            response.raise_for_status()
            try:
                return True, response.json()
            except requests.exceptions.JSONDecodeError as json_err:
                # 2xx, тело не JSON - запрос ДОСТАВЛЕН и принят сервером,
                # просто не смогли распарсить ответ. Повтор того же
                # запроса не изменит содержимое ответа - считаем доставленным.
                logger.error(f"SmartParking ответил не-JSON телом при успешном статусе: {json_err}")
                return True, None
        except requests.exceptions.HTTPError as http_err:
            status = response.status_code
            if status >= 500 or status in (408, 429):
                # Временная проблема на стороне сервера (перегружен,
                # разворачивается, редеплоится) или мы душим его слишком
                # часто - имеет смысл повторить, оставляем в outbox.
                # Раньше ЛЮБОЙ HTTPError (в т.ч. 502/503 во время
                # передеплоя SmartParking) считался "доставлено" и строка
                # удалялась из outbox - событие терялось безвозвратно
                # ровно в тот момент, ради которого outbox и заводили.
                logger.warning(f"HTTP {status} (временная ошибка сервера) - оставляем в outbox: {http_err}")
                return False, http_err
            # Остальные 4xx - осознанный отказ сервера (напр. "камера не
            # найдена", "нет открытой сессии"), повтор того же запроса
            # ничего не изменит.
            logger.error(f"HTTP error occurred: {http_err} - {response.text}")
            return True, None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as net_err:
            return False, net_err
        except requests.exceptions.RequestException as req_err:
            # Нет доказательства, что сервер вообще получил запрос (ошибка
            # формирования запроса, DNS и т.п., не пойманная более
            # специфичными случаями выше). Раньше это тоже считалось
            # "доставлено" и теряло событие - теперь ретраится как обычная
            # сетевая проблема, а не молча списывается.
            logger.warning(f"Не удалось отправить запрос (доставка не подтверждена): {req_err}")
            return False, req_err

    def push_parking_event(self, event_data: dict, image_data: bytes = None, retries: int = 3, backoff_seconds: float = 2.0):
        """
        Быстрый путь: несколько попыток подряд с коротким бэкоффом - для
        типичного кратковременного сбоя. НЕ единственный шанс события
        долететь: вызывающий (callbacks.py) кладёт событие в outbox ДО
        вызова этого метода, так что финальная неудача здесь не теряет
        событие - его добьёт фоновый drain-поток (см. services/outbox.py).

        Возвращает (delivered: bool, response_json_or_None).
        """
        last_error = None
        for attempt in range(1, retries + 1):
            delivered, result = self._send_once(event_data, image_data)
            if delivered:
                return True, result
            last_error = result
            logger.warning(
                f"Попытка {attempt}/{retries} не удалась (сеть/таймаут): {last_error}"
            )
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)

        logger.warning(
            f"Не удалось доставить событие после {retries} попыток (сеть/таймаут): "
            f"{last_error} - остаётся в outbox, фоновый поток добьёт при восстановлении связи."
        )
        return False, None
