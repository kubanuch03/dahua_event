"""Журнал событий моста: суточные файлы JSONL, кадр события отдельным файлом, строки доставки получателей."""
import datetime as dt
import json
import os
import shutil
import threading

SCHEMA = 1


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


class Journal:
    def __init__(self, data_dir, site, camera, camera_id, run_id, clock=utc_now, keep_days=14):
        self.dir = os.path.join(data_dir, "journal")
        self.site, self.camera, self.camera_id, self.run_id = site, camera, camera_id, run_id
        self.clock, self.keep_days = clock, keep_days
        self.lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)
        self.last_ts = None
        self.written = 0

    def _day(self, ts):
        return ts.astimezone(dt.timezone.utc).strftime("%Y%m%d")

    def _append_line(self, day, rec):
        line = (json.dumps(rec, ensure_ascii=True, default=str) + "\n").encode("utf-8")
        with self.lock:
            with open(os.path.join(self.dir, day + ".jsonl"), "ab") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def event_id(self, n):
        return "%s_%s_%s_%s" % (self.site, self.camera, self.run_id, n)

    def _copy_frame(self, jpeg_path, ts, n):
        day = self._day(ts)
        os.makedirs(os.path.join(self.dir, day), exist_ok=True)
        base = "%s_%03d_%s" % (ts.strftime("%Y%m%dT%H%M%S"), ts.microsecond // 1000, n)
        for i in range(10):
            name = base + (".jpg" if i == 0 else "-%d.jpg" % i)
            dest = os.path.join(self.dir, day, name)
            try:
                with open(jpeg_path, "rb") as src, open(dest, "xb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                return "%s/%s" % (day, name), os.path.getsize(dest)
            except FileExistsError:
                continue
        raise OSError("нет свободного имени кадра")

    def append_event(self, n, a, jpeg_path, av, av_extra, model, outcome, sent, branch):
        """Строка события; outcome=queued — тело `sent` ждёт получателей."""
        ts = self.clock()
        frame, size, ferr = None, None, None
        try:
            frame, size = self._copy_frame(jpeg_path, ts, n)
        except Exception as e:  # noqa: BLE001
            ferr = repr(e)[:200]
        a = a if isinstance(a, dict) else {}
        rec = {"v": SCHEMA, "type": "event", "ts": ts.isoformat(), "site": self.site, "camera": self.camera, "camera_id": self.camera_id,
               "run": self.run_id, "n": n, "event_id": self.event_id(n), "branch": branch,
               "frame_src": os.path.basename(jpeg_path or ""), "frame": frame, "frame_size": size, "frame_error": ferr,
               "cam_plate": a.get("plate_number_str", ""), "cam_conf": a.get("plate_conf"), "cam_time": a.get("time_str"), "attrs": a,
               "av": av, "av_extra": av_extra, "model": model, "outcome": outcome, "sent": sent,
               "country": (sent or {}).get("licenseplatecountrycode"), "dolibarr": None, "event": normalized_event(a)}
        self._append_line(self._day(ts), rec)
        self.last_ts, self.written = ts, self.written + 1
        return rec

    def append_av2(self, out):
        """Итог второго номера."""
        ts = self.clock()
        self._append_line(self._day(ts), dict({"type": "av2", "ts": ts.isoformat(), "camera": self.camera, "run": self.run_id}, **out))

    def append_delivery(self, rec, outcome, dolibarr):
        """Итог доставки события получателем."""
        ts = self.clock()
        self._append_line(self._day(ts), {"type": "delivery", "ts": ts.isoformat(), "camera": self.camera, "run": rec.get("run"), "n": rec.get("n"),
                                          "event_id": rec.get("event_id"), "outcome": outcome, "dolibarr": dolibarr})

    def files(self):
        try:
            return sorted(f for f in os.listdir(self.dir) if len(f) == 14 and f.endswith(".jsonl") and f[:8].isdigit())
        except OSError:
            return []

    def read_from(self, fname, line, limit=100):
        """Целые строки файла начиная с номера line → [(номер, запись | None)]."""
        try:
            with open(os.path.join(self.dir, fname), "rb") as f:
                raw = f.read()
        except OSError:
            return []
        out = []
        for i, ln in enumerate(raw[:raw.rfind(b"\n") + 1].split(b"\n")[:-1]):
            if i < line:
                continue
            try:
                r = json.loads(ln.decode("utf-8"))
                out.append((i, r if isinstance(r, dict) else None))
            except (ValueError, UnicodeDecodeError):
                out.append((i, None))
            if len(out) >= limit:
                break
        return out

    def frame_bytes(self, rec):
        if not rec.get("frame"):
            return None
        try:
            with open(os.path.join(self.dir, rec["frame"]), "rb") as f:
                return f.read()
        except OSError:
            return None

    def purge(self, now=None):
        """Удалить файлы и каталоги кадров старше keep_days суток."""
        cut = ((now or self.clock()) - dt.timedelta(days=self.keep_days)).strftime("%Y%m%d")
        removed = 0
        for name in os.listdir(self.dir):
            if name[:8].isdigit() and name[:8] < cut:
                path = os.path.join(self.dir, name)
                try:
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed


def normalized_event(a):
    """Поля события в виде, который ждёт центр."""
    conf = None
    for k in ("plate_conf", "plate_confidence_pct"):
        try:
            v = int(a.get(k))
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 100:
            conf = v
            break
    return {"event_time": a.get("time_str") or "", "camera_plate": (a.get("plate_number_str") or "").strip().upper(), "camera_conf": conf,
            "vehicle_type": a.get("object_subType_str", ""), "vehicle_color": a.get("vehicle_color_str", ""),
            "plate_color": a.get("plate_color_str", ""), "cam_view": a.get("view") or {}}
