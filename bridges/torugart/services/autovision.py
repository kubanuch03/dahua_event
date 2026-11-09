from logger import get_logger
import os
import requests
import base64
import re

logger = get_logger("AUTOVISION")

# plate already has KG inserted by traffic_info, e.g. "01KG123ABC"
KG_PLATE_RE = re.compile(r"^(0[1-9]|10)KG\d{3}[A-Z]{3}$")

# AutoVision (20.10, решение владельца): в Dolibarr уходит лучший номер — камеры или модели.
# Модель — микросервис av-plate-reader в той же docker-сети (ставится из центра AutoVision, мост его не содержит).
# Режим — файл /app/data/av_mode: off | shadow (по умолчанию: в Dolibarr номер камеры, решение только в журнал) | best.
AV_URL = os.environ.get("AV_READER_URL", "http://av-plate-reader:8090/v1/read")
AV_TIMEOUT = float(os.environ.get("AV_READER_TIMEOUT", "2"))
AV_MODE_FILE = "/app/data/av_mode"
AV_MIN_CONF = float(os.environ.get("AV_MIN_CONF", "0.931"))
AV_SURE_CONF = float(os.environ.get("AV_SURE_CONF", "0.9955"))
AV_REGION_CONF = float(os.environ.get("AV_REGION_CONF", "0.990"))
# Страны, для которых номер модели может заменить номер камеры (владелец 20.10: «только для KG, UZ, если уверена»)
AV_COUNTRIES = {c.strip() for c in os.environ.get("AV_COUNTRIES", "KG,UZ").split(",") if c.strip()}
_KG_TAIL = re.compile(r"^\d{3}[A-Z]{2,3}$"); _KG_FULL = re.compile(r"^(0[1-9]|1[01])\d{3}[A-Z]{2,3}$")
_KG_NEW = re.compile(r"^(0[1-9]|10)(\d{3}[A-Z]{3})$")      # как traffic_info._add_kg_region
_FMT = [re.compile(rf"^{x}$") for x in (
    r"(0[1-9]|1[01])KG\d{3}[A-Z]{2,3}", r"(0[1-9]|1[01])\d{3}[A-Z]{2,3}", r"(0[1-9]|1[01])\d{4}[A-Z]", r"[A-Z]\d{4}[A-Z]{1,2}",
    r"(D|T|C|CMD)\d{5}", r"KG[A-Z]{2}\d{4}", r"\d{4}[A-Z]{2}",                       # KG
    r"\d{2}[A-Z]\d{3}[A-Z]{2}", r"\d{5}[A-Z]{3}", r"\d{2}[A-Z]\d{6}",                # UZ
    r"\d{3}[A-Z]{2,3}\d{2}", r"[A-Z]\d{3}[A-Z]{2}\d{2,3}",                            # KZ, RU
    r"[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5}")]                                                  # CN хвост


def av_mode():
    try:
        m = open(AV_MODE_FILE).read().strip()
    except OSError:
        return "shadow"
    return m if m in ("off", "shadow", "best") else "shadow"


def _tail(p):  # CN: без иероглифа; I→1, O→0 (в номерах КНР нет I/O, камера их путает)
    p = (p or "").strip().upper()
    if p and not p[0].isascii():
        p = p[1:].replace("I", "1").replace("O", "0")
    return p


def _norm_cam(p):
    return (p or "").replace("KG", "", 1) if KG_PLATE_RE.match(p or "") else (p or "")


def _to_erp(plate):
    """Номер модели → формат, который ERP получает от камеры: KG 01123ABC → 01KG123ABC, CN — без иероглифа."""
    t = _tail(plate)
    return _KG_NEW.sub(r"\1KG\2", t)


def _lcs(a, b):
    """Длина наибольшей общей подпоследовательности."""
    d = [0] * (len(b) + 1)
    for x in a:
        prev = 0
        for j, y in enumerate(b, 1):
            cur = d[j]; d[j] = prev + 1 if x == y else max(d[j], d[j - 1]); prev = cur
    return d[-1]


# валидность номера камеры судим по СТРАНЕ, которую определила модель страны (98 %): 75105ABC «валиден» как UZ, но это KG-фура
_CAM_VALID = {"KG": re.compile(r"^(0[1-9]|1[01])\d{3}[A-Z]{2,3}$"),
              "UZ": re.compile(r"^((01|10|20|25|30|40|50|60|70|75|80|85|90|95)\d{3}[A-Z]{3}|\d{2}[A-Z]\d{3}[A-Z]{2}|\d{2}[A-Z]\d{6})$")}


def _region_conf(model):
    """Уверенность слотов региона (1–2) у основного чтения; None — сервис объекта не отдаёт (старый plate_reader)."""
    for r in (model.get("reads") or []):
        if r.get("version") == model.get("main_version") and r.get("region_conf") is not None:
            return float(r["region_conf"])
    return None


def _ok_format(p):
    return any(rx.match(p or "") for rx in _FMT)


