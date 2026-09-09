# callbacks.py (ОБНОВЛЕННЫЙ)
import os
import time
from ctypes import cast, POINTER, c_ubyte, c_longlong, c_ulong, c_void_p, c_ulonglong, c_int
from services.traffic_info import TrafficCallBackAlarmInfo
# --- УДАЛЕНО --- Убираем зависимость от внешнего сервиса Autovision
# from services.autovision import StateNumberDetector 
from services.smart_parking import SmartParking
from services.outbox import Outbox, start_drain_thread

from NetSDK.SDK_Enum import *
from NetSDK.SDK_Callback import *
from logger import get_logger

logger = get_logger("CALLBACKS")

# --- УДАЛЕНО --- Экземпляр детектора больше не нужен
# detector = StateNumberDetector(...)

# Инициализируем SmartParking, как и раньше
smart_parking = SmartParking(
    api_url=os.environ["SMARTPARKING_DATA_PROCESS_URL"],
)

# Durable-очередь на диске (см. services/outbox.py) - каждое событие
# сначала пишется сюда, потом уже пытается уйти в SmartParking. Файл
# лежит в примонтированной data/ (пер-контейнерной, docker-compose.yml -
# ./data/<role>_<slot>:/app/data) - переживает и обрыв сети, и рестарт
# самого контейнера.
outbox = Outbox(os.path.join(os.path.abspath("data"), "outbox.db"))


def start_background_drain():
    """
    Запускает фоновый поток, добивающий недоставленные события из outbox.
    Вызывается один раз из traffic_monitor.py перед входом в основной
    блокирующий цикл SDK.
    """
    def _sender(event_data, photo):
        delivered, _ = smart_parking.push_parking_event(event_data, photo, retries=1)
        return delivered

    return start_drain_thread(outbox, _sender)

# Локальная debug-копия снимков в data/Global/ - реальное фото и так уходит
# в SmartParking через push_parking_event ниже. На Balykchy этот "для
# отладки" каталог без единого ограничения вырос до 23-24GB за ~год на
# каждую роль. По умолчанию выключено - включать явно только при живой
# отладке на месте, не постоянно в проде.
SAVE_DEBUG_SNAPSHOTS = os.environ.get("SAVE_DEBUG_SNAPSHOTS", "false").lower() == "true"

callback_num = 0

# Vendor enum for szObjectSubType (NetSDK/SDK_Struct.py, SDK_MSG_OBJECT
# comment) - "Vehicle Category" values. Everything NOT in this deny-set is
# treated as a reportable vehicle. Deliberately a denylist, not an
# allowlist: this is a paid-parking gate camera, and an allowlist risks
# silently rejecting a legitimate but unlisted vehicle type (the vendor
# list already includes odd entries like "DregsCar"/"Excavator"/"Crane" -
# construction vehicles genuinely do use parking lots) and blocking a real
# paying customer, which is worse than occasionally letting through a
# borderline non-car detection that still carries a plausible plate.
#
# CONFIRMED GAP (live on archa, 2026-09-04): real camera firmware sends
# subtypes NOT in the documented SDK enum at all - "MPV"/"SUV"/
# "MidPassengerCar" (fine, obviously vehicles, denylist correctly let them
# through) and "Twocycle" (NOT fine - a two-wheeler that slipped through
# because it wasn't in this set, despite "Bicycle"/"Motorcycle" being
# denied). The vendor doc comment is not a reliable full list of what
# firmware actually emits - see _log_unrecognized_subtype below, which
# catches the NEXT such gap loudly instead of silently in either direction.
_NON_VEHICLE_SUBTYPES = {
    "", "unknown", "non-motor", "bicycle", "motorcycle", "tricycle",
    "electricbike", "twocycle", "passerby",  # "Passerby" = pedestrian
}

# Subtypes confirmed to be real vehicles, for the "is this a brand new,
# never-seen value" check below - NOT used for the actual filter decision
# (that's still purely denylist-based, see _NON_VEHICLE_SUBTYPES above).
# Includes both the documented vendor enum's vehicle entries and the
# undocumented-but-observed-live ones (mpv/suv/midpassengercar).
_KNOWN_VEHICLE_SUBTYPES = {
    "motor", "bus", "passengercar", "largetruck", "midtruck", "salooncar",
    "microbus", "microtruck", "dregscar", "excavator", "bulldozer",
    "crane", "pumptruck", "machineshoptruck", "forklift",
    "mpv", "suv", "midpassengercar",
}


