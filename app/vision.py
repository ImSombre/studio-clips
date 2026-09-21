"""
Cadrage intelligent : regarde l'image pour savoir OÙ cadrer.

1. Analyse (2 images/s, en 640 px, rapide même sans carte graphique) :
   - visages (détecteur YuNet d'OpenCV) : où est la personne qui parle ;
   - zones d'info : panneau / capture / article collé à l'écran, blocs de texte ;
   - changements de plan de la vidéo d'origine.
2. Plan de cadrage du clip, plan par plan (les plans changent aux fins de phrase et aux changements
   de plan de la vidéo d'origine, jamais au milieu d'un mot) :
   - visage seul      -> recadrage 9:16 centré sur le visage (zoom vers le visage) ;
   - info seule       -> cadrage sur l'info (vidéo entière + fond flou si l'info est large) ;
   - visage + info    -> écran partagé : l'info en haut, le visage en bas ;
   - rien de reconnu  -> cadrage par défaut choisi par l'utilisateur.
Tous les rectangles sont en coordonnées normalisées (0..1) de la vidéo source.
"""

import os
import subprocess

MODELE_VISAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modeles", "face_detection_yunet_2023mar.onnx")
CADENCE = 2                 # images analysées par seconde
LARGEUR_ANALYSE = 640
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ZOOMS = (1.0, 1.08, 1.0, 1.14)   # zooms successifs des plans « visage » (vers le visage, pas le centre)
PLAN_CIBLE, PLAN_MIN = 6.0, 1.5  # durée visée d'un plan, et plus court plan autorisé


# ----------------------------------------------------------------------
# 1. Analyse de l'image
# ----------------------------------------------------------------------
def taille_video(source):
    """(largeur, hauteur) telles qu'affichées (une vidéo de téléphone tournée est corrigée)."""
    import json
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_side_data=rotation", "-of", "json", source],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
    flux = json.loads(r.stdout or "{}").get("streams", [{}])[0]
    largeur, hauteur = int(flux.get("width", 1920)), int(flux.get("height", 1080))
    rotation = next((abs(int(d.get("rotation", 0))) for d in flux.get("side_data_list", []) if "rotation" in d), 0)
    if rotation in (90, 270):
        largeur, hauteur = hauteur, largeur
    return largeur, hauteur


