"""
Событие «нахождение в зоне» -> данные для SmartParking (без NetSDK).

Запуск: python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.wander_info import PulseThrottle, build_payload, is_vehicle  # noqa: E402


class FakeUTC:
    dwYear, dwMonth, dwDay = 2026, 10, 3
    dwHour, dwMinute, dwSecond = 9, 15, 7


class FakeObj:
    def __init__(self, kind):
        self.szObjectType = kind.encode() + b"\x00junk"


class FakeWander:
    def __init__(self, action=1, objects=("Vehicle",), event_id=42, name=b"Rule1\x00\xff"):
        self.bEventAction = action
        self.nEventID = event_id
        self.UTC = FakeUTC()
        self.szName = name
        self.stuObjectIDs = [FakeObj(o) for o in objects] + [FakeObj("")] * 4
        self.nObjectNum = len(objects)


class TestWanderPayload(unittest.TestCase):
    def test_start_payload(self):
        p = build_payload(FakeWander(action=1), camera_id="cam-1", routing_key="exit_2")
        self.assertEqual(p["event_type"], "dwell")
        self.assertEqual(p["dwell_action"], "start")
        self.assertEqual(p["camera"], "exit_2")
        self.assertEqual(p["event_id"], "cam-1_w42_start")
        self.assertEqual(p["event_time"], "2026-10-03T09:15:07Z")
        self.assertEqual(p["rule"], "Rule1")
        self.assertEqual(p["object_type"], "Vehicle")

    def test_actions(self):
        self.assertEqual(build_payload(FakeWander(action=0), camera_id="c", routing_key="r")["dwell_action"], "pulse")
        self.assertEqual(build_payload(FakeWander(action=2), camera_id="c", routing_key="r")["dwell_action"], "stop")
        self.assertEqual(build_payload(FakeWander(action=9), camera_id="c", routing_key="r")["dwell_action"], "pulse")

    def test_pedestrian_is_skipped_vehicle_or_unknown_kept(self):
        self.assertIsNone(build_payload(FakeWander(objects=("Human",)), camera_id="c", routing_key="r"))
        self.assertIsNotNone(build_payload(FakeWander(objects=()), camera_id="c", routing_key="r"))
        self.assertTrue(is_vehicle(["Human", "Vehicle"]))

    def test_nonmotor_is_not_a_car(self):
        self.assertIsNone(build_payload(FakeWander(objects=("NonMotor",)), camera_id="c", routing_key="r"))

    def test_garbage_date_gives_empty_time(self):
        w = FakeWander()
        w.UTC.dwMonth = 0
        self.assertEqual(build_payload(w, camera_id="c", routing_key="r")["event_time"], "")

    def test_broken_time_does_not_lose_event(self):
        w = FakeWander()
        w.UTC = object()
        self.assertEqual(build_payload(w, camera_id="c", routing_key="r")["event_time"], "")

    def test_object_count_beyond_array_is_clamped(self):
        w = FakeWander(objects=("Vehicle",))
        w.nObjectNum = 999
        self.assertIsNotNone(build_payload(w, camera_id="c", routing_key="r"))


class TestPulseThrottle(unittest.TestCase):
    def test_pulse_once_per_window_start_stop_always(self):
        t = PulseThrottle(window_seconds=15)
        self.assertTrue(t.allow("cam", "start", 0))
        self.assertFalse(t.allow("cam", "pulse", 5))
        self.assertTrue(t.allow("cam", "pulse", 16))
        self.assertFalse(t.allow("cam", "pulse", 20))
        self.assertTrue(t.allow("cam", "stop", 21))
        self.assertTrue(t.allow("other", "pulse", 21))


if __name__ == "__main__":
    unittest.main()


class TestCallbackLayout(unittest.TestCase):
    """Отладочные кадры ANPR остались в ветке TRAFFICJUNCTION, а не уехали в WANDER."""

    def test_debug_snapshots_block_is_before_wander_branch(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "services", "callbacks.py")).read()
        debug = src.index("# 4. Логика сохранения изображений")
        wander = src.index("elif dwAlarmType == EM_EVENT_IVS_TYPE.WANDERDETECTION")
        self.assertLess(debug, wander)
