# v7.1 (28.10): правки Main после просмотра 20 кадров. Правило владельца «китайская - камера, KG - модель». Чистая функция.
import re
from difflib import SequenceMatcher
_KGUZ = [re.compile(rf"^{x}$") for x in (
    r"(0[1-9]|1[01])\d{3}[A-Z]{2,3}", r"(0[1-9]|1[01])\d{4}[A-Z]", r"[A-Z]\d{4}[A-Z]{1,2}", r"(D|T|C|CMD)\d{5}", r"KG[A-Z]{2}\d{4}",
    r"\d{2}[A-Z]\d{3}[A-Z]{2}", r"\d{5}[A-Z]{3}", r"\d{2}[A-Z]\d{6}")]   # формат рамки модели (как раньше, мягкий)
_KG_STRICT = re.compile(r"^(0[1-9]|1[01])\d{3}[A-Z]{3}$")
_UZ_STRICT = re.compile(r"^((01|10|20|25|30|40|50|60|70|75|80|85|90|95)\d{3}[A-Z]{3}|\d{2}[A-Z]\d{3}[A-Z]{2}|\d{2}[A-Z]\d{6})$")
_CN = re.compile(r"^[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}$")
_KG_NEW = re.compile(r"^(0[1-9]|10)(\d{3}[A-Z]{3})$")
NM_T = 0.85

def _clean(p):
    p = (p or "").strip().upper().replace(" ", "")
    return p[1:] if p and not p[0].isascii() else p
def _norm(p):
    p = _clean(p)
    return re.sub(r"^((?:0[1-9]|1[01]))KG", r"\1", p)
def _cmp(p):
    return _norm(p).replace("I", "1").replace("O", "0")
def _erp_fmt(p):   # старое: KG вставляется по формату (для сравнения в реплее)
    return _KG_NEW.sub(r"\1KG\2", _clean(p))
def _erp(p, country=None):   # слово владельца 28.10: KG вставляем только если страна рамки KG; UZ - как есть
    return _KG_NEW.sub(r"\1KG\2", _clean(p)) if country == "KG" else _clean(p)
def kguz_ok(p):
    n = _norm(p); return any(rx.match(n) for rx in _KGUZ)
def cam_strict(p, uz_ok=True):
    n = _norm(p); return bool(_KG_STRICT.match(n) or (uz_ok and _UZ_STRICT.match(n)))
def related(a, b):
    """хвост >=3 общих знака или общая подстрока >=4 (после I->1, O->0)"""
    a, b = _cmp(a), _cmp(b)
    if not a or not b: return False
    k = 0
    while k < min(len(a), len(b)) and a[-1 - k] == b[-1 - k]: k += 1
    if k >= 3: return True
    return SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b)).size >= 4