def _images(source, debut, fin, largeur_src, hauteur_src):
    """Images (t, image BGR) à CADENCE images/s entre debut et fin, via FFmpeg (aucune écriture disque)."""
    import numpy as np
    lw = LARGEUR_ANALYSE
    lh = max(2, int(round(hauteur_src * lw / largeur_src / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{debut:.3f}", "-to", f"{fin:.3f}", "-i", source,
           "-vf", f"fps={CADENCE}:start_time=0,scale={lw}:{lh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WIN)
    taille, k = lw * lh * 3, 0
    try:
        while True:
            brut = proc.stdout.read(taille)
            if len(brut) < taille:
                break
            yield debut + k / CADENCE, np.frombuffer(brut, np.uint8).reshape(lh, lw, 3)
            k += 1
    finally:
        proc.kill()
        proc.wait()


def _visages(detecteur, img):
    h, w = img.shape[:2]
    detecteur.setInputSize((w, h))
    _, res = detecteur.detect(img)
    visages = []
    for f in res if res is not None else []:
        x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
        if fw >= 0.03 * w:
            visages.append([(x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, round(score, 2)])
    return sorted(visages, key=lambda v: -v[2] * v[3])


def _lignes_de_texte(gris):
    """Rectangles de lignes de texte (caractères rapprochés), en pixels."""
    import cv2
    grad = cv2.morphologyEx(gris, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, nb = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    lie = cv2.morphologyEx(nb, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (13, 3)))
    contours, _ = cv2.findContours(lie, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gris.shape
    lignes = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if not (0.012 * h <= ch <= 0.1 * h and cw >= 2.2 * ch and cw >= 0.06 * w):
            continue
        zone = nb[y:y + ch, x:x + cw]
        if zone.mean() / 255 < 0.18:
            continue
        # du texte = beaucoup d'alternances noir/blanc sur la ligne du milieu (les stores, eux, sont continus)
        milieu = zone[ch // 2] > 0
        alternances = int((milieu[1:] != milieu[:-1]).sum())
        if alternances < max(6, cw / (ch * 1.6)):
            continue
        lignes.append((x, y, cw, ch))
    return lignes


def _panneaux(gris):
    """Grands rectangles nets (capture, article, image collée), en pixels."""
    import cv2
    bords = cv2.Canny(cv2.GaussianBlur(gris, (5, 5), 0), 60, 160)
    bords = cv2.dilate(bords, None)
    contours, _ = cv2.findContours(bords, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gris.shape
    res = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        aire = cw * ch
        if not (0.06 * w * h <= aire <= 0.9 * w * h) or cw < 0.15 * w or ch < 0.15 * h:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.contourArea(approx) >= 0.85 * aire:
            res.append((x, y, cw, ch))
    return res


def _infos(img, visages):
    """Zones d'information de l'image, normalisées [x, y, w, h]."""
    import cv2
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gris.shape
    lignes = _lignes_de_texte(gris)
    zones = []
    for (x, y, cw, ch) in _panneaux(gris):
        dedans = [l for l in lignes if l[0] >= x - 4 and l[1] >= y - 4 and l[0] + l[2] <= x + cw + 4 and l[1] + l[3] <= y + ch + 4]
        contient_visage = any(x <= v[0] * w <= x + cw and y <= v[1] * h <= y + ch for v in visages)
        if dedans and not contient_visage:
            zones.append([x / w, y / h, cw / w, ch / h])
    if not zones and len(lignes) >= 3:
        # pas de cadre net : on regroupe les lignes de texte proches en un bloc
        masque = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) * 0
        for (x, y, cw, ch) in lignes:
            masque[y:y + ch, x:x + cw] = 255
        masque = cv2.dilate(masque, cv2.getStructuringElement(cv2.MORPH_RECT, (31, 25)))
        contours, _ = cv2.findContours(masque, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            n = sum(1 for l in lignes if x <= l[0] and l[0] + l[2] <= x + cw and y <= l[1] and l[1] + l[3] <= y + ch)
            if n >= 3 and cw * ch >= 0.05 * w * h:
                zones.append([x / w, y / h, cw / w, ch / h])
    if not zones and not visages and len(lignes) >= 3:
        # pas de visage et plusieurs lignes de texte : capture d'écran / diapo plein cadre
        x0, y0 = min(l[0] for l in lignes), min(l[1] for l in lignes)
        x1, y1 = max(l[0] + l[2] for l in lignes), max(l[1] + l[3] for l in lignes)
        if (x1 - x0) * (y1 - y0) >= 0.08 * w * h:
            zones.append([x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h])
    # une zone qui recouvre surtout le visage principal, c'est la personne, pas une info
    if visages:
        v = visages[0]
        zones = [z for z in zones if not (z[0] <= v[0] <= z[0] + z[2] and z[1] <= v[1] <= z[1] + z[3])]
    return zones


def analyser(source, zones_temps, progression=lambda f: None, arreter=lambda: False):
    """Analyse les passages [(debut, fin)] de la vidéo. Retourne {taille, echantillons}."""
    import cv2
    largeur, hauteur = taille_video(source)
    detecteur = cv2.FaceDetectorYN.create(MODELE_VISAGES, "", (LARGEUR_ANALYSE, LARGEUR_ANALYSE), 0.72, 0.3, 50)
    total = sum(b - a for a, b in zones_temps) or 1
    fait, echantillons = 0.0, []
    for debut, fin in zones_temps:
        precedent = None
        for t, img in _images(source, debut, fin, largeur, hauteur):
            if arreter():
                raise InterruptedError()
            visages = _visages(detecteur, img)
            hsv = cv2.calcHist([cv2.cvtColor(img, cv2.COLOR_BGR2HSV)], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(hsv, hsv)
            coupe = precedent is not None and cv2.compareHist(precedent, hsv, cv2.HISTCMP_CORREL) < 0.6
            precedent = hsv
            echantillons.append({"t": round(t, 2), "visages": visages[:3], "infos": _infos(img, visages), "coupe": coupe})
            progression(min(1.0, (fait + (t - debut)) / total))
        fait += fin - debut
    return {"taille": [largeur, hauteur], "echantillons": echantillons}


def zones_manquantes(vision, zones_temps):
    """Parties des passages demandés qui n'ont pas encore été analysées."""
    deja = sorted(e["t"] for e in (vision or {}).get("echantillons", []))
    manque = []
    for a, b in zones_temps:
        dedans = [t for t in deja if a - 0.6 <= t <= b + 0.6]
        if len(dedans) < max(1, (b - a) * CADENCE * 0.8):
            manque.append((a, b))
    return manque


# ----------------------------------------------------------------------
# 2. Plan de cadrage
# ----------------------------------------------------------------------
def _borne(v, bas, haut):
    return max(bas, min(haut, v))


def _rect_autour(cx, cy, larg, haut, ancre_y=0.5):
    """Rectangle normalisé de taille (larg, haut) centré en cx et placé pour que cy soit à ancre_y."""
    larg, haut = min(larg, 1.0), min(haut, 1.0)
    x = _borne(cx - larg / 2, 0, 1 - larg)
    y = _borne(cy - ancre_y * haut, 0, 1 - haut)
    return [round(x, 4), round(y, 4), round(larg, 4), round(haut, 4)]


def _rect_rapport(boite, rapport_px, sw, sh, marge=0.1):
    """Plus petit rectangle de rapport largeur/hauteur (en pixels) `rapport_px` contenant la boîte."""
    x, y, w, h = boite
    # un peu plus d'air au-dessus : les en-têtes (nom du journal, titre) sont souvent juste au-dessus du cadre détecté
    x, y, w, h = x - w * marge, y - h * marge * 1.8, w * (1 + 2 * marge), h * (1 + 2.8 * marge)
    w_px, h_px = w * sw, h * sh
    if w_px / h_px > rapport_px:
        h_px = w_px / rapport_px
    else:
        w_px = h_px * rapport_px
    if h_px > sh:                       # trop grand : on garde toute la hauteur
        h_px, w_px = sh, sh * rapport_px
    if w_px > sw:
        w_px, h_px = sw, sw / rapport_px
    return _rect_autour(x + w / 2, y + h / 2, w_px / sw, h_px / sh)


def _decoupe_plans(blocs, segments, coupes_source):
    """[(a, b)] : plans de ~6 s, coupés aux fins de phrase et aux changements de plan d'origine."""
    fins_phrase = sorted({round(s["end"], 2) for s in segments or []})
    plans = []
    for a, b in blocs:
        points = sorted(t for t in coupes_source if a + PLAN_MIN < t < b - PLAN_MIN)
        bornes = [a] + points + [b]
        for pa, pb in zip(bornes, bornes[1:]):
            debut = pa
            while pb - debut > PLAN_CIBLE * 1.4:
                candidats = [t for t in fins_phrase if debut + PLAN_CIBLE * 0.6 <= t <= debut + PLAN_CIBLE * 1.4 and t < pb - PLAN_MIN]
                if not candidats:
                    break
                coupe = min(candidats, key=lambda t: abs(t - (debut + PLAN_CIBLE)))
                plans.append((debut, coupe))
                debut = coupe
            plans.append((debut, pb))
    return plans


def _synthese(echantillons, a, b):
    """Visage principal et info dominante d'un plan (présents sur au moins la moitié des images)."""
    dedans = [e for e in echantillons if a - 0.25 <= e["t"] <= b + 0.25]
    if not dedans:
        return None, None
    avec_visage = [e["visages"][0] for e in dedans if e["visages"]]
    visage = None
    if len(avec_visage) >= 0.4 * len(dedans):
        med = lambda i: sorted(v[i] for v in avec_visage)[len(avec_visage) // 2]
        visage = [med(0), med(1), med(2), med(3)]
    infos = [z for e in dedans for z in e["infos"][:1]]
    info = None
    if len(infos) >= 0.5 * len(dedans):
        med = lambda i: sorted(z[i] for z in infos)[len(infos) // 2]
        info = [med(0), med(1), med(2), med(3)]
    return visage, info


def _textes_hors_contenu(r, sw, sh):
    """Contenu affiché en entier sur fond flou : titre au-dessus et sous-titres en dessous, dans le flou."""
    hauteur_contenu = min(1.0, (9 / 16) * (r[3] * sh) / (r[2] * sw))   # part de la hauteur 9:16 occupée
    haut, bas = (1 - hauteur_contenu) / 2 * 100, (1 + hauteur_contenu) / 2 * 100
    return {"ti": round(max(4, haut - 9), 1) if haut > 13 else 5,
            "st": round(min(91, bas + 6), 1) if 100 - bas > 13 else 86}


def plan_cadrage(blocs, segments, vision, cadrage, montage):
    """Liste de plans {de, a, type, r, r2} (temps de la source). Utilisée par l'export ET l'aperçu."""
    cadrage = cadrage or {}
    zooms = (montage or {}).get("zooms", True)
    auto = cadrage.get("auto", True) and vision and vision.get("echantillons")
    sw, sh = (vision or {}).get("taille") or [1920, 1080]
    rapport_916 = 9 / 16
    largeur_916 = min(1.0, (sh * rapport_916) / sw)
    echantillons = (vision or {}).get("echantillons") or []
    coupes = [e["t"] for e in echantillons if e.get("coupe")] if auto else []
    if auto:
        # une info qui apparaît / disparaît (stable sur 2 images) change aussi de plan
        for e1, e2, e3 in zip(echantillons, echantillons[1:], echantillons[2:]):
            if bool(e1["infos"]) != bool(e2["infos"]) and bool(e2["infos"]) == bool(e3["infos"]):
                coupes.append(e2["t"])
        coupes.sort()
    plans = _decoupe_plans(blocs, segments, coupes)

    def par_defaut(z):
        if cadrage.get("mode", "remplir") == "flou":
            return {"type": "flou", "r": [0, 0, 1, 1]}
        cx = 0.5 + max(-50, min(50, float(cadrage.get("decalage", 0)))) / 100 * (1 - largeur_916)
        return {"type": "rect", "r": _rect_autour(cx, 0.5, largeur_916 / z, 1 / z)}

    res, precedent = [], None
    for i, (a, b) in enumerate(plans):
        z = ZOOMS[i % len(ZOOMS)] if zooms else 1.0
        visage, info = _synthese(echantillons, a, b) if auto else (None, None)
        if auto and b - a < PLAN_MIN and precedent:
            forme = {k: v for k, v in precedent.items() if k not in ("de", "a")}   # plan trop court : on ne change pas
        elif visage and info:
            haut_visage = _borne(visage[3] * 2.8, 0.35, 1.0) / z
            # texte : titre à la jointure des deux moitiés, sous-titres en bas (sur le visage, pas sur l'info)
            forme = {"type": "partage", "r": _rect_rapport(info, 9 / 8, sw, sh),
                     "r2": _rect_autour(visage[0], visage[1], haut_visage * sh * 9 / 8 / sw, haut_visage, 0.42),
                     "ti": 52, "st": 84}
        elif info:
            w_px, h_px = info[2] * sw, info[3] * sh
            if w_px / h_px > 0.8:   # info large (capture d'écran…) : entière, sur fond flou
                r = _rect_autour(info[0] + info[2] / 2, info[1] + info[3] / 2, min(1, info[2] * 1.08), min(1, info[3] * 1.08))
                forme = {"type": "flou", "r": r, **_textes_hors_contenu(r, sw, sh)}
            else:
                forme = {"type": "rect", "r": _rect_rapport(info, rapport_916, sw, sh)}
        elif visage:
            forme = {"type": "rect", "r": _rect_autour(visage[0], visage[1], largeur_916 / z, 1 / z, 0.38)}
        else:
            forme = par_defaut(z)
            if forme["type"] == "flou":
                forme.update(_textes_hors_contenu(forme["r"], sw, sh))
        plan = {"de": round(a, 3), "a": round(b, 3), **forme}
        res.append(plan)
        precedent = plan
    return res
