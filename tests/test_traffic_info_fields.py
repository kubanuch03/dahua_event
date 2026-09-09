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


class FakeAlarmInfo:
    def __init__(self, **kw):
        self.UTC = FakeUTC()
        self.stTrafficCar = FakeTrafficCar(**{k: v for k, v in kw.items()
                                              if k in ("plate", "plate_color",
                                                       "vehicle_color", "vehicle_sign")})
        self.stuVehicle = FakeVehicle(**{k: v for k, v in kw.items() if k == "subtype"})
        self.stCommInfo = FakeCommInfo(**{k: v for k, v in kw.items()
                                          if k in ("country", "province")})


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


if __name__ == "__main__":
    unittest.main()
