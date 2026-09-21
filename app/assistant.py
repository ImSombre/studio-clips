"""
L'assistant de montage du chat.

Deux étages :
1. Un lecteur de commandes RAPIDE (sans IA) pour les demandes courantes
   (« coupe les 5 premières secondes », « sous-titres jaunes en haut »...) :
   réponse instantanée, fiable même sur un PC modeste.
2. Sinon, l'IA locale (Ollama) : elle répond librement ET peut renvoyer des
   actions de montage, qui sont vérifiées avant d'être appliquées.
"""

import copy
import json
import re
import unicodedata

import requests

import modele_ia
from analyze import OLLAMA_URL, NUM_CTX
import montage as _montage
from montage import STYLE_DEFAUT, CADRAGE_DEFAUT, TEXTE_DEFAUT, MONTAGE_DEFAUT, POLICES, mots_du_passage

CE_QUE_JE_SAIS_FAIRE = (
    "Voilà ce que je sais faire :\n"
    "• couper / rallonger : « coupe les 3 premières secondes », « rajoute 2 s à la fin », « fais-le durer 1 min »\n"
    "• sous-titres : « en jaune », « plus gros », « en haut », « 2 mots à la fois », « karaoké en rose », « sans majuscules »\n"
    "• titre à l'écran : « mets le titre et le numéro de partie », « enlève le titre »\n"
    "• cadrage : « plein écran », « fond flou », « cadre plus à gauche »\n"
    "• montage : « enlève les zooms », « garde les blancs », « sans barre », « sans accroche », « sous-titres sans animation »\n"
    "• titres : « trouve-moi un titre », « donne un titre à tous les clips », « renomme le clip en … »\n"
    "• plusieurs morceaux dans un clip : « assemble les meilleurs moments », « ajoute le passage de 3:10 à 3:40 », "
    "« enlève le 2e passage », « sans fondus »\n"
    "• « trouve-moi un passage drôle », « exporte », « annule »\n"
    "Ajoute « sur tous les clips » pour appliquer partout."
)
MOTS_TOUS = r"\b(tous|toutes|toute les|chaque|partout|les autres)\b"

COULEURS = {
    "blanc": "#FFFFFF", "noir": "#000000", "jaune": "#FFE14D", "rouge": "#FF3B30", "vert": "#34E07A",
    "bleu": "#3BA7FF", "rose": "#FF5FA2", "orange": "#FF8A1F", "violet": "#A974FF", "cyan": "#2EE6F0",
    "turquoise": "#2EE6D0", "gris": "#B8B8B8", "dore": "#FFC933", "or": "#FFC933",
}
HISTORIQUE_MAX = 40
DUREE_MIN_CLIP = 3.0


def _mmss(t):
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def _instant(texte):
    """« 3:10 », « 3 min 10 », « 190 s » -> secondes, ou None."""
    m = re.match(r"\s*(\d{1,3})\s*[:hm]\s*(\d{1,2})", texte)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(s|sec\w*)?", texte)
    return float(m.group(1).replace(",", ".")) if m else None


def sans_accents(t):
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def lire_durees(texte):
    """« 1 min a 1min05 » -> [60, 65] ; « 45 s » -> [45] ; « 1m30 » -> [90]."""
    t = sans_accents(texte)
    durees = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(min(?:utes?)?|mn|m)\s*(\d{1,2})?(?!\d)|(\d+(?:[.,]\d+)?)\s*(s|sec|secondes?)\b", t):
        if m.group(1):
            durees.append(float(m.group(1).replace(",", ".")) * 60 + float(m.group(3) or 0))
        else:
            durees.append(float(m.group(4).replace(",", ".")))
    return durees


def fourchette_duree(consigne):
    """Durée de clip demandée dans la consigne d'accueil, ou None."""
    t = sans_accents(consigne or "")
    if not re.search(r"clip|video|tiktok|short|dur|long|format|fai[st]", t):
        return None
    d = [x for x in lire_durees(t) if 10 <= x <= 180]
    if not d:
        return None
    if len(d) == 1:
        return max(10, d[0] - 3), d[0] + 3
    return min(d[:2]), max(d[:2])


# ----------------------------------------------------------------------
# Application des actions (commune aux deux étages)
# ----------------------------------------------------------------------
def _fin_sur_un_mot(segments, debut, fin_visee):
    """Recale une fin sur la fin du dernier mot prononcé (évite de couper un mot en deux)."""
    mots = mots_du_passage(segments, debut, fin_visee + 0.01)
    fins = [w["end"] for w in mots if w["end"] <= fin_visee + 0.25]
    if fins and fin_visee - max(fins) < 1.5:
        return round(max(fins) + 0.15, 2)
    return round(fin_visee, 2)


def memoriser(clip):
    etat = {k: copy.deepcopy(v) for k, v in clip.items() if k != "historique"}
    clip.setdefault("historique", []).append(etat)
    del clip["historique"][:-HISTORIQUE_MAX]


def annuler(clip):
    if not clip.get("historique"):
        return False
    precedent = clip["historique"].pop()
    for k, v in precedent.items():
        clip[k] = v
    return True


def poser_passages(clip, liste, duree_video=None):
    """Range les morceaux assemblés du clip (tri, fusion de ceux qui se touchent) et recale debut/fin."""
    propre = []
    for x in sorted((float(a), float(b)) for a, b in liste):
        a, b = x
        if duree_video:
            a, b = max(0.0, min(a, duree_video)), min(b, duree_video)
        if b - a < DUREE_MIN_CLIP:
            continue
        if propre and a <= propre[-1][1] + 0.05:
            propre[-1][1] = max(propre[-1][1], round(b, 2))
        else:
            propre.append([round(a, 2), round(b, 2)])
    if not propre:
        return False
    clip["passages"] = propre
    clip["debut"], clip["fin"] = propre[0][0], propre[-1][1]
    return True