def _log_unrecognized_subtype(subtype: str, plate: str) -> None:
    """
    Safety net for the exact class of bug found live today: a Dahua
    subtype that's neither in the deny-list nor previously confirmed as a
    real vehicle. Doesn't change the filter decision (still pure
    denylist) - just makes a brand-new undocumented value show up as a
    WARNING immediately, instead of being discovered days later as "why
    did a bike get a parking session".
    """
    if subtype and subtype not in _NON_VEHICLE_SUBTYPES and subtype not in _KNOWN_VEHICLE_SUBTYPES:
        logger.warning(
            f"Неизвестный object_subType_str от камеры: {subtype!r} (plate={plate!r}) - "
            f"не в денай-листе (пропущено как транспорт) и не в списке подтверждённых "
            f"типов техники. Проверьте вручную, не очередной ли это случай вроде "
            f"'Twocycle' (велосипед, который сначала пролез мимо фильтра)."
        )

# (camera_id, plate) -> monotonic timestamp of last accepted push. Backstop
# dedup only - the primary mechanism is alarm_info.nSequence below (SDK's
# own "1 = last shot of this burst" marker, see DEV_EVENT_TRAFFICJUNCTION_INFO
# in NetSDK/SDK_Struct.py: "如3,2,1,1表示抓拍结束,0表示异常结束" - a
# countdown per physical crossing, 1 is the final/definitive shot, 0 is an
# aborted burst with no clean final shot). This dict only guards against
# the SDK re-hitting nSequence==1 twice for what should be one crossing
# (firmware quirks, retriggers) - one process per camera (see main.py), so
# no cross-camera key collisions possible.
_recent_pushes = {}
_DEDUP_WINDOW_SECONDS = 15

