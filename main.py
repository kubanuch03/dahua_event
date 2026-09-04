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
    Забирает актуальный список камер у SmartParking и резолвит СВОЮ (по
    точному совпадению action+slot, не по позиции в списке - раньше брали
    CAMERA_SLOT-ую по порядку (`cameras[CAMERA_SLOT - 1]`), а Django ничего
    не гарантирует насчёт порядка без explicit order_by, так что "второй
    въезд" мог тихо съехать на другую физическую камеру при любом
    изменении набора камер в БД). Ровно 0 или 2+ совпадений - explicit
    ошибка конфигурации, а не тихий выбор не той камеры.
    """
    action = ROLE_TO_ACTION[CAMERA_ROLE]
    cameras = [
        c for c in fetch_cameras()
        if c.get("action") == action and c.get("slot") == CAMERA_SLOT
    ]
    if len(cameras) != 1:
        raise RuntimeError(
            f"SmartParking вернул {len(cameras)} камер с action={action!r}, "
            f"slot={CAMERA_SLOT} - ожидалась ровно одна. Проверьте Camera.slot "
            f"в админке (это НЕ то же самое, что alias)."
        )
    return cameras[0]


def start_monitor_for_camera(camera):
    try:
        camera_ip = camera["ip_address"]
        camera_port = int(camera.get("port") or 37777)
        camera_username = camera["username"]
        camera_password = camera["password"]
        camera_id = camera.get("id", 1)
        camera_name = camera.get("name") or f"{CAMERA_ROLE}-{CAMERA_SLOT}"
        # Ключ маршрутизации строим из СВОИХ ЖЕ env-переменных, не из
        # camera["alias"] - тот теперь только человекочитаемая метка в
        # админке SmartParking, её можно переименовать без последствий.
        # DataProcessor.data_event_processing() на SmartParking резолвит
        # камеру по (action, slot), не по alias (см. _resolve_camera_by_slot_key
        # в parking_service.py).
        camera_routing_key = f"{CAMERA_ROLE}_{CAMERA_SLOT}"
        camera_code = 8
        logger.info(
            f"Starting traffic monitor for camera {camera_id} at {camera_ip}:{camera_port} "
            f"(routing_key={camera_routing_key!r}, alias={camera.get('alias')!r})"
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
            camera_routing_key=camera_routing_key,
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