def appliquer(projet, clip, actions):
    """Applique des actions sur le clip. Retourne (descriptions, speciales)."""
    duree_video = projet.get("duree") or 10 ** 9
    segments = projet.get("segments", [])
    fait, speciales = [], []
    memorise = False

    def avant_modif():
        nonlocal memorise
        if not memorise:
            memoriser(clip)
            memorise = True

    def bornes(debut, fin, recaler=False):
        debut = max(0.0, min(float(debut), duree_video - DUREE_MIN_CLIP))
        fin = max(debut + DUREE_MIN_CLIP, min(float(fin), duree_video))
        if recaler:
            fin = max(debut + DUREE_MIN_CLIP, _fin_sur_un_mot(segments, debut, fin))
        ps = _montage.passages_du_clip(clip)
        if len(ps) > 1:
            ps[0] = (min(debut, ps[0][1] - DUREE_MIN_CLIP), ps[0][1])
            ps[-1] = (ps[-1][0], max(fin, ps[-1][0] + DUREE_MIN_CLIP))
            poser_passages(clip, ps, duree_video)
            return
        clip["debut"], clip["fin"] = round(debut, 2), round(fin, 2)
        if clip.get("passages"):
            clip["passages"] = [[clip["debut"], clip["fin"]]]

    for a in actions:
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type", "")).lower()
        try:
            if typ == "couper_debut":
                s = float(a["secondes"])
                avant_modif()
                bornes(clip["debut"] + s, clip["fin"])
                fait.append(f"{abs(s):g} s {'coupées' if s >= 0 else 'ajoutées'} au début")
            elif typ == "couper_fin":
                s = float(a["secondes"])
                avant_modif()
                bornes(clip["debut"], clip["fin"] - s)
                fait.append(f"{abs(s):g} s {'coupées' if s >= 0 else 'ajoutées'} à la fin")
            elif typ == "bornes":
                avant_modif()
                bornes(a.get("debut", clip["debut"]), a.get("fin", clip["fin"]))
                fait.append("passage recadré")
            elif typ == "duree":
                s = float(a["secondes"])
                avant_modif()
                # avec la coupe des blancs, il faut prendre plus large pour garder la durée voulue
                fin_visee = _montage.fin_pour_duree(segments, clip["debut"], s, clip.get("montage"), duree_video)
                bornes(clip["debut"], fin_visee, recaler=True)
                fait.append(f"durée réglée sur {s:g} s")
            elif typ == "passages":
                avant_modif()
                if poser_passages(clip, a.get("liste") or [], duree_video):
                    fait.append(f"{len(clip['passages'])} morceau(x) assemblé(s)")
            elif typ == "ajouter_passage":
                avant_modif()
                d, f = float(a.get("debut", 0)), float(a.get("fin", 0))
                if f - d >= DUREE_MIN_CLIP and poser_passages(clip, _montage.passages_du_clip(clip) + [(d, f)], duree_video):
                    fait.append(f"passage {_mmss(d)} → {_mmss(f)} ajouté")
            elif typ == "retirer_passage":
                avant_modif()
                ps = _montage.passages_du_clip(clip)
                i = a.get("index")
                if i is None and a.get("instant") is not None:   # « enlève le passage de 2:30 »
                    t_ = float(a["instant"])
                    i = min(range(len(ps)), key=lambda k: abs((ps[k][0] + ps[k][1]) / 2 - t_)) if ps else None
                i = len(ps) - 1 if i in (None, -1) else int(i)
                if len(ps) > 1 and 0 <= i < len(ps):
                    retire = ps.pop(i)
                    poser_passages(clip, ps, duree_video)
                    fait.append(f"passage {_mmss(retire[0])} → {_mmss(retire[1])} retiré")
            elif typ == "sous_titres":
                avant_modif()
                st = {**STYLE_DEFAUT, **clip.get("style", {})}
                if "couleur" in a and a["couleur"]:
                    st["couleur"] = _hex(a["couleur"]) or st["couleur"]
                if "contour" in a and a["contour"]:
                    st["contour"] = _hex(a["contour"]) or st["contour"]
                if "surligne" in a:
                    v = a["surligne"]
                    st["surligne"] = "" if v in (None, "", "aucun", False) else (_hex(v) or st["surligne"])
                if "taille" in a:
                    st["taille"] = max(36, min(150, int(float(a["taille"]))))
                if "position" in a:
                    p = a["position"]
                    p = {"haut": 22, "milieu": 50, "centre": 50, "bas": 78}.get(str(p).lower(), p)
                    st["position"] = max(8, min(92, float(p)))
                if "majuscules" in a:
                    st["majuscules"] = bool(a["majuscules"])
                if "mots" in a:
                    st["mots"] = max(1, min(8, int(float(a["mots"]))))
                if "police" in a:
                    trouve = [p for p in POLICES if sans_accents(p).startswith(sans_accents(str(a["police"]))[:4])]
                    if trouve:
                        st["police"] = trouve[0]
                clip["style"] = st
                fait.append("sous-titres déplacés" if set(a) - {"type"} == {"position"} else "sous-titres modifiés")
            elif typ == "cadrage":
                avant_modif()
                c = {**CADRAGE_DEFAUT, **clip.get("cadrage", {})}
                if a.get("mode") in ("flou", "remplir"):
                    c["mode"] = a["mode"]
                if "decalage" in a:
                    c["decalage"] = max(-50, min(50, float(a["decalage"])))
                if "auto" in a:
                    c["auto"] = bool(a["auto"])
                elif a.get("mode") in ("flou", "remplir") or "decalage" in a:
                    c["auto"] = False   # un cadrage demandé explicitement reste fixe
                clip["cadrage"] = c
                if "auto" in a and len(a) <= 2:
                    fait.append("cadrage intelligent activé" if c["auto"] else "cadrage fixe")
                else:
                    fait.append("plein écran" if c["mode"] == "remplir" else "fond flouté")
            elif typ == "montage":
                avant_modif()
                mt = {**MONTAGE_DEFAUT, **clip.get("montage", {})}
                noms = {"coupes": "blancs coupés", "zooms": "zooms", "anim": "sous-titres animés",
                        "accroche": "accroche", "barre": "barre de progression", "transitions": "fondus"}
                for cle, nom in noms.items():
                    if cle in a:
                        mt[cle] = bool(a[cle])
                        if cle == "coupes":
                            fait.append("blancs coupés" if mt[cle] else "blancs gardés")
                        else:
                            fait.append(f"{nom} {'activé(e)' if mt[cle] else 'retiré(e)'}")
                clip["montage"] = mt
            elif typ == "texte_ecran":
                avant_modif()
                tx = {**TEXTE_DEFAUT, **clip.get("texte", {})}
                for champ in ("titre", "partie"):
                    if champ in a:
                        tx[champ] = bool(a[champ])
                if "position" in a:
                    p = a["position"]
                    p = {"haut": 11, "milieu": 40, "centre": 40}.get(str(p).lower(), p)
                    tx["position"] = max(3, min(60, float(p)))
                if "taille" in a:
                    tx["taille"] = max(30, min(110, int(float(a["taille"]))))
                if "contenu" in a:
                    tx["contenu"] = str(a["contenu"] or "").strip()[:120]
                    if tx["contenu"]:
                        tx["titre"] = True
                if "numero" in a:
                    tx["numero"] = None if a["numero"] in (None, "", "auto") else max(1, int(float(a["numero"])))
                    if tx["numero"]:
                        tx["partie"] = True
                clip["texte"] = tx
                if "contenu" in a:
                    fait.append(f"texte à l'écran : « {tx['contenu'] or clip.get('titre', '')} »")
                if "numero" in a:
                    fait.append(f"numéro de partie : {tx['numero'] or 'automatique'}")
                mis = [x for x, cle in (("titre", "titre"), ("numéro de partie", "partie")) if a.get(cle) is True]
                retires = [x for x, cle in (("titre", "titre"), ("numéro de partie", "partie")) if a.get(cle) is False]
                if mis and not (clip.get("historique") and clip["historique"][-1].get("texte", {}).get(mis[0].split()[0])):
                    fait.append(" + ".join(mis) + " affiché en haut de la vidéo")
                if retires:
                    fait.append(" + ".join(retires) + " retiré de la vidéo")
                if not mis and not retires:
                    fait.append("titre déplacé" if "position" in a else "titre à l'écran réglé")
            elif typ == "titre" and str(a.get("texte", "")).strip():
                avant_modif()
                clip["titre"] = str(a["texte"]).strip()[:80]
                fait.append(f"renommé « {clip['titre']} »")
            elif typ in ("tous", "aide", "proposer_titres", "titrer_tous", "assembler"):
                speciales.append(typ)
            elif typ == "annuler":
                speciales.append("annuler")
            elif typ == "exporter":
                speciales.append("exporter")
            elif typ == "nouveau_clip":
                speciales.append(("nouveau_clip", str(a.get("consigne", "")).strip()))
        except (KeyError, ValueError, TypeError):
            continue
    return fait, speciales