def _hdrs(camera_plate):
    # номер камеры - читалке для выбора рамки при 2+ номерах в кадре (T-0035); только ASCII, до 32 знаков
    h = {"Content-Type": "image/jpeg"}
    cp = "".join(ch for ch in (camera_plate or "") if ch.isascii() and ch.isalnum())[:32]
    if cp:
        h["X-Camera-Plate"] = cp
    return h


def ask_model_bytes(data, camera_plate=""):
    try:
        r = requests.post(AV_URL, data=data, headers=_hdrs(camera_plate), timeout=AV_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AV roi: модель недоступна: {e}")
        return None


def roi_read(jpeg_path, cam_box):
    """Q-033: вырезка по рамке камеры с полями -> читалка (второе чтение)."""
    import io
    from PIL import Image
    im = Image.open(jpeg_path).convert("RGB")
    c = im.crop(_av_nm.roi_rect(cam_box, im.size))
    if c.width < _av_nm.ROI_MIN_W:
        c = c.resize((_av_nm.ROI_MIN_W, int(c.height * _av_nm.ROI_MIN_W / c.width)), Image.BICUBIC)
    buf = io.BytesIO(); c.save(buf, "JPEG", quality=90)
    return ask_model_bytes(buf.getvalue())


def ask_model(jpeg_path, camera_plate=""):
    try:
        with open(jpeg_path, "rb") as f:
            r = requests.post(AV_URL, data=f.read(), headers=_hdrs(camera_plate), timeout=AV_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 — модель недоступна: работаем как раньше, по камере
        logger.warning(f"AV: модель недоступна: {e}")
        return None


def choose(camera_plate, model):
    """(номер для ERP, источник, причина). Владелец 20.10: модель берём только для AV_COUNTRIES (KG, UZ) и только «уверенно»;
    всё остальное (CN, KZ… и сомнительное) — номер камеры, как раньше."""
    cam = camera_plate or ""
    if not model or not model.get("plate"):
        return cam, "camera", "модель не ответила"
    mp = _to_erp(model.get("plate_full") or model["plate"])
    status, country = model.get("status"), model.get("country")
    cam_cmp = _tail(_norm_cam(cam)).replace("I", "1") if cam else ""
    if cam and cam_cmp == _norm_cam(mp).replace("I", "1"):
        return cam, "agree", "камера = модель"
    if country not in AV_COUNTRIES:
        return cam, "camera", f"страна {country}: модель только для {','.join(sorted(AV_COUNTRIES))}"
    if status != "confident":
        return cam, "camera", f"{country}, модель {status}"
    # 20.10: камера дала KG без региона, модель его дописала — на стёртом регионе модель уверенно выдумывает (98–100 %);
    # выдуманный регион в ERP хуже отсутствующего → номер камеры.
    mp_cmp = _norm_cam(mp).replace("I", "1")
    if country == "KG" and _KG_TAIL.match(cam_cmp) and _KG_FULL.match(mp_cmp) and mp_cmp.endswith(cam_cmp):
        # 20.10 арбитр (69 случаев «камера = хвост без региона»): регион на кадре виден в 35 (модель права в 29), скрыт в 34 (камера честнее).
        # Уверенность слотов региона region_conf ≥ 0,999 пропускает 22 из 35 видимых и 4 из 34 скрытых. Пока сервис объекта не отдаёт region_conf — камера.
        rc = _region_conf(model)
        if rc is None or rc < AV_REGION_CONF:
            return cam, "camera", "KG, камера без региона, модель дописала регион" + ("" if rc is None else f" (регион неуверенно {rc})")
    # 20.10 22:40 замена Lane4: камера 01723ABCD (лишняя буква) → модель 01728ABC при уверенности 0,9458; на номере 123 (тот же грузовик на въезде читался 01123ABC) —
    # модель на пересвеченном номере ошиблась. Верные замены за вечер шли при уверенности ≥ 0,9954. Порог замены — 0,99.
    if float(model.get("confidence") or 0) < AV_MIN_CONF:
        return cam, "camera", f"{country}, уверенность модели {float(model.get('confidence') or 0):.3f} < {AV_MIN_CONF} — для замены номера камеры мало"
    # 20.10 21:20 ночь, первые замены в best (по кадрам): 01KG123ABC → модель 723ABC — МОДЕЛЬ НЕ ПРАВА (на номере 123, уверенность 0,919);
    # 75105ABC → 05751ABC — права модель (на номере 05 751 ABC, камера перепутала порядок). Если оба номера «валидны», кто прав — неизвестно,
    # поэтому номер камеры подменяется ТОЛЬКО когда он сам невалиден (регион вне 01–11, нет формата KG/UZ) или камера номера не дала.
    # 20.10 ночь, арбитр Codex по 656 расхождениям камера ≠ модель (Торугарт, KG): при уверенности модели ≥ 0,9999 (376 случаев) модель права в 357,
    # камера одна — ни разу; при невалидной камере и ≥ 0,99 (397) исправлено 371, испорчено 0. При валидной камере и уверенности < 0,9999 —
    # камера (там модель права реже всего: 0,99–0,9999 — 5 испорчено на 38 исправленных).
    if cam_cmp and _CAM_VALID[country].match(cam_cmp) and float(model.get("confidence") or 0) < AV_SURE_CONF:
        return cam, "camera", f"{country}, номер камеры валиден, уверенность модели < {AV_SURE_CONF} — различия не решаем"
    # 20.10 Торугарт Lane4: две фуры в кадре, камера прочла правую (4512345A), модель уверенно — левую (01678ABC).
    # Модель подменяет номер камеры, только если это похоже на ТУ ЖЕ машину: общая подпоследовательность ≥ половины номера
    # (62208ABC ↔ 08622ABC: 6/8 — та же; 4512345A ↔ 01678ABC — другая). Камера номера не дала — сравнивать не с чем.
    if cam_cmp and _lcs(cam_cmp, _norm_cam(mp).replace("I", "1")) < 0.5 * max(len(cam_cmp), len(mp)):
        return cam, "camera", f"{country}, модель уверенно, но номер другой машины"
    return mp, "model", f"{country}, модель уверенно" + ("" if cam else ", камера номер не дала")


# --- AV второй номер в кадре (22.10, «2 полосы»): av_second = off | shadow | on, по умолчанию off ---
# Второй номер кадра (рамка, не совпавшая с камерой) - кандидат, если: формат KG/UZ, страна рамки в AV_COUNTRIES, уверенность >= AV2_MIN_CONF,
# не равен номеру камеры/выбранному. Решение кандидата через AV2_WINDOW с: дубль, если за это время (и до него) на ЭТОМ мосте был похожий номер
# (стоящая рядом машина имеет своё событие: замер 22.10 - 77 %). Соседние мосты мост не видит. shadow: ERP не пишется, только журнал.
# Журнал -> центр: ключи av_second* идут в ОСНОВНУЮ строку av_ (центр привязывает её к событию позиционно); итог через 300 с - отдельная строка
# {'av2': 'final', 'av2_frame': 'Global_ImgN.jpg', 'av2_status': duplicate|would_send|sent, ...} (центр сопоставляет по камере и N кадра).
# Тревога: больше AV2_ALARM_PER_HOUR итогов would_send за час -> av_second сам переключается в off.
import threading, time, collections
from difflib import SequenceMatcher
AV2_FILE = "/app/data/av_second"
AV2_MIN_CONF = float(os.environ.get("AV2_MIN_CONF", "0.931"))
AV2_WINDOW = float(os.environ.get("AV2_WINDOW", "300"))
AV2_ALARM_PER_HOUR = int(os.environ.get("AV2_ALARM_PER_HOUR", "10"))
AV2_DEDUP = float(os.environ.get("AV2_DEDUP", "3600"))
_AV2_LOCK = threading.Lock()
_AV2_RECENT = collections.deque()   # (ts, ключ номера): камера и выбранный номер событий моста
_AV2_SENT = collections.deque()     # ts итогов would_send
_AV2_DONE = collections.deque()     # (ts, ключ) уже отправленных/«отправил бы» вторых номеров этого моста (дедупликация, AV2_DEDUP с)
_AV2_FMT = [re.compile(rf"^{x}$") for x in (
    r"(0[1-9]|1[01])\d{3}[A-Z]{2,3}", r"(0[1-9]|1[01])\d{4}[A-Z]", r"[A-Z]\d{4}[A-Z]{1,2}", r"(D|T|C|CMD)\d{5}", r"KG[A-Z]{2}\d{4}",  # KG
    r"\d{2}[A-Z]\d{3}[A-Z]{2}", r"\d{5}[A-Z]{3}", r"\d{2}[A-Z]\d{6}")]                                                            # UZ


def av_second():
    try:
        m = open(AV2_FILE).read().strip()
    except OSError:
        return "off"
    return m if m in ("off", "shadow", "on") else "off"


def _av2_key(p):
    return "".join(c for c in (p or "") if c.isascii() and c.isalnum()).upper().replace("I", "1").replace("O", "0").replace("KG", "", 1)


def _av2_same(a, b):
    return bool(a) and bool(b) and SequenceMatcher(None, a, b).ratio() >= 0.85


def _av2_note(*plates):
    now = time.time()
    with _AV2_LOCK:
        for p in plates:
            k = _av2_key(p)
            if k:
                _AV2_RECENT.append((now, k))
        while _AV2_RECENT and now - _AV2_RECENT[0][0] > 2 * AV2_WINDOW:
            _AV2_RECENT.popleft()


def _av2_final(cand):
    try:
        ts, key = cand["ts"], cand["key"]
        with _AV2_LOCK:
            dup = any(abs(t - ts) <= AV2_WINDOW and _av2_same(k, key) for t, k in _AV2_RECENT)
        mode = av_second()
        out = {"av2": "final", "av2_frame": cand["frame"], "av2_plate": cand["plate"], "av2_mode": mode}
        if dup:
            out.update(av2_status="duplicate", av2_reason="номер есть на этом мосте ±%d с" % AV2_WINDOW)
            logger.info(out); return
        now = time.time()
        with _AV2_LOCK:
            while _AV2_DONE and now - _AV2_DONE[0][0] > AV2_DEDUP:
                _AV2_DONE.popleft()
            again = any(_av2_same(k, key) for _, k in _AV2_DONE)
            if not again:
                _AV2_DONE.append((now, key))
        if again:
            out.update(av2_status="duplicate", av2_reason="уже отправлен на этом мосте ≤%d мин" % (AV2_DEDUP // 60))
            logger.info(out); return
        with _AV2_LOCK:
            _AV2_SENT.append(now)
            while _AV2_SENT and now - _AV2_SENT[0] > 3600:
                _AV2_SENT.popleft()
            over = len(_AV2_SENT) > AV2_ALARM_PER_HOUR
        if over:
            try:
                open(AV2_FILE, "w").write("off")
            except OSError:
                pass
            logger.warning(f"AV2 СТОП: больше {AV2_ALARM_PER_HOUR} вторых номеров за час, av_second -> off")
            return
        out.update(av2_status="would_send" if mode != "on" else "sent", av2_reason="нет события с этим номером на мосте",
                   av2_conf=cand["conf"], av2_country=cand["country"], av2_camera=cand["camera"], av2_main=cand["main"])
        logger.info(out)
        # mode == "on": запись в ERP (формат обычного события + пометка «2-й номер») добавляется отдельным шагом, после shadow 24 ч.
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AV2: {e}")


# --- model_first (26.10, владелец: «модель основной ответ, камера запасной»). Режим /app/data/av_mode = model_first, порог AV_MF_T (по умолчанию 0.931) ---
AV_MF_T = float(os.environ.get("AV_MF_T", "0.85"))
AV_MF_LCS = float(os.environ.get("AV_MF_LCS", "0.7"))      # «другая машина»: общая подпоследовательность < AV_MF_LCS·длина
AV_MF_SURE = float(os.environ.get("AV_MF_SURE", "0.9955"))  # валидный номер камеры заменяем моделью только при conf >= AV_MF_SURE
AV_MF_FILE = "/app/data/av_mf"
AV_MF_DOUBTFUL = os.environ.get("AV_MF_DOUBTFUL", "0") == "1"   # брать ли статус doubtful при conf >= T (по умолчанию нет)


AV_NM_FILE = "/app/data/av_nm"
try:
    from services import av_nm as _av_nm
except Exception:  # noqa: BLE001
    try:
        import av_nm as _av_nm
    except Exception:  # noqa: BLE001
        _av_nm = None


AV_CC_FILE = "/app/data/av_cc"
AV_SC_FILE = "/app/data/av_sc"


def _flag_on(path):
    try:
        return open(path).read().strip() == "on"
    except OSError:
        return False


def _nm_on():
    try:
        return _av_nm is not None and open(AV_NM_FILE).read().strip() == "on"
    except OSError:
        return False


def av_mf_mode():
    """/app/data/av_mf: off | on (по умолчанию off). Действует только при av_mode=best; откат: echo off > /app/data/av_mf (без рестарта)."""
    try:
        return "on" if open(AV_MF_FILE).read().strip() == "on" else "off"
    except OSError:
        return "off"


def choose_mf(camera_plate, model):
    """(номер для ERP, источник, причина). KG/UZ модели при conf >= AV_MF_T и статусе confident - основной ответ; иначе номер камеры.
    Камера остаётся: модель молчит, no_plate/unknown_format, формат не KG/UZ, номер другой машины (LCS<0.5), регион дописан неуверенно."""
    cam = camera_plate or ""
    if not model or not model.get("plate"):
        return cam, "camera", "модель не ответила"
    mp = _to_erp(model.get("plate_full") or model["plate"])
    status, country = model.get("status"), model.get("country")
    conf = float(model.get("confidence") or 0)
    cam_cmp = _tail(_norm_cam(cam)).replace("I", "1") if cam else ""
    mp_cmp = _norm_cam(mp).replace("I", "1")
    if cam and cam_cmp == mp_cmp:
        return cam, "agree", "камера = модель"
    if status in ("no_plate", "unknown_format") or country not in AV_COUNTRIES:
        return cam, "camera", f"модель {status}/{country}"
    if status != "confident" and not AV_MF_DOUBTFUL:
        return cam, "camera", f"{country}, модель {status}"
    if conf < AV_MF_T:
        return cam, "camera", f"{country}, уверенность {conf:.3f} < {AV_MF_T}"
    if not _ok_format(mp):
        return cam, "camera", "формат номера модели не KG/UZ"
    if country == "KG" and _KG_TAIL.match(cam_cmp) and _KG_FULL.match(mp_cmp) and mp_cmp.endswith(cam_cmp):
        rc = _region_conf(model)
        if rc is None or rc < AV_REGION_CONF:
            return cam, "camera", "KG, камера без региона, регион модели неуверенно"
    if cam_cmp and _CAM_VALID[country].match(cam_cmp) and conf < AV_MF_SURE:
        return cam, "camera", f"{country}, номер камеры валиден, уверенность модели < {AV_MF_SURE}"
    if cam_cmp and _lcs(cam_cmp, mp_cmp) < AV_MF_LCS * max(len(cam_cmp), len(mp)):   # как в choose(): len(mp) с вставкой KG
        return cam, "camera", f"{country}, номер другой машины"
    return mp, "model", f"{country}, модель основной ответ"


# --- Полосы (25.10, сценарии 2 номеров): зона своей полосы по x-диапазону, файл /app/data/lane_zone = {"x":[x1,x2]} (центр рамки номера), нет файла = зона неизвестна ---
AV_LANE_FILE = "/app/data/lane_zone"
AV2_EDGE = 12   # рамка ближе к краю кадра (px) - номер обрезан, в ERP не идёт


def _lane_x():
    try:
        import json as _j
        x = _j.load(open(AV_LANE_FILE)).get("x")
        return (float(x[0]), float(x[1])) if x and len(x) == 2 else None
    except Exception:  # noqa: BLE001
        return None


def _box_zone(b):
    """'in' | 'out' | 'na' - центр рамки по x относительно зоны своей полосы."""
    z = _lane_x(); bb = b.get("bbox")
    if not z or not bb or len(bb) < 4:
        return "na"
    return "in" if z[0] <= (bb[0] + bb[2]) / 2 <= z[1] else "out"


def _img_wh(path):
    try:
        with open(path, "rb") as f:
            d = f.read(65536)
        i = 2
        while i + 9 < len(d):
            if d[i] != 0xFF:
                i += 1; continue
            m = d[i + 1]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                return (d[i + 7] << 8) | d[i + 8], (d[i + 5] << 8) | d[i + 6]
            i += 2 + ((d[i + 2] << 8) | d[i + 3])
    except Exception:  # noqa: BLE001
        pass
    return None


def lane_guard(camera_plate, model, plate, src, why):
    """После choose(): замена камеры моделью допустима, если главная рамка совпала с камерой, либо (камера мусор и рамка в своей полосе).
    Рамка вне зоны своей полосы и без совпадения с камерой -> номер камеры, статус doubtful. -> (plate, src, why, 'in|out|na|-')."""
    try:
        if src == "model" and not _ok_format(plate):   # 25.10: пустая камера + «уверенный» мусор модели ("ZZZZZ") раньше уходил в ERP
            return camera_plate or "", "camera", "формат номера модели не KG/UZ", "-"
        if src != "model" or not model:
            return plate, src, why, "-"
        mb = next((b for b in model.get("boxes") or [] if b.get("main")), None)
        if not mb:
            return plate, src, why, "-"
        z = _box_zone(mb)
        if mb.get("matches_camera") in (True, "full", "partial"):
            return plate, src, why, z
        if z == "out":
            return camera_plate or "", "camera", "doubtful: рамка вне зоны своей полосы и не совпала с камерой", z
        return plate, src, why, z
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AV lane: {e}")
        return plate, src, why, "-"

# --- CN: нормализация номера камеры (25.10, решение Main/submain 1: камера путает I/O с 1/0; в истине 627 CN-хвостов I/O нет) ---
# Страна CN от читалки (или иероглиф + хвост из 6 знаков): в позициях 1-5 хвоста I->1, O->0, позиция 0 (буква серии) не трогается; иероглиф в ERP не пишем.
_CN_TAIL = re.compile(r"^[A-Z][A-Z0-9]{5}$")


def cn_norm(camera_plate, model):
    """-> номер для ERP (хвост без иероглифа, исправленный) или None, если правило не применимо."""
    try:
        p = (camera_plate or "").strip().upper()
        had_hier = bool(p) and not p[0].isascii()
        tail = p[1:] if had_hier else p
        if not _CN_TAIL.match(tail):
            return None
        if not (had_hier or (model or {}).get("country") == "CN"):
            return None
        if (model or {}).get("country") in ("KG", "UZ"):
            return None
        return tail[0] + tail[1:].replace("I", "1").replace("O", "0")
    except Exception:  # noqa: BLE001
        return None


# --- v5 (25.10, Main): номер камеры короче AV_SHORT_LEN/пуст + модель confident -> номер модели для KG/UZ/CN. Режим /app/data/av_short: off | shadow | on (по умолчанию off) ---
AV_SHORT_FILE = "/app/data/av_short"
AV_SHORT_LEN = 4


def av_short_mode():
    try:
        m = open(AV_SHORT_FILE).read().strip()
    except OSError:
        return "off"
    return m if m in ("off", "shadow", "on") else "off"


def short_fill(camera_plate, model, src):
    """-> (номер для ERP | None, режим). Только когда choose() оставил камеру, а номер камеры короче AV_SHORT_LEN или пуст."""
    mode = av_short_mode()
    try:
        if mode == "off" or src != "camera" or not model:
            return None, mode
        cam = "".join(c for c in (camera_plate or "") if c.isalnum())
        if len(cam) >= AV_SHORT_LEN or model.get("status") != "confident":
            return None, mode
        country = model.get("country"); raw = model.get("plate_full") or model.get("plate")
        if not raw or float(model.get("confidence") or 0) < AV_MF_T:   # 26.10: confident, но не ниже порога T
            return None, mode
        if country in ("KG", "UZ"):
            p = _to_erp(raw)
            return (p if _ok_format(p) else None), mode
        if country == "CN":
            p = cn_norm(raw, model)
            return (p if p and _CN_TAIL.match(p) else None), mode
        return None, mode
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AV short: {e}")
        return None, mode



# AV-086 (03.11, владелец): поля Dolibarr «Номер распознан» (source) и «Распознаватель» (sourceinfo).
# source — кто дал ушедший номер: dahua (камера) / autovision (наша модель; при совпадении с камерой тоже).
# sourceinfo — версия модели и как выбрано. Флаг /app/data/av_source.
_SRC_INFO = {"agree": "совпало с камерой", "model": "модель вместо камеры", "cnm": "CN: модель основная",
             "roi": "модель по рамке камеры", "camera": "модель не уверена/не нашла", "none": "не распознан"}


def av_source_fields(src, model, sent_plate, camera_plate):
    mv = ((model or {}).get("main_version") or (model or {}).get("version") or "").split("+")[0]
    by_model = src in ("agree", "model", "cnm", "roi")
    if not by_model and sent_plate and camera_plate and sent_plate != camera_plate:
        by_model = True   # номер исправлен правилом (I→1/O→0 у CN, хвост модели) — уже не номер камеры как есть
    info = (mv or "модель") if by_model else "Dahua ITC"   # 03.11 владелец: в «Распознаватель» только модель, без «AutoVision»
    return {"source": "autovision" if by_model else "dahua", "sourceinfo": info[:120]}

def second_check(model, camera_plate, chosen_plate, jpeg_path):
    """→ dict ключей av_second* для основной строки av_. Никогда не бросает: основной поток важнее."""
    try:
        _av2_note(camera_plate, chosen_plate)
        mode = av_second()
        res = {"av_second_mode": mode}
        if mode == "off" or not model:
            return res
        mk, ck = _av2_key(chosen_plate), _av2_key(camera_plate)
        best = None   # (conf, текст, статус, причина) по самой уверенной не-основной рамке
        for b in model.get("boxes") or []:
            if b.get("main"):
                continue
            t = _tail(b.get("text") or "")
            if not t:
                continue
            conf = float(b.get("conf") or 0); k = _av2_key(t); zn = _box_zone(b)
            bb = b.get("bbox") or [0, 0, 0, 0]; wh = _img_wh(jpeg_path)
            edge = len(bb) >= 4 and (bb[0] < AV2_EDGE or bb[1] < AV2_EDGE or (wh and (bb[2] > wh[0] - AV2_EDGE or bb[3] > wh[1] - AV2_EDGE)))
            if zn == "in" and not (b.get("matches_camera") in (True, "full", "partial") or _av2_same(k, mk) or _av2_same(k, ck)):
                st, why = "skipped", "в зоне своей полосы (прицеп/та же машина)"
            elif edge:
                st, why = "skipped", "рамка у края кадра"
            elif b.get("matches_camera") in (True, "full", "partial") or _av2_same(k, mk) or _av2_same(k, ck):
                st, why = "skipped", "равен камере/основному"
            elif conf < AV2_MIN_CONF:
                st, why = "skipped", "conf<%s" % AV2_MIN_CONF
            elif b.get("country") not in AV_COUNTRIES:
                st, why = "skipped", "страна"
            elif not any(x.match(t.replace("I", "1")) or x.match(t) for x in _AV2_FMT):
                st, why = "skipped", "формат"
            else:
                st, why = "candidate", ""
                cand = {"ts": time.time(), "key": k, "plate": t, "conf": b.get("conf"), "country": b.get("country"), "camera": camera_plate,
                        "main": chosen_plate, "frame": os.path.basename(jpeg_path or "")}
                tm = threading.Timer(AV2_WINDOW, _av2_final, args=(cand,)); tm.daemon = True; tm.start()
            if best is None or st == "candidate" or conf > best[0]:
                best = (conf, t, st, why)
            if st == "candidate":
                break
        if best:
            res.update(av_second=best[1], av_second_status=best[2], av_second_reason=best[3])
        return res
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AV2: {e}")
        return {}
# --- /AV второй номер ---


AV_ATTR_CONF = float(os.environ.get("AV_ATTR_CONF", "0.9"))
# 03.11 (AV-099): марке свой порог = min_conf модели марки (0.6); при 0.9 brand-v0.4 почти всегда пуста (медиана 0.83)
AV_BRAND_CONF = float(os.environ.get("AV_BRAND_CONF", "0.6"))


def _fill_attrs(data, model):
    """Q-028: в Dolibarr марка шла как object_subType камеры (тип кузова), три точности были 0.
    Теперь: марка - модель при conf >= AV_ATTR_CONF (камера марку не отдаёт: szVehicleSign пуст), иначе пусто;
    цвет и кузов - модель при conf >= AV_ATTR_CONF (score = conf), иначе значения камеры, score 0 (камера точности не даёт).
    Флаг /app/data/av_attrs (по умолчанию выкл.), ошибка не должна ронять событие."""
    at = (model or {}).get("attrs") or {}
    def pick(name, thr=AV_ATTR_CONF):
        a = at.get(name) or {}
        v, c = a.get("value"), a.get("conf")
        return (v, round(float(c), 4)) if v and c is not None and float(c) >= thr else (None, None)
    v, c = pick("brand", AV_BRAND_CONF)
    data.update(carbrandcode=v or "", carbrandscore=str(c if c is not None else "0.00"))
    v, c = pick("color")
    if v:
        data.update(carcolorcode=v, carcolorscore=c)
    v, c = pick("body")
    if v:
        data.update(cartypebodycode=v, cartypebodyscore=c)


class StateNumberDetector:
    def detect_state_number(self, jpeg_path: str, camera_code, camera_id, a) -> dict:
        if not os.path.exists(jpeg_path):
            logger.warning(f"Image not found: {jpeg_path}")
            return {}

        try:
            data = self._prepare_data(jpeg_path, camera_code, camera_id, a)
            mode = av_mode()
            if mode == "off":
                return data
            model = ask_model(jpeg_path, a.get("plate_number_str", ""))
            plate, src, why = choose(a.get("plate_number_str", ""), model)
            mf = av_mf_mode() == "on" and mode == "best"
            nm = _nm_on() and mode == "best"
            nmb = None
            if nm:
                plate, src, why, nmb = _av_nm.choose_nm_box(a.get("plate_number_str", ""), model)
                lane = "-"
            else:
                if mf:
                    plate, src, why = choose_mf(a.get("plate_number_str", ""), model)
                plate, src, why, lane = lane_guard(a.get("plate_number_str", ""), model, plate, src, why)
            cnm = None
            if mode == "best" and _av_nm is not None and src != "model" and _flag_on("/app/data/av_cnm"):
                try:   # Q-036: для CN основной номер модели, камера - страховка
                    cnm = _av_nm.cn_primary(a.get("plate_number_str", ""), _av_nm.parse_box(((a.get("view") or {}).get("plate_box_abs"))), model)
                    if cnm:
                        plate, src, why = cnm[0], "cnm", cnm[1]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV cnm: {e}")
            roi = None
            if mode == "best" and _av_nm is not None and src not in ("model", "agree", "cnm") and _flag_on("/app/data/av_roi"):
                try:   # Q-033: вторая попытка по ROI рамки камеры, когда читалка не нашла эту рамку
                    cb = _av_nm.parse_box(((a.get("view") or {}).get("plate_box_abs")))
                    if cb and not _av_nm.box_found(cb, (model or {}).get("boxes")):
                        rm = roi_read(jpeg_path, cb)
                        pick = _av_nm.roi_pick(a.get("plate_number_str", ""), rm)
                        roi = {"box": cb, "read": (rm or {}).get("plate"), "conf": (rm or {}).get("confidence"), "status": (rm or {}).get("status"),
                               "pick": bool(pick)}
                        if pick:
                            plate, src, why = pick[0], "roi", pick[1]
                            roi.update(country=pick[2], conf_pick=pick[3])
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV roi: {e}")
            np_why = None
            if mode == "best" and _av_nm is not None and src not in ("roi", "cnm") and _flag_on("/app/data/av_np"):
                try:
                    np_why = _av_nm.np_drop(a.get("plate_number_str", ""), a.get("plate_conf"), model)
                except Exception:  # noqa: BLE001
                    np_why = None
                if np_why:
                    plate, src, why = "", "none", np_why
            cn = cn_norm(a.get("plate_number_str", ""), model) if mode == "best" and src != "model" else None
            sf, sf_mode = short_fill(a.get("plate_number_str", ""), model, src) if mode == "best" else (None, "off")
            # строка для AutoVision (live_pull): что сказала модель и что ушло бы/ушло в Dolibarr
            logger.info({"av_mode": mode, "av_camera": a.get("plate_number_str", ""),
                         "av_model": (model or {}).get("plate_full") or (model or {}).get("plate"),
                         "av_status": (model or {}).get("status"), "av_conf": (model or {}).get("confidence"),
                         "av_sent": plate if mode == "best" else a.get("plate_number_str", ""),
                         "av_choice": cn or plate, "av_src": src, "av_reason": why, "av_mf": mf, "av_nm": nm, "av_lane": lane, "av_cn": cn, "av_short": sf, "av_short_mode": sf_mode, "av_roi": roi, "av_cnm": cnm,
                         **second_check(model, a.get("plate_number_str", ""), plate, jpeg_path)})
            if np_why:   # av_np: событие в Dolibarr не слать (callbacks не пушит номер короче MIN_PLATE_LEN)
                data.update(label="", licenseplatenumber="")
                return data
            # Только номер KG и только когда выбрана модель (владелец 20.10). Страна и атрибуты модели в Dolibarr
            # не идут: при номере камеры они противоречили бы ему, а марка/кузов модели ещё слабые (KIA → Hyundai).
            if sf and sf_mode == "on":
                data.update(label=sf, licenseplatenumber=sf, licenseplatenumberscore=round(float(model.get("confidence") or 0), 2),
                            licenseplatecountrycode="KG" if model.get("country") in ("KG", "UZ") else "Unknown",
                            licenseplatecountryscore=round(float(model.get("country_conf") or 0), 2))
            if cn and cn != a.get("plate_number_str", ""):
                data.update(label=cn, licenseplatenumber=cn)
            if mode == "best" and src == "cnm" and cnm:
                data.update(label=plate, licenseplatenumber=plate, licenseplatenumberscore=round(float(cnm[2]), 2),
                            licenseplatecountrycode="CN", licenseplatecountryscore=round(float(cnm[3]), 4))
            if mode == "best" and src == "roi" and roi and roi.get("pick"):
                data.update(label=plate, licenseplatenumber=plate, licenseplatenumberscore=round(float(roi.get("conf_pick") or 0), 2),
                            licenseplatecountrycode=roi.get("country") or "Unknown", licenseplatecountryscore=0.0)
            if mode == "best" and src == "model":
                data.update(label=plate, licenseplatenumber=plate,
                            licenseplatenumberscore=round(float((nmb or {}).get("conf") or model.get("confidence") or 0), 2),
                            licenseplatecountrycode="KG",
                            licenseplatecountryscore=round(float((nmb or {}).get("country_conf") or model.get("country_conf") or 0), 2))
            if mode == "best" and _av_nm is not None and src not in ("roi", "cnm") and (_flag_on(AV_CC_FILE) or _flag_on(AV_SC_FILE)):
                try:
                    cur = data.get("licenseplatenumber") or ""
                    p2, ccode, cscore, _cc, obox = _av_nm.cc_apply(a.get("plate_number_str", ""), cur, (model or {}).get("boxes"), src)
                    if _flag_on(AV_CC_FILE) and ccode:
                        if p2 != cur:
                            data.update(label=p2, licenseplatenumber=p2)
                        data.update(licenseplatecountrycode=ccode, licenseplatecountryscore=cscore)
                    if _flag_on(AV_SC_FILE):
                        if src == "model":
                            pass   # номер модели: score уже = conf выбранной рамки
                        else:
                            pc = float(a.get("plate_conf", -1))
                            if pc >= 0:
                                data.update(licenseplatenumberscore=round(pc / 100.0, 4))
                        if obox is not None and obox.get("country_conf") is not None and not _flag_on(AV_CC_FILE):
                            data.update(licenseplatecountryscore=round(float(obox["country_conf"]), 4))
                    logger.info({"av_cc": data.get("licenseplatecountrycode"), "av_cc_on": _flag_on(AV_CC_FILE), "av_sc_on": _flag_on(AV_SC_FILE),
                                 "av_nscore": data.get("licenseplatenumberscore"), "av_cscore": data.get("licenseplatecountryscore"), "av_plate_out": data.get("licenseplatenumber")})
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV cc/sc: {e}")
            if mode == "best" and _av_nm is not None and src not in ("roi", "cnm") and _flag_on("/app/data/av_cc2"):   # 03.11: страна D (A1 + C + спор -> Unknown)
                try:
                    c0, s0 = data.get("licenseplatecountrycode"), data.get("licenseplatecountryscore")
                    c2, s2, why2 = _av_nm.cc2_apply(c0, s0, (model or {}).get("country"), (model or {}).get("country_conf"))
                    if why2:
                        data.update(licenseplatecountrycode=c2, licenseplatecountryscore=s2)
                    logger.info({"av_cc2": c2, "av_cc2_was": c0, "av_cc2_why": why2, "av_cc2_model": (model or {}).get("country"), "av_cc2_mconf": (model or {}).get("country_conf")})
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV cc2: {e}")
            if mode == "best" and _flag_on("/app/data/av_attrs"):   # Q-028 (30.10): марка/цвет/кузов из модели при уверенности >= AV_ATTR_CONF
                try:
                    _fill_attrs(data, model)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV attrs: {e}")
            if mode == "best" and _flag_on("/app/data/av_source"):
                try:
                    data.update(av_source_fields(src, model, data.get("licenseplatenumber") or "", a.get("plate_number_str", "")))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"AV source: {e}")
            return data

        except (requests.RequestException, KeyError) as e:
            logger.error(f"Error detecting state number in {jpeg_path}: {e}")
            return {}

    def _prepare_data(
        self, jpeg_path: str, camera_code: str, camera_id: int, a
    ) -> dict:
        logger.info("+++++++++++++++++++++++++++++++")
        logger.info(a["plate_number_str"])
        logger.info(type(a["plate_number_str"]))
        logger.info(type(str(a["plate_number_str"])))
        logger.info("+++++++++++++++++++++++++++++++")
        with open(jpeg_path, "rb") as file:
            encoded_image = base64.b64encode(file.read()).decode("utf-8")

        data = {
            "module": "equipments",
            "ref": "(PROV)",
            "status": 0,
            "label": a["plate_number_str"],
            "photocontent": encoded_image,
            "licenseplatenumber": a["plate_number_str"],
            "licenseplatenumberscore": 0.00,
            "licenseplatecountrycode": "KG"
            if KG_PLATE_RE.match(a["plate_number_str"])
            else "Unknown",
            "licenseplatecountryscore": 0.00,
            "carbrandcode": a["object_subType_str"],
            "carbrandscore": "0.00",
            "carcolorcode": a["vehicle_color_str"],
            "carcolorscore": 0.00,
            "cartypebodycode": a["object_subType_str"],
            "cartypebodyscore": 0.00,
            "camera": camera_id,
            "cameracode": camera_code,
            "direction": 0,
        }

        return data