def choose_nm(cam, boxes):
    """(номер, источник agree|model|camera, причина). boxes - рамки читалки [{text,country,conf,matches_camera}]; osd без текста пропускаются."""
    cam = cam or ""
    bs = [b for b in (boxes or []) if b.get("text")]
    if not bs:
        return cam, "camera", "модель молчит/нет рамок"
    if cam and any(_cmp(b["text"]) == _cmp(cam) or _cmp(_erp(b["text"])) == _cmp(cam) for b in bs):
        return cam, "agree", "камера = рамка модели"
    rel = [b for b in bs if cam and ((b.get("matches_camera") or "") == "full" or related(cam, b["text"]))]
    if any(b.get("country") == "CN" for b in rel):
        return cam, "camera", "рамка камеры - китайская"
    # v7.3: номер камеры заканчивается полным текстом KG/UZ-рамки модели (мусорный префикс: DDB1234AL <- B1234AL) -> модель
    if cam:
        for b in sorted(bs, key=lambda x: -float(x.get("conf") or 0)):
            t = _cmp(b["text"])
            if b.get("country") in ("KG", "UZ") and len(t) >= 5 and _cmp(cam).endswith(t) and _cmp(cam) != t \
                    and float(b.get("conf") or 0) >= NM_T and kguz_ok(_erp(b["text"], b.get("country"))):
                return _erp(b["text"], b.get("country")), "model", "камера = мусорный префикс + рамка модели, KG/UZ %s %.4f" % (b.get("country"), float(b.get("conf") or 0))
    kgbest = [b for b in bs if b.get("country") in ("KG", "UZ")]
    uz_ok = not any(b.get("country") == "KG" and float(b.get("conf") or 0) >= NM_T for b in bs)   # камера «валидна как UZ», а модель видит KG - валидной не считаем
    ok = lambda b: b.get("country") in ("KG", "UZ") and float(b.get("conf") or 0) >= NM_T and kguz_ok(_erp(b["text"], b.get("country")))
    # v7.2 п.1: камера переставила знаки (тот же набор символов, что у рамки модели) - одна машина, берём модель
    if cam:
        sc = sorted(_cmp(cam))
        for b in sorted([x for x in bs if ok(x)], key=lambda x: -float(x.get("conf") or 0)):
            if sorted(_cmp(b["text"])) == sc:
                return _erp(b["text"], b.get("country")), "model", "перестановка знаков камеры, KG/UZ %s %.4f" % (b.get("country"), float(b.get("conf") or 0))
    # v7.2 п.2: камера похожа на китайский номер (буква в первых двух знаках, >=4 цифр, не строгий KG/UZ), CN-рамки нет -> камера
    if cam and not cam_strict(cam):
        c = _cmp(cam)
        # v7.4: не срабатывает, если есть валидная KG/UZ-рамка >=0,85 с общим хвостом >=3 знаков с камерой (B1234ABC <- 01234ABC)
        tail3 = lambda t: len(c) >= 3 and len(_cmp(t)) >= 3 and _cmp(t)[-3:] == c[-3:]
        if any(ch.isalpha() for ch in c[:2]) and sum(ch.isdigit() for ch in c) >= 4 and not any(ok(b) and tail3(b["text"]) for b in bs):
            return cam, "camera", "камера похожа на китайский номер, CN-рамки нет"
    if cam and cam_strict(cam, uz_ok):
        return cam, "camera", "камера строго валидна, рамки с тем же номером нет"
    ours = [b for b in rel if b.get("country") in ("KG", "UZ")]
    if ours:        # рамка нашей машины определена по камере: берём её, не main
        ours = [b for b in ours if ok(b)]
        if not ours:
            return cam, "camera", "рамка по камере есть, но уверенность/формат не проходят"
        b = max(ours, key=lambda x: float(x.get("conf") or 0))
    else:
        c = [b for b in bs if ok(b)]
        if not c:
            return cam, "camera", "нет валидной KG/UZ рамки >= %.2f" % NM_T
        b = max(c, key=lambda x: (bool(x.get("main")), float(x.get("conf") or 0)))
    return _erp(b["text"], b.get("country")), "model", "KG/UZ %s %.4f%s" % (b.get("country"), float(b.get("conf") or 0), " (по камере)" if ours else "")


def choose_nm_box(cam, model):
    """(номер, источник, причина, рамка|None) для живого ответа читалки (model['boxes'])."""
    boxes = (model or {}).get("boxes") or []
    plate, src, why = choose_nm(cam, boxes)
    box = None
    if src == "model":
        for b in boxes:
            if b.get("text") and _erp(b["text"], b.get("country")) == plate:
                box = b; break
    return plate, src, why, box


_KG_ANY = re.compile(r"^(0[1-9]|10)(\d{3}[A-Z]{2,3})$")
CC_CONF = 0.9
CC_COUNTRIES = ("KG", "UZ", "CN")


