"""
Moteur de montage : sous-titres, cadrage et export MP4 d'un clip.

L'aperçu dans l'interface est calculé en direct par le navigateur à partir
des MÊMES réglages (style, cadrage, bornes). FFmpeg n'est lancé qu'à
l'export : c'est ce qui rend le montage instantané, même sur un PC modeste.

Les sous-titres sont écrits en .ass sur une base 1080x1920 : toutes les
tailles ci-dessous sont en vrais pixels de la vidéo finale.
"""

import math
import os
import re
import subprocess
import tempfile
import shutil

LARGEUR, HAUTEUR = 1080, 1920

STYLE_DEFAUT = {
    "police": "Arial Black",
    "taille": 76,            # px sur la vidéo 1080x1920
    "couleur": "#FFFFFF",
    "contour": "#000000",
    "epaisseur": 6,          # épaisseur du contour, px
    "surligne": "#FFE14D",   # couleur du mot en cours (karaoké), "" = aucun
    "position": 68,          # hauteur du texte, en % depuis le haut
    "majuscules": True,
    "mots": 3,               # mots affichés en même temps
}
# « remplir » = vrai format TikTok : l'image occupe tout l'écran 9:16 (les côtés sont coupés).
# « flou » = vidéo entière au centre + fond flouté.
# « auto » = cadrage intelligent (visages + infos, voir vision.py) ; sinon mode + décalage manuels.
CADRAGE_DEFAUT = {"mode": "remplir", "decalage": 0, "auto": True}   # remplir | flou ; décalage -50..50
# Titre incrusté en haut de la vidéo (+ « PARTIE N »), affiché pendant tout le clip.
# « contenu » vide = on affiche le titre du clip ; « numero » vide = le rang du clip.
TEXTE_DEFAUT = {"titre": False, "partie": False, "position": 11, "taille": 56, "contenu": "", "numero": None}

POLICES = ["Arial Black", "Impact", "Segoe UI Black", "Verdana", "Trebuchet MS", "Comic Sans MS"]

MAX_SECONDES_SOUS_TITRE = 1.6
PAUSE_COUPURE = 0.6


# ----------------------------------------------------------------------
# Sous-titres
# ----------------------------------------------------------------------
def mots_du_passage(segments, debut, fin):
    """Mots (temps absolus de la vidéo source) compris dans [debut, fin]."""
    mots = []
    for seg in segments:
        if seg["end"] <= debut or seg["start"] >= fin:
            continue
        if seg.get("words"):
            mots.extend(w for w in seg["words"] if w["end"] > debut and w["start"] < fin)
        else:
            mots.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
    return mots


def groupes_sous_titres(mots, debut, fin, mots_max):
    """Regroupe les mots en sous-titres. Même algorithme que l'aperçu (studio.js)."""
    groupes, courant = [], []
    for m in mots:
        if courant and (
            len(courant) >= mots_max
            or m["end"] - courant[0]["start"] > MAX_SECONDES_SOUS_TITRE
            or m["start"] - courant[-1]["end"] > PAUSE_COUPURE
            or courant[-1]["text"].endswith((".", "?", "!"))
        ):
            groupes.append(courant)
            courant = []
        courant.append(m)
    if courant:
        groupes.append(courant)

    resultat = []
    for g in groupes:
        a = max(g[0]["start"], debut) - debut
        b = min(g[-1]["end"], fin) - debut
        if b - a >= 0.05:
            resultat.append({"debut": a, "fin": b, "mots": [
                {"debut": max(w["start"], debut) - debut, "fin": min(w["end"], fin) - debut, "texte": w["text"]}
                for w in g
            ]})
    # Jamais deux sous-titres à l'écran en même temps.
    for g, suivant in zip(resultat, resultat[1:]):
        g["fin"] = min(g["fin"], suivant["debut"])
    return resultat


