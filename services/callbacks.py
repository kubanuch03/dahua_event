# callbacks.py (ОБНОВЛЕННЫЙ)
import os
from ctypes import cast, POINTER, c_ubyte, c_longlong, c_ulong, c_void_p, c_ulonglong, c_int
from services.traffic_info import TrafficCallBackAlarmInfo
# --- УДАЛЕНО --- Убираем зависимость от внешнего сервиса Autovision
# from services.autovision import StateNumberDetector 
from services.smart_parking import SmartParking

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

# Локальная debug-копия снимков в data/Global/ - реальное фото и так уходит
# в SmartParking через push_parking_event ниже. На Balykchy этот "для
# отладки" каталог без единого ограничения вырос до 23-24GB за ~год на
# каждую роль. По умолчанию выключено - включать явно только при живой
# отладке на месте, не постоянно в проде.
SAVE_DEBUG_SNAPSHOTS = os.environ.get("SAVE_DEBUG_SNAPSHOTS", "false").lower() == "true"

callback_num = 0

class Callbacks:
    camera_code = None
    camera_id = None
    camera_name = None
    camera_alias = None

    @classmethod
    def set_camera_info(cls, camera_code, camera_id, camera_name, camera_alias=None):
        cls.camera_code = camera_code
        cls.camera_id = camera_id
        cls.camera_name = camera_name
        cls.camera_alias = camera_alias

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
        camera_alias = Callbacks.camera_alias

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

            # 3. Проверяем, что номер был распознан камерой
            if plate_number_from_sdk and plate_number_from_sdk.strip():
                smart_parking_data = {
                    "license_plate": plate_number_from_sdk.strip(),
                    "license_plate_country": "KG", 
                    "color": a.get("vehicle_color_str", "unknown"),
                    "event_id": f"{camera_id}_{callback_num}",
                    # camera_alias идёт от SmartParking (Camera.alias в БД,
                    # см. CameraConfigAPIView) - DataProcessor резолвит
                    # камеру ТОЛЬКО точным совпадением по alias, хардкод
                    # здесь ломал бы это молча на любом объекте кроме
                    # Balykchy.
                    "camera": camera_alias,
                    "recognize": "Dahua SDK Direct",
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
                    logger.info(f"Sending to SmartParking (from SDK): {smart_parking_data}")
                    # Передаем и текстовые данные, и байты изображения
                    smart_parking.push_parking_event(
                        event_data=smart_parking_data, 
                        image_data=image_buffer
                    )
                except Exception as e:
                    logger.error(f"Failed to send data to SmartParking: {e}")
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