class Callbacks:
    camera_code = None
    camera_id = None
    camera_name = None
    camera_routing_key = None

    @classmethod
    def set_camera_info(cls, camera_code, camera_id, camera_name, camera_routing_key=None):
        cls.camera_code = camera_code
        cls.camera_id = camera_id
        cls.camera_name = camera_name
        cls.camera_routing_key = camera_routing_key

    @staticmethod
    def _should_report(alarm_info, parsed_info, camera_id):
        """
        Решает, стоит ли отправлять это срабатывание в SmartParking.
        Возвращает (bool, reason_for_skip_or_None).

        1. object_subType_str - реальный транспорт, не пешеход/велосипед/
           неизвестный объект (см. _NON_VEHICLE_SUBTYPES выше).
        2. alarm_info.nSequence - SDK шлёт серию кадров на один физический
           проезд с обратным отсчётом (3,2,1), 1 = финальный/итоговый кадр
           серии, 0 = серия прервалась аварийно. Отчитываемся ТОЛЬКО по
           финальному кадру (nSequence == 1) - это и есть дедупликация:
           кадры 3 и 2 того же проезда просто не долетают до SmartParking.
        3. Короткое окно-подстраховка по (camera_id, номер) на случай, если
           прошивка когда-нибудь не дойдёт ровно до nSequence==1 дважды для
           одного проезда - основной механизм (2), это только бэкстоп.
        """
        subtype = (parsed_info.get("object_subType_str") or "").strip().lower()
        _log_unrecognized_subtype(subtype, parsed_info.get("plate_number_str"))
        if subtype in _NON_VEHICLE_SUBTYPES:
            return False, f"non-vehicle subtype {subtype!r}"

        sequence = alarm_info.nSequence
        if sequence == 0:
            return False, "aborted burst (nSequence=0)"
        if sequence != 1:
            return False, f"not final frame of burst (nSequence={sequence})"

        plate = (parsed_info.get("plate_number_str") or "").strip()
        if plate:
            key = (camera_id, plate)
            now = time.monotonic()
            last_seen = _recent_pushes.get(key)
            if last_seen is not None and (now - last_seen) < _DEDUP_WINDOW_SECONDS:
                return False, f"duplicate within {_DEDUP_WINDOW_SECONDS}s window"
            _recent_pushes[key] = now
            # Дешёвая уборка старых записей - трафик одной полосы никогда
            # не раздует этот словарь настолько, чтобы это было проблемой,
            # но не копить его бесконечно тоже не стоит.
            stale = [k for k, ts in _recent_pushes.items() if now - ts > _DEDUP_WINDOW_SECONDS * 4]
            for k in stale:
                _recent_pushes.pop(k, None)

        return True, None

    @CB_FUNCTYPE(
        None, c_longlong, c_ulong, c_void_p, POINTER(c_ubyte),
        c_ulong, c_ulonglong, c_int, c_void_p,
    )
    def AnalyzerDataCallBack(
        lAnalyzerHandle, dwAlarmType, pAlarmInfo, pBuffer, dwBufSize,
        dwUser, nSequence, reserved=None,
    ):
        camera_code = Callbacks.camera_code
        camera_id = Callbacks.camera_id
        camera_name = Callbacks.camera_name
        camera_routing_key = Callbacks.camera_routing_key

        global callback_num
        if dwAlarmType == EM_EVENT_IVS_TYPE.TRAFFICJUNCTION:
            callback_num += 1
            
            # 1. Получаем всю информацию напрямую из SDK камеры
            show_info = TrafficCallBackAlarmInfo()
            alarm_info = cast(pAlarmInfo, POINTER(DEV_EVENT_TRAFFICJUNCTION_INFO)).contents
            
            # 'a' теперь наш главный источник данных о распознавании
            a = show_info.get_alarm_info(alarm_info)
            logger.debug(f"Received from SDK: {a}")

            # 2. Получаем номерной знак из данных SDK
            plate_number_from_sdk = a.get("plate_number_str")

            # 3. Проверяем, что номер был распознан камерой, что это
            # финальный кадр серии (не дубль внутри одного проезда) и что
            # объект - действительно транспорт (не пешеход/велосипед и т.п.)
            should_report, skip_reason = Callbacks._should_report(alarm_info, a, camera_id)
            if not should_report:
                if plate_number_from_sdk and plate_number_from_sdk.strip():
                    logger.debug(
                        f"Skipping event ({skip_reason}): plate={plate_number_from_sdk!r} "
                        f"subtype={a.get('object_subType_str')!r} nSequence={alarm_info.nSequence}"
                    )
            elif plate_number_from_sdk and plate_number_from_sdk.strip():
                smart_parking_data = {
                    "license_plate": plate_number_from_sdk.strip(),
                    # НАМЕРЕННО константа, а не распознанная камерой страна
                    # (та уходит отдельным полем plate_country ниже).
                    # SmartParking считает по этому полю ДЕНЬГИ: в
                    # price_calculation/{first,add}_payment.py стоит
                    # `if license_plate_country != KG` (KG = "KG"), и любое
                    # другое значение переводит машину на иностранный тариф
                    # base_external_amount/add_external_amount. Пока не
                    # увидим на реальном трафике, что именно отдаёт камера в
                    # szCountry ("KG"? "KGZ"? пусто?), подставлять сюда
                    # значение из SDK нельзя - одна неудачная строка
                    # переведёт весь местный поток на чужой тариф.
                    "license_plate_country": "KG",
                    "color": a.get("vehicle_color_str", "unknown"),
                    # Цвет номера и марка - камера распознаёт их сама и
                    # отдаёт даром, раньше они просто выбрасывались.
                    # Жёлтый номер в KG = коммерческий транспорт, марка -
                    # 147 логотипов по даташиту DHI-ITC413-PW4D.
                    "plate_color": a.get("plate_color_str") or "unknown",
                    "vehicle_brand": a.get("vehicle_sign_str") or "unknown",
                    # Тип кузова - то же значение, по которому выше работает
                    # фильтр "транспорт или пешеход" (_should_report). До
                    # SmartParking оно раньше не доезжало, хотя камера
                    # отдаёт его в каждом событии: SaloonCar, SUV, Pickup,
                    # MPV, Microbus, MicroTruck и т.д. Именно на этом поле
                    # вылез Twocycle, из-за которого велосипед однажды
                    # получил парковочную сессию, - на сервере оно теперь
                    # видно в админке, а не только в логах моста.
                    "body_type": a.get("object_subType_str") or "unknown",
                    # Страна номера, как её распознала камера. Отдельно от
                    # license_plate_country (см. выше) - это справочное
                    # значение, на тариф оно не влияет. Пустая строка
                    # означает "камера не определила" (nRegionCode = -1).
                    "plate_country": a.get("plate_country_str") or "unknown",
                    "event_id": f"{camera_id}_{callback_num}",
                    # camera_routing_key = "{CAMERA_ROLE}_{CAMERA_SLOT}",
                    # построен в main.py из СВОИХ ЖЕ env-переменных этого
                    # контейнера (не из Camera.alias - тот теперь только
                    # для отображения в админке). DataProcessor резолвит
                    # камеру по (Camera.action, Camera.slot), см.
                    # parking_service.py::_resolve_camera_by_slot_key.
                    "camera": camera_routing_key,
                    "recognize": "Dahua SDK Direct",
                    # Реальное время детекции с камеры - SmartParking сам
                    # санити-чекает его (часы камеры не гарантированно
                    # синхронизированы по NTP) и откатывается на время
                    # сервера, если оно выглядит неправдоподобно.
                    "event_time": a.get("event_time_iso"),
                }
                
                # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ЗДЕСЬ ---
                image_buffer = None
                # Проверяем, есть ли изображение в событии
                if alarm_info.stuObject.bPicEnble or dwBufSize > 0:
                    try:
                        # Извлекаем байты изображения из буфера
                        image_buffer = bytes(cast(pBuffer, POINTER(c_ubyte * dwBufSize)).contents)

                        # Сохраняем изображение на диск для отладки - см.
                        # SAVE_DEBUG_SNAPSHOTS выше, реальное фото и так
                        # уходит в SmartParking через push_parking_event.
                        if SAVE_DEBUG_SNAPSHOTS:
                            local_path = os.path.abspath("data/")
                            global_dir = os.path.join(local_path, "Global")
                            os.makedirs(global_dir, exist_ok=True)
                            global_img_path = os.path.join(global_dir, f"Global_Img_{camera_name}_{callback_num}.jpg")
                            with open(global_img_path, "wb+") as global_pic:
                                global_pic.write(image_buffer)
                            logger.debug(f"Saved event image for debugging: {global_img_path}")

                    except Exception as e:
                        logger.error(f"Error extracting or saving image: {e}")

                try:
                    logger.info(f"Queueing for SmartParking (from SDK): {smart_parking_data}")
                    # Единственный путь доставки - durable outbox +
                    # фоновый drain-поток (start_background_drain). Раньше
                    # этот же поток захвата ЕЩЁ и пытался отправить событие
                    # немедленно сразу после enqueue() - drain-поток мог
                    # подхватить ту же самую только что записанную строку и
                    # отправить её ВТОРОЙ раз параллельно с этой немедленной
                    # попыткой (гонка, воспроизводимая в штатном режиме, не
                    # только при сбоях). Теперь ровно один потребитель
                    # очереди - drain-поток; он же почти сразу заберёт
                    # событие (enqueue() будит его немедленно, см.
                    # Outbox._has_work), так что задержка на здоровом пути
                    # не растёт.
                    outbox.enqueue(smart_parking_data, image_buffer)
                except Exception as e:
                    # Событие не попало даже в durable-очередь - это
                    # единственный путь к SmartParking для этого проезда, и
                    # он только что отказал (диск полон/заблокирован/
                    # недоступен). Событие теряется безвозвратно - должно
                    # быть заметно сразу, а не тихой ERROR-строкой среди
                    # сотен обычных логов.
                    logger.critical(
                        f"НЕ УДАЛОСЬ поставить событие в очередь - будет ПОТЕРЯНО: {e}. "
                        f"Данные события: {smart_parking_data}"
                    )
            else:
                logger.warning("No license plate detected by camera SDK. Skipping SmartParking.")

            # 4. Логика сохранения изображений - тоже под SAVE_DEBUG_SNAPSHOTS,
            # иначе КАЖДОЕ срабатывание камеры без распознанного номера
            # (а таких большинство - блики, случайные объекты) пишет файл
            # на диск без единого ограничения - именно это раздуло data/
            # на Balykchy до 23-24GB за ~год.
            if SAVE_DEBUG_SNAPSHOTS and (alarm_info.stuObject.bPicEnble or dwBufSize > 0):
                local_path = os.path.abspath("data/")
                global_dir = os.path.join(local_path, "Global")
                os.makedirs(global_dir, exist_ok=True)

                try:
                    image_buffer = cast(pBuffer, POINTER(c_ubyte * dwBufSize)).contents
                    global_img_path = os.path.join(global_dir, f"Global_Img_{camera_name}_{callback_num}.jpg")
                    with open(global_img_path, "wb+") as global_pic:
                        global_pic.write(bytes(image_buffer))
                    logger.debug(f"Saved event image for debugging: {global_img_path}")
                except Exception as e:
                    logger.error(f"Error saving event image: {e}")