def _hex(v):
    v = str(v).strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
        return "#" + v.lstrip("#").upper()
    return COULEURS.get(sans_accents(v))


# ----------------------------------------------------------------------
# Étage 1 : lecteur de commandes rapide
# ----------------------------------------------------------------------
def lecture_rapide(message, historique=None):
    t = sans_accents(message).strip()
    actions = []

    if re.fullmatch(r"(annule|annuler|reviens en arriere|retour arriere|ctrl ?z|undo)\W*", t):
        return [{"type": "annuler"}]
    if re.search(r"(qu.?est.?ce que tu (sais|peux) faire|tu sais faire quoi|tu peux faire quoi|^aide\W*$|^help\W*$|quelles? commandes?)", t):
        return [{"type": "aide"}]
    # « trouve-moi un titre », « propose des titres », « donne un titre à tous les clips »
    if re.search(r"\b(trouve|propose|donne|invente|genere|fais|ecris|idee)\w*\b.{0,25}\btitres?\b|\btitres?\b.{0,15}\b(accrocheur|stylé|style|viral)", t) \
            and not re.search(r"\ben haut\b|a l.?ecran|sur la video", t):
        if re.search(MOTS_TOUS, t):
            return [{"type": "titrer_tous"}]
        return [{"type": "proposer_titres"}]
    if re.search(r"\b(exporte|exporter|enregistre|telecharge|sors?[- ]moi le (clip|mp4))\b", t):
        return [{"type": "exporter"}]
    if re.search(r"\b(un autre|nouveau|nouvel|trouve[- ]moi|cherche[- ]moi|cherche)\s+(\w+\s+){0,3}?(clip|passage|extrait|moment)", t):
        return [{"type": "nouveau_clip", "consigne": message}]

    # « ajoute le passage de 3:10 à 3:40 », « assemble les meilleurs moments », « enlève le 2e passage »
    m = re.search(r"(ajoute|rajoute|colle|met[s]?)\w*\b[^0-9]{0,30}(\d{1,3}\s*[:hm]\s*\d{1,2}|\d+(?:[.,]\d+)?\s*s?)"
                  r"\s*(?:a|à|jusqu.?a|->|-)\s*(\d{1,3}\s*[:hm]\s*\d{1,2}|\d+(?:[.,]\d+)?\s*s?)", t)
    if m and re.search(r"passage|morceau|bout|extrait|moment", t):
        d, f = _instant(m.group(2)), _instant(m.group(3))
        if d is not None and f is not None and f > d:
            actions.append({"type": "ajouter_passage", "debut": d, "fin": f})
    if re.search(r"assemble|best.?of|meilleurs? (moments|passages)|plusieurs (passages|morceaux|extraits)|"
                 r"resume(r|z)? la video|compile", t):
        actions.append({"type": "assembler"})
    m = re.search(r"(enleve|retire|supprime|vire)\w*\D{0,12}(\d{1,2})?\w*\s*(passage|morceau)", t)
    if m:
        actions.append({"type": "retirer_passage", "index": int(m.group(2)) - 1 if m.group(2) else None})
    elif re.search(r"(enleve|retire|supprime|vire)\w* (le |les )?(dernier|derniers?) (passage|morceau)", t):
        actions.append({"type": "retirer_passage", "index": None})

    nombre = r"(\d+(?:[.,]\d+)?)"
    m = re.search(rf"(coupe|enleve|retire|supprime|vire|raccourci\w*)\D*?{nombre}\s*(s|sec\w*)?\b.*?(premi|debut|avant)", t)
    if m:
        actions.append({"type": "couper_debut", "secondes": float(m.group(2).replace(",", "."))})
    m = re.search(rf"(coupe|enleve|retire|supprime|vire|raccourci\w*)\D*?{nombre}\s*(s|sec\w*)?\b.*?(derni|fin|apres)", t)
    if m:
        actions.append({"type": "couper_fin", "secondes": float(m.group(2).replace(",", "."))})
    m = re.search(rf"(rajoute|ajoute|allonge|commence)\D*?{nombre}\s*(s|sec\w*)?\b.*?(avant|plus tot|debut)", t)
    if m:
        actions.append({"type": "couper_debut", "secondes": -float(m.group(2).replace(",", "."))})
    m = re.search(rf"(rajoute|ajoute|allonge|termine|finis)\D*?{nombre}\s*(s|sec\w*)?\b.*?(apres|plus tard|fin)", t)
    if m:
        actions.append({"type": "couper_fin", "secondes": -float(m.group(2).replace(",", "."))})
    if re.search(r"\b(dure|durer|duree|longueur|fais[- ]le|mets[- ]le|clips? de|videos? de)\b", t) and not actions:
        d = lire_durees(t)
        if d:
            actions.append({"type": "duree", "secondes": d[-1] if len(d) == 1 else sum(d[:2]) / 2})

    st = {}
    parle_texte = re.search(r"sous[- ]?titre|texte|ecrit|police|lettre|mot", t)
    couleurs = [c for c in COULEURS if re.search(rf"\b{c}s?\b", t)]
    if re.search(r"(sans|pas de|enleve|retire|vire).{0,12}(surlign|karaok)", t):
        st["surligne"] = "aucun"
    elif couleurs and re.search(r"surlign|karaok|mot (actif|en cours)", t):
        st["surligne"] = COULEURS[couleurs[-1]]
    elif couleurs and re.search(r"contour|bord", t):
        st["contour"] = COULEURS[couleurs[-1]]
    elif couleurs and (parle_texte or len(t) < 40):
        st["couleur"] = COULEURS[couleurs[0]]
    if re.search(r"plus (gros|grand|gras)|agrandi|en plus gros", t):
        st["taille_rel"] = 12
    if re.search(r"plus petit|reduis|moins gros|en plus petit", t):
        st["taille_rel"] = -12
    if re.search(r"\ben haut\b|\btout en haut\b", t):
        st["position"] = "haut"
    elif re.search(r"\bau (milieu|centre)\b", t):
        st["position"] = "milieu"
    elif re.search(r"\ben bas\b", t) and parle_texte:
        st["position"] = "bas"
    if re.search(r"(pas|plus|sans) (de |en )?majuscule|minuscule", t):
        st["majuscules"] = False
    elif re.search(r"majuscule|en caps", t):
        st["majuscules"] = True
    m = re.search(r"\b(\d)\s*mots?\b", t)
    if m:
        st["mots"] = int(m.group(1))
    for p in POLICES:
        if sans_accents(p).split()[0] in t and parle_texte:
            st["police"] = p
    if st:
        actions.append({"type": "sous_titres", **st})

    if re.search(r"plein ecran|remplis?|sans (le )?flou|enleve (le )?flou|coupe les bords|zoome", t):
        actions.append({"type": "cadrage", "mode": "remplir"})
    elif re.search(r"fond flou|avec (le )?flou|remets? (le )?flou|video entiere|bandes", t):
        actions.append({"type": "cadrage", "mode": "flou"})
    if re.search(r"cadrage fixe|(enleve|desactive|coupe|arrete)\w* (le )?cadrage (auto|intelligent|malin)", t):
        actions.append({"type": "cadrage", "auto": False})
    elif re.search(r"cadrage (auto|intelligent|malin)|suis (le|la) (visage|personne|tete)|zoome? sur (les )?(infos?|articles?|ecran)", t):
        actions.append({"type": "cadrage", "auto": True})
    m = re.search(r"(cadre|decale|recentre)\w*.*?\b(gauche|droite)\b", t)
    if m:
        actions.append({"type": "cadrage", "mode": "remplir", "decalage_rel": -15 if m.group(2) == "gauche" else 15})

    brut = message.strip()
    renomme = (
        re.search(r"\b(?:renomme|appelle|intitule)\w*\s+(?:le\s+)?(?:clip\s+)?(?:en\s+)?[\"«']?\s*(.+?)\s*[\"»']?$", brut, re.I)
        or re.search(r"\b(?:change|modifie|mets?|remplace)\s+(?:le\s+)?titre\s+(?:en|par|à|a|:)\s*[\"«']?\s*(.+?)\s*[\"»']?$", brut, re.I)
        or re.search(r"^titre\s*:\s*[\"«']?\s*(.+?)\s*[\"»']?$", brut, re.I)
    )
    if renomme and len(renomme.group(1)) > 1:
        actions.append({"type": "titre", "texte": renomme.group(1)})

    # Texte libre affiché sur la vidéo : « écris Le dark web en haut », « le texte à l'écran c'est … »
    # On cherche sur la version sans accents (même longueur), et on découpe le texte D'ORIGINE.
    plat = sans_accents(brut)
    ecrire = (re.search(r"\b(?:ecris|ecrit|marque|affiche)\s+[\"«']?(.+?)[\"»']?\s*(?:en haut|a l.?ecran|sur la video|comme titre)\s*$", plat)
              or re.search(r"\b(?:le )?texte (?:a l.?ecran|en haut|affiche)\s*(?:c.?est|:|=)\s*[\"«']?(.+?)[\"»']?\s*$", plat)
              or re.search(r"\b(?:ecris|ecrit|marque|affiche)\s+(?:en haut|a l.?ecran|sur la video)\s*:?\s*[\"«']?(.+?)[\"»']?\s*$", plat))
    if ecrire and len(ecrire.group(1).strip()) > 1:
        a, b = ecrire.span(1)
        actions.append({"type": "texte_ecran", "contenu": brut[a:b].strip(" \"«»'"), "titre": True})
    m = re.search(r"\bpartie\s*(?:n\s*°?\s*)?(\d{1,2})\b", t)
    if m and re.search(r"\b(mets?|met|change|numero|partie)\b", t) and not ecrire:
        actions.append({"type": "texte_ecran", "numero": int(m.group(1)), "partie": True})
    elif re.search(r"\bnumero\w*\s+(auto|automatique)", t):
        actions.append({"type": "texte_ecran", "numero": None})

    # Titre / numéro de partie incrustés sur la vidéo
    parle_titre = re.search(r"(?<!sous-)(?<!sous )\btitres?\b", t) and not renomme and not ecrire
    parle_partie = re.search(r"numero|\bparties?\b|episode|n ?°", t)
    if parle_titre or parle_partie:
        a = {"type": "texte_ecran"}
        valeur = not re.search(r"(enleve|retire|vire|supprime|cache|sans)\s+(le |les |la |l'|un |des )?(titre|numero|partie|episode)", t)
        if parle_titre:
            a["titre"] = valeur
        if parle_partie:
            a["partie"] = valeur
        if re.search(r"\bplus (gros|grand)\b", t):
            a["taille"] = 70
        actions = [x for x in actions if not (x["type"] == "sous_titres" and set(x) <= {"type", "taille_rel"})]
        actions.append(a)
    elif not actions and (re.search(r"\b(mets?|affiche|ajoute|rajoute)\b.*\b(sur (la|les) videos?|a l.?ecran|dessus)\b", t)
                          or re.search(r"\b(ya|y a|il y a)\s+(tjr|toujours)\s+pas\b", t)):
        # « mets-la sur la vidéo » : on regarde de quoi on parlait juste avant
        recent = sans_accents(" ".join(h["texte"] for h in (historique or [])[-4:]))
        if "titre" in recent or "partie" in recent or "numero" in recent:
            actions.append({"type": "texte_ecran", "titre": "titre" in recent,
                            "partie": "partie" in recent or "numero" in recent})

    # Montage automatique : blancs, zooms, animation, accroche, barre
    mt = {}
    non = r"(sans|enleve|retire|vire|supprime|pas d.?|plus d.?|stop|arrete|desactive|enlever)"
    if re.search(r"(coupe|enleve|retire|supprime|vire)\w*.{0,15}(blancs?|silences?|pauses?|temps morts?)", t):
        mt["coupes"] = True
    if re.search(r"(garde|remets?|laisse|ne coupe pas|pas couper).{0,20}(blancs?|silences?|pauses?)", t):
        mt["coupes"] = False
    if re.search(r"zooms?\b", t):
        mt["zooms"] = not re.search(non + r".{0,12}zooms?\b", t)
    if re.search(r"\banim", t) and re.search(r"sous[- ]?titre|texte|mot|anim", t):
        mt["anim"] = not re.search(non + r".{0,15}anim", t)
    if re.search(r"accroche", t):
        mt["accroche"] = not re.search(non + r".{0,12}accroche", t)
    if re.search(r"\bbarre\b", t):
        mt["barre"] = not re.search(non + r".{0,12}barre", t)
    if re.search(r"transition|fondus?\b", t):
        mt["transitions"] = not re.search(non + r".{0,14}(transition|fondus?)", t)
    if mt:
        actions = [x for x in actions if not (x["type"] == "cadrage" and "zoom" in t and "auto" not in x)]
        actions.append({"type": "montage", **mt})

    # Placement : « espace-les », « rapproche-les », « monte le titre », « descends les sous-titres »
    sous = re.search(r"sous[- ]?titres?|texte|ecrit", t)
    titre_seul = re.search(r"(?<!sous-)(?<!sous )\btitres?\b", t)
    if re.search(r"\b(espace|ecarte|eloigne|separe|decolle)\w*|plus d.?espace|plus loin", t):
        actions.append({"type": "texte_ecran", "position_rel": -4})
        actions.append({"type": "sous_titres", "position_rel": 8})
    elif re.search(r"\b(rapproche|colle|resserre)\w*", t):
        actions.append({"type": "texte_ecran", "position_rel": 4})
        actions.append({"type": "sous_titres", "position_rel": -8})
    else:
        sens = -1 if re.search(r"\b(monte|remonte|leve|plus haut)\w*", t) else 1 if re.search(r"\b(descend|baisse|plus bas)\w*", t) else 0
        if sens and sous:
            actions.append({"type": "sous_titres", "position_rel": 8 * sens})
        elif sens and titre_seul:
            actions.append({"type": "texte_ecran", "position_rel": 5 * sens})

    if ecrire:
        # « écris X en haut » parle du texte à l'écran, pas de la position des sous-titres
        actions = [x for x in actions if not (x["type"] == "sous_titres" and set(x) <= {"type", "position"})]
    if actions and re.search(MOTS_TOUS, t):
        actions.append({"type": "tous"})
    return actions


