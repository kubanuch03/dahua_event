"""Получатели событий: у каждого свой читатель журнала с курсором, паузами и мёртвыми письмами."""
import base64
import json
import os
import sqlite3
import threading
import time

import requests

MAX_ATTEMPTS = 20


class Result:
    def __init__(self, kind, reason="", outcome="", dolibarr=None):
        self.kind, self.reason, self.outcome, self.dolibarr = kind, reason, outcome, dolibarr

    @staticmethod
    def ok(dolibarr=None):
        return Result("ok", outcome="sent", dolibarr=dolibarr)

    @staticmethod
    def retry(reason):
        return Result("retry", reason)

    @staticmethod
    def drop(reason, outcome="dead"):
        return Result("drop", reason, outcome)


class Recipient:
    name = "base"

    def __init__(self, mode="on"):
        self.mode = mode if mode in ("on", "shadow", "off") else "on"

    def accepts(self, rec):
        return False

    def build(self, rec, frame):
        raise NotImplementedError

    def deliver(self, payload):
        raise NotImplementedError


def dolibarr_resp(resp):
    """Ответ Dolibarr в виде, который пишется в журнал."""
    if resp is None:
        return None
    try:
        body = (resp.text or "")[:300]
        rid = None
        try:
            j = json.loads(body)
            if isinstance(j, (int, str)) and str(j).strip().isdigit():
                rid = int(j)
            elif isinstance(j, dict):
                return {"http": resp.status_code, "id": j.get("id") or j.get("rowid"), "ref": j.get("ref"), "body": body[:200]}
        except ValueError:
            pass
        return {"http": resp.status_code, "id": rid, "body": body[:200]}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:200]}


class DolibarrRecipient(Recipient):
    """Отправка события в Dolibarr; повтор только при отказе соединения, отсев повторов номера по окну."""
    name = "dolibarr"

    def __init__(self, cfg, mode="on", data_dir="/app/data", dedup_window=300, timeout=15, clock=time.time, post=None):
        Recipient.__init__(self, mode)
        self.url = "%s/autovisionhistorys" % cfg.dolibarr_url
        self.key, self.timeout = cfg.dolibarr_key, timeout
        self.data_dir, self.window, self.clock = data_dir, dedup_window, clock
        self._post = post or (lambda *a, **k: requests.post(*a, **k))
        self._dup = {}
        self._passed = []

    def accepts(self, rec):
        return rec.get("type", "event") == "event" and rec.get("outcome") == "queued" and bool(rec.get("sent"))

    def _is_dup(self, plate, event_id):
        if not os.path.exists(os.path.join(self.data_dir, "av_dedup")) or event_id in self._passed:
            return False
        key = (plate or "").upper().replace("I", "1").replace("O", "0")
        now = self.clock()
        for k in [k for k, t in self._dup.items() if now - t >= self.window]:
            del self._dup[k]
        if key in self._dup:
            return True
        self._dup[key] = now
        self._passed.append(event_id)
        del self._passed[:-200]
        return False

    def build(self, rec, frame):
        if frame is None:
            return None
        body = dict(rec["sent"])
        body["photocontent"] = base64.b64encode(frame).decode("utf-8")
        return body

    def deliver(self, payload):
        if self._is_dup(payload.get("licenseplatenumber") or "", payload.get("_event_id", "")):
            return Result.drop("тот же номер за последние %d с" % self.window, outcome="dedup")
        body = {k: v for k, v in payload.items() if not k.startswith("_")}
        try:
            r = self._post(self.url, headers={"DOLAPIKEY": self.key}, json=body, timeout=self.timeout)
        except requests.ConnectionError as e:
            return Result.retry("соединение: %s" % str(e)[:80])
        except requests.RequestException as e:
            return Result.drop("запрос мог уйти, не повторяем: %s" % str(e)[:80], outcome="push_error: %s" % str(e)[:80])
        if r.status_code == 200:
            return Result.ok(dolibarr_resp(r))
        res = Result.drop("ответ %s" % r.status_code, outcome="sent")
        res.dolibarr = dolibarr_resp(r)
        return res


