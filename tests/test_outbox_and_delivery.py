"""
Регрессионные тесты на два бага, найденные code review 2026-09-04:

1. services/outbox.py - гонка между немедленной отправкой из потока
   захвата и фоновым drain-потоком могла доставить одно и то же событие в
   SmartParking дважды. Фикс: единственный потребитель очереди -
   drain-поток, callbacks.py теперь только enqueue().
2. services/smart_parking.py - любой HTTPError (в т.ч. 5xx) считался
   "доставлено" и строка удалялась из outbox - временный сбой сервера
   (например, во время передеплоя SmartParking) терял событие безвозвратно.

Не трогает NetSDK/реальные камеры - services/outbox.py и
services/smart_parking.py не импортируют NetSDK, тестируются напрямую.

Запуск: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.outbox import Outbox, start_drain_thread  # noqa: E402
from services.smart_parking import SmartParking  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, json_data=None, text="", raise_json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_json_error = raise_json_error

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if self._raise_json_error:
            import requests
            raise requests.exceptions.JSONDecodeError("bad json", "doc", 0)
        return self._json_data


class TestSmartParkingDeliveryClassification(unittest.TestCase):
    """Фикс #1: только 5xx/408/429 - retryable, остальное как раньше."""

    def setUp(self):
        self.client = SmartParking(api_url="http://example.invalid/event")

    def _send_with_status(self, status_code, **kwargs):
        with mock.patch("requests.post", return_value=FakeResponse(status_code, **kwargs)):
            return self.client._send_once({"license_plate": "01AAA01"})

    def test_200_is_delivered(self):
        delivered, result = self._send_with_status(200, json_data={"success": True})
        self.assertTrue(delivered)
        self.assertEqual(result, {"success": True})

    def test_200_with_non_json_body_is_delivered(self):
        delivered, result = self._send_with_status(200, raise_json_error=True)
        self.assertTrue(delivered, "2xx с не-JSON телом - запрос ДОСТАВЛЕН, повтор бессмыслен")

    def test_404_business_rejection_is_delivered_not_retried(self):
        delivered, _ = self._send_with_status(404, text="Камера не найдена")
        self.assertTrue(delivered)

    def test_400_business_rejection_is_delivered_not_retried(self):
        delivered, _ = self._send_with_status(400, text="Нет открытой сессии")
        self.assertTrue(delivered)

    def test_500_is_retryable_not_delivered(self):
        delivered, _ = self._send_with_status(500)
        self.assertFalse(
            delivered,
            "5xx - временная проблема сервера (напр. редеплой SmartParking), "
            "должно остаться в outbox, а не считаться доставленным",
        )

    def test_502_503_504_are_retryable(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                delivered, _ = self._send_with_status(status)
                self.assertFalse(delivered)

    def test_429_is_retryable(self):
        delivered, _ = self._send_with_status(429)
        self.assertFalse(delivered)

    def test_connection_error_is_retryable(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
            delivered, _ = self.client._send_once({"license_plate": "01AAA01"})
        self.assertFalse(delivered)

    def test_unclassified_request_exception_is_retryable_not_delivered(self):
        """Раньше ЛЮБОЙ RequestException (кроме Connection/Timeout) считался
        "доставлено" - реальная потеря события без доказательства, что
        сервер вообще получил запрос."""
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.RequestException("weird")):
            delivered, _ = self.client._send_once({"license_plate": "01AAA01"})
        self.assertFalse(delivered)


class TestOutboxNoDoubleDelivery(unittest.TestCase):
    """Фикс #2: ровно один потребитель очереди (drain-поток) - событие не
    может быть отправлено дважды параллельно."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "outbox.db")
        self.outbox = Outbox(self.db_path)
        self.delivered_ids = []
        self.lock = threading.Lock()

    def test_rapid_enqueue_each_event_delivered_exactly_once(self):
        n_events = 20

        def sender(event_data, photo):
            # Имитация небыстрого HTTP-запроса - если бы был второй
            # потребитель очереди (старый баг), он успел бы забрать ту же
            # строку, пока эта "отправка" ещё не завершилась.
            time.sleep(0.02)
            with self.lock:
                self.delivered_ids.append(event_data["id"])
            return True

        thread = start_drain_thread(self.outbox, sender, idle_sleep=0.05, min_backoff=0.05, max_backoff=0.1)

        for i in range(n_events):
            self.outbox.enqueue({"id": i, "license_plate": f"EVT{i}"})

        deadline = time.time() + 5
        while len(self.delivered_ids) < n_events and time.time() < deadline:
            time.sleep(0.05)

        self.assertEqual(
            len(self.delivered_ids), n_events,
            f"ожидали {n_events} доставок, получили {len(self.delivered_ids)}: {self.delivered_ids}",
        )
        self.assertEqual(
            len(set(self.delivered_ids)), n_events,
            f"событие доставлено больше одного раза: {self.delivered_ids}",
        )
        self.assertEqual(self.outbox.pending_count(), 0)

    def test_failed_delivery_stays_in_queue_and_retries(self):
        attempts = {"count": 0}

        def flaky_sender(event_data, photo):
            attempts["count"] += 1
            return attempts["count"] >= 3  # первые 2 попытки - "сеть недоступна"

        start_drain_thread(self.outbox, flaky_sender, idle_sleep=0.05, min_backoff=0.05, max_backoff=0.1)
        self.outbox.enqueue({"id": 1, "license_plate": "RETRY1"})

        deadline = time.time() + 5
        while attempts["count"] < 3 and time.time() < deadline:
            time.sleep(0.05)

        time.sleep(0.1)  # дать успешной попытке долиться до delete()
        self.assertGreaterEqual(attempts["count"], 3)
        self.assertEqual(self.outbox.pending_count(), 0)

    def test_poison_row_is_dropped_after_repeated_parse_failures_not_blocking_forever(self):
        # Пишем валидную строку напрямую в БД, потом портим её JSON руками -
        # имитация повреждения на диске, которое enqueue() в норме не
        # допускает.
        row_id = self.outbox.enqueue({"id": 1, "license_plate": "GOOD"})
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE events SET event_data = ? WHERE id = ?", ("{not valid json", row_id))
        conn.commit()
        conn.close()

        self.outbox.enqueue({"id": 2, "license_plate": "AFTER_POISON"})

        delivered_plates = []

        def sender(event_data, photo):
            delivered_plates.append(event_data["license_plate"])
            return True

        start_drain_thread(self.outbox, sender, idle_sleep=0.05, min_backoff=0.02, max_backoff=0.05)

        deadline = time.time() + 5
        while "AFTER_POISON" not in delivered_plates and time.time() < deadline:
            time.sleep(0.05)

        self.assertIn(
            "AFTER_POISON", delivered_plates,
            "событие ПОСЛЕ отравленной строки должно доставиться, а не застрять в FIFO навсегда",
        )
        # Небольшой зазор между "sender() вернул True" (проверяется выше) и
        # фактическим outbox.delete() той же строки в drain-потоке - ждём
        # его, а не проверяем pending_count() мгновенно.
        deadline = time.time() + 2
        while self.outbox.pending_count() != 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.outbox.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
