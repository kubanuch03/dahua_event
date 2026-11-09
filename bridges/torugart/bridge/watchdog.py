"""Watchdog подписки SDK: ступени переподписка, перелогин, выход по тишине выше порога часа суток."""
import datetime as dt
import json
import os

DEFAULT_T = 1800


def event_times(journal, days=7, now=None):
    """UTC-метки событий журнала за последние days суток."""
    now = now or journal.clock()
    cut = (now - dt.timedelta(days=days)).strftime("%Y%m%d")
    out = []
    for fn in journal.files():
        if fn[:8] < cut:
            continue
        for _, r in journal.read_from(fn, 0, 10 ** 9):
            if r and r.get("type", "event") == "event" and r.get("ts"):
                try:
                    t = dt.datetime.fromisoformat(r["ts"])
                except ValueError:
                    continue
                out.append(t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))
    out.sort()
    return out


def gap_threshold(times, hour_utc, mult, lo, hi, default=DEFAULT_T):
    """Порог тишины для часа суток: mult × p95 интервалов, зажатый в [lo, hi]."""
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if a.hour == hour_utc and (b - a).total_seconds() < 6 * 3600]
    if len(gaps) < 10:
        return max(lo, min(hi, default))
    gaps.sort()
    p95 = gaps[int(len(gaps) * 0.95) - 1]
    return max(lo, min(hi, mult * p95))


class Watchdog:
    def __init__(self, journal, clock, mult=10.0, lo=900, hi=7200, enabled=True, started=None):
        self.j, self.clock, self.mult, self.lo, self.hi, self.enabled = journal, clock, mult, lo, hi, enabled
        self.anchor = started or clock()          # последнее событие/действие/старт
        self.stage = 0
        self._cache = (None, None)
        self.last_action = None
        self.actions = 0

    def note_event(self, now=None):
        self.anchor, self.stage = (now or self.clock()), 0

    def threshold(self, now):
        hour = now.astimezone(dt.timezone.utc).hour
        key = (now.strftime("%Y%m%d"), hour)
        if self._cache[0] != key:
            self._cache = (key, gap_threshold(event_times(self.j, now=now), hour, self.mult, self.lo, self.hi))
        return self._cache[1]

    def tick(self, connected=True):
        """→ None | 'resubscribe' | 'relogin' | 'exit'."""
        if not self.enabled:
            return None
        now = self.clock()
        if not connected:
            self.anchor = now
            return None
        t = self.threshold(now)
        silent = (now - self.anchor).total_seconds()
        if self.stage == 0 and silent >= t:
            act = "resubscribe"
        elif self.stage == 1 and silent >= 2 * t:
            act = "relogin"
        elif self.stage == 2 and silent >= 4 * t:
            act = "exit"
        else:
            return None
        self.stage += 1
        self.last_action, self.actions = act, self.actions + 1
        return act
