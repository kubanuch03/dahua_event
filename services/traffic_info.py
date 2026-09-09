from logger import get_logger

logger = get_logger("TRAFFIC INFO")


class TrafficCallBackAlarmInfo:
    def __init__(self):
        self.time_str = ""
        self.event_time_iso = ""
        self.plate_number_str = ""
        self.plate_color_str = ""
        self.object_subType_str = ""
        self.vehicle_color_str = ""
        self.vehicle_sign_str = ""
        self.plate_country_str = ""
        self.plate_province_str = ""

    def get_alarm_info(self, alarm_info):
        self.time_str = (
            f"{alarm_info.UTC.dwYear}-{alarm_info.UTC.dwMonth:02}-{alarm_info.UTC.dwDay:02} "
            f"{alarm_info.UTC.dwHour:02}:{alarm_info.UTC.dwMinute:02}:{alarm_info.UTC.dwSecond:02}"
        )
        # ISO8601 для передачи в SmartParking как event_time. Часы камеры
        # НЕ гарантированно синхронизированы по NTP (подтверждено вживую
        # на archa 2026-09-04 - камера отдала "2000-01-14" вместо
        # настоящей даты) - санити-чек этого значения делает SmartParking
        # при получении (см. parking_service.py::resolve_event_time), не
        # здесь: dahua_event просто честно передаёт то, что сказала камера.
        self.event_time_iso = (
            f"{alarm_info.UTC.dwYear:04}-{alarm_info.UTC.dwMonth:02}-{alarm_info.UTC.dwDay:02}T"
            f"{alarm_info.UTC.dwHour:02}:{alarm_info.UTC.dwMinute:02}:{alarm_info.UTC.dwSecond:02}Z"
        )

        self.plate_number_str = self._decode_string(
            alarm_info.stTrafficCar.szPlateNumber, encoding="gb2312"
        )
        self.plate_color_str = self._decode_string(alarm_info.stTrafficCar.szPlateColor)
        self.object_subType_str = self._decode_string(
            alarm_info.stuVehicle.szObjectSubType
        )
        self.vehicle_color_str = self._decode_string(
            alarm_info.stTrafficCar.szVehicleColor
        )
        # Марка автомобиля. Камера распознаёт её сама (даташит
        # DHI-ITC413-PW4D: "Recognizes 147 vehicle logos") и отдаёт готовой
        # строкой - szVehicleSign в stTrafficCar. Числовые wColorLogoIndex/
        # wSubBrand в stuVehicle намеренно НЕ трогаем: это индексы, которые
        # без вендорской таблицы соответствия бесполезны, а szVehicleSign
        # уже человекочитаем.
        self.vehicle_sign_str = self._decode_string(
            alarm_info.stTrafficCar.szVehicleSign
        )
        # Страна и регион номера - stCommInfo (EVENT_COMM_INFO), а НЕ
        # stTrafficCar, где лежит всё остальное про машину. Заполняются
        # алгоритмом распознавания зарубежных номеров; при неудаче
        # приходят пустыми (nRegionCode при этом -1). Обработка пустого
        # значения - на стороне вызывающего (callbacks.py), здесь честно
        # отдаём то, что сказала камера.
        self.plate_country_str = self._decode_string(alarm_info.stCommInfo.szCountry)
        self.plate_province_str = self._decode_string(alarm_info.stCommInfo.szProvince)

        return self._get_alarm_info_dict()

    def _decode_string(self, byte_string, encoding="utf-8"):
        # errors="replace", а не строгий режим: эти байты приходят прямо из
        # прошивки камеры, и один битый символ в любом из полей ронял бы
        # UnicodeDecodeError внутри ctypes-колбэка. Там исключение уже
        # некому поймать - оно печатается в stderr, а событие проезда
        # теряется целиком, ДО попадания в durable-outbox. Испорченный
        # символ в марке или цвете - несопоставимо меньшая беда, чем
        # потерянный проезд.
        try:
            return str(byte_string, encoding, errors="replace")
        except Exception as e:  # не bytes / неизвестная кодировка
            logger.warning(f"Не удалось декодировать поле от камеры ({encoding}): {e}")
            return ""

    def _get_alarm_info_dict(self):
        return {
            "time_str": self.time_str,
            "event_time_iso": self.event_time_iso,
            "plate_number_str": self.plate_number_str,
            "plate_color_str": self.plate_color_str,
            "object_subType_str": self.object_subType_str,
            "vehicle_color_str": self.vehicle_color_str,
            "vehicle_sign_str": self.vehicle_sign_str,
            "plate_country_str": self.plate_country_str,
            "plate_province_str": self.plate_province_str,
        }
