"""
Moteur de montage : sous-titres, cadrage et export MP4 d'un clip.

L'aperçu dans l'interface est calculé en direct par le navigateur à partir
des MÊMES réglages (style, cadrage, bornes). FFmpeg n'est lancé qu'à
l'export : c'est ce qui rend le montage instantané, même sur un PC modeste.

Les sous-titres sont écrits en .ass sur une base 1080x1920 : toutes les
tailles ci-dessous sont en vrais pixels de la vidéo finale.
"""

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
CADRAGE_DEFAUT = {"mode": "remplir", "decalage": 0}   # remplir | flou ; décalage -50..50
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


def ligne_titre(texte, titre, numero, duree):
    """Évènement ASS du titre incrusté (boîte sombre en haut), ou None."""
    t = {**TEXTE_DEFAUT, **(texte or {})}
    if not (t["titre"] or t["partie"]) or duree <= 0:
        return None
    y = int(HAUTEUR * max(3, min(60, float(t["position"]))) / 100)
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
    return (f"Dialogue: 1,{_temps_ass(0)},{_temps_ass(duree)},Titre,,0,0,0,,"
            f"{{\\an8\\pos(540,{y})}}" + "\\N".join(morceaux))


LANGUES_SANS_ESPACES = ("zh", "ja", "ko", "th", "yue", "lo", "my")


def ecrire_ass(groupes, style, chemin, titre_ass=None, taille_titre=None, langue="fr"):
    s = {**STYLE_DEFAUT, **(style or {})}
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
        "&H26000000,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,8,90,90,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lignes = [entete]
    if titre_ass:
        lignes.append(titre_ass)
    pos = f"{{\\an5\\pos(540,{y})}}"

    def mot(t):
        t = _texte_ass(t)
        return t.upper() if s["majuscules"] else t

    for g in groupes:
        textes = [mot(w["texte"]) for w in g["mots"]]
        if not s.get("surligne"):
            lignes.append(f"Dialogue: 0,{_temps_ass(g['debut'])},{_temps_ass(g['fin'])},Default,,0,0,0,,"
                          f"{pos}{sep.join(textes)}")
            continue
        # Karaoké : un évènement par mot, le mot en cours prend la couleur de surlignage.
        jaune = _couleur_ass(s["surligne"])
        for k, w in enumerate(g["mots"]):
            a = g["debut"] if k == 0 else w["debut"]
            b = g["mots"][k + 1]["debut"] if k + 1 < len(g["mots"]) else g["fin"]
            if b - a < 0.01:
                continue
            morceaux = [
                f"{{\\c{jaune}}}{t}{{\\c{blanc}}}" if i == k else t for i, t in enumerate(textes)
            ]
            lignes.append(f"Dialogue: 0,{_temps_ass(a)},{_temps_ass(b)},Default,,0,0,0,,{pos}{sep.join(morceaux)}")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------
def _filtre_video(cadrage):
    c = {**CADRAGE_DEFAUT, **(cadrage or {})}
    if c["mode"] == "remplir":
        f = 0.5 + max(-50, min(50, float(c["decalage"]))) / 100
        return (f"scale={LARGEUR}:{HAUTEUR}:force_original_aspect_ratio=increase,"
                f"crop={LARGEUR}:{HAUTEUR}:(iw-{LARGEUR})*{f:.3f}:(ih-{HAUTEUR})/2,setsar=1[v0]")
    return ";".join([
        "split=2[bg][fg]",
        f"[bg]scale={LARGEUR}:{HAUTEUR}:force_original_aspect_ratio=increase,crop={LARGEUR}:{HAUTEUR},boxblur=25:5[bgb]",
        f"[fg]scale={LARGEUR}:{HAUTEUR}:force_original_aspect_ratio=decrease[fgs]",
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]",
    ])


def chemin_unique(chemin):
    if not os.path.exists(chemin):
        return chemin
    base, ext = os.path.splitext(chemin)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def exporter_clip(source, segments, clip, sortie, progression=lambda pct: None):
    """Fabrique le MP4 final. progression(pct) est appelée pendant l'encodage."""
    debut, fin = float(clip["debut"]), float(clip["fin"])
    duree = fin - debut
    style = {**STYLE_DEFAUT, **(clip.get("style") or {})}
    groupes = groupes_sous_titres(mots_du_passage(segments, debut, fin), debut, fin, int(style["mots"]))

    dossier_tmp = tempfile.mkdtemp(prefix="studio_export_")
    try:
        texte = {**TEXTE_DEFAUT, **(clip.get("texte") or {})}
        titre_ass = ligne_titre(texte, clip.get("titre"), clip.get("numero"), duree)
        ecrire_ass(groupes, style, os.path.join(dossier_tmp, "subs.ass"), titre_ass, texte["taille"],
                   clip.get("langue", "fr"))
        # On encode dans un fichier provisoire : un export raté ne laisse jamais de MP4 coupé.
        provisoire = os.path.splitext(os.path.abspath(sortie))[0] + ".encodage.mp4"
        # FFmpeg tourne DEPUIS le dossier temporaire : le nom du .ass est donné seul,
        # donc aucun souci d'échappement (apostrophes, « : », espaces dans les chemins).
        filtre = "[0:v]" + _filtre_video(clip.get("cadrage")) + ";[v0]ass=subs.ass[vout]"
        cmd = [
            "ffmpeg", "-y", "-ss", f"{debut:.3f}", "-i", os.path.abspath(source), "-t", f"{duree:.3f}",
            "-filter_complex", filtre, "-map", "[vout]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", provisoire,
        ]
        journal = os.path.join(dossier_tmp, "ffmpeg.log")
        with open(journal, "w", encoding="utf-8", errors="replace") as err:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=dossier_tmp, stdout=subprocess.PIPE, stderr=err, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError:
                raise RuntimeError("FFmpeg est introuvable : relance INSTALLER.bat.") from None
            for ligne in proc.stdout:
                if ligne.startswith("out_time_us=") or ligne.startswith("out_time_ms="):
                    try:
                        t = int(ligne.split("=", 1)[1]) / 1_000_000
                        progression(min(99, int(t / duree * 100)))
                    except ValueError:
                        pass
            proc.wait()
        if proc.returncode != 0:
            with open(journal, encoding="utf-8", errors="replace") as f:
                print("Export FFmpeg échoué :", f.read()[-2000:], flush=True)
            try:
                os.remove(provisoire)
            except OSError:
                pass
            raise RuntimeError("L'export n'a pas marché. Vérifie qu'il reste de la place sur le disque "
                               "et que la vidéo d'origine est toujours là, puis réessaie.")
        os.replace(provisoire, os.path.abspath(sortie))
        progression(100)
    finally:
        shutil.rmtree(dossier_tmp, ignore_errors=True)


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
