"""
Событие «нахождение в зоне» (IVS WanderDetection) -> данные для SmartParking.

Камера сообщает, что объект стоит в зоне перед стрелой дольше порога
правила (на Ала-Арче - 20 с): начало (start), продолжение (pulse) и конец
(stop, объект ушёл). Номера в событии нет - SmartParking берёт его из
последнего распознавания на этом гейте и продлевает «ожидание у стрелы».

NetSDK здесь не нужен: функции принимают уже разобранную структуру и
проверяются на подделках (tests/test_wander_info.py).
"""
from __future__ import annotations

ACTIONS = {0: "pulse", 1: "start", 2: "stop"}

# Объекты, которые нас интересуют: машина. Пешеход в зоне шлагбаума - не повод.
# Велосипеды и мопеды (NonMotor) - не машины, как и в фильтре ANPR
# (callbacks._NON_VEHICLE_SUBTYPES).
_VEHICLE_TYPES = {"vehicle", "motorvehicle", "car"}


def _text(raw) -> str:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).split(b"\x00", 1)[0]
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw or "").strip()


def _iso(utc) -> str:
    """Время события или "" - кривое время (месяц 0, 31 февраля) не отправляем."""
    from datetime import datetime

    try:
        moment = datetime(int(utc.dwYear), int(utc.dwMonth), int(utc.dwDay),
                          int(utc.dwHour), int(utc.dwMinute), int(utc.dwSecond))
    except Exception:  # noqa: BLE001 - кривое время не должно терять событие
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def object_types(info) -> list[str]:
    try:
        count = max(0, min(int(info.nObjectNum), len(info.stuObjectIDs)))
    except Exception:  # noqa: BLE001
        return []
    return [_text(info.stuObjectIDs[i].szObjectType) for i in range(count)]


def is_vehicle(types) -> bool:
    """Нет данных об объекте - считаем машиной: правило стоит на полосе машин."""
    if not types:
        return True
    return any(t.strip().lower() in _VEHICLE_TYPES for t in types)


def build_payload(info, *, camera_id, routing_key) -> dict | None:
    """Данные для SmartParking или None, если событие не про машину."""
    types = object_types(info)
    if not is_vehicle(types):
        return None
    action = ACTIONS.get(int(getattr(info, "bEventAction", 0) or 0), "pulse")
    event_id = int(getattr(info, "nEventID", 0) or 0)
    return {
        "event_type": "dwell",
        "dwell_action": action,
        "camera": routing_key,
        "event_id": f"{camera_id}_w{event_id}_{action}",
        "event_time": _iso(info.UTC),
        "rule": _text(getattr(info, "szName", b"")),
        "object_type": ",".join(t for t in types if t) or "unknown",
        "recognize": "Dahua SDK IVS WanderDetection",
    }


class PulseThrottle:
    """
    «Продолжение» камера шлёт, пока объект стоит; в SmartParking хватит
    одного в окне на камеру. Начало и конец не режутся никогда.
    """

    def __init__(self, window_seconds: float = 15.0):
        self.window = window_seconds
        self._last = {}

    def allow(self, camera_id, action: str, now: float) -> bool:
        if action != "pulse":
            self._last[camera_id] = now
            return True
        last = self._last.get(camera_id)
        if last is not None and now - last < self.window:
            return False
        self._last[camera_id] = now
        return True
