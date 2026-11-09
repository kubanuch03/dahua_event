"""Обработка события камеры: выбор номера и запись в журнал, отправку делают получатели."""
from bridge import capture
from logger import get_logger

MIN_PLATE_LEN = 4


class Pipeline:
    def __init__(self, detector, journal, av_mode, camera_code, camera_id):
        self.detector, self.journal, self.av_mode = detector, journal, av_mode
        self.camera_code, self.camera_id = camera_code, camera_id
        self.log = get_logger("PIPELINE")

    def handle(self, a, jpeg_path, n, branch="alarm"):
        """Выбрать номер и записать событие в журнал → строка журнала."""
        capture.begin()
        result, outcome = None, "not_sent"
        if branch == "alarm" and len(a["plate_number_str"]) < MIN_PLATE_LEN and self.av_mode() != "best":
            self.log.info(f"Plate too short, not sent: {a['plate_number_str']!r}")
            outcome = "short"
        else:
            try:
                result = self.detector.detect_state_number(jpeg_path=jpeg_path, camera_code=self.camera_code, camera_id=self.camera_id, a=a)
            except Exception as e:  # noqa: BLE001
                err = {"av_src": "error", "av_camera": a.get("plate_number_str", ""), "av_error": repr(e)}
                if branch != "alarm":
                    err["av_branch"] = branch
                get_logger("AUTOVISION").info(err)
                result = None
                if branch != "alarm":
                    outcome = f"model_error: {e!r}"[:200]
            if result and len(result.get("licenseplatenumber") or "") >= MIN_PLATE_LEN:
                outcome = "queued"
            else:
                if outcome == "not_sent":
                    self.log.info("AV: номер не определён ни камерой, ни моделью — не отправлено" + (" (без alarm info)" if branch != "alarm" else ""))
        av, extra, model = capture.current()
        sent = {k: v for k, v in result.items() if k != "photocontent"} if isinstance(result, dict) else None
        return self.journal.append_event(n, a, jpeg_path, av, extra, capture.model_short(model), outcome, sent, branch)
