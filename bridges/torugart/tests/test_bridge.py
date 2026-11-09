"""Тесты ядра моста: журнал, перехват решения, обработчик события, получатели, watchdog, /health, настройки."""
import base64, datetime as dt, json, logging, os, re, sys, tempfile, unittest, urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import requests  # noqa: E402
from bridge import capture, health as H, journal as J, pipeline as P, recipients as R, watchdog as W  # noqa: E402
from services import config  # noqa: E402

ENV = {"DOLIBARR_URL": "http://dolibarr.example/api", "DOLIBARR_API_KEY": "k", "BRIDGE_CAMERA_CODE": "Cam_Tor_VGK2", "BRIDGE_CAMERA_IP": "10.0.0.2", "BRIDGE_CAMERA_PASSWORD": "pw"}
T0 = dt.datetime(2026, 11, 5, 10, 0, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t
    def add(self, s): self.t += dt.timedelta(seconds=s)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = self.tmp.name
        self.clock = Clock()
        self.j = J.Journal(self.d, "torugart", "Cam_Tor_VGK2", 7, "20261105_090000", clock=self.clock)
        self.frame = os.path.join(self.d, "Global_Img1.jpg")
        with open(self.frame, "wb") as f: f.write(b"\xff\xd8JPEG-BYTES")

    def tearDown(self): self.tmp.cleanup()

    def attrs(self, plate="01123ABC", **kw):
        a = {"plate_number_str": plate, "time_str": "2026-11-05 16:00:01", "object_subType_str": "Truck", "vehicle_color_str": "White",
             "plate_color_str": "Blue", "plate_conf": 87, "view": {"lane_no": 2}}
        a.update(kw); return a

    def ev(self, n=1, plate="01123ABC", queued=True, **kw):
        sent = {"label": plate, "licenseplatenumber": plate, "camera": 7, "cameracode": "Cam_Tor_VGK2", "status": 0} if queued else None
        return self.j.append_event(n, self.attrs(plate), self.frame, {"av_mode": "best", "av_src": "agree"}, None, None,
                                   "queued" if queued else "not_sent", sent, "alarm")


class JournalTests(Base):
    def test_row_has_fields_read_by_reports_and_collector(self):
        rec = self.ev(5)
        self.assertEqual(rec["event_id"], "torugart_Cam_Tor_VGK2_20261105_090000_5")
        self.assertRegex(rec["frame"], r"^20261105/20261105T100000_000_5\.jpg$")
        r = json.loads(open(os.path.join(self.d, "journal", "20261105.jsonl"), "rb").read())
        for k in ("v", "type", "ts", "camera", "camera_id", "run", "n", "branch", "frame", "frame_size", "cam_plate", "cam_conf", "cam_time", "attrs",
                  "av", "av_extra", "model", "outcome", "sent", "country", "dolibarr"):
            self.assertIn(k, r)
        self.assertEqual((r["type"], r["run"], r["n"], r["cam_conf"]), ("event", "20261105_090000", 5, 87))
        self.assertEqual(open(os.path.join(self.d, "journal", rec["frame"]), "rb").read(), b"\xff\xd8JPEG-BYTES")

    def test_one_line_per_event_with_line_separators_in_values(self):
        self.ev(1, plate="AB CD E\x85F")
        raw = open(os.path.join(self.d, "journal", "20261105.jsonl"), "rb").read()
        self.assertEqual(raw.count(b"\n"), 1); self.assertTrue(raw.isascii())

    def test_day_rollover_and_partial_tail(self):
        self.clock.t = dt.datetime(2026, 11, 5, 23, 59, 59, tzinfo=dt.timezone.utc); self.ev(1)
        self.clock.add(2); self.ev(2)
        self.assertEqual(self.j.files(), ["20261105.jsonl", "20261106.jsonl"])
        with open(os.path.join(self.d, "journal", "20261106.jsonl"), "ab") as f: f.write('{"n": 3, "обрыв'.encode())
        self.assertEqual([r["n"] for _, r in self.j.read_from("20261106.jsonl", 0)], [2])

    def test_av2_and_delivery_rows(self):
        self.j.append_av2({"av2": "final", "av2_frame": "Global_Img9.jpg", "av2_status": "would_send"})
        rec = self.ev(3); self.j.append_delivery(rec, "sent", {"http": 200})
        rows = [r for _, r in self.j.read_from("20261105.jsonl", 0)]
        self.assertEqual([r["type"] for r in rows], ["av2", "event", "delivery"])
        self.assertEqual((rows[2]["event_id"], rows[2]["outcome"], rows[2]["dolibarr"]), (rec["event_id"], "sent", {"http": 200}))

    def test_missing_frame_is_recorded(self):
        rec = self.j.append_event(1, self.attrs(), "/nonexistent.jpg", None, None, None, "queued", {"licenseplatenumber": "X"}, "alarm")
        self.assertIsNone(rec["frame"]); self.assertTrue(rec["frame_error"])

    def test_purge_keeps_fourteen_days(self):
        os.makedirs(os.path.join(self.d, "journal", "20261001"))
        with open(os.path.join(self.d, "journal", "20261001.jsonl"), "w") as f: f.write("{}\n")
        self.ev(1); self.assertEqual(self.j.purge(), 2); self.assertEqual(self.j.files(), ["20261105.jsonl"])


class FakeResp:
    def __init__(self, code=200, text="42"): self.status_code, self.text = code, text


def mkrun(base, post, mode="on", dedup=False, clock=None):
    cfg = config.Config(dict(ENV), file_path="/nonexistent")
    if dedup:
        with open(os.path.join(base.d, "av_dedup"), "w") as f: f.write("on")
    rc = R.DolibarrRecipient(cfg, mode=mode, data_dir=base.d, post=post, clock=clock or __import__("time").time)
    st = R.State(os.path.join(base.d, "state", "recipients.sqlite"))
    return R.Runner(base.j, rc, st, clock=clock or __import__("time").time), st


def rows(base):
    return [r for f in base.j.files() for _, r in base.j.read_from(f, 0)]


class RecipientTests(Base):
    def test_delivers_exact_body_and_writes_delivery_row(self):
        sent = []; run, _ = mkrun(self, lambda url, **kw: sent.append((url, kw)) or FakeResp(200, "42"))
        self.ev(1, plate="01KG123ABC"); run.process_once()
        url, kw = sent[0]
        self.assertTrue(url.endswith("/autovisionhistorys")); self.assertEqual(kw["headers"], {"DOLAPIKEY": "k"})
        self.assertEqual(kw["json"]["photocontent"], base64.b64encode(b"\xff\xd8JPEG-BYTES").decode())
        self.assertNotIn("_event_id", kw["json"])
        d = [r for r in rows(self) if r["type"] == "delivery"][0]
        self.assertEqual((d["outcome"], d["dolibarr"]["http"], d["dolibarr"]["id"]), ("sent", 200, 42))

    def test_not_queued_events_are_skipped(self):
        sent = []; run, st = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp())
        self.ev(1, queued=False); self.ev(2); run.process_once(); self.assertEqual(len(sent), 1)

    def test_connection_error_retries_with_growing_pause(self):
        t = [1000.0]; calls = []
        def post(*a, **k):
            calls.append(1)
            if len(calls) < 3: raise requests.ConnectionError("down")
            return FakeResp()
        run, st = mkrun(self, post, clock=lambda: t[0]); self.ev(1); self.ev(2)
        run.process_once(); self.assertEqual(st.get("dolibarr")[1], 0)
        t[0] += 6; self.assertEqual(run.process_once(), 0)
        t[0] += 6; run.process_once(); self.assertEqual(st.get("dolibarr")[1], 0)
        t[0] += 21; run.process_once(); self.assertEqual(len(calls), 4); self.assertGreaterEqual(st.get("dolibarr")[1], 2)      # курсор прошёл обе записи и их строки доставки

    def test_http_error_keeps_live_outcome_and_goes_dead(self):
        run, st = mkrun(self, lambda *a, **k: FakeResp(500, "err")); self.ev(1); run.process_once()
        d = [r for r in rows(self) if r["type"] == "delivery"][0]
        self.assertEqual((d["outcome"], d["dolibarr"]["http"]), ("sent", 500)); self.assertEqual(st.dead_count("dolibarr"), 1)

    def test_timeout_not_retried(self):
        def tmo(*a, **k): raise requests.Timeout("slow")
        run, st = mkrun(self, tmo); self.ev(1); run.process_once()
        d = [r for r in rows(self) if r["type"] == "delivery"][0]
        self.assertTrue(d["outcome"].startswith("push_error")); self.assertEqual(run.process_once(), 0)

    def test_missing_frame_goes_dead(self):
        run, st = mkrun(self, lambda *a, **k: FakeResp()); rec = self.ev(1)
        os.remove(os.path.join(self.d, "journal", rec["frame"])); run.process_once()
        self.assertEqual(st.dead_count("dolibarr"), 1)

    def test_start_does_not_resend_history_and_cursor_survives_restart(self):
        sent = []; self.ev(1); self.ev(2)
        run, st = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp()); run.process_once(); self.assertEqual(sent, [])
        self.ev(3); run.process_once(); self.assertEqual(len(sent), 1)
        run2, _ = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp()); run2.process_once(); self.assertEqual(len(sent), 1)

    def test_dedup_window(self):
        t = [5000.0]; sent = []
        run, st = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp(), dedup=True, clock=lambda: t[0])
        self.ev(1, plate="PI1234"); self.ev(2, plate="P11234"); self.ev(3, plate="PI1234")
        run.process_once(); self.assertEqual(len(sent), 1)
        self.assertEqual([r["outcome"] for r in rows(self) if r["type"] == "delivery"], ["sent", "dedup", "dedup"])
        t[0] += 301; self.ev(4, plate="PI1234"); run.process_once(); self.assertEqual(len(sent), 2)

    def test_shadow_writes_file_without_network(self):
        sent = []; run, _ = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp(), mode="shadow")
        self.ev(1); run.process_once()
        p = os.path.join(self.d, "recipients", "dolibarr.shadow.jsonl")
        self.assertEqual(sent, []); self.assertIn("b64", open(p).read())

    def test_off_does_nothing(self):
        sent = []; run, _ = mkrun(self, lambda *a, **k: sent.append(1) or FakeResp(), mode="off"); self.ev(1)
        self.assertEqual(run.process_once(), 0); self.assertEqual(sent, [])