def cc_apply(cam, plate, boxes, src=None):
    """28.10 (слово владельца): страна - от рамки НАШЕЙ машины (текст рамки = выбранный номер, иначе рамка, совпавшая с камерой по хвосту>=3/общей подстроке>=4/matches_camera=full).
    -> (номер, код страны|None, score|None, страна рамки|None, рамка|None). KG вставляется по стране KG (3 и 2 буквы); UZ/CN без вставки; прочие страны или country_conf<0,9 -> поле не трогаем (None)."""
    bs = [b for b in (boxes or []) if b.get("text")]
    ours = None
    pc = _cmp(plate)
    for b in sorted(bs, key=lambda x: -float(x.get("conf") or 0)):
        if _cmp(b["text"]) == pc or _cmp(_erp(b["text"], b.get("country"))) == pc:
            ours = b; break
    if ours is None and cam:
        for b in sorted(bs, key=lambda x: -float(x.get("conf") or 0)):
            if (b.get("matches_camera") or "") == "full" or related(cam, b["text"]):
                ours = b; break
    if ours is None:
        return plate, None, None, None, None
    cc = ours.get("country"); cf = float(ours.get("country_conf") or 0)
    if src == "model" and cc in CC_COUNTRIES:   # 28.10 Main: номер выбран этой рамкой - страна по её классу, score как есть (порог 0,9 только для camera/agree)
        if cc == "KG":
            m = _KG_ANY.match(plate or "")
            if m:
                plate = m.group(1) + "KG" + m.group(2)
        return plate, cc, round(cf, 4), cc, ours
    if cc not in CC_COUNTRIES or cf < CC_CONF:
        return plate, None, None, cc, ours
    if cc == "KG":
        m = _KG_ANY.match(plate or "")
        if m:
            plate = m.group(1) + "KG" + m.group(2)
    return plate, cc, round(cf, 2), cc, ours


_KG_STRICT_NP = re.compile(r"^(0[1-9]|10|11)\d{3}[A-Z]{2,3}$")
NP_CAM_CONF = 50
NP_MIN_LEN = 6
_NP_OLD_KG = re.compile(r"^[A-Z]\d{4}$")   # старый KG из 5 знаков (буква + 4 цифры) - не отсекаем


def np_drop(cam, cam_conf, model):
    """28.10 (слово владельца, строгий вариант): -> причина, если событие в Dolibarr НЕ слать, иначе None.
    (а) ответ читалки без валидной рамки: model пуст, status None (нет рамок) или no_plate; (б) уверенность камеры известна и < 50 %; (в) номер камеры не строгий KG ^(0[1-9]|10|11)\\d{3}[A-Z]{2,3}$."""
    try:
        st = (model or {}).get("status")
        if st not in (None, "no_plate"):
            return None
        # 30.10 (Q-026): Lane2 отправила один короткий номер камеры 208 раз (камера 54-78 %, модель no_plate): уверенность камеры
        # не признак номера. Короче 6 знаков (кратчайший формат KG/UZ/CN - 6-7) при молчащей модели - не слать независимо от уверенности.
        if len(_norm(cam)) < NP_MIN_LEN and not _NP_OLD_KG.match(_norm(cam)):
            return "av_np: модель %s + номер камеры короче %d знаков" % (st or "молчит", NP_MIN_LEN)
        if cam_conf is None or float(cam_conf) < 0 or float(cam_conf) >= NP_CAM_CONF:
            return None
        if _KG_STRICT_NP.match(_norm(cam)):
            return None
        return "av_np: модель %s + камера %s %% + номер камеры не KG" % (st or "молчит", cam_conf)
    except Exception:
        return None


# --- Q-033 (30.10): вторая попытка по ROI рамки камеры -------------------------------------------------------------
# Камера читает номер неверно/не тот (соседний, мусор), а читалка по всему кадру не нашла рамку этого номера. Рамка камеры
# (plate_box_abs) используется НЕ как выбор из рамок, а как область для ДОПОЛНИТЕЛЬНОГО чтения: вырезка с полями -> читалка.
# Принимаем только уверенное (status confident, conf >= ROI_CONF), без «?», формат KG/UZ/CN.
ROI_CONF = 0.99
ROI_PX, ROI_PY = 0.5, 0.8     # поля вырезки в долях ширины/высоты рамки камеры
ROI_MIN_W = 640               # минимальная ширина вырезки (апскейл)
ROI_REGION_CONF = 0.999
ROI_OVERLAP = 0.3             # IoU рамки камеры и рамки читалки, при котором «рамка камеры найдена читалкой»
_CN_ROI = re.compile(r"^[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}$")


