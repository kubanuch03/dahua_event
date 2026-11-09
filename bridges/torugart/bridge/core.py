"""Сборка ядра одной камеры: журнал, получатели, обработчик события, /health, watchdog."""
import os
import threading

from bridge import capture
from bridge.health import Health
from bridge.journal import Journal
from bridge.pipeline import Pipeline
from bridge.recipients import DolibarrRecipient, Runner, State
from bridge.watchdog import Watchdog
from logger import get_logger

logger = get_logger("BRIDGE")


class Core:
    def __init__(self, cfg, camera_code, camera_id, detector, av_mode, run_id, av_module, clock=None, journal_clock=None):
        self.cfg = cfg
        kw = {"clock": journal_clock} if journal_clock else {}
        self.journal = Journal(cfg.data_dir, cfg.site, camera_code, camera_id, run_id, keep_days=cfg.journal_keep_days, **kw)
        capture.install(av_module)
        capture.on_av2(self.journal.append_av2)
        self.state = State(os.path.join(cfg.data_dir, "state", "recipients.sqlite"))
        self.runners = []
        for name in cfg.recipients:
            if name == "dolibarr":
                self.runners.append(Runner(self.journal, DolibarrRecipient(cfg, mode=cfg.dolibarr_mode, data_dir=cfg.data_dir), self.state))
            else:
                logger.warning(f"неизвестный получатель {name!r}")
        self.pipeline = Pipeline(detector, self.journal, av_mode, camera_code, camera_id)
        wd_clock = clock or self.journal.clock
        self.watchdog = Watchdog(self.journal, wd_clock, mult=cfg.wd_mult, lo=cfg.wd_min_s, hi=cfg.wd_max_s, enabled=cfg.wd_enabled)
        self.health = Health(camera_code, run_id, self.journal, self.runners, self.watchdog, cfg.describe(), clock=clock)
        self.stop = threading.Event()

    def start(self):
        """Запускает читателей получателей, уборку журнала и /health."""
        for r in self.runners:
            threading.Thread(target=r.run, args=(self.stop,), daemon=True, name="recipient-" + r.r.name).start()
        threading.Thread(target=self._housekeeping, daemon=True, name="housekeeping").start()
        if self.cfg.health_port:
            self.health.serve(self.cfg.health_port)
        logger.info({"bridge_core": "started", "recipients": [r.r.name for r in self.runners], "health_port": self.cfg.health_port})

    def _housekeeping(self):
        while not self.stop.wait(3600):
            try:
                self.journal.purge()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"уборка журнала: {e}")