class FakeDetector:
    def __init__(self, ret=None, exc=None, log=None, model=None):
        self.ret, self.exc, self.log, self.model, self.calls = ret, exc, log, model, 0

    def detect_state_number(self, jpeg_path, camera_code, camera_id, a):
        self.calls += 1
        if self.model is not None:
            capture._tls.model = self.model
        for m in (self.log or []):
            logging.getLogger("AUTOVISION").info(m)
        if self.exc: raise self.exc
        return self.ret


class PipelineTests(Base):
    def setUp(self):
        Base.setUp(self)
        lg = logging.getLogger("AUTOVISION"); lg.setLevel(logging.INFO)
        import types
        capture.install(types.SimpleNamespace(ask_model=lambda *a, **k: None))

    def go(self, det, mode="best", plate="01123ABC", branch="alarm"):
        return P.Pipeline(det, self.j, lambda: mode, "Cam_Tor_VGK2", 7).handle(self.attrs(plate), self.frame, 1, branch)

    def test_queued_keeps_body_without_photo_and_captures_decision(self):
        det = FakeDetector({"licenseplatenumber": "01KG123ABC", "photocontent": "AAA", "licenseplatecountrycode": "KG"},
                           log=[{"av_mode": "best", "av_src": "agree"}, {"av_cc": "KG", "av_cc_on": True}], model={"plate": "01123ABC", "boxes": [{"text": "x", "conf": 1}]})
        rec = self.go(det)
        self.assertEqual(rec["outcome"], "queued"); self.assertNotIn("photocontent", rec["sent"]); self.assertEqual(rec["country"], "KG")
        self.assertEqual(rec["av"]["av_src"], "agree"); self.assertEqual(rec["av_extra"][0]["av_cc"], "KG"); self.assertEqual(rec["model"]["plate"], "01123ABC")

    def test_short_plate_outside_best_skips_detector(self):
        det = FakeDetector({"licenseplatenumber": "X"}); rec = self.go(det, mode="shadow", plate="AB1")
        self.assertEqual((det.calls, rec["outcome"], rec["sent"]), (0, "short", None))

    def test_short_plate_in_best_goes_to_model(self):
        det = FakeDetector({"licenseplatenumber": "01KG123ABC"}); rec = self.go(det, plate="AB1")
        self.assertEqual((det.calls, rec["outcome"]), (1, "queued"))

    def test_dropped_event_is_journaled_not_queued(self):
        rec = self.go(FakeDetector({"label": "", "licenseplatenumber": ""}, log=[{"av_mode": "best", "av_src": "none"}]))
        self.assertEqual((rec["outcome"], rec["av"]["av_src"]), ("not_sent", "none"))

    def test_detector_exception_by_branch(self):
        rec = self.go(FakeDetector(exc=RuntimeError("boom")))
        self.assertEqual(rec["outcome"], "not_sent")
        rec2 = self.go(FakeDetector(exc=RuntimeError("boom")), branch="no_alarm_info")
        self.assertTrue(rec2["outcome"].startswith("model_error"))

    def test_av2_sink_writes_journal_row(self):
        capture.on_av2(self.j.append_av2)
        logging.getLogger("AUTOVISION").info({"av2": "final", "av2_frame": "Global_Img3.jpg", "av2_status": "would_send"})
        self.assertEqual([r["type"] for r in rows(self)], ["av2"])


