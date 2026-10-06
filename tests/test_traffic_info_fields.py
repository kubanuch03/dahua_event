"""
Разбор полей события камеры: марка, цвет номера, страна номера.

Камера отдаёт эти три вещи в каждом событии и раньше они выбрасывались:
марку (szVehicleSign - по даташиту DHI-ITC413-PW4D распознаётся 147
логотипов), цвет номера (szPlateColor - жёлтый в KG означает коммерческий
транспорт) и распознанную страну номера (szCountry).

Отдельно проверяется устойчивость декодирования: поля приходят сырыми
байтами из прошивки, и раньше один битый символ ронял UnicodeDecodeError
прямо внутри ctypes-колбэка, где исключение уже некому поймать - событие
проезда терялось целиком, ДО попадания в durable-outbox.

NetSDK здесь не нужен: services/traffic_info.py его не импортирует.

Запуск: python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.traffic_info import TrafficCallBackAlarmInfo  # noqa: E402


class FakeUTC:
    dwYear, dwMonth, dwDay = 2026, 9, 8
    dwHour, dwMinute, dwSecond = 14, 30, 5


class FakeTrafficCar:
    def __init__(self, plate=b"01577AGZ", plate_color=b"White", vehicle_color=b"Silver",
                 vehicle_sign=b"Toyota"):
        self.szPlateNumber = plate
        self.szPlateColor = plate_color
        self.szVehicleColor = vehicle_color
        self.szVehicleSign = vehicle_sign


class FakeVehicle:
    def __init__(self, subtype=b"SaloonCar"):
        self.szObjectSubType = subtype


class FakeCommInfo:
    def __init__(self, country=b"KG", province=b"Bishkek"):
        self.szCountry = country
        self.szProvince = province


class FakeObject:
    def __init__(self, confidence, kind=b"Plate", text=b"01577AGZ"):
        self.nConfidence = confidence
        self.szObjectType = kind
        self.szText = text


class FakeAlarmInfo:
    def __init__(self, **kw):
        self.UTC = FakeUTC()
        self.stTrafficCar = FakeTrafficCar(**{k: v for k, v in kw.items()
                                              if k in ("plate", "plate_color",
                                                       "vehicle_color", "vehicle_sign")})
        self.stuVehicle = FakeVehicle(**{k: v for k, v in kw.items() if k == "subtype"})
        self.stCommInfo = FakeCommInfo(**{k: v for k, v in kw.items()
                                          if k in ("country", "province")})
        self.stuObject = FakeObject(kw.get("confidence", 230), kw.get("kind", b"Plate"),
                                    kw.get("text", b"01577AGZ"))


class TestNewRecognitionFields(unittest.TestCase):
    def test_brand_plate_color_and_country_are_parsed(self):
        info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo())
        self.assertEqual(info["vehicle_sign_str"], "Toyota")
        self.assertEqual(info["plate_color_str"], "White")
        self.assertEqual(info["plate_country_str"], "KG")
        self.assertEqual(info["plate_province_str"], "Bishkek")

    def test_existing_fields_still_parsed(self):
        """Новые поля не должны сдвинуть то, что работало раньше."""
        info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo())
        self.assertEqual(info["plate_number_str"], "01577AGZ")
        self.assertEqual(info["vehicle_color_str"], "Silver")
        self.assertEqual(info["object_subType_str"], "SaloonCar")
        self.assertEqual(info["event_time_iso"], "2026-09-08T14:30:05Z")

    def test_unrecognized_country_comes_back_empty_not_crashing(self):
        """Камера не определила страну (nRegionCode = -1) - поле пустое."""
        info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo(country=b"", province=b""))
        self.assertEqual(info["plate_country_str"], "")

    def test_broken_bytes_do_not_raise(self):
        """
        Главный кейс: битые байты в любом поле НЕ должны кидать исключение.
        В ctypes-колбэке его никто не поймает, и потерянным окажется весь
        проезд, а не одно испорченное поле.
        """
        broken = b"\xff\xfe\x80Toyota"
        info = TrafficCallBackAlarmInfo().get_alarm_info(
            FakeAlarmInfo(vehicle_sign=broken, plate_color=broken, country=broken)
        )
        self.assertIn("Toyota", info["vehicle_sign_str"])
        self.assertIsInstance(info["plate_color_str"], str)
        self.assertIsInstance(info["plate_country_str"], str)

    def test_non_bytes_field_does_not_raise(self):
        """Совсем неожиданный тип в поле - пустая строка, а не падение."""
        alarm = FakeAlarmInfo()
        alarm.stTrafficCar.szVehicleSign = None
        info = TrafficCallBackAlarmInfo().get_alarm_info(alarm)
        self.assertEqual(info["vehicle_sign_str"], "")



class TestPlateConfidence(unittest.TestCase):
    """stuObject.nConfidence (0..255) - уверенность распознавания номера, в процентах."""

    def test_confidence_scaled_to_percent(self):
        info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo(confidence=230))
        self.assertEqual(info["plate_confidence_raw"], 230)
        self.assertEqual(info["plate_confidence_pct"], 90)

    def test_scale_edges(self):
        for raw, pct in ((255, 100), (1, 0), (128, 50)):
            info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo(confidence=raw))
            self.assertEqual(info["plate_confidence_pct"], pct, raw)

    def test_zero_and_out_of_range_mean_not_reported(self):
        """Прошивка, не заполняющая поле, шлёт 0 - это «не сообщила», а не 0 %."""
        for raw in (0, -1, 256, 1000):
            info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo(confidence=raw))
            self.assertIsNone(info["plate_confidence_pct"], raw)
            self.assertIsNone(info["plate_confidence_raw"], raw)

    def test_non_plate_object_is_ignored(self):
        """stuObject - не номер (машина, пусто) - уверенность не отдаём."""
        info = TrafficCallBackAlarmInfo().get_alarm_info(
            FakeAlarmInfo(confidence=230, kind=b"Vehicle", text=b""))
        self.assertIsNone(info["plate_confidence_pct"])

    def test_plate_text_without_type_is_accepted(self):
        info = TrafficCallBackAlarmInfo().get_alarm_info(
            FakeAlarmInfo(confidence=230, kind=b"", text=b"01577AGZ"))
        self.assertEqual(info["plate_confidence_pct"], 90)

    def test_debug_keeps_raw_values_before_filtering(self):
        info = TrafficCallBackAlarmInfo().get_alarm_info(FakeAlarmInfo(confidence=300))
        self.assertIsNone(info["plate_confidence_pct"])
        self.assertIn("nConfidence=300", info["confidence_debug"])
        self.assertIn("type='Plate'", info["confidence_debug"])

    def test_missing_field_does_not_lose_the_event(self):
        alarm = FakeAlarmInfo()
        del alarm.stuObject
        info = TrafficCallBackAlarmInfo().get_alarm_info(alarm)
        self.assertIsNone(info["plate_confidence_pct"])
        self.assertEqual(info["plate_number_str"], "01577AGZ")




class CountryOffsetDriftTests(unittest.TestCase):
    """
    Страна номера лежит в памяти не там, где её объявляет обёртка NetSDK.

    Замерено на боевой камере ITC413 (прошивка 5.003.0000000.3.R,
    15.09.2026): обёртка объявляет szCountry по смещению 2076, реальная
    строка "KGZ" - на 2084. Штатное поле поэтому пустое во всех событиях,
    и без обхода страна не доезжает до SmartParking вообще.
    """

    def _comm_info(self, payload: bytes, drift: int):
        """ctypes-структура, где строка лежит со сдвигом от объявленного поля."""
        import ctypes

        class FakeCommInfoStruct(ctypes.Structure):
            _fields_ = [
                ("bReserved", ctypes.c_byte * 16),
                ("szCountry", ctypes.c_char * 20),
                ("bTail", ctypes.c_byte * 32),
            ]

        info = FakeCommInfoStruct()
        info.szProvince = b""
        offset = FakeCommInfoStruct.szCountry.offset + drift
        buf = (ctypes.c_char * len(payload)).from_buffer(info, offset)
        buf.raw = payload
        return info

    def test_country_read_from_drifted_offset(self):
        info = self._comm_info(b"KGZ\x00", drift=8)
        parser = TrafficCallBackAlarmInfo()
        self.assertEqual(parser._read_country(info), "KGZ")

    def test_alarm_parsing_uses_the_drift_fallback(self):
        """Через весь разбор события, а не только _read_country: обход
        должен быть подключён в get_alarm_info."""
        alarm = FakeAlarmInfo()
        alarm.stCommInfo = self._comm_info(b"KGZ\x00", drift=8)
        info = TrafficCallBackAlarmInfo().get_alarm_info(alarm)
        self.assertEqual(info["plate_country_str"], "KGZ")

    def test_declared_field_wins_when_filled(self):
        """Если обёртку починят, берём штатное поле и никуда не лезем."""
        info = self._comm_info(b"XXX\x00", drift=8)
        info.szCountry = b"KGZ"
        parser = TrafficCallBackAlarmInfo()
        self.assertEqual(parser._read_country(info), "KGZ")

    def test_garbage_at_drifted_offset_is_ignored(self):
        """Читаем из области, которую обёртка считает резервом - мусор в базу не пускаем."""
        info = self._comm_info(b"\x01\x02\xff\x00", drift=8)
        parser = TrafficCallBackAlarmInfo()
        self.assertEqual(parser._read_country(info), "")

    def test_plain_object_without_ctypes_does_not_raise(self):
        """Фейки из остальных тестов - не ctypes; фолбэк обязан промолчать."""
        parser = TrafficCallBackAlarmInfo()
        self.assertEqual(parser._read_country(FakeCommInfo(country=b"")), "")