def _ressemble_a_un_ordre(message):
    t = sans_accents(message).strip()
    return bool(re.search(
        r"^(mets?|ajoute|rajoute|enleve|retire|change|fai[st]|coupe|remets?|affiche|cree|ecris|supprime|modifie|"
        r"agrandi\w*|reduis|bouge|deplace|monte|descends?)\b"
        r"|\b(tu (n.?)?as pas|t.?as pas|ya (tjr|toujours|pas)|y a (toujours|pas)|il (n.?)?y a (toujours )?pas|ca marche pas|rien (n.?)?a change)\b",
        t))


def _promesse_vide(reponse, clip):
    r = sans_accents(reponse)
    if re.search(r"\b(je vais|je mets|j.?ajoute|j.?ai (ajoute|mis|fait|modifie|change)|c.?est fait|voila qui est fait|c.?est ajoute)\b", r):
        return True
    return sans_accents(clip.get("titre", "")).strip(" .!") == r.strip(" .!")


def _resoudre_relatifs(clip, actions):
    actions = [a for a in (actions if isinstance(actions, list) else []) if isinstance(a, dict)]
    for a in actions:
        if a.get("type") == "sous_titres" and "taille_rel" in a:
            base = clip.get("style", {}).get("taille", STYLE_DEFAUT["taille"])
            a["taille"] = base + a.pop("taille_rel")
        if a.get("type") == "sous_titres" and "position_rel" in a:
            base = clip.get("style", {}).get("position", STYLE_DEFAUT["position"])
            a["position"] = max(8, min(92, base + a.pop("position_rel")))
        if a.get("type") == "texte_ecran" and "position_rel" in a:
            base = clip.get("texte", {}).get("position", TEXTE_DEFAUT["position"])
            a["position"] = max(3, min(60, base + a.pop("position_rel")))
        if a.get("type") == "cadrage" and "decalage_rel" in a:
            base = clip.get("cadrage", {}).get("decalage", 0)
            a["decalage"] = base + a.pop("decalage_rel")
    return actions