class WatchdogTests(Base):
    def test_threshold_clamped_and_default(self):
        t = T0 - dt.timedelta(days=1)
        for i in range(60):
            self.clock.t = t; self.ev(i + 1); t += dt.timedelta(seconds=60)
        self.clock.t = T0
        times = W.event_times(self.j, now=T0)
        self.assertEqual(W.gap_threshold(times, times[10].hour, 10, 900, 7200), 900)
        self.assertEqual(W.gap_threshold([], 3, 10, 900, 7200, default=100000), 7200)

    def test_stages_and_reset(self):
        wd = W.Watchdog(self.j, self.clock, lo=100, hi=100, mult=1); acts = []
        for _ in range(450):
            self.clock.add(1); a = wd.tick(); acts.append(a) if a else None
        self.assertEqual(acts, ["resubscribe", "relogin", "exit"])
        wd2 = W.Watchdog(self.j, self.clock, lo=100, hi=100, mult=1)
        self.clock.add(150); self.assertEqual(wd2.tick(), "resubscribe")
        wd2.note_event(); self.clock.add(50); self.assertIsNone(wd2.tick()); self.assertEqual(wd2.stage, 0)

    def test_silent_when_disconnected_or_disabled(self):
        wd = W.Watchdog(self.j, self.clock, lo=10, hi=10, mult=1)
        for _ in range(100): self.clock.add(5); self.assertIsNone(wd.tick(connected=False))
        wd3 = W.Watchdog(self.j, self.clock, lo=10, hi=10, mult=1, enabled=False); self.clock.add(10 ** 5); self.assertIsNone(wd3.tick())