def parse_box(s):
    """'x1,y1,x2,y2' (plate_box_abs камеры) -> (x1,y1,x2,y2) | None."""
    try:
        b = tuple(int(float(v)) for v in str(s).split(","))
        return b if len(b) == 4 and b[2] > b[0] and b[3] > b[1] else None
    except Exception:
        return None


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1])); i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def box_found(cam_box, boxes):
    """Читалка нашла рамку номера на месте рамки камеры (IoU >= ROI_OVERLAP с любой не-osd рамкой)."""
    for b in boxes or []:
        bb = b.get("bbox")
        if not b.get("osd") and bb and len(bb) >= 4 and _iou(cam_box, tuple(bb[:4])) >= ROI_OVERLAP:
            return True
    return False


def roi_rect(cam_box, size):
    """Прямоугольник вырезки (x0,y0,x1,y1) вокруг рамки камеры с полями, в пределах кадра size=(W,H)."""
    W, H = size
    x1, y1, x2, y2 = cam_box; w, h = x2 - x1, y2 - y1
    return (max(0, int(x1 - w * ROI_PX)), max(0, int(y1 - h * ROI_PY)), min(W, int(x2 + w * ROI_PX)), min(H, int(y2 + h * ROI_PY)))


def _same_plate_exact(a, b):
    """Точное сравнение номеров без замены I->1, O->0 (только вставка KG после региона не различает)."""
    f = lambda p: re.sub(r"^(\d{2})KG", r"\1", _clean(p).upper())
    return f(a) == f(b)


def roi_pick(cam, roi_model):
    """-> (номер, причина, страна, conf) если вырезка дала уверенное валидное чтение, отличное от камеры; иначе None."""
    m = roi_model or {}
    text = (m.get("plate_full") or m.get("plate") or "").strip()
    if not text or m.get("status") != "confident" or "?" in text:
        return None
    conf = float(m.get("confidence") or 0)
    if conf < ROI_CONF:
        return None
    boxes = [b for b in (m.get("boxes") or []) if b.get("text") and not b.get("osd")]
    country = (max(boxes, key=lambda b: float(b.get("conf") or 0)).get("country") if boxes else None)
    n = _clean(text)
    if country in ("KG", "UZ"):
        if not kguz_ok(_erp(text, country)):
            return None
        out = _erp(text, country)
    elif country == "CN" or (not country and _CN_ROI.match(n)):
        if not _CN_ROI.match(n):
            return None
        out, country = n, "CN"
    else:
        return None
    if cam and _same_plate_exact(out, cam):
        return None            # камера уже верна: менять нечего (AV-072, 31.10: точное сравнение, PI1234 и P11234 - РАЗНЫЕ строки)
    # камера без региона (хвост), а регион дописывает модель: регион на стёртом месте модель выдумывает (арбитр 20.10) -
    # только при уверенности слотов региона region_conf >= ROI_REGION_CONF (как AV_REGION_CONF в autovision.py)
    if cam and country in ("KG", "UZ") and _cmp(out).endswith(_cmp(cam)) and len(_cmp(cam)) < len(_cmp(out)):
        rcs = [b.get("region_conf") for b in boxes if b.get("region_conf") is not None]
        if not rcs or max(float(x) for x in rcs) < ROI_REGION_CONF:
            return None
    return out, "вырезка по рамке камеры, %s %.4f" % (country, conf), country, conf


# --- Q-036 (30.10, решение владельца): для CN основной - номер модели, камера - страховка ---------------------------------
# Модель cn-1.4.0 (хвост 6-7 знаков + иероглиф провинции, θ=0,7) основная, если статус confident, conf >= CNM_CONF и хвост проходит
# формат CN; иначе номер камеры. Защита: если рамка выбранного номера модели не пересекается с рамкой камеры (читалка выбрала
# номер другой машины) и номер камеры валиден как CN - остаётся камера.
CNM_CONF = 0.85  # 02.11 владелец: порог как у KG
_CN_STRICT = re.compile(r"^[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}$")


