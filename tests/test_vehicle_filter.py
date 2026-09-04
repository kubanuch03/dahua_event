"""
_should_report() - фильтр "только транспорт" + дедуп по nSequence.

Регрессия на реальный live-инцидент 2026-09-04: камера на archa вернула
object_subType_str='Twocycle' (велосипед/двухколёсный, недокументированное
в SDK-энуме значение) - его не было в денай-листе, событие прошло фильтр и
создало сессию. Полный список реальных значений, увиденных в тот день на
всех 4 камерах archa: MicroTruck, MidPassengerCar, Motorcycle, MPV,
SaloonCar, SUV, Twocycle, Unknown - три из них (MidPassengerCar/MPV/SUV)
тоже не в SDK-энуме, но это настоящий транспорт и они ПРАВИЛЬНО прошли
фильтр (подтверждает, что денай-лист - верная стратегия, просто список
неполный).

Тестируем через синтетические объекты (без реального SDK/железа) - NetSDK
замокан в sys.modules, чтобы callbacks.py вообще импортировался.
"""
import sys
import types
import unittest
from unittest.mock import MagicMock


def _install_netsdk_mocks():
    if "NetSDK" in sys.modules:
        return
    netsdk_pkg = types.ModuleType("NetSDK")
    sdk_enum = types.ModuleType("NetSDK.SDK_Enum")
    sdk_callback = types.ModuleType("NetSDK.SDK_Callback")

    class _EnumPlaceholder:
        TRAFFICJUNCTION = 1

    sdk_enum.EM_EVENT_IVS_TYPE = _EnumPlaceholder

    def _cb_functype(*_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator

    sdk_callback.CB_FUNCTYPE = _cb_functype

    sys.modules["NetSDK"] = netsdk_pkg
    sys.modules["NetSDK.SDK_Enum"] = sdk_enum
    sys.modules["NetSDK.SDK_Callback"] = sdk_callback
    netsdk_pkg.SDK_Enum = sdk_enum
    netsdk_pkg.SDK_Callback = sdk_callback

    # traffic_info.py imports DEV_EVENT_TRAFFICJUNCTION_INFO from callbacks
    # module scope via "from NetSDK.SDK_Enum import *" - callbacks.py itself
    # doesn't need it directly, only main.py/traffic_monitor.py do, which
    # this test doesn't import.


_install_netsdk_mocks()

import os  # noqa: E402
import tempfile  # noqa: E402
os.environ.setdefault("SMARTPARKING_DATA_PROCESS_URL", "http://example.invalid/data_process/")

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo_root)

# callbacks.py creates a module-level Outbox(./data/outbox.db) on import
# (relative to CWD) - point CWD at a throwaway writable dir first so this
# test doesn't need write access to the real repo's data/ (which real
# container runs may have left root-owned).
os.chdir(tempfile.mkdtemp())

from services.callbacks import Callbacks, _NON_VEHICLE_SUBTYPES  # noqa: E402


class FakeAlarmInfo:
    def __init__(self, nSequence=1):
        self.nSequence = nSequence


class TestVehicleFilter(unittest.TestCase):
    def test_twocycle_is_rejected_regression(self):
        """Точный кейс, найденный вживую на archa 2026-09-04."""
        info = {"object_subType_str": "Twocycle", "plate_number_str": "01ABC"}
        should_report, reason = Callbacks._should_report(FakeAlarmInfo(), info, camera_id=1)
        self.assertFalse(should_report)
        self.assertIn("non-vehicle", reason)

    def test_all_real_vehicle_subtypes_seen_live_are_accepted(self):
        """MicroTruck/MidPassengerCar/SaloonCar/MPV/SUV - реально увиденные
        сегодня на archa значения, включая недокументированные в SDK
        (MidPassengerCar/MPV/SUV) - денай-лист должен пропускать все."""
        for i, subtype in enumerate(("MicroTruck", "MidPassengerCar", "SaloonCar", "MPV", "SUV")):
            with self.subTest(subtype=subtype):
                # Разный номер и camera_id на каждую итерацию - иначе
                # 15-секундный дедуп-бэкстоп по (camera_id, plate) считает
                # вторую и далее итерации дублем того же проезда.
                info = {"object_subType_str": subtype, "plate_number_str": f"PLATE{i}"}
                should_report, reason = Callbacks._should_report(FakeAlarmInfo(), info, camera_id=100 + i)
                self.assertTrue(should_report, f"{subtype} should be reported as a vehicle, got: {reason}")

    def test_known_non_vehicle_subtypes_still_rejected(self):
        for i, subtype in enumerate(("Bicycle", "Motorcycle", "Non-Motor", "Passerby", "Unknown", "Tricycle", "Electricbike")):
            with self.subTest(subtype=subtype):
                info = {"object_subType_str": subtype, "plate_number_str": f"PLATE{i}"}
                should_report, reason = Callbacks._should_report(FakeAlarmInfo(), info, camera_id=200 + i)
                self.assertFalse(should_report, f"{subtype} should be rejected, was accepted")

    def test_denylist_lowercase_contains_twocycle(self):
        self.assertIn("twocycle", _NON_VEHICLE_SUBTYPES)


if __name__ == "__main__":
    unittest.main()