class HealthTests(Base):
    def test_snapshot_and_http(self):
        run, _ = mkrun(self, lambda *a, **k: FakeResp()); wd = W.Watchdog(self.j, self.clock)
        h = H.Health("Cam_Tor_VGK2", "r1", self.j, [run], wd, {"x": 1}, clock=self.clock)
        self.assertFalse(h.snapshot()["ok"]); self.assertIsNone(h.snapshot()["last_event_at"])
        h.set_sdk(logged_in=True, disconnected_at=None); self.ev(1); self.clock.add(90)
        s = h.snapshot(); self.assertTrue(s["ok"]); self.assertEqual(s["last_event_age_s"], 90)
        h.set_sdk(disconnected_at=self.clock.t.timestamp() - 300); self.assertFalse(h.snapshot()["ok"])
        h.set_sdk(disconnected_at=None)
        srv = h.serve(0); port = srv.server_address[1]
        try:
            self.assertEqual(json.loads(urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3).read())["camera"], "Cam_Tor_VGK2")
            m = urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port, timeout=3).read().decode()
            self.assertIn('bridge_up{camera="Cam_Tor_VGK2"} 1', m)
        finally:
            srv.shutdown()


class ConfigTests(unittest.TestCase):
    def test_required_defaults_and_layers(self):
        c = config.Config(dict(ENV), file_path="/nonexistent")
        self.assertEqual((c.camera_port, c.camera_user, c.health_port, c.creds_from_dolibarr, c.require_dolibarr_fields), (37777, "admin", 8099, False, False))
        for name in ("DOLIBARR_API_KEY", "BRIDGE_CAMERA_CODE", "BRIDGE_CAMERA_IP", "BRIDGE_CAMERA_PASSWORD"):
            env = dict(ENV); env.pop(name)
            with self.assertRaises(config.ConfigError): config.Config(env, file_path="/nonexistent")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bridge.json"); json.dump({"BRIDGE_HEALTH_PORT": 9100, "BRIDGE_DOLIBARR_MODE": "shadow"}, open(p, "w"))
            c = config.Config({**ENV, "BRIDGE_DOLIBARR_MODE": "on"}, file_path=p)
            self.assertEqual((c.health_port, c.dolibarr_mode), (9100, "on"))

    def test_describe_has_no_secrets(self):
        d = str(config.Config(dict(ENV), file_path="/nonexistent").describe())
        self.assertNotIn("'pw'", d); self.assertNotIn("'k'", d)


