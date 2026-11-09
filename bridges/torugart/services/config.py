"""Настройки моста: умолчания, необязательный файл /app/config/bridge.json, env (сильнее файла)."""
import json
import os

CONFIG_FILE = "/app/config/bridge.json"


class ConfigError(RuntimeError):
    pass


def _req(env, name):
    v = (env.get(name) or "").strip()
    if not v:
        raise ConfigError(f"не задана переменная {name}")
    return v


def _flag(env, name):
    return (env.get(name) or "0").strip() == "1"


class Config:
    def __init__(self, env=None, file_path=CONFIG_FILE):
        env = dict(os.environ if env is None else env)
        try:
            with open(file_path, encoding="utf-8") as f:
                layered = {k: str(v) for k, v in json.load(f).items() if not str(k).startswith("_")}
        except (OSError, ValueError, AttributeError):
            layered = {}
        env = {**layered, **env}
        self.dolibarr_url = _req(env, "DOLIBARR_URL").rstrip("/")
        self.dolibarr_key = _req(env, "DOLIBARR_API_KEY")
        self.camera_code = _req(env, "BRIDGE_CAMERA_CODE")
        self.camera_ip = _req(env, "BRIDGE_CAMERA_IP")
        self.camera_port = int(env.get("BRIDGE_CAMERA_PORT") or 37777)
        self.camera_user = env.get("BRIDGE_CAMERA_USER") or "admin"
        self.camera_password = _req(env, "BRIDGE_CAMERA_PASSWORD")
        # Dolibarr отдаёт логин/пароль камеры в списке камер: брать их оттуда, а не из env.
        self.creds_from_dolibarr = _flag(env, "BRIDGE_CREDS_FROM_DOLIBARR")
        # Пропускать камеру, если в записи Dolibarr нет полей подключения.
        self.require_dolibarr_fields = _flag(env, "BRIDGE_REQUIRE_DOLIBARR_FIELDS")
        self.site = env.get("BRIDGE_SITE") or "torugart"
        self.data_dir = env.get("BRIDGE_DATA_DIR") or "/app/data"
        self.recipients = [x.strip() for x in (env.get("BRIDGE_RECIPIENTS") or "dolibarr").split(",") if x.strip()]
        self.dolibarr_mode = env.get("BRIDGE_DOLIBARR_MODE") or "on"
        self.health_port = int(env.get("BRIDGE_HEALTH_PORT") or 8099)
        self.journal_keep_days = int(env.get("BRIDGE_JOURNAL_KEEP_DAYS") or 14)
        self.wd_enabled = (env.get("WATCHDOG_ENABLED") or "1") == "1"
        self.wd_min_s = int(env.get("WATCHDOG_MIN_SILENCE_S") or 900)
        self.wd_max_s = int(env.get("WATCHDOG_MAX_SILENCE_S") or 7200)
        self.wd_mult = float(env.get("WATCHDOG_MULT") or 10)

    def describe(self):
        """Настройки без секретов."""
        return {"dolibarr_url": self.dolibarr_url, "dolibarr_key": "задан", "camera_code": self.camera_code, "camera_ip": self.camera_ip,
                "camera_port": self.camera_port, "camera_user": self.camera_user, "camera_password": "задан",
                "creds_from_dolibarr": self.creds_from_dolibarr, "require_dolibarr_fields": self.require_dolibarr_fields, "site": self.site,
                "data_dir": self.data_dir, "recipients": self.recipients, "dolibarr_mode": self.dolibarr_mode, "health_port": self.health_port,
                "watchdog": {"enabled": self.wd_enabled, "min_s": self.wd_min_s, "max_s": self.wd_max_s, "mult": self.wd_mult},
                "journal_keep_days": self.journal_keep_days}


_CFG = None


def get():
    """Настройки процесса (читаются при первом обращении)."""
    global _CFG
    if _CFG is None:
        _CFG = Config()
    return _CFG