# ----------------------------------------------------------------------
# Étage 2 : l'IA locale
# ----------------------------------------------------------------------
CONSIGNE_IA = """Tu es l'assistant de montage de l'appli « Studio Clips » : tu aides à monter des clips TikTok.
Tu parles français, de façon courte, sympa et directe (2 phrases max).
Tu peux MODIFIER le clip ouvert grâce à des actions. Réponds UNIQUEMENT avec ce JSON :
{"reponse": "ton message", "actions": [ ... ]}

Actions possibles (mets une liste vide s'il n'y a rien à modifier) :
{"type":"couper_debut","secondes":5}      coupe 5 s au début (négatif = rajoute avant)
{"type":"couper_fin","secondes":5}        coupe 5 s à la fin (négatif = rajoute après)
{"type":"duree","secondes":65}            règle la durée totale du clip
{"type":"bornes","debut":120.5,"fin":185} choisit un autre passage (secondes de la vidéo complète)
{"type":"sous_titres","couleur":"#FFFFFF","surligne":"#FFE14D","taille":76,"position":"haut|milieu|bas","majuscules":true,"mots":3,"police":"Impact"}
   (ne mets que les champs à changer ; "surligne":"aucun" enlève l'effet karaoké)
{"type":"cadrage","mode":"remplir"}       plein écran (coupe les côtés) ; "flou" = vidéo entière + fond flou
{"type":"cadrage","auto":true}            cadrage intelligent : suit le visage, montre les infos à l'écran (écran partagé)
{"type":"titre","texte":"Nouveau titre"}  renomme le clip
{"type":"texte_ecran","titre":true,"partie":true,"position":11,"contenu":"Mon texte","numero":3}  AFFICHE un texte et « PARTIE N » en haut de la vidéo
   (titre/partie false = enlève ; "contenu" vide = le titre du clip ; "numero" vide = le rang du clip)
   "position" (sous-titres comme titre) = hauteur en % depuis le haut de l'écran : 0 = tout en haut, 100 = tout en bas.
   Pour « espacer » le titre et les sous-titres : baisse la position du titre ET augmente celle des sous-titres.
{"type":"ajouter_passage","debut":190,"fin":220}  AJOUTE un 2e morceau de la video AU MEME clip (temps en secondes)
{"type":"retirer_passage","index":1}      enleve le 2e morceau assemble (index 0 = le premier)
{"type":"assembler"}                      fabrique un clip qui assemble les MEILLEURS moments de toute la video
{"type":"nouveau_clip","consigne":"un passage drôle"}  cherche un NOUVEAU clip dans la vidéo
{"type":"exporter"}                       fabrique le fichier MP4
{"type":"tous"}                           à AJOUTER si la demande vaut pour tous les clips
RÈGLES :
- Si tu dis que tu fais une modif, l'action DOIT être dans "actions". Sinon ne dis pas que c'est fait.
- Si aucune action ne correspond à la demande, dis honnêtement que tu ne sais pas encore le faire.
- N'invente pas de timestamps : utilise ceux de la transcription fournie.
EXEMPLE : « mets le titre sur toutes les vidéos » ->
{"reponse":"J'affiche le titre en haut de tous les clips.","actions":[{"type":"texte_ecran","titre":true},{"type":"tous"}]}"""