class DolibarrFieldsTests(unittest.TestCase):
    def extract(self, env, items):
        from services import dolibarr, config as cfgmod
        cfgmod._CFG = config.Config({**ENV, **env}, file_path="/nonexistent")
        d = dolibarr.Dolibarr("h", "c", "k", "p"); d.camera_data = items
        try: return d.extract_camera_fields()
        finally: cfgmod._CFG = None

    def test_variants(self):
        item = {"status": 1, "id": 3, "code": "Cam_Tor_VGK2", "username": "u", "password": "p", "porthttp": "1", "portrtsp": "2", "entrypointrtsp": "r", "ipaddress": "9.9.9.9"}
        r = self.extract({}, [item, dict(item, code="Other"), dict(item, status=0)])
        self.assertEqual([(x["id"], x["ipaddress"], x["username"], x["password"], x["port"]) for x in r], [(3, "10.0.0.2", "admin", "pw", 37777)])
        self.assertEqual(self.extract({"BRIDGE_CREDS_FROM_DOLIBARR": "1"}, [item])[0]["username"], "u")
        self.assertEqual(self.extract({"BRIDGE_CREDS_FROM_DOLIBARR": "1"}, [dict(item, username=None, password=None)])[0]["password"], "pw")
        self.assertIsNone(self.extract({"BRIDGE_REQUIRE_DOLIBARR_FIELDS": "1"}, [dict(item, porthttp=None)]))
        self.assertIsNotNone(self.extract({}, [dict(item, porthttp=None)]))


if __name__ == "__main__":
    unittest.main()