def _couleur_ass(hexa, alpha="00"):
    """#RRGGBB -> &HAABBGGRR (ordre inversé propre à l'ASS)."""
    h = (hexa or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _temps_ass(s):
    cs = int(round(max(0.0, s) * 100))
    h, r = divmod(cs, 360000)
    m, r = divmod(r, 6000)
    sec, c = divmod(r, 100)
    return f"{h}:{m:02d}:{sec:02d}.{c:02d}"


def _texte_ass(t):
    return t.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", " ").strip()


def ligne_titre(texte, titre, numero, duree, tranches=None):
    """Évènement(s) ASS du titre incrusté (boîte sombre), ou None.
    tranches = [(debut, fin, % hauteur ou None)] : le cadrage déplace le titre hors de l'info montrée
    (seulement si la personne n'a pas choisi elle-même une position)."""
    t = {**TEXTE_DEFAUT, **(texte or {})}
    if not (t["titre"] or t["partie"]) or duree <= 0:
        return None
    y = int(HAUTEUR * max(3, min(60, float(t["position"]))) / 100)
    if not tranches or float(t["position"]) != TEXTE_DEFAUT["position"]:
        tranches = [(0, duree, None)]
    taille = int(t["taille"])
    numero = t.get("numero") or numero
    contenu = (t.get("contenu") or "").strip() or titre
    morceaux = []
    if t["partie"] and numero:
        morceaux.append(f"{{\\fs{int(taille * 0.72)}\\c&H2E4DFF&}}PARTIE {numero}{{\\fs{taille}\\c&HFFFFFF&}}")
    if t["titre"] and contenu:
        morceaux.append(_texte_ass(contenu))
    if not morceaux:
        return None
    texte_ass = "\\N".join(morceaux)
    return "\n".join(f"Dialogue: 1,{_temps_ass(a)},{_temps_ass(min(b, duree))},Titre,,0,0,0,,"
                     f"{{\\an8\\pos(540,{int(HAUTEUR * pct / 100) if pct is not None else y})}}{texte_ass}"
                     for a, b, pct in tranches if min(b, duree) - a > 0.01)


LANGUES_SANS_ESPACES = ("zh", "ja", "ko", "th", "yue", "lo", "my")


# ----------------------------------------------------------------------
# Montage automatique : coupes des blancs, zooms, accroche, barre de progression
# (studio.js reproduit EXACTEMENT les mêmes calculs pour l'aperçu)
# ----------------------------------------------------------------------
# coupes = retirer les blancs : désactivé par défaut (sur un passage continu, ça hache pour rien)
MONTAGE_DEFAUT = {"coupes": False, "zooms": True, "anim": True, "accroche": True, "barre": True,
                  "transitions": True,   # fondu entre deux passages assemblés
                  "musique": "", "musique_volume": 0.35}   # musique de fond choisie par l'utilisateur
FONDU = 0.25             # durée du fondu image (s) ; le son a le sien, plus court
FPS = 30
TROU_MIN = 0.45          # silence (s) à partir duquel on coupe
MARGE = 0.12             # on garde un peu d'air avant/après chaque mot
HESITATIONS = {"euh", "heu", "euuh", "hum", "hmm", "hmmm", "humm", "uh", "um", "uhm", "erm"}
MOTS_VIDES = {
    "alors", "aussi", "avait", "avant", "avec", "cette", "comme", "comment", "dans", "depuis", "donc", "elle", "elles",
    "encore", "entre", "est-ce", "faire", "fait", "jamais", "juste", "leurs", "mais", "même", "moins", "notre",
    "nous", "parce", "pendant", "peut", "plus", "pour", "pourquoi", "quand", "quelque", "sans", "sont", "sous",
    "tout", "toute", "toutes", "tous", "très", "trop", "vais", "votre", "vous", "about", "after", "again", "because",
    "before", "being", "could", "their", "there", "these", "thing", "think", "those", "would", "really", "right",
}


def _norm(t):
    return re.sub(r"[^\w]", "", (t or "").lower())


def blocs_gardes(segments, debut, fin, montage=None):
    """Passages conservés [(a, b)] en temps de la vidéo source. Sans « coupes » : le clip entier."""
    m = {**MONTAGE_DEFAUT, **(montage or {})}
    if not m["coupes"]:
        return [(debut, fin)]
    mots = [w for w in mots_du_passage(segments, debut, fin) if _norm(w["text"]) not in HESITATIONS]
    if not mots:
        return [(debut, fin)]
    blocs, fin_mot = [], None
    for w in mots:
        if blocs and w["start"] - fin_mot <= TROU_MIN:
            blocs[-1][1] = min(fin, w["end"] + MARGE)
        else:
            blocs.append([max(debut, w["start"] - MARGE), min(fin, w["end"] + MARGE)])
        fin_mot = w["end"]
    res = []
    for a, b in blocs:   # calés sur la grille des images : son et image restent synchronisés
        a = debut + math.floor((a - debut) * FPS) / FPS
        b = min(fin, debut + math.ceil((b - debut) * FPS) / FPS)
        if res and a <= res[-1][1]:
            res[-1][1] = max(res[-1][1], b)
        elif b - a >= 0.2:
            res.append([a, b])
    return [tuple(x) for x in res] or [(debut, fin)]


def passages_du_clip(clip):
    """Morceaux de la vidéo assemblés dans ce clip : [(a, b), …]. Un seul par défaut."""
    bruts = clip.get("passages") or [[clip["debut"], clip["fin"]]]
    passages = []
    for p in bruts:
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b - a > 0.05:
            passages.append((a, b))
    return sorted(passages) or [(float(clip["debut"]), float(clip["fin"]))]


def blocs_du_clip(segments, clip, montage=None):
    """Passages gardés de TOUT le clip + les jonctions entre deux morceaux assemblés.

    Retourne (blocs, jonctions) : `jonctions` = index des blocs qui commencent un nouveau
    morceau (c'est là qu'on met un fondu, pas aux coupes de blancs d'un même passage)."""
    blocs, jonctions = [], set()
    for a, b in passages_du_clip(clip):
        part = blocs_gardes(segments, a, b, montage)
        if blocs and part:
            jonctions.add(len(blocs))
        blocs += part
    return blocs, jonctions


# Mots qui ne valent rien en ouverture de clip : on démarre APRÈS eux.
DEBUTS_MOUS = HESITATIONS | {"alors", "donc", "bah", "ben", "voila", "voilà", "enfin", "bref", "et", "mais",
                             "puis", "apres", "après", "la", "là", "ouais", "ok", "bon"}


def debut_sur_une_phrase(segments, debut, marge=4.0):
    """Recale le début du clip sur un début de phrase et saute les « alors… euh… du coup… ».
    Ne décale jamais de plus de `marge + 2` secondes : le passage choisi par l'IA reste le même."""
    mots = mots_du_passage(segments, max(0.0, debut - marge), debut + marge + 5)
    if not mots:
        return round(debut, 2)
    phrases = [w["start"] for i, w in enumerate(mots)
               if i and mots[i - 1]["text"].rstrip().endswith((".", "?", "!")) and abs(w["start"] - debut) <= marge]
    base = min(phrases, key=lambda t: abs(t - debut)) if phrases else max(debut, mots[0]["start"])
    plafond = debut + marge + 2
    i = next((k for k, w in enumerate(mots) if w["start"] >= base - 0.01), None)
    while i is not None and i < len(mots) - 1:   # on saute le remplissage en tête
        mot, suivant = _norm(mots[i]["text"]), _norm(mots[i + 1]["text"])
        if mot in DEBUTS_MOUS and mots[i]["end"] <= plafond:
            base, i = mots[i]["end"], i + 1
        elif mot == "du" and suivant == "coup" and mots[i + 1]["end"] <= plafond:
            base, i = mots[i + 1]["end"], i + 2
        else:
            break
    return round(max(0.0, min(max(base - 0.12, debut - marge), plafond)), 2)


def duree_montee(blocs):
    return sum(b - a for a, b in blocs)


def vers_sortie(t, blocs):
    """Instant t de la source -> instant dans le clip monté."""
    total = 0.0
    for a, b in blocs:
        if t < a:
            return total
        if t <= b:
            return total + (t - a)
        total += b - a
    return total


def fin_pour_duree(segments, debut, voulu, montage, fin_max):
    """Fin à donner au clip pour qu'APRÈS suppression des blancs il dure `voulu` secondes."""
    fin_max = max(debut + 3, fin_max)
    m = {**MONTAGE_DEFAUT, **(montage or {})}
    bas = min(debut + voulu, fin_max)
    if not m["coupes"] or duree_montee(blocs_gardes(segments, debut, bas, m)) >= voulu - 0.3:
        return bas
    haut = min(fin_max, debut + voulu * 1.8)
    for _ in range(18):
        milieu = (bas + haut) / 2
        if duree_montee(blocs_gardes(segments, debut, milieu, m)) < voulu:
            bas = milieu
        else:
            haut = milieu
    return haut


def mot_cle(mots):
    """Index du mot à mettre en couleur dans un groupe (le plus « lourd »), ou None."""
    meilleur, score = None, 0
    for i, w in enumerate(mots):
        n = _norm(w["texte"])
        s = len(n) + (3 if any(c.isdigit() for c in n) else 0) + (2 if w["texte"].endswith("!") else 0)
        if n in MOTS_VIDES or len(n) < 5:
            continue
        if s > score:
            meilleur, score = i, s
    return meilleur


COULEUR_CLE = "#FFE14D"


def ecrire_ass(groupes, style, chemin, titre_ass=None, taille_titre=None, langue="fr",
               anim=False, accroche=None, barre_duree=None, tranches=None):
    """tranches = [(debut, fin, % hauteur ou None)] : hauteur des sous-titres imposée par le cadrage
    (hors de l'info affichée), seulement si la personne garde la position par défaut."""
    s = {**STYLE_DEFAUT, **(style or {})}
    if float(s["position"]) != STYLE_DEFAUT["position"]:
        tranches = None
    sep = "" if langue in LANGUES_SANS_ESPACES else " "
    y = int(HAUTEUR * max(5, min(95, float(s["position"]))) / 100)
    blanc = _couleur_ass(s["couleur"])
    entete = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{s['police']},{int(s['taille'])},{blanc},&H000000FF,{_couleur_ass(s['contour'])},"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{int(s['epaisseur'])},2,5,90,90,0,1\n"
        # BorderStyle 3 = boîte opaque derrière le texte (couleur = OutlineColour, marge = Outline)
        f"Style: Titre,Arial Black,{int(taille_titre or TEXTE_DEFAUT['taille'])},&H00FFFFFF,&H000000FF,"
        "&H26000000,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,8,90,90,0,1\n"
        "Style: Accroche,Arial Black,96,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,9,4,5,70,70,0,1\n"
        "Style: Barre,Arial,10,&H002E4DFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lignes = [entete]
    if titre_ass:
        lignes.append(titre_ass)
    def position(t):
        for a, b, pct in tranches or ():
            if a - 0.01 <= t < b and pct is not None:
                return f"{{\\an5\\pos(540,{int(HAUTEUR * pct / 100)})}}"
        return f"{{\\an5\\pos(540,{y})}}"
    rebond = "{\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)}"
    cle = _couleur_ass(COULEUR_CLE)

    def mot(t):
        t = _texte_ass(t)
        return t.upper() if s["majuscules"] else t

    for g in groupes:
        textes = [mot(w["texte"]) for w in g["mots"]]
        i_cle = mot_cle(g["mots"]) if anim else None
        if i_cle is not None:
            textes[i_cle] = f"{{\\c{cle}}}{textes[i_cle]}{{\\c{blanc}}}"
        if not s.get("surligne"):
            lignes.append(f"Dialogue: 0,{_temps_ass(g['debut'])},{_temps_ass(g['fin'])},Default,,0,0,0,,"
                          f"{position(g['debut'])}{rebond if anim else ''}{sep.join(textes)}")
            continue
        # Karaoké : un évènement par mot, le mot en cours prend la couleur de surlignage.
        jaune = _couleur_ass(s["surligne"])
        for k, w in enumerate(g["mots"]):
            a = g["debut"] if k == 0 else w["debut"]
            b = g["mots"][k + 1]["debut"] if k + 1 < len(g["mots"]) else g["fin"]
            if b - a < 0.01:
                continue
            brut = mot(w["texte"])
            morceaux = [f"{{\\c{jaune}}}{brut}{{\\c{blanc}}}" if i == k else t for i, t in enumerate(textes)]
            anime = rebond if anim and k == 0 else ""
            lignes.append(f"Dialogue: 0,{_temps_ass(a)},{_temps_ass(b)},Default,,0,0,0,,{position(g['debut'])}{anime}{sep.join(morceaux)}")

    if accroche:
        texte, duree = accroche
        yc = int(HAUTEUR * 0.40)
        lignes.append(f"Dialogue: 2,{_temps_ass(0)},{_temps_ass(duree)},Accroche,,0,0,0,,"
                      f"{{\\an5\\pos(540,{yc})\\fad(0,250)\\fscx70\\fscy70\\t(0,160,\\fscx106\\fscy106)"
                      f"\\t(160,260,\\fscx100\\fscy100)}}{_texte_ass(texte).upper()}")
    if barre_duree:
        ms = int(barre_duree * 1000)
        fond = "m 0 0 l 1080 0 1080 14 0 14"
        lignes.append(f"Dialogue: 3,{_temps_ass(0)},{_temps_ass(barre_duree)},Barre,,0,0,0,,"
                      f"{{\\an7\\pos(0,1906)\\c&HFFFFFF&\\1a&HB4&\\p1}}{fond}{{\\p0}}")
        lignes.append(f"Dialogue: 4,{_temps_ass(0)},{_temps_ass(barre_duree)},Barre,,0,0,0,,"
                      f"{{\\an7\\pos(0,1906)\\clip(0,1900,0,1920)\\t(0,{ms},\\clip(0,1900,1080,1920))\\p1}}{fond}{{\\p0}}")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


def _vers_sortie_groupes(groupes, debut, blocs):
    """Les sous-titres sont calculés en temps du clip source : on les recale sur le clip monté."""
    res = []
    for g in groupes:
        mots = [{**w, "debut": vers_sortie(debut + w["debut"], blocs), "fin": vers_sortie(debut + w["fin"], blocs)}
                for w in g["mots"]]
        a, b = vers_sortie(debut + g["debut"], blocs), vers_sortie(debut + g["fin"], blocs)
        if b - a >= 0.05:
            res.append({"debut": a, "fin": b, "mots": mots})
    return res


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------
def _px(r, sw, sh):
    """Rectangle normalisé -> crop FFmpeg en pixels pairs, toujours dans l'image."""
    w = max(2, min(sw, int(round(r[2] * sw / 2)) * 2))
    h = max(2, min(sh, int(round(r[3] * sh / 2)) * 2))
    x = max(0, min(sw - w, int(round(r[0] * sw))))
    y = max(0, min(sh - h, int(round(r[1] * sh))))
    return f"crop={w}:{h}:{x}:{y}"


def _fondus(graphe, duree, entree, sortie):
    """Ajoute un fondu au noir en début et/ou en fin de plan (la durée du plan ne change pas)."""
    if not (entree or sortie) or duree <= 0.05:
        return graphe
    d = min(FONDU, duree / 2)
    f = []
    if entree:
        f.append(f"fade=t=in:st=0:d={d:.3f}")
    if sortie:
        f.append(f"fade=t=out:st={max(0, duree - d):.3f}:d={d:.3f}")
    return graphe.replace("[v]", "," + ",".join(f) + "[v]").replace(",,", ",")


def filtre_plan(plan, sw, sh, duree=0.0, entree=False, sortie=False):
    """Graphe FFmpeg d'un plan (entrée [0:v], sortie [v] en 1080x1920)."""
    debut = f"[0:v]fps={FPS}:start_time=0,"
    fini = lambda g: _fondus(g, duree, entree, sortie)
    if plan["type"] == "partage":   # l'info en haut, le visage en bas
        moitie = HAUTEUR // 2
        return fini(debut + f"split=2[h][b];[h]{_px(plan['r'], sw, sh)},scale={LARGEUR}:{moitie},setsar=1[hh];"
                f"[b]{_px(plan['r2'], sw, sh)},scale={LARGEUR}:{moitie},setsar=1[bb];"
                f"[hh][bb]vstack=inputs=2,drawbox=x=0:y={moitie - 3}:w={LARGEUR}:h=6:color=black:t=fill[v]")
    if plan["type"] == "flou":      # le contenu entier, sur un fond flouté
        return fini(debut + f"split=2[bg][fg];[bg]scale={LARGEUR}:{HAUTEUR}:force_original_aspect_ratio=increase,"
                f"crop={LARGEUR}:{HAUTEUR},boxblur=25:5[bgb];[fg]{_px(plan['r'], sw, sh)},"
                f"scale={LARGEUR}:{HAUTEUR}:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    return fini(debut + f"{_px(plan['r'], sw, sh)},scale={LARGEUR}:{HAUTEUR},setsar=1[v]")


def chemin_unique(chemin):
    if not os.path.exists(chemin):
        return chemin
    base, ext = os.path.splitext(chemin)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _ffmpeg(cmd, dossier, suivi=None):
    """Lance FFmpeg ; suivi(secondes) reçoit l'avancement. Lève une erreur claire si ça rate."""
    journal = os.path.join(dossier, "ffmpeg.log")
    with open(journal, "a", encoding="utf-8", errors="replace") as err:
        try:
            proc = subprocess.Popen(cmd, cwd=dossier, stdout=subprocess.PIPE, stderr=err, text=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            raise RuntimeError("FFmpeg est introuvable. Réinstalle Studio Clips avec Studio-Clips.exe.") from None
        for ligne in proc.stdout:
            if suivi and (ligne.startswith("out_time_us=") or ligne.startswith("out_time_ms=")):
                try:
                    suivi(int(ligne.split("=", 1)[1]) / 1_000_000)
                except ValueError:
                    pass
        proc.wait()
    if proc.returncode != 0:
        with open(journal, encoding="utf-8", errors="replace") as f:
            print("Export FFmpeg échoué :", f.read()[-2500:], "\nCommande :", " ".join(cmd)[:3000], flush=True)
        raise RuntimeError("L'export n'a pas marché. Vérifie qu'il reste de la place sur le disque "
                           "et que la vidéo d'origine est toujours là, puis réessaie.")


def exporter_clip(source, segments, clip, sortie, progression=lambda pct: None, avec_son=True, vision=None):
    """Fabrique le MP4 final en suivant le plan de cadrage (le même que l'aperçu)."""
    import vision as _vision
    style = {**STYLE_DEFAUT, **(clip.get("style") or {})}
    montage = {**MONTAGE_DEFAUT, **(clip.get("montage") or {})}
    cadrage = {**CADRAGE_DEFAUT, **(clip.get("cadrage") or {})}
    passages = passages_du_clip(clip)
    debut = passages[0][0]
    blocs, jonctions = blocs_du_clip(segments, clip, montage)
    plans = _vision.plan_cadrage(blocs, segments, vision, cadrage, montage)
    for p in plans:   # plans calés sur la grille des images : les sous-titres ne glissent jamais
        p["de"] = debut + round((p["de"] - debut) * FPS) / FPS
        p["a"] = debut + round((p["a"] - debut) * FPS) / FPS
    plans = [p for p in plans if p["a"] - p["de"] >= 1 / FPS]
    sw, sh = (vision or {}).get("taille") or _vision.taille_video(source)
    # un fondu là où deux morceaux différents se rejoignent (jamais au milieu d'un passage)
    debuts_jonction = {blocs[i][0] for i in jonctions}
    fins_jonction = {blocs[i - 1][1] for i in jonctions}
    proche = lambda t, ens: any(abs(t - x) < 1.5 / FPS for x in ens)
    groupes = []
    for a, b in passages:   # les sous-titres ne se groupent jamais par-dessus une jonction
        groupes += _vers_sortie_groupes(
            groupes_sous_titres(mots_du_passage(segments, a, b), a, b, int(style["mots"])), a, blocs)

    dossier_tmp = tempfile.mkdtemp(prefix="studio_export_")
    try:
        # 1. Chaque plan, cadré à sa façon (image exacte, son en PCM : aucun décalage au recollage)
        images = [max(1, round((p["a"] - p["de"]) * FPS)) for p in plans]
        total = sum(images) / FPS
        tranches_st, tranches_ti, t0 = [], [], 0.0
        for plan, n in zip(plans, images):
            tranches_st.append((t0, t0 + n / FPS, plan.get("st")))
            tranches_ti.append((t0, t0 + n / FPS, plan.get("ti")))
            t0 += n / FPS
        duree_sortie, fait, liste = total, 0.0, []
        for i, (plan, n) in enumerate(zip(plans, images)):
            morceau = f"plan_{i:03d}.mkv"
            duree_plan = n / FPS
            fondu = montage.get("transitions", True) and len(passages) > 1
            entree = fondu and proche(plan["de"], debuts_jonction)
            sortie_f = fondu and proche(plan["a"], fins_jonction)
            graphe = filtre_plan(plan, sw, sh, duree_plan, entree, sortie_f)
            if avec_son:
                d = min(0.12, duree_plan / 2)
                sons = [f"[0:a]atrim=0:{duree_plan:.6f}", "asetpts=PTS-STARTPTS", "aresample=48000"]
                if entree:
                    sons.append(f"afade=t=in:st=0:d={d:.3f}")
                if sortie_f:
                    sons.append(f"afade=t=out:st={max(0, duree_plan - d):.3f}:d={d:.3f}")
                graphe += ";" + ",".join(sons) + "[a]"
            _ffmpeg(["ffmpeg", "-y", "-ss", f"{plan['de']:.3f}", "-i", os.path.abspath(source),
                     "-filter_complex", graphe, "-map", "[v]", "-frames:v", str(n),
                     *(["-map", "[a]", "-c:a", "pcm_s16le"] if avec_son else []),
                     # intermédiaire : le plus rapide possible, la qualité est gardée par un crf bas
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p",
                     "-progress", "pipe:1", "-nostats", morceau], dossier_tmp,
                    lambda t, f0=fait: progression(min(74, int((f0 + t) / max(total, 0.1) * 75))))
            fait += n / FPS
            liste.append(f"file '{morceau}'")
        with open(os.path.join(dossier_tmp, "liste.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(liste) + "\n")

        # 2. Recollage + sous-titres, accroche, barre, titre
        texte = {**TEXTE_DEFAUT, **(clip.get("texte") or {})}
        titre_ass = ligne_titre(texte, clip.get("titre"), clip.get("numero"), duree_sortie, tranches_ti)
        accroche = None
        if montage["accroche"]:
            accroche = ((texte.get("contenu") or "").strip() or clip.get("titre") or "", min(2.5, duree_sortie))
        ecrire_ass(groupes, style, os.path.join(dossier_tmp, "subs.ass"), titre_ass, texte["taille"],
                   clip.get("langue", "fr"), anim=montage["anim"], accroche=accroche if accroche and accroche[0] else None,
                   barre_duree=duree_sortie if montage["barre"] else None, tranches=tranches_st)
        musique = (montage.get("musique") or "").strip()
        musique = musique if avec_son and musique and os.path.exists(musique) else ""
        if musique:
            vol = max(0.0, min(1.0, float(montage.get("musique_volume", 0.35))))
            # La musique est d'abord RAMENÉE À UN NIVEAU CONNU : sans ça, un morceau doux devient
            # inaudible et un morceau fort écrase la voix. Le réglage choisit ce niveau (-38 à -22 LUFS),
            # la voix finissant vers -16 : la musique reste dessous. Puis elle baisse encore dès qu'on parle.
            cible = -38 + 16 * vol
            filtre_son = (f"[1:a]aloop=loop=-1:size=2000000000,atrim=0:{duree_sortie:.3f},asetpts=PTS-STARTPTS,"
                          f"loudnorm=I={cible:.0f}:TP=-3:LRA=7,"
                          f"afade=t=out:st={max(0, duree_sortie - 1.2):.3f}:d=1.2[mus];"
                          "[0:a]asplit=2[voix1][voix2];"
                          "[mus][voix2]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[musb];"
                          "[voix1][musb]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")

        provisoire = os.path.splitext(os.path.abspath(sortie))[0] + ".encodage.mp4"
        try:
            son = (["-filter_complex", filtre_son, "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
                   if musique else
                   ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k"] if avec_son else ["-an"])
            _ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "liste.txt",
                     *(["-i", os.path.abspath(musique)] if musique else []),
                     "-vf", "ass=subs.ass",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
                     # son remonté au niveau attendu par TikTok (-14 LUFS) : sinon le clip paraît étouffé
                     *son, "-movflags", "+faststart",
                     "-progress", "pipe:1", "-nostats", provisoire], dossier_tmp,
                    lambda t: progression(75 + min(24, int(t / max(duree_sortie, 0.1) * 25))))
        except RuntimeError:
            try:
                os.remove(provisoire)
            except OSError:
                pass
            raise
        os.replace(provisoire, os.path.abspath(sortie))
        couverture(os.path.abspath(sortie), min(1.2, duree_sortie / 3))
        progression(100)
    finally:
        shutil.rmtree(dossier_tmp, ignore_errors=True)


def couverture(clip_mp4, instant=1.2):
    """Image de couverture du clip (à côté du MP4) : pratique pour la publier sur TikTok."""
    image = os.path.splitext(clip_mp4)[0] + ".jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.1, instant):.2f}", "-i", clip_mp4,
                        "-frames:v", "1", "-q:v", "3", image],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        return image if os.path.exists(image) else ""
    except OSError:
        return ""


def dimensions(video):
    """(largeur, hauteur) d'un fichier, pour vérifier qu'un export est bien vertical."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "csv=p=0:s=x", video]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        l, h = r.stdout.strip().split("x")[:2]
        return int(l), int(h)
    except (FileNotFoundError, ValueError):
        return None


def miniature(source, instant, sortie):
    cmd = ["ffmpeg", "-y", "-ss", f"{max(0, instant):.2f}", "-i", source, "-frames:v", "1",
           "-vf", "scale=320:-2", "-q:v", "4", sortie]
    try:
        r = subprocess.run(cmd, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return False
    return r.returncode == 0 and os.path.exists(sortie)
