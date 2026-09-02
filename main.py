import os
import sys
import time

import requests

from services.traffic_monitor import start_traffic_monitor
from logger import setup_logging, get_logger

setup_logging()

logger = get_logger("MAIN")

# CAMERA_ROLE/CAMERA_SLOT выбирают, какую камеру брать из SmartParking (не
# хардкод IP - камеры объекта на DHCP без резерваций, IP постоянно меняется,
# см. Obsidian "Сеть и устройства объекта.md"). БД остаётся единственным
# источником правды по актуальным IP - обновляется отдельно от кода этого
# сервиса. Один и тот же образ обслуживает все 4 физические камеры объекта
# (2 въезда, 2 выезда) - роль/слот приходят только через env контейнера,
# без дефолта: раньше у каждой копии дублировался код с ролью, "зашитой"
# в имя директории, здесь копия одна и роль обязана быть явной.
CAMERA_ROLE = os.environ["CAMERA_ROLE"]
CAMERA_SLOT = int(os.environ.get("CAMERA_SLOT", "1"))
SMARTPARKING_API_URL = os.environ["SMARTPARKING_API_URL"]  # напр. http://smart_parking_backend:8833/parking/api/cameras/config/
SMARTPARKING_API_TOKEN = os.environ.get("SMARTPARKING_API_TOKEN")  # опционально, если эндпоинт защищён токеном, а не IP-allowlist

ROLE_TO_ACTION = {
    "entrance": "Въезд ТС",
    "exit": "Выезд ТС",
}


def fetch_cameras():
    headers = {"Authorization": f"Bearer {SMARTPARKING_API_TOKEN}"} if SMARTPARKING_API_TOKEN else {}
    resp = requests.get(SMARTPARKING_API_URL, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_camera_for_this_slot():
    """
    Забирает актуальный список камер у SmartParking, фильтрует по роли
    (CAMERA_ROLE) и берёт CAMERA_SLOT-ую по порядку (1-indexed) - так один
    и тот же образ обслуживает несколько физических камер одной роли
    (2 въезда, 2 выезда), просто с разным CAMERA_SLOT в env.
    """
    action = ROLE_TO_ACTION[CAMERA_ROLE]
    cameras = [c for c in fetch_cameras() if c.get("action") == action]
    if len(cameras) < CAMERA_SLOT:
        raise RuntimeError(
            f"SmartParking вернул {len(cameras)} камер с action={action!r}, "
            f"а запрошен CAMERA_SLOT={CAMERA_SLOT} - недостаточно записей."
        )
    return cameras[CAMERA_SLOT - 1]


def start_monitor_for_camera(camera):
    try:
        camera_ip = camera["ip_address"]
        camera_port = int(camera.get("port") or 37777)
        camera_username = camera["username"]
        camera_password = camera["password"]
        camera_id = camera.get("id", 1)
        camera_name = camera.get("name") or f"{CAMERA_ROLE}-{CAMERA_SLOT}"
        camera_alias = camera.get("alias")
        camera_code = 8
        logger.info(
            f"Starting traffic monitor for camera {camera_id} at {camera_ip}:{camera_port} (alias={camera_alias!r})"
        )
        if not camera_alias:
            # DataProcessor.data_event_processing() на SmartParking резолвит
            # камеру ТОЛЬКО точным совпадением по Camera.alias - без него
            # каждое событие проезда будет молча отклонено с "Камера не
            # найдена". Не блокируем старт (SDK-коннект и логи всё ещё
            # полезны для диагностики), но кричим в лог сразу, а не постфактум.
            logger.error(
                f"У камеры {camera_id} не задан alias в SmartParking - "
                f"события проезда будут отклоняться до заполнения Camera.alias в БД."
            )
        # Именованные аргументы намеренно - позиционные camera_id/camera_name
        # в оригинале Balykchy были перепутаны местами относительно
        # сигнатуры start_traffic_monitor (см. services/traffic_monitor.py)
        # и путали event_id/имя файла снимка местами.
        start_traffic_monitor(
            camera_ip=camera_ip,
            camera_port=camera_port,
            camera_username=camera_username,
            camera_password=camera_password,
            camera_code=camera_code,
            camera_name=camera_name,
            camera_id=camera_id,
            camera_alias=camera_alias,
        )
    except Exception as e:
        logger.error(f"Failed to start monitor for camera {camera.get('id')}: {e}")
        return camera.get("id"), False
    return camera.get("id"), True


if __name__ == "__main__":
    # Ретраи на случай, если SmartParking ещё не поднялся (общий docker
    # compose стек, порядок старта не гарантирован).
    camera = None
    for attempt in range(10):
        try:
            camera = get_camera_for_this_slot()
            break
        except Exception as e:
            logger.warning(f"Не удалось получить конфиг камеры (попытка {attempt + 1}/10): {e}")
            time.sleep(5)

    if camera is None:
        logger.error("SmartParking API недоступен после всех попыток. Выход.")
        sys.exit(1)

    camera_id, ok = start_monitor_for_camera(camera)
    if not ok:
        sys.exit(1)
