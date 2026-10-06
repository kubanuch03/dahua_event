import re

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
        self.plate_country_str = self._read_country(alarm_info.stCommInfo)
        self.plate_province_str = self._decode_string(alarm_info.stCommInfo.szProvince)
        # Уверенность распознавания НОМЕРА. В событии TrafficJunction
        # stuObject - это номерной знак (машина - в stuVehicle). Заголовок
        # NetSDK 2.0.0.1 обещает 0..255, но ITC413 5.003 шлёт 0..100 -
        # проверено на проде 06.10.2026 (см. _read_confidence).
        self.plate_confidence_raw, self.plate_confidence_pct = self._read_confidence(alarm_info)
        self.confidence_debug = self._confidence_debug(alarm_info)

        return self._get_alarm_info_dict()

    @staticmethod
    def _read_confidence(alarm_info):
        """
        (сырое, проценты 0..100) или (None, None).

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
        # Шкала 0..100, а НЕ 0..255 из заголовка NetSDK: сверка на проде
        # после выката 06.10.2026 - 10 проездов с трёх камер ITC413 5.003,
        # сырое 78..94, ни одного выше 100; перевод /255 давал 31..37 %.
        # Выше 100 - шкала неизвестна, процент не выдумываем.
        if raw <= 0 or raw > 100:
            return None, None
        return raw, raw

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

    #: На сколько байт реальное положение szCountry в памяти события
    #: разошлось с тем, что объявляет обёртка NetSDK. Замерено на ITC413
    #: (DHI-ITC413-PW4D-IZ1, прошивка 5.003.0000000.3.R от 2024-12-20,
    #: 15.09.2026): обёртка объявляет szCountry по смещению 2076, строка
    #: "KGZ" лежит на 2084. Поле дописано в самый хвост EVENT_COMM_INFO,
    #: сразу за bReserved*112, и ревизия структуры в прошивке отличается
    #: от той, по которой собрана обёртка.
    _COUNTRY_OFFSET_DRIFT = 8

    #: Правдоподобное значение страны: латиница, 2-19 символов ("KGZ",
    #: "Unknown"). Нужна именно проверка, а не доверие байтам: читаем из
    #: области, которую обёртка считает резервом, и мусор оттуда попал бы
    #: в базу как название страны.
    _COUNTRY_RE = re.compile(rb"^[A-Za-z]{2,19}$")

    def _read_country(self, comm_info):
        """
        Страна номера с обходом расхождения layout'а обёртки и прошивки.

        Сначала штатное поле - если однажды обёртку NetSDK или прошивку
        приведут в соответствие, оно заработает само и обход не понадобится.
        Пока оно пустое во ВСЕХ событиях: замер по 30 151 событию боевой
        базы Ала-Арчи - страна не определилась ни разу, при том что цвет
        номера из соседней структуры приходит в 99.7-100%. По HTTP та же
        камера отдаёт Country="KGZ", то есть данные есть, теряются на
        стыке.

        Фолбэк читает те же байты со смещением _COUNTRY_OFFSET_DRIFT и
        проверяет результат на правдоподобие. Любая неожиданность - не
        ctypes-структура, короткий буфер, мусор в байтах - это пустая
        строка, как и раньше: потерять справочное поле несопоставимо
        дешевле, чем уронить колбэк и потерять сам проезд.
        """
        value = self._decode_string(comm_info.szCountry).strip()
        if value:
            return value

        try:
            import ctypes

            raw = ctypes.string_at(ctypes.byref(comm_info), ctypes.sizeof(comm_info))
            offset = type(comm_info).szCountry.offset + self._COUNTRY_OFFSET_DRIFT
            chunk = raw[offset:offset + 20].split(b"\x00")[0]
        except Exception:
            # Не ctypes-структура (тесты с фейками) или SDK отдал что-то
            # неожиданное - молча остаёмся без страны.
            return ""

        if not self._COUNTRY_RE.match(chunk):
            return ""
        return chunk.decode("ascii", "replace")

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