def _contexte(projet, clip):
    st = {**STYLE_DEFAUT, **clip.get("style", {})}
    cad = {**CADRAGE_DEFAUT, **clip.get("cadrage", {})}
    texte_clip = " ".join(w["text"] for w in mots_du_passage(projet.get("segments", []), clip["debut"], clip["fin"]))
    avant = [s for s in projet.get("segments", []) if clip["debut"] - 40 <= s["start"] < clip["debut"]]
    apres = [s for s in projet.get("segments", []) if clip["fin"] < s["start"] <= clip["fin"] + 40]
    fmt = lambda segs: "\n".join(f"[{s['start']:.1f}s] {s['text']}" for s in segs)
    return (
        f"CLIP OUVERT : « {clip['titre']} » — de {clip['debut']:.1f}s à {clip['fin']:.1f}s "
        f"(durée {clip['fin'] - clip['debut']:.0f} s). Vidéo complète : {projet.get('duree', 0):.0f} s.\n"
        f"Sous-titres : police {st['police']}, taille {st['taille']}, couleur {st['couleur']}, "
        f"surlignage {st['surligne'] or 'aucun'}, position {st['position']:.0f}% depuis le haut, "
        f"majuscules {'oui' if st['majuscules'] else 'non'}, {st['mots']} mots à la fois.\n"
        f"Cadrage : {'plein écran' if cad['mode'] == 'remplir' else 'vidéo entière + fond flou'}.\n"
        f"Titre affiché sur la vidéo : {'oui' if clip.get('texte', {}).get('titre') else 'non'} ; "
        f"numéro de partie affiché : {'oui' if clip.get('texte', {}).get('partie') else 'non'} ; "
        f"position du titre : {clip.get('texte', {}).get('position', TEXTE_DEFAUT['position']):.0f}%.\n\n"
        f"CE QUI EST DIT DANS LE CLIP :\n{texte_clip[:2500]}\n\n"
        f"JUSTE AVANT LE CLIP :\n{fmt(avant)[-900:]}\n\nJUSTE APRÈS LE CLIP :\n{fmt(apres)[:900]}"
    )