class State:
    """Курсоры и мёртвые письма получателей в SQLite."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS cursor(name TEXT PRIMARY KEY, file TEXT, line INTEGER, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, last_error TEXT DEFAULT '')")
        self.db.execute("CREATE TABLE IF NOT EXISTS dead(id INTEGER PRIMARY KEY, name TEXT, file TEXT, line INTEGER, event_id TEXT, reason TEXT, at REAL)")
        self.db.commit()
        self.lock = threading.Lock()

    def get(self, name):
        with self.lock:
            return self.db.execute("SELECT file,line,attempts,next_try,last_error FROM cursor WHERE name=?", (name,)).fetchone()

    def put(self, name, file, line, attempts=0, next_try=0.0, last_error=""):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cursor VALUES(?,?,?,?,?,?)", (name, file, line, attempts, next_try, last_error))
            self.db.commit()

    def dead(self, name, file, line, event_id, reason, at):
        with self.lock:
            self.db.execute("INSERT INTO dead(name,file,line,event_id,reason,at) VALUES(?,?,?,?,?,?)", (name, file, line, event_id, reason, at))
            self.db.commit()

    def dead_count(self, name):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM dead WHERE name=?", (name,)).fetchone()[0]


class Runner:
    """Читатель журнала одного получателя: process_once — один проход, run — поток."""

    def __init__(self, journal, recipient, state, clock=time.time):
        self.j, self.r, self.st, self.clock = journal, recipient, state, clock
        self.last_error = ""
        self.delivered = 0
        self._cursor()

    def _cursor(self):
        c = self.st.get(self.r.name)
        if c:
            return c[0], c[1], c[2], c[3]
        files = self.j.files()
        if not files:
            self.st.put(self.r.name, "", 0)
            return "", 0, 0, 0
        n = len(self.j.read_from(files[-1], 0, 10 ** 9))
        self.st.put(self.r.name, files[-1], n)
        return files[-1], n, 0, 0

    def lag(self):
        """Строк журнала впереди курсора."""
        f, line, _, _ = self._cursor()
        return sum(len(self.j.read_from(name, line if name == f else 0, 10 ** 9)) for name in self.j.files() if name >= f)

    def process_once(self, limit=100):
        if self.r.mode == "off":
            return 0
        f, line, attempts, next_try = self._cursor()
        now = self.clock()
        if next_try and now < next_try:
            return 0
        done = 0
        while done < limit:
            rows = self.j.read_from(f, line, 1)
            if not rows:
                nxt = [x for x in self.j.files() if x > f]
                if not nxt:
                    break
                f, line, attempts = nxt[0], 0, 0
                self.st.put(self.r.name, f, line)
                continue
            no, rec = rows[0]
            if rec is None:
                self.st.dead(self.r.name, f, no, "", "битая строка журнала", now)
            elif self.r.accepts(rec):
                payload = self.r.build(rec, self.j.frame_bytes(rec))
                if payload is None:
                    self.st.dead(self.r.name, f, no, rec.get("event_id", ""), "нет кадра в журнале", now)
                    self.j.append_delivery(rec, "dead: нет кадра", None)
                else:
                    payload["_event_id"] = rec.get("event_id", "")
                    res = self._deliver(payload, rec)
                    if res.kind == "retry":
                        attempts += 1
                        self.last_error = res.reason
                        if attempts >= MAX_ATTEMPTS:
                            self.st.dead(self.r.name, f, no, rec.get("event_id", ""), "лимит попыток: " + res.reason, now)
                            self.j.append_delivery(rec, "dead: лимит попыток", None)
                            attempts = 0
                        else:
                            self.st.put(self.r.name, f, no, attempts, now + min(300, 5 * 2 ** min(attempts, 6)), res.reason)
                            return done
                    else:
                        if res.kind == "drop":
                            self.last_error = res.reason
                            if res.outcome != "dedup":
                                self.st.dead(self.r.name, f, no, rec.get("event_id", ""), res.reason, now)
                        else:
                            self.delivered += 1
                        self.j.append_delivery(rec, res.outcome, res.dolibarr)
            line, attempts = no + 1, 0
            self.st.put(self.r.name, f, line)
            done += 1
        return done

    def _deliver(self, payload, rec):
        if self.r.mode == "shadow":
            d = os.path.join(self.j.dir, "..", "recipients")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s.shadow.jsonl" % self.r.name), "a", encoding="utf-8") as fh:
                body = {k: (v if k != "photocontent" else "<%d b64>" % len(v)) for k, v in payload.items() if not k.startswith("_")}
                fh.write(json.dumps({"event_id": rec.get("event_id"), "payload": body}, ensure_ascii=True) + "\n")
            return Result.ok()
        return self.r.deliver(payload)

    def run(self, stop, idle=1.0):
        while not stop.is_set():
            try:
                n = self.process_once()
            except Exception as e:  # noqa: BLE001
                self.last_error = "%s: %s" % (type(e).__name__, e)
                n = 0
            if not n:
                stop.wait(idle)
