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
        self.plate_confidence_raw = None
        self.plate_confidence_pct = None
        self.confidence_debug = ""

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
        # Уверенность распознавания НОМЕРА. В событии TrafficJunction
        # stuObject - это номерной знак (машина - в stuVehicle), nConfidence
        # по заголовку NetSDK 2.0.0.1: 0..255, больше - увереннее. Отдаём в
        # процентах (0..100) и сырым значением - для сверки на живом потоке:
        # заполняет ли поле прошивка ITC413 5.003, ещё не проверено (у
        # szCountry обёртка уже читала мимо данных, см. fix/plate-country-offset).
        self.plate_confidence_raw, self.plate_confidence_pct = self._read_confidence(alarm_info)
        self.confidence_debug = self._confidence_debug(alarm_info)

        return self._get_alarm_info_dict()

    @staticmethod
    def _read_confidence(alarm_info):
        """
        (сырое 0..255, проценты 0..100) или (None, None).

        0 и значения вне шкалы - «камера не сообщила», а не «уверена на 0 %»:
        прошивка, не заполняющая поле, присылает 0, и показывать его как
        процент значило бы ложно помечать каждый проезд недостоверным.

        Берём только если stuObject - действительно номер (szObjectType
        "Plate" или непустой szText): иначе под видом уверенности номера
        ушла бы уверенность обнаружения машины (ревью, 06.10.2026).
        """
        try:
            obj = alarm_info.stuObject
            kind = TrafficCallBackAlarmInfo._raw_text(obj.szObjectType).lower()
            text = TrafficCallBackAlarmInfo._raw_text(obj.szText)
            if kind != "plate" and not text:
                return None, None
            raw = int(obj.nConfidence)
        except Exception:  # noqa: BLE001 - поле необязательное, проезд не терять
            return None, None
        if raw <= 0 or raw > 255:
            return None, None
        return raw, round(raw * 100 / 255)

    @staticmethod
    def _raw_text(value) -> str:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).split(b"\x00")[0].decode("utf-8", "replace").strip()
        return str(value or "").strip()

    @staticmethod
    def _confidence_debug(alarm_info) -> str:
        """
        Сырые значения для сверки на живом потоке - ДО отсева _read_confidence:
        тип объекта, nConfidence как есть (0 и мусор различимы) и запасное
        stCommInfo.nPlateRecogniseConf (шкала не документирована; лежит в
        EVENT_COMM_INFO, где обёртка уже расходилась с прошивкой, - поэтому
        только в лог, не в данные).
        """
        parts = []
        try:
            obj = alarm_info.stuObject
            parts.append(f"type={TrafficCallBackAlarmInfo._raw_text(obj.szObjectType)!r}")
            parts.append(f"nConfidence={int(obj.nConfidence)}")
        except Exception:  # noqa: BLE001
            parts.append("stuObject=?")
        try:
            parts.append(f"nPlateRecogniseConf={int(alarm_info.stCommInfo.nPlateRecogniseConf)}")
        except Exception:  # noqa: BLE001
            pass
        return " ".join(parts)

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
            "plate_confidence_raw": self.plate_confidence_raw,
            "plate_confidence_pct": self.plate_confidence_pct,
            "confidence_debug": self.confidence_debug,
        }