def demander_ia(projet, clip, message, historique):
    messages = [{"role": "system", "content": CONSIGNE_IA + "\n\n" + _contexte(projet, clip)}]
    for h in historique[-6:]:
        role = "user" if h["role"] == "moi" else "assistant"
        messages.append({"role": role, "content": h["texte"]})
    messages.append({"role": "user", "content": message})
    if modele_ia.etat["etat"] == "telechargement" and not modele_ia.etat["modele"]:
        return f"Je suis en train de télécharger ma nouvelle IA ({modele_ia.etat['pct']} %). En attendant, les ordres simples marchent (« coupe les 3 premières secondes »…).", []
    modele = modele_ia.actif()
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": modele, "messages": messages, "format": "json", "stream": False,
            **({"think": False} if modele.startswith("qwen3") else {}),
            "options": {"temperature": 0.3, "num_ctx": NUM_CTX},
        }, timeout=300)
    except requests.exceptions.ConnectionError:
        return "Je n'arrive pas à joindre l'IA (Ollama). Relance l'appli, ça la redémarre.", []
    except requests.exceptions.Timeout:
        return "L'IA a mis trop de temps à répondre. Réessaie avec une phrase plus courte.", []
    if r.status_code == 404:
        return f"Le modèle d'IA « {modele} » n'est pas installé. Ferme et relance l'appli, elle le télécharge.", []
    try:
        brut = r.json()["message"]["content"]
        m = re.search(r"\{.*\}", brut, re.S)
        data = json.loads(m.group(0) if m else brut)
        if not isinstance(data, dict):
            raise ValueError("réponse qui n'est pas un objet")
    except (ValueError, KeyError, TypeError, AttributeError):
        return "Je n'ai pas bien compris, tu peux reformuler ?", []
    reponse = str(data.get("reponse") or data.get("réponse") or "").strip() or "C'est noté."
    actions = data.get("actions") or []
    return reponse, actions if isinstance(actions, list) else []


# ----------------------------------------------------------------------
# Titres tirés du contenu du clip
# ----------------------------------------------------------------------
CONSIGNE_TITRES = """Tu écris des titres de vidéos TikTok.
On te donne ce qui est dit dans UN clip de {duree} secondes. Propose {n} titres différents :
- écrits en {langue} (la langue parlée dans le clip) ;
- adaptés au TYPE de contenu que tu devines (humour, podcast / discussion, gaming, tuto, info, sport,
  histoire vraie, débat…) : drôle pour de l'humour, intrigant pour une révélation, clair pour un tuto ;
- courts (6 mots maximum, 45 caractères maximum), accrocheurs, qui donnent envie de regarder ;
- fidèles à ce qui est VRAIMENT dit dans le clip (n'invente rien, pas de nom qui n'y est pas) ;
- styles variés : une question, une affirmation choc, un « POV » ou « Quand… » ;
- pas de hashtag, pas de guillemets, au plus un emoji.
Réponds UNIQUEMENT avec ce JSON : {{"titres": ["titre 1", "titre 2"]}}"""
NOMS_LANGUES = {"fr": "français", "en": "anglais", "es": "espagnol", "ar": "arabe", "de": "allemand",
                "it": "italien", "pt": "portugais", "nl": "néerlandais", "tr": "turc", "ru": "russe",
                "pl": "polonais", "ro": "roumain", "ja": "japonais", "zh": "chinois", "ko": "coréen"}


OUVERTURES = re.compile(r"^(alors|donc|bon|bah|ben|euh|voila|voilà|du coup|en fait|bref|salut|bonjour|"
                        r"bienvenue|aujourd.hui|je vais|on va|comme je|tu sais|vous savez)\b", re.I)


def _note_phrase(phrase):
    """À quel point une phrase ferait un bon titre : chiffres, mots forts, question… (plus c'est haut, mieux c'est)."""
    mots = [m for m in phrase.split() if m]
    if not 4 <= len(mots) <= 40:
        return -1
    note = 0.0
    note += 2.5 if any(c.isdigit() for c in phrase) else 0            # « six prélèvements », « mille euros »
    note += 1.5 if phrase.rstrip().endswith(("?", "!")) else 0        # une question accroche
    note += sum(1 for m in mots if len(_montage._norm(m)) >= 7) * 0.5          # mots concrets plutôt que du remplissage
    note -= sum(1 for m in mots if _montage._norm(m) in _montage.MOTS_VIDES) * 0.4
    note -= 3 if OUVERTURES.match(phrase.strip()) else 0              # « alors donc voilà… » : du vide
    return note


def _titre_secours(texte_clip):
    """Sans IA : la phrase la PLUS PARLANTE du clip (pas la première, qui n'est que du bla-bla d'intro)."""
    phrases = [p.strip() for p in re.split(r"(?<=[.?!])\s", texte_clip.strip()) if p.strip()] or ["Extrait"]
    phrase = max(phrases, key=_note_phrase)
    if _note_phrase(phrase) < 0:
        phrase = next((p for p in phrases if len(p.split()) >= 4), phrases[0])
    mots = phrase.split()
    titre = " ".join(mots[:7]).rstrip(",;:.") + ("…" if len(mots) > 7 else "")
    return titre[:1].upper() + titre[1:]


