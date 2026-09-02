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

    def push_parking_event(self, event_data: dict, image_data: bytes = None, retries: int = 3, backoff_seconds: float = 2.0):
        """
        Событие проезда - одноразовое (машина уже уехала), других шансов
        долететь до SmartParking нет. Раньше при любой сетевой ошибке
        (в т.ч. кратковременной недоступности backend, например во время
        передеплоя) событие просто терялось без единой попытки повторить.
        """
        files_payload = None
        if image_data:
            files_payload = {
                'photo': ('event_image.jpg', image_data, 'image/jpeg')
            }

        last_error = None
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(
                    self.api_url,
                    data=event_data,
                    files=files_payload,
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                return response.json()
            except requests.exceptions.HTTPError as http_err:
                # 4xx - сервер принял запрос и осознанно отказал (например,
                # неизвестная камера) - повтор того же запроса ничего не
                # изменит, ретраить бессмысленно.
                logger.error(f"HTTP error occurred: {http_err} - {response.text}")
                return None
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as net_err:
                last_error = net_err
                logger.warning(
                    f"Попытка {attempt}/{retries} не удалась (сеть/таймаут): {net_err}"
                )
                if attempt < retries:
                    time.sleep(backoff_seconds * attempt)
            except requests.exceptions.RequestException as req_err:
                logger.error(f"An error occurred: {req_err}")
                return None

        logger.error(f"Не удалось доставить событие после {retries} попыток: {last_error}")
        return None
