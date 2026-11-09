"""Наблюдаемость процесса камеры: GET /health (JSON) и /metrics (Prometheus)."""
import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

RELOGIN_AFTER_S = 120


class Health:
    def __init__(self, camera, run_id, journal, runners, watchdog=None, cfg_describe=None, clock=None, version="step1"):
        self.camera, self.run_id, self.j, self.runners, self.wd = camera, run_id, journal, runners, watchdog
        self.cfg, self.version = cfg_describe or {}, version
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.sdk = {"logged_in": False, "disconnected_at": None, "resubscribes": 0}
        self.started = self.clock()

    def set_sdk(self, logged_in=None, disconnected_at="keep", resubscribes_inc=0):
        if logged_in is not None:
            self.sdk["logged_in"] = logged_in
        if disconnected_at != "keep":
            self.sdk["disconnected_at"] = disconnected_at
        self.sdk["resubscribes"] += resubscribes_inc

    def snapshot(self):
        now = self.clock()
        last = self.j.last_ts
        age = (now - last).total_seconds() if last else None
        disc = self.sdk["disconnected_at"]
        disc_s = (now.timestamp() - disc) if disc else 0
        rec = {}
        for r in self.runners:
            rec[r.r.name] = {"mode": r.r.mode, "cursor_lag_lines": r.lag(), "dead": r.st.dead_count(r.r.name),
                             "delivered": r.delivered, "last_error": r.last_error}
        ok = self.sdk["logged_in"] and disc_s < RELOGIN_AFTER_S
        return {"ok": bool(ok), "camera": self.camera, "run": self.run_id, "version": self.version,
                "sdk": {"logged_in": self.sdk["logged_in"], "disconnected_s": int(disc_s), "resubscribes": self.sdk["resubscribes"]},
                "last_event_at": last.isoformat() if last else None, "last_event_age_s": int(age) if age is not None else None,
                "journal": {"events_written": self.j.written, "files": len(self.j.files())},
                "recipients": rec, "watchdog": {"stage": self.wd.stage if self.wd else None, "last_action": self.wd.last_action if self.wd else None},
                "config": self.cfg, "uptime_s": int((now - self.started).total_seconds())}

    def metrics(self):
        s = self.snapshot()
        c = self.camera
        lines = ['bridge_up{camera="%s"} %d' % (c, 1 if s["ok"] else 0),
                 'bridge_events_written_total{camera="%s"} %d' % (c, s["journal"]["events_written"]),
                 'bridge_sdk_resubscribes_total{camera="%s"} %d' % (c, s["sdk"]["resubscribes"])]
        if s["last_event_age_s"] is not None:
            lines.append('bridge_last_event_age_seconds{camera="%s"} %d' % (c, s["last_event_age_s"]))
        for n, v in s["recipients"].items():
            lines.append('bridge_recipient_lag_lines{camera="%s",recipient="%s"} %d' % (c, n, v["cursor_lag_lines"]))
            lines.append('bridge_recipient_dead_total{camera="%s",recipient="%s"} %d' % (c, n, v["dead"]))
        return "\n".join(lines) + "\n"

    def serve(self, port):
        health = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/health"):
                    body, ctype = json.dumps(health.snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8"
                    code = 200 if json.loads(body)["ok"] else 503
                elif self.path.startswith("/metrics"):
                    body, ctype, code = health.metrics().encode(), "text/plain; version=0.0.4", 200
                else:
                    body, ctype, code = b'{"error":"not found"}', "application/json", 404
                self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