def note_titre(titre):
    """À quel point un titre donne envie de cliquer : un chiffre, une bonne longueur, pas de « … »."""
    mots = (titre or "").split()
    note = 0.0
    note += 2 if any(c.isdigit() for c in titre or "") else 0
    note += 1 if 3 <= len(mots) <= 8 else -1
    note += 1 if 15 <= len(titre or "") <= 55 else 0
    note += 0.5 if (titre or "").rstrip().endswith(("?", "!")) else 0
    note -= 1.5 if (titre or "").rstrip().endswith(("…", "...")) else 0   # phrase coupée = recopie de la parole
    return note


def titre_faible(titre, texte_clip):
    """Vrai si le titre ne fait que recopier le début du clip (ou ne dit rien) : il faudra le refaire."""
    t, texte = _montage._norm(titre or ""), _montage._norm(texte_clip or "")
    if not t or t in ("extrait", "clip"):
        return True
    if len(titre) > 90:
        return True
    return bool(texte) and texte.startswith(t[:max(12, int(len(t) * 0.8))])


def proposer_titres(projet, clip, n=3):
    """Titres accrocheurs d'après la transcription du clip. Retourne toujours au moins un titre."""
    texte_clip = " ".join(w["text"] for w in mots_du_passage(projet.get("segments", []), clip["debut"], clip["fin"]))
    if not texte_clip.strip():
        return [clip.get("titre") or "Extrait"]
    modele = modele_ia.actif()
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": modele, "format": "json", "stream": False,
            **({"think": False} if modele.startswith("qwen3") else {}),
            "messages": [{"role": "system", "content": CONSIGNE_TITRES.format(
                n=n, duree=round(clip["fin"] - clip["debut"]),
                langue=NOMS_LANGUES.get(projet.get("langue", "fr"), projet.get("langue", "fr")))},
                         {"role": "user", "content": f"Ce qui est dit dans le clip :\n{texte_clip[:3000]}"}],
            "options": {"temperature": 0.8, "num_ctx": NUM_CTX},
        }, timeout=180)
        r.raise_for_status()
        brut = r.json()["message"]["content"]
        m = re.search(r"\{.*\}", brut, re.S)
        titres = json.loads(m.group(0) if m else brut).get("titres", [])
    except Exception:  # noqa: BLE001 — IA indisponible : on propose quand même quelque chose
        titres = []
    propres = []
    for t in titres if isinstance(titres, list) else []:
        t = re.sub(r"#\w+", "", str(t)).strip(" \"«»'").strip()
        if 2 <= len(t) <= 80 and t.lower() not in (x.lower() for x in propres) and not titre_faible(t, texte_clip):
            propres.append(t)   # un titre qui recopie les premières paroles ne sert à rien
    return propres[:n] or [_titre_secours(texte_clip)]


def traiter_message(projet, clip, message, historique):
    """Retourne (reponse, speciales, rapide, actions)."""
    actions = lecture_rapide(message, historique)
    rapide = bool(actions)
    if actions:
        reponse = None
    else:
        reponse, actions = demander_ia(projet, clip, message, historique)
    actions = _resoudre_relatifs(clip, actions)
    fait, speciales = appliquer(projet, clip, actions)
    if "aide" in speciales:
        return CE_QUE_JE_SAIS_FAIRE, speciales, rapide, actions
    if "proposer_titres" in speciales:
        titres = proposer_titres(projet, clip)
        liste = "\n".join(f"{i}. {x}" for i, x in enumerate(titres, 1))
        speciales.append(("titres", titres))
        return (f"Idées de titre d'après ce qui est dit dans le clip :\n{liste}\n"
                "Clique sur celui que tu veux (au-dessus des réglages)."), speciales, rapide, actions
    if "titrer_tous" in speciales:
        return ("Je trouve un titre pour chaque clip d'après ce qui y est dit, ça prend quelques secondes par clip…",
                speciales, rapide, actions)
    if not rapide and not fait and not speciales and (_ressemble_a_un_ordre(message) or _promesse_vide(reponse, clip)):
        # L'IA n'a rien modifié : on ne la laisse pas prétendre le contraire.
        debut = ("Je n'ai pas bien compris ta question, tu peux reformuler ? "
                 if message.strip().endswith("?") else
                 "Je n'ai pas réussi à faire ça, je ne sais pas encore le faire tout seul. ")
        return debut + CE_QUE_JE_SAIS_FAIRE, speciales, rapide, []
    if reponse is None or (fait and not reponse.strip()):
        if fait:
            blocs, _j = _montage.blocs_du_clip(projet.get("segments", []), clip, clip.get("montage"))
            duree = _montage.duree_montee(blocs)   # durée du clip MONTÉ (morceaux assemblés, blancs retirés)
            reponse = ("C'est fait : " + ", ".join(fait) + f". Le clip dure {duree:.0f} s"
                       + (" (moins d'1 min : TikTok ne le rémunérera pas)." if duree < 60 else "."))
        elif "annuler" in speciales:
            reponse = ""
        elif "exporter" in speciales:
            reponse = "J'exporte le clip en MP4, je te préviens quand c'est prêt."
        elif any(isinstance(s, tuple) for s in speciales):
            reponse = "Je fouille la vidéo pour te trouver un nouveau passage, ça peut prendre quelques minutes."
        else:
            reponse = "C'est noté."
    elif fait and not reponse.lower().startswith("c'est fait"):
        # Réponse de l'IA + ce qui a VRAIMENT été modifié (jamais l'un sans l'autre).
        reponse = reponse.rstrip() + "\n→ " + ", ".join(fait) + "."
    if "tous" in speciales and fait:
        reponse += " (sur tous les clips)"
    return reponse, speciales, rapide, actions