def cn_primary(cam, cam_box, model):
    """-> (номер, причина, conf, country_conf) если для CN берём номер модели; иначе None (остаётся камера)."""
    m = model or {}
    text = (m.get("plate_full") or m.get("plate") or "").strip()
    if not text or "?" in text:
        return None
    if m.get("status") != "confident":
        # AV-083 (02.11, владелец): камера прилепила мусор к китайскому номеру (GWNX00000 при модели 新X00000,
        # doubtful 0,93) - берём модель и при doubtful, если хвост модели целиком есть в номере камеры
        cam_n = _clean(cam or "").upper().replace("I", "1").replace("O", "0")
        tail_n = _clean(text).upper().replace("I", "1").replace("O", "0")
        tail_n = "".join(ch for ch in tail_n if "A" <= ch <= "Z" or "0" <= ch <= "9")
        if not (m.get("status") == "doubtful" and cam_n and not _CN_STRICT.match(cam_n)
                and len(tail_n) >= 6 and tail_n in cam_n and len(cam_n) > len(tail_n)):
            return None
    boxes = [b for b in (m.get("boxes") or []) if b.get("text") and not b.get("osd")]
    main = next((b for b in boxes if b.get("main")), boxes[0] if boxes else None)
    hier = any("一" <= ch <= "鿿" for ch in text)
    if not (hier or (main and main.get("country") == "CN")):
        return None                       # не китайский номер: правило KG/UZ остаётся прежним
    conf = float(m.get("confidence") or 0)
    tail = _clean(text).upper()
    if conf < CNM_CONF or not _CN_STRICT.match(tail):
        return None
    # Защита (проверка глазами Main, 7 ошибок модели из 79: уверенное чтение ДРУГОГО номера): камера после I->1, O->0 валидна как CN
    # и не имеет с хвостом модели общих >= 3 знаков подряд - разные машины, остаётся камера. Нормализация только для сравнения,
    # в Dolibarr уходит номер камеры как есть.
    if cam:
        cn = _clean(cam).upper().replace("I", "1").replace("O", "0")
        if _CN_STRICT.match(cn) and SequenceMatcher(None, cn, tail, autojunk=False).find_longest_match(0, len(cn), 0, len(tail)).size < 3:
            return None
    if cam and _clean(cam).upper() == tail:
        return None                       # то же самое: менять нечего
    cc = float((main or {}).get("country_conf") or 0)
    return tail, "CN: номер модели основной, conf %.4f" % conf, conf, cc


# 03.11 (слово владельца: «согласие или Unknown»): правило страны D = A1 + C + спор -> Unknown.
CC2_CONF = 0.99
_CC2_GROUP = {"KG": "KG", "UZ": "KG", "CN": "CN"}   # KG и UZ - одна группа (мост шлёт KG для обоих), спор только KG/UZ против CN


def cc2_apply(code, score, mcountry, mconf):
    """code/score - страна, которую мост собирается отправить; mcountry/mconf - ответ модели страны по вырезке номера.
    -> (код, score, причина|None). Модель уверена (>= 0,99) и знает страну: (A1) на месте Unknown или при score < 0,99 - страна модели;
    (D) известная страна моста со score >= 0,99, но другой группы - Unknown. Модель не уверена: (C) KG со score 0,0 -> Unknown."""
    try:
        cur = code if code not in (None, "") else "Unknown"
        cs = float(score or 0)
        g = _CC2_GROUP.get(mcountry)
        mc = float(mconf or 0)
        if g and mc >= CC2_CONF:
            if cur not in _CC2_GROUP or cs < CC2_CONF:
                return mcountry, round(mc, 4), "A1: страна модели %s %.3f вместо %s %.2f" % (mcountry, mc, cur, cs)
            if _CC2_GROUP[cur] != g:
                return "Unknown", 0.0, "D: спор мост %s %.2f против модели %s %.3f" % (cur, cs, mcountry, mc)
            return cur, cs, None
        if cur == "KG" and cs == 0.0:
            return "Unknown", 0.0, "C: KG при score 0,0"
        return cur, cs, None
    except Exception:
        return code, score, None
