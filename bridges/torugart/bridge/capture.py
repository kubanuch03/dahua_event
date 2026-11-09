"""Перехват решения выбора номера (строки av_*, av_cc, итог второго номера) и ответа читалки в потоке события."""
import json
import logging
import threading

_tls = threading.local()
_MODEL_KEYS = ("plate", "plate_full", "status", "confidence", "country", "country_conf", "kg_type", "main_version",
               "version", "country_version", "cn_province", "cn_province_conf", "attrs", "region_conf")
_sinks = []


def begin():
    """Начало события: сбросить пойманное от предыдущего."""
    _tls.av, _tls.extra, _tls.model = None, [], None


def current():
    """→ (av, av_extra, model) текущего события."""
    return getattr(_tls, "av", None), getattr(_tls, "extra", None) or None, getattr(_tls, "model", None)


def on_av2(fn):
    """Подписка на итоги второго номера: fn(dict)."""
    _sinks.append(fn)


def _jsonable(v):
    return json.loads(json.dumps(v, ensure_ascii=False, default=str))


def model_short(m):
    """Ответ читалки без тяжёлого: ключи верхнего уровня и до 6 рамок."""
    if not isinstance(m, dict):
        return None
    out = {k: m[k] for k in _MODEL_KEYS if k in m}
    boxes = m.get("boxes")
    if isinstance(boxes, list):
        out["boxes"] = [{k: b.get(k) for k in ("text", "conf", "country", "country_conf", "main", "bbox")} for b in boxes[:6] if isinstance(b, dict)]
    return out


class _Handler(logging.Handler):
    def emit(self, record):
        msg = record.msg
        if not isinstance(msg, dict):
            return
        if msg.get("av2") == "final":
            for fn in list(_sinks):
                try:
                    fn(_jsonable(msg))
                except Exception:  # noqa: BLE001
                    pass
        elif "av_src" in msg or "av_mode" in msg:
            _tls.av = dict(msg)
        elif "av_cc" in msg:
            if getattr(_tls, "extra", None) is None:
                _tls.extra = []
            _tls.extra.append(dict(msg))


def install(autovision_module):
    """Обработчик на логгер AUTOVISION и обёртка ask_model, запоминающая ответ читалки."""
    lg = logging.getLogger("AUTOVISION")
    if not any(isinstance(h, _Handler) for h in lg.handlers):
        lg.addHandler(_Handler(level=logging.INFO))
    if not getattr(autovision_module.ask_model, "_captured", False):
        orig = autovision_module.ask_model

        def ask_model(*args, **kw):
            r = orig(*args, **kw)
            _tls.model = r
            return r

        ask_model._captured = True
        autovision_module.ask_model = ask_model
