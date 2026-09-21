"""
Studio Clips — lance l'appli : un petit serveur local + une fenêtre (Edge en mode appli).

Tout reste sur le PC : la vidéo n'est jamais envoyée sur internet.
Les projets sont rangés dans le dossier « projets » à côté de ce fichier.
"""

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime

APP = os.path.dirname(os.path.abspath(__file__))
PROJETS = os.path.join(APP, "projets")
os.makedirs(PROJETS, exist_ok=True)
os.environ["PATH"] = os.path.join(APP, "bin") + os.pathsep + os.environ.get("PATH", "")

# pythonw n'a pas de console : tout ce qui s'affiche part dans studio.log
if sys.stdout is None or not sys.stdout.isatty():
    _chemin_log = os.path.join(APP, "studio.log")
    try:
        if os.path.getsize(_chemin_log) > 5 * 1024 * 1024:
            os.replace(_chemin_log, _chemin_log + ".ancien")
    except OSError:
        pass
    _log = open(_chemin_log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

from flask import Flask, jsonify, request, send_file, abort  # noqa: E402

import assistant  # noqa: E402
import lanceur  # noqa: E402
import modele_ia  # noqa: E402
import montage  # noqa: E402
from analyze import find_best_clips  # noqa: E402
from transcribe import transcribe_video, get_video_duration, codec_video as montage_codec  # noqa: E402

VERSION = "2.8"
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FORMATS_LISIBLES = (".mp4", ".m4v", ".webm", ".mov")
CODECS_LISIBLES = ("h264", "vp8", "vp9", "av1")   # ce que le lecteur d'Edge sait afficher sans extension

app = Flask(__name__, static_folder=os.path.join(APP, "static"), static_url_path="/static")
verrou = threading.RLock()
verrou_export = threading.Lock()   # un seul encodage à la fois : un PC modeste ne suit pas plus
jobs = {}
cache = {}
verrou_jobs = threading.Lock()
verrou_lourd = threading.Lock()          # une seule transcription/analyse à la fois (PC modeste)
verrou_miniatures = threading.Semaphore(2)
TACHES_IA = ("analyse", "nouveau_clip", "titres")


class Arret(Exception):
    """Levée dans une tâche quand l'utilisateur clique sur « Arrêter »."""


def taches(pid=None):
    with verrou_jobs:
        liste = list(jobs.values())
    return [j for j in liste if pid is None or j["projet"] == pid]


def occupe(pid=None, *types):
    return any(j["etat"] == "en_cours" and (not types or j["type"] in types) for j in taches(pid))


def message_clair(e):
    """Ce que l'utilisateur lit quand une tâche échoue : jamais de jargon technique."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, Arret):
        return "Arrêté."
    if isinstance(e, HTTPException):
        return "Ce clip ou ce projet n'existe plus."
    if isinstance(e, RuntimeError):
        return str(e)
    if isinstance(e, MemoryError):
        return "Ton PC manque de mémoire. Ferme les autres logiciels puis réessaie."
    if isinstance(e, OSError):
        if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) == 112:
            return "Le disque est plein. Libère de la place puis réessaie."
        return "Un fichier est inaccessible (déplacé, supprimé ou ouvert ailleurs). Réessaie."
    return "Problème inattendu. Réessaie ; si ça recommence, envoie le fichier studio.log."


# ----------------------------------------------------------------------
# Projets (un dossier + un projet.json chacun)
# ----------------------------------------------------------------------
def _dossier(pid):
    if not re.fullmatch(r"[a-f0-9]{12}", pid or ""):
        abort(404)
    return os.path.join(PROJETS, pid)


def charger(pid):
    with verrou:
        if pid not in cache:
            chemin = os.path.join(_dossier(pid), "projet.json")
            if not os.path.exists(chemin):
                abort(404)
            for essai in (chemin, chemin + ".bak"):   # projet.json abîmé : on reprend la copie précédente
                try:
                    with open(essai, encoding="utf-8") as f:
                        cache[pid] = json.load(f)
                    break
                except (OSError, ValueError):
                    continue
            else:
                abort(404)
        return cache[pid]


def sauver(projet, creer=False):
    with verrou:
        dossier = _dossier(projet["id"])
        if not os.path.isdir(dossier):
            if not creer:
                return   # projet supprimé pendant qu'une tâche tournait : on ne le ressuscite pas
            os.makedirs(dossier, exist_ok=True)
        final = os.path.join(dossier, "projet.json")
        tmp = final + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(projet, f, ensure_ascii=False)
        for essai in range(8):   # l'antivirus ou l'indexeur Windows peut bloquer le fichier un instant
            try:
                if os.path.exists(final):
                    shutil.copyfile(final, final + ".bak")
                os.replace(tmp, final)
                break
            except PermissionError:
                if essai == 7:
                    raise
                time.sleep(0.15)
        cache[projet["id"]] = projet


def clip_de(projet, cid):
    for c in projet["clips"]:
        if c["id"] == cid:
            return c
    abort(404)


def vue_clip(c, p=None):
    v = {k: v for k, v in c.items() if k != "historique"}
    v["peut_annuler"] = bool(c.get("historique"))
    v["texte"] = {**montage.TEXTE_DEFAUT, **c.get("texte", {})}
    v["numero"] = p["clips"].index(c) + 1 if p and c in p["clips"] else None
    v["style"] = {**montage.STYLE_DEFAUT, **c.get("style", {})}
    v["cadrage"] = {**montage.CADRAGE_DEFAUT, **c.get("cadrage", {})}
    return v


def vue_projet(p, avec_segments=True):
    v = {k: p[k] for k in ("id", "nom", "source", "consigne", "cree", "etat", "erreur", "duree") if k in p}
    v["clips"] = [vue_clip(c, p) for c in p["clips"]]
    v["chat"] = p.get("chat", [])[-200:]
    v["jobs"] = taches(p["id"])
    v["source_existe"] = os.path.exists(p["source"])
    if avec_segments:
        v["segments"] = p.get("segments", [])
    return v


def nouveau_clip(c, segments):
    fin = assistant._fin_sur_un_mot(segments, c["start"], c["end"])
    return {
        "id": uuid.uuid4().hex[:8], "titre": c.get("title") or "Extrait", "raison": c.get("reason", ""),
        "note": c.get("score", 5), "debut": round(c["start"], 2), "fin": max(round(c["start"] + 3, 2), fin),
        "style": dict(montage.STYLE_DEFAUT), "cadrage": dict(montage.CADRAGE_DEFAUT),
        "texte": dict(montage.TEXTE_DEFAUT), "exporte": None, "historique": [],
    }


def dire(projet, texte, cid=None):
    projet.setdefault("chat", []).append({"role": "ia", "texte": texte, "cid": cid, "t": time.time()})


# ----------------------------------------------------------------------
# Tâches de fond (analyse, recherche d'un clip, export, aperçu)
# ----------------------------------------------------------------------
def lancer_job(pid, type_, fonction, cid=None, etape=""):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "projet": pid, "type": type_, "clip": cid, "etat": "en_cours",
           "pct": 0, "etape": etape, "message": "", "erreur": None, "debut": time.time()}
    with verrou_jobs:
        # on oublie les tâches terminées depuis plus d'une heure
        for vieux in [k for k, j in jobs.items() if j["etat"] != "en_cours" and time.time() - j.get("fin", 0) > 3600]:
            del jobs[vieux]
        jobs[jid] = job

    def corps():
        try:
            fonction(job)
            job["etat"], job["pct"] = "fini", 100
        except Arret:
            job["etat"], job["erreur"] = "arrete", None
        except Exception as e:  # noqa: BLE001 — tout échec doit remonter dans l'interface, en clair
            traceback.print_exc()
            job["etat"], job["erreur"] = "erreur", message_clair(e)
        job["fin"] = time.time()

    threading.Thread(target=corps, daemon=True).start()
    return job


def _texte_horodate(segments):
    return "\n".join(f"[{s['start']:.2f}s -> {s['end']:.2f}s] {s['text']}" for s in segments)


def _chemin_apercu(pid):
    return os.path.join(_dossier(pid), "apercu.mp4")


def _fabriquer_apercu(pid, source, job, pct_debut, pct_fin):
    """Copie légère (540p) lisible par le lecteur, pour les formats que le navigateur ne lit pas."""
    job["etape"] = "Préparation de l'aperçu"
    sortie = _chemin_apercu(pid)
    provisoire = sortie + ".tmp.mp4"
    duree = get_video_duration(source) or 1
    cmd = ["ffmpeg", "-y", "-i", source, "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", provisoire]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WIN)
    except FileNotFoundError:
        raise RuntimeError("FFmpeg est introuvable. Réinstalle Studio Clips avec Studio-Clips.exe.") from None
    for ligne in proc.stdout:
        if job.get("annule"):
            proc.kill()
            break
        if ligne.startswith("out_time_us="):
            try:
                t = int(ligne.split("=")[1]) / 1e6
                job["pct"] = int(pct_debut + (pct_fin - pct_debut) * min(1, t / duree))
            except ValueError:
                pass
    if proc.wait() != 0 or job.get("annule"):
        try:
            os.remove(provisoire)
        except OSError:
            pass
        if job.get("annule"):
            raise Arret()
        raise RuntimeError("Impossible de préparer l'aperçu de cette vidéo (format non pris en charge).")
    os.replace(provisoire, sortie)


def _surveiller(job):
    """À appeler régulièrement dans une tâche : interrompt si l'utilisateur a cliqué sur Arrêter."""
    if job.get("annule"):
        raise Arret()


def _decoupage_de_secours(p, bornes):
    """Quand l'IA ne trouve rien : on découpe la parole en morceaux réguliers, pour ne jamais rendre 0 clip."""
    segs = p.get("segments") or []
    if not segs:
        return []
    cible = (bornes[0] + bornes[1]) / 2 if bornes else 60
    debut_parole, fin_parole = segs[0]["start"], segs[-1]["end"]
    cible = min(cible, max(5, fin_parole - debut_parole))
    clips, t = [], debut_parole
    while t < fin_parole - min(10, cible * 0.5) and len(clips) < 5:
        fin = min(t + cible, fin_parole + 0.3)
        if clips and fin - t < cible * 0.8:
            t = max(debut_parole, fin - cible)   # dernier morceau trop court : il démarre un peu plus tôt
        texte = " ".join(s["text"] for s in segs if s["end"] > t and s["start"] < fin)
        clips.append({"start": t, "end": fin, "title": assistant._titre_secours(texte),
                      "reason": "Découpage automatique (l'IA n'a pas trouvé de passage marquant).", "score": 5})
        suivant = next((s["start"] for s in segs if s["start"] >= fin), None)
        if suivant is None:
            break
        t = suivant
    return clips


def job_analyse(pid):
    def travail(job):
        p = charger(pid)
        p["etat"], p["erreur"] = "traitement", None
        sauver(p)
        try:
            if not os.path.exists(p["source"]):
                raise RuntimeError("La vidéo d'origine est introuvable (déplacée, renommée ou clé USB débranchée). "
                                   "Clique sur « Retrouver la vidéo ».")
            job["etape"] = "En attente de la tâche en cours"
            with verrou_lourd:
                _surveiller(job)
                # Aperçu : nécessaire si le navigateur ne sait pas lire le fichier (HEVC d'iPhone, AVI…).
                codec = montage_codec(p["source"])
                if not os.path.exists(_chemin_apercu(pid)) and (
                        not p["source"].lower().endswith(FORMATS_LISIBLES) or codec not in CODECS_LISIBLES):
                    try:
                        _fabriquer_apercu(pid, p["source"], job, 0, 8)
                    except Arret:
                        raise
                    except Exception:  # noqa: BLE001 — sans aperçu, la transcription reste possible
                        traceback.print_exc()

                if not p.get("transcrit"):
                    job["etape"] = "Transcription de la vidéo"

                    def log_transcription(msg):
                        _surveiller(job)
                        job["message"] = msg.strip()
                        m = re.search(r"(\d+) % transcrit", msg)
                        if m:
                            job["pct"] = 8 + int(int(m.group(1)) * 0.57)
                    job["pct"] = 8
                    # Modèle de transcription selon la puissance du PC (base = ~3x plus rapide que small)
                    taille_whisper = "small" if modele_ia.ram_go() >= 15 else "base"
                    segments, _, langue = transcribe_video(p["source"], model_size=taille_whisper, log=log_transcription)
                    p["segments"], p["langue"], p["transcrit"] = segments, langue, True
                    p["duree"] = get_video_duration(p["source"]) or (segments[-1]["end"] if segments else 0)
                    sauver(p)

                if not p["segments"]:
                    p["clips"], p["etat"] = [], "pret"
                    dire(p, "Je n'ai entendu personne parler dans cette vidéo (musique ou silence ?). "
                            "Studio Clips choisit les passages d'après ce qui est dit : essaie avec une vidéo où quelqu'un parle.")
                    sauver(p)
                    return

                job["pct"] = 66
                modele_ia.attendre_pret(lambda e: job.update(etape=f"Téléchargement de l'IA ({e['pct']} %)"))
                job["etape"] = "L'IA choisit les meilleurs passages"

                def log_analyse(msg):
                    _surveiller(job)
                    job["message"] = msg.strip()
                    m = re.search(r"partie (\d+)/(\d+)", msg)
                    if m:
                        job["pct"] = 66 + int(32 * (int(m.group(1)) - 1) / int(m.group(2)))

                bornes = assistant.fourchette_duree(p.get("consigne", ""))
                options = {"clip_min": bornes[0], "clip_max": bornes[1]} if bornes else {}
                trouves = find_best_clips(
                    _texte_horodate(p["segments"]), model=modele_ia.actif(), log=log_analyse,
                    custom_instructions=p.get("consigne", ""), video_duration=p.get("duree"), **options,
                )
                secours = False
                if not trouves:
                    trouves, secours = _decoupage_de_secours(p, bornes), True
                with verrou:
                    p["clips"] = [nouveau_clip(c, p["segments"]) for c in trouves]
                    p["etat"] = "pret"
                    if p["clips"] and secours:
                        dire(p, f"L'IA n'a pas trouvé de passage vraiment marquant, alors j'ai découpé la vidéo en "
                                f"{len(p['clips'])} partie(s). Tu peux demander « trouve-moi un passage drôle » "
                                "ou « donne un titre à tous les clips ».")
                    elif p["clips"]:
                        dire(p, f"J'ai trouvé {len(p['clips'])} clip(s). Clique sur un clip pour le regarder, "
                                "puis dis-moi ce que tu veux changer : « coupe les 3 premières secondes », "
                                "« sous-titres en jaune plus gros », « trouve-moi un titre »…")
                    else:
                        dire(p, "Je n'ai pas trouvé de passage à découper. Essaie avec une vidéo plus longue.")
                    sauver(p)
        except Arret:
            p["etat"], p["erreur"] = "interrompu", None
            sauver(p)
            raise
        except Exception as e:
            p["etat"], p["erreur"] = "erreur", message_clair(e)
            sauver(p)
            raise
    return lancer_job(pid, "analyse", travail, etape="Démarrage")


def job_nouveau_clip(pid, consigne):
    def travail(job):
        p = charger(pid)
        if not p.get("segments"):
            with verrou:
                dire(p, "Il n'y a rien de parlé dans cette vidéo : je ne peux pas y chercher de passage.")
                sauver(p)
            return
        job["etape"] = "L'IA cherche un nouveau passage"

        def log(msg):
            _surveiller(job)
            job["message"] = msg.strip()
            m = re.search(r"partie (\d+)/(\d+)", msg)
            if m:
                job["pct"] = int(95 * (int(m.group(1)) - 1) / int(m.group(2)))

        bornes = assistant.fourchette_duree(consigne) or assistant.fourchette_duree(p.get("consigne", ""))
        options = {"clip_min": bornes[0], "clip_max": bornes[1]} if bornes else {}
        modele_ia.attendre_pret()
        with verrou_lourd:
            trouves = find_best_clips(_texte_horodate(p["segments"]), model=modele_ia.actif(), log=log,
                                      custom_instructions=consigne,
                                      video_duration=p.get("duree"), max_clips=5, **options)
        with verrou:
            existants = p["clips"]

            def recouvre(c):
                return any(min(c["end"], e["fin"]) - max(c["start"], e["debut"]) > 0.5 * (c["end"] - c["start"])
                           for e in existants)
            libres = sorted((c for c in trouves if not recouvre(c)), key=lambda c: -c["score"])
            if not libres:
                dire(p, "Je n'ai pas trouvé de nouveau passage qui colle. Essaie de préciser (un sujet, un moment…).")
            else:
                c = nouveau_clip(libres[0], p["segments"])
                p["clips"] = sorted(p["clips"] + [c], key=lambda x: x["debut"])
                job["resultat"] = c["id"]
                dire(p, f"Trouvé : « {c['titre']} » (note {c['note']:.0f}/10). {c['raison']}".strip(), c["id"])
            sauver(p)
    return lancer_job(pid, "nouveau_clip", travail, etape="Recherche")


def job_titrer_tous(pid):
    """Un titre accrocheur pour chaque clip, tiré de ce qui y est dit."""
    def travail(job):
        p = charger(pid)
        modele_ia.attendre_pret()
        a_titrer = list(p["clips"])
        for i, c in enumerate(a_titrer, 1):
            _surveiller(job)
            job["etape"] = f"Titre du clip {i}/{len(a_titrer)}"
            job["pct"] = int(100 * (i - 1) / max(len(a_titrer), 1))
            titre = assistant.proposer_titres(p, c, n=3)[0]
            with verrou:
                if c not in p["clips"]:
                    continue   # clip supprimé entre-temps
                assistant.memoriser(c)
                c["titre"] = titre[:80]
                c["exporte"] = None
                sauver(p)
        with verrou:
            dire(p, f"C'est fait : {len(a_titrer)} clip(s) ont un nouveau titre tiré de leur contenu. "
                    "Tu peux en changer un avec ✨ Idées de titre, ou annuler clip par clip.")
            sauver(p)
    return lancer_job(pid, "titres", travail, etape="Titres")


NOMS_RESERVES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def _empreinte(c):
    return json.dumps([c.get(k) for k in ("debut", "fin", "style", "cadrage", "texte", "titre")], sort_keys=True)


def job_export(pid, cid):
    def travail(job):
        job["etape"] = "En attente de l'export précédent"
        with verrou_export:
            p = charger(pid)
            with verrou:
                c = clip_de(p, cid)
                a_exporter = copy.deepcopy(c)
                a_exporter["numero"] = p["clips"].index(c) + 1
                a_exporter["langue"] = p.get("langue", "fr")
                empreinte = _empreinte(c)
            if not os.path.exists(p["source"]):
                raise RuntimeError("La vidéo d'origine est introuvable (déplacée, renommée ou clé USB débranchée). "
                                   "Clique sur « Retrouver la vidéo ».")
            job["etape"] = f"Export de « {a_exporter['titre']} »"
            sortie_dir = os.path.join(os.path.dirname(p["source"]), "clips_tiktok")
            try:
                os.makedirs(sortie_dir, exist_ok=True)
            except OSError:   # dossier de la vidéo en lecture seule : on range les clips dans Vidéos
                sortie_dir = os.path.join(os.path.expanduser("~"), "Videos", "clips_tiktok")
                os.makedirs(sortie_dir, exist_ok=True)
            nom = "".join(ch for ch in a_exporter["titre"] if ch.isalnum() or ch in " -_").strip()[:50].rstrip(" .") or "clip"
            if nom.lower() in NOMS_RESERVES:
                nom += " clip"
            sortie = montage.chemin_unique(os.path.join(sortie_dir, f"{nom}.mp4"))

            def prog(pct):
                job["pct"] = pct
            montage.exporter_clip(p["source"], p["segments"], a_exporter, sortie, prog)
            job["resultat"] = sortie
            taille = montage.dimensions(sortie)
            format_ = f" — {taille[0]}×{taille[1]}" + (" (format TikTok ✔)" if taille[0] < taille[1] else "") if taille else ""
            with verrou:
                if c in p["clips"] and _empreinte(c) == empreinte:
                    c["exporte"] = sortie   # sinon le clip a changé pendant l'encodage : ce MP4 est déjà périmé
                dire(p, f"Clip « {a_exporter['titre']} » exporté{format_}, {a_exporter['fin'] - a_exporter['debut']:.0f} s. "
                        f"Il est dans le dossier {sortie_dir}.", cid)
                sauver(p)
    return lancer_job(pid, "export", travail, cid=cid, etape="Export")


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/")
def accueil():
    return send_file(os.path.join(APP, "static", "index.html"))


@app.get("/api/projets")
def liste_projets():
    res = []
    for pid in os.listdir(PROJETS):
        if not re.fullmatch(r"[a-f0-9]{12}", pid):
            continue
        try:
            p = charger(pid)
        except Exception:  # noqa: BLE001 — un projet illisible ne doit pas bloquer l'accueil
            continue
        if p["etat"] == "traitement" and not occupe(pid, "analyse"):
            p["etat"] = "interrompu"
        res.append({"id": pid, "nom": p["nom"], "cree": p["cree"], "etat": p["etat"],
                    "clips": len(p["clips"]), "exportes": sum(1 for c in p["clips"] if c.get("exporte")),
                    "premier": p["clips"][0]["id"] if p["clips"] else None})
    res.sort(key=lambda x: x["cree"], reverse=True)
    return jsonify(res)


def _choisir_fichier(titre):
    code = (
        "import tkinter as tk; from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"p = filedialog.askopenfilename(title={titre!r}, filetypes=[('Vidéos', "
        "'*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.wmv *.flv *.ts'), ('Tous les fichiers', '*.*')])\n"
        "print(p or '')"
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    r = subprocess.run([exe, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, creationflags=NO_WIN)
    chemin = r.stdout.strip()
    return os.path.normpath(chemin) if chemin else ""


@app.post("/api/choisir")
def choisir_video():
    return jsonify({"chemin": _choisir_fichier("Choisis ta vidéo")})


@app.post("/api/projets")
def creer_projet():
    d = request.get_json(force=True)
    source = d.get("chemin", "")
    if not os.path.isfile(source):
        return jsonify({"erreur": "Vidéo introuvable."}), 400
    pid = uuid.uuid4().hex[:12]
    p = {"id": pid, "nom": os.path.splitext(os.path.basename(source))[0], "source": source,
         "consigne": (d.get("consigne") or "").strip(), "cree": datetime.now().isoformat(timespec="seconds"),
         "etat": "traitement", "erreur": None, "duree": get_video_duration(source), "segments": [],
         "clips": [], "chat": []}
    sauver(p, creer=True)
    job_analyse(pid)
    return jsonify({"id": pid})


@app.get("/api/projets/<pid>")
def lire_projet(pid):
    return jsonify(vue_projet(charger(pid)))


@app.delete("/api/projets/<pid>")
def supprimer_projet(pid):
    with verrou:
        charger(pid)
        for j in taches(pid):
            j["annule"] = True   # les tâches en cours s'arrêtent d'elles-mêmes
        cache.pop(pid, None)
        shutil.rmtree(_dossier(pid), ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/projets/<pid>/relancer")
def relancer(pid):
    d = request.get_json(silent=True) or {}
    p = charger(pid)
    if occupe(pid, "analyse"):
        return jsonify({"ok": True, "deja": True})
    with verrou:
        if "consigne" in d:
            p["consigne"] = d["consigne"]
        sauver(p)
    job_analyse(pid)
    return jsonify({"ok": True})


@app.post("/api/projets/<pid>/arreter")
def arreter(pid):
    """Bouton « Arrêter » : interrompt l'analyse / la recherche / les titres en cours."""
    charger(pid)
    n = 0
    for j in taches(pid):
        if j["etat"] == "en_cours" and j["type"] in TACHES_IA + ("apercu",):
            j["annule"] = True
            n += 1
    return jsonify({"ok": True, "arretees": n})


@app.post("/api/projets/<pid>/retrouver")
def retrouver_video(pid):
    """La vidéo a été déplacée : l'utilisateur la désigne à nouveau."""
    p = charger(pid)
    chemin = _choisir_fichier("Où est la vidéo « " + p["nom"] + " » ?")
    if not chemin:
        return jsonify({"ok": False})
    if not os.path.isfile(chemin):
        return jsonify({"erreur": "Ce fichier n'existe pas."}), 400
    duree = get_video_duration(chemin)
    if p.get("duree") and duree and abs(duree - p["duree"]) > 2:
        return jsonify({"erreur": "Ce n'est pas la même vidéo (la durée ne correspond pas)."}), 400
    with verrou:
        p["source"] = chemin
        sauver(p)
    return jsonify(vue_projet(p, avec_segments=False))


@app.get("/api/projets/<pid>/etat")
def etat_projet(pid):
    """Interrogé toutes les secondes par l'interface : tâches + clips + chat (sans la transcription)."""
    return jsonify(vue_projet(charger(pid), avec_segments=False))


@app.get("/media/<pid>")
def media(pid):
    p = charger(pid)
    apercu = _chemin_apercu(pid)
    chemin = apercu if os.path.exists(apercu) else p["source"]
    if not os.path.exists(chemin):
        abort(404)
    type_ = "video/webm" if chemin.lower().endswith((".webm", ".mkv")) else "video/mp4"
    return send_file(chemin, conditional=True, mimetype=type_)


@app.post("/api/projets/<pid>/apercu")
def demander_apercu(pid):
    p = charger(pid)
    if not os.path.exists(p["source"]):
        return jsonify({"erreur": "La vidéo d'origine est introuvable."}), 404
    if os.path.exists(_chemin_apercu(pid)):
        return jsonify({"ok": True, "existe": True})
    if not occupe(pid, "apercu"):
        lancer_job(pid, "apercu", lambda job: _fabriquer_apercu(pid, p["source"], job, 0, 100), etape="Aperçu")
    return jsonify({"ok": True})


@app.get("/miniature/<pid>/<cid>")
def vignette(pid, cid):
    p = charger(pid)
    c = clip_de(p, cid)
    instant = c["debut"] + min(1.5, (c["fin"] - c["debut"]) / 2)
    dossier = os.path.join(_dossier(pid), "miniatures")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"{cid}_{int(instant * 10)}.jpg")
    if not os.path.exists(chemin):
        with verrou_miniatures:   # pas 30 FFmpeg en même temps sur un petit PC
            if not os.path.exists(chemin) and not montage.miniature(p["source"], instant, chemin):
                abort(404)
    return send_file(chemin, max_age=3600)


@app.route("/api/projets/<pid>/clips/<cid>", methods=["PATCH", "POST"])   # POST : envoi à la fermeture de la fenêtre
def modifier_clip(pid, cid):
    d = request.get_json(force=True)
    with verrou:
        p = charger(pid)
        c = clip_de(p, cid)
        avant = _empreinte(c)
        actions = []
        if "debut" in d or "fin" in d:
            actions.append({"type": "bornes", "debut": d.get("debut", c["debut"]), "fin": d.get("fin", c["fin"])})
        if isinstance(d.get("style"), dict):
            actions.append({"type": "sous_titres", **d["style"]})
        if isinstance(d.get("cadrage"), dict):
            actions.append({"type": "cadrage", **d["cadrage"]})
        if isinstance(d.get("texte"), dict):
            actions.append({"type": "texte_ecran", **d["texte"]})
        if d.get("titre"):
            actions.append({"type": "titre", "texte": d["titre"]})
        historique_avant = len(c.get("historique", []))
        assistant.appliquer(p, c, actions)
        if _empreinte(c) == avant:
            del c.get("historique", [])[historique_avant:]   # rien n'a changé : pas d'étape d'annulation vide
        else:
            c["exporte"] = None
        sauver(p)
        return jsonify(vue_clip(c, p))


@app.post("/api/projets/<pid>/clips/<cid>/appliquer-a-tous")
def appliquer_a_tous(pid, cid):
    """Recopie l'habillage du clip (sous-titres, cadrage, titre à l'écran) sur tous les autres."""
    with verrou:
        p = charger(pid)
        modele = clip_de(p, cid)
        for c in p["clips"]:
            if c is modele:
                continue
            assistant.memoriser(c)
            for champ in ("style", "cadrage", "texte"):
                c[champ] = copy.deepcopy(modele.get(champ, {}))
            c["texte"]["contenu"] = ""   # chaque clip garde SON titre, pas celui du modèle
            c["texte"]["numero"] = None
            c["exporte"] = None
        sauver(p)
        return jsonify(vue_projet(p, avec_segments=False))


@app.post("/api/projets/<pid>/clips/<cid>/annuler")
def annuler_clip(pid, cid):
    with verrou:
        p = charger(pid)
        c = clip_de(p, cid)
        ok = assistant.annuler(c)
        if ok:
            c["exporte"] = None
        sauver(p)
        return jsonify({**vue_clip(c, p), "annule": ok})


@app.delete("/api/projets/<pid>/clips/<cid>")
def supprimer_clip(pid, cid):
    with verrou:
        p = charger(pid)
        clip_de(p, cid)
        p["clips"] = [c for c in p["clips"] if c["id"] != cid]
        sauver(p)
    return jsonify({"ok": True})


@app.post("/api/projets/<pid>/clips/<cid>/titres")
def idees_titres(pid, cid):
    p = charger(pid)
    return jsonify({"titres": assistant.proposer_titres(p, clip_de(p, cid))})


@app.post("/api/projets/<pid>/titrer-tous")
def titrer_tous(pid):
    charger(pid)
    deja = next((j for j in taches(pid) if j["type"] == "titres" and j["etat"] == "en_cours"), None)
    return jsonify(deja or job_titrer_tous(pid))


def _export_en_cours(pid, cid):
    return next((j for j in taches(pid) if j["type"] == "export" and j["clip"] == cid and j["etat"] == "en_cours"), None)


@app.post("/api/projets/<pid>/clips/<cid>/exporter")
def exporter(pid, cid):
    clip_de(charger(pid), cid)
    return jsonify(_export_en_cours(pid, cid) or job_export(pid, cid))


@app.post("/api/projets/<pid>/exporter-tout")
def exporter_tout(pid):
    p = charger(pid)
    lances = []
    for c in list(p["clips"]):
        if _export_en_cours(pid, c["id"]) or (c.get("exporte") and os.path.exists(c["exporte"])):
            continue   # déjà en cours, ou déjà exporté et inchangé
        lances.append(job_export(pid, c["id"]))
    return jsonify(lances)


TYPES_MONTAGE = ("couper_debut", "couper_fin", "bornes", "duree", "sous_titres", "cadrage", "texte_ecran", "titre")


@app.post("/api/projets/<pid>/chat")
def chat(pid):
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()[:1000]
    if not message:
        return jsonify({"erreur": "Message vide."}), 400
    p = charger(pid)
    if p["etat"] == "traitement":
        return jsonify({"erreur": "Attends la fin de l'analyse de la vidéo, puis je suis à toi."}), 409
    cid = d.get("clip")
    with verrou:
        p.setdefault("chat", []).append({"role": "moi", "texte": message, "cid": cid, "t": time.time()})
        sauver(p)
    if not p["clips"] or not cid:
        # Pas de clip ouvert : seule la recherche d'un passage a du sens.
        with verrou:
            if not p.get("segments"):
                dire(p, "Il n'y a rien de parlé dans cette vidéo : je ne peux pas y chercher de passage.")
            elif occupe(pid, "nouveau_clip"):
                dire(p, "Je cherche déjà un passage, patiente un peu.")
            else:
                job_nouveau_clip(pid, message)
                dire(p, "Je fouille la vidéo pour te trouver un passage, ça peut prendre quelques minutes.")
            sauver(p)
        return jsonify(vue_projet(p, avec_segments=False))

    with verrou:
        c = clip_de(p, cid)
        historique = [h for h in p["chat"][:-1] if h.get("cid") == cid]
        copie = copy.deepcopy(c)
    if occupe(pid, *TACHES_IA) and not assistant.lecture_rapide(message, historique):
        with verrou:
            dire(p, "Je suis en train de travailler (analyse ou titres). Les ordres simples marchent tout de suite "
                    "(« coupe les 3 premières secondes », « sous-titres en jaune »…) ; pour le reste, réessaie dans un moment.", cid)
            sauver(p)
        return jsonify(vue_projet(p, avec_segments=False))

    # L'IA peut mettre du temps : on réfléchit sur une copie, puis on applique les MÊMES actions
    # au vrai clip (sans écraser ce qui a pu changer entre-temps).
    reponse, speciales, _, actions = assistant.traiter_message(p, copie, message, historique)
    with verrou:
        if c not in p["clips"]:
            dire(p, "Ce clip a été supprimé entre-temps.", cid)
            sauver(p)
            return jsonify(vue_projet(p, avec_segments=False))
        avant = _empreinte(c)
        montage_actions = [a for a in actions if isinstance(a, dict) and a.get("type") in TYPES_MONTAGE]
        if montage_actions:
            assistant.appliquer(p, c, copy.deepcopy(montage_actions))
        if "annuler" in speciales:
            reponse = (reponse + " " if reponse else "") + (
                "J'ai annulé la dernière modif." if assistant.annuler(c) else "Il n'y a rien à annuler.")
        if _empreinte(c) != avant:
            c["exporte"] = None   # le MP4 déjà fabriqué ne correspond plus
        if "tous" in speciales:
            # Même demande appliquée aux autres clips (sauf ce qui n'a de sens que pour un seul).
            communes = [a for a in montage_actions if a.get("type") not in ("bornes", "titre")]
            for autre in p["clips"]:
                if autre is not c and communes:
                    assistant.appliquer(p, autre, copy.deepcopy(communes))
                    autre["exporte"] = None
        dire(p, reponse.strip() or "C'est noté.", cid)
        sauver(p)
    propositions = None
    for s in speciales:
        if s == "titrer_tous" and not occupe(pid, "titres"):
            job_titrer_tous(pid)
        elif s == "exporter" and not _export_en_cours(pid, cid):
            job_export(pid, cid)
        elif isinstance(s, tuple) and s[0] == "titres":
            propositions = s[1]
        elif isinstance(s, tuple) and s[0] == "nouveau_clip" and not occupe(pid, "nouveau_clip"):
            job_nouveau_clip(pid, s[1] or message)
    reponse_json = vue_projet(p, avec_segments=False)
    if propositions:
        reponse_json["propositions"] = propositions
    return jsonify(reponse_json)


@app.post("/api/projets/<pid>/ouvrir")
def ouvrir(pid):
    d = request.get_json(silent=True) or {}
    p = charger(pid)
    cible = os.path.join(os.path.dirname(p["source"]), "clips_tiktok")
    if d.get("clip"):
        cible = clip_de(p, d["clip"]).get("exporte") or cible
    if not os.path.exists(cible):
        cible = os.path.join(os.path.expanduser("~"), "Videos", "clips_tiktok")
    if os.path.exists(cible):
        os.startfile(cible)  # noqa: S606 — chemin issu du projet, jamais du client
        return jsonify({"ok": True})
    return jsonify({"erreur": "Aucun clip exporté pour l'instant."}), 404


# ----------------------------------------------------------------------
# Fenêtre + arrêt automatique quand elle est fermée
# ----------------------------------------------------------------------
dernier_ping = [None]


@app.get("/api/version")
def version():
    return jsonify({"version": VERSION})


_maj = {"t": 0, "infos": None}


@app.get("/api/maj")
def verifier_maj():
    """Une version plus récente est-elle publiée ? (vérifié au plus toutes les 10 min)"""
    if time.time() - _maj["t"] > 600 and "OWNER/" not in lanceur.URL_VERSION:
        _maj["t"] = time.time()
        try:
            _maj["infos"] = json.loads(lanceur.lire(lanceur.URL_VERSION, timeout=5))
        except Exception:  # noqa: BLE001 — hors ligne : on réessaiera plus tard
            _maj["infos"] = None
    distante = (_maj["infos"] or {}).get("version")
    return jsonify({"locale": VERSION, "distante": distante,
                    "disponible": bool(distante and lanceur.plus_recente(distante, VERSION))})


@app.post("/api/maj/installer")
def installer_maj():
    """Relance l'appli par le lanceur, qui installe la mise à jour puis rouvre la fenêtre."""
    if occupe():
        return jsonify({"erreur": "Attends la fin du travail en cours (export ou analyse)."}), 409
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    subprocess.Popen([exe, os.path.join(APP, "lanceur.py"), "--apres-fermeture", str(os.getpid())],
                     cwd=APP, creationflags=lanceur.DETACHE, close_fds=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


@app.get("/api/ia")
def etat_ia():
    return jsonify({**modele_ia.etat, "actif": modele_ia.actif()})


@app.post("/api/ping")
def ping():
    dernier_ping[0] = time.time()
    return jsonify({"ok": True, "studio": True})


@app.post("/api/bye")
def bye():
    dernier_ping[0] = time.time() - 140
    return ("", 204)


def surveiller_fermeture():
    while True:
        time.sleep(10)
        if dernier_ping[0] and time.time() - dernier_ping[0] > 150 and not occupe():
            os._exit(0)


def ouvrir_fenetre(url):
    for edge in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(edge):
            profil = os.path.join(APP, ".fenetre")
            subprocess.Popen([edge, f"--app={url}", "--window-size=1480,920", f"--user-data-dir={profil}",
                              "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
            return
    webbrowser.open(url)


def port_deja_ouvert():
    try:
        with open(os.path.join(APP, ".studio-port")) as f:
            port = int(f.read().strip())
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            if json.load(r).get("studio"):
                return port
    except Exception:  # noqa: BLE001
        return None


_mutex = [None]


def premiere_instance():
    """Un seul serveur à la fois (double-clic répété sur le raccourci) : verrou système Windows."""
    try:
        import ctypes
        _mutex[0] = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\StudioClipsServeur")
        return ctypes.windll.kernel32.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 — hors Windows
        return True


def remettre_en_ordre():
    """Au démarrage : les analyses coupées par une fermeture brutale passent en « interrompu »."""
    for pid in os.listdir(PROJETS):
        if not re.fullmatch(r"[a-f0-9]{12}", pid):
            continue
        try:
            p = charger(pid)
            if p.get("etat") == "traitement":
                p["etat"] = "interrompu"
                sauver(p)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def main():
    if not premiere_instance():
        for _ in range(30):   # l'autre instance est peut-être encore en train de démarrer
            port = port_deja_ouvert()
            if port:
                ouvrir_fenetre(f"http://127.0.0.1:{port}/")
                return
            time.sleep(0.5)
        return
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)   # pas une ligne de journal par requête
    remettre_en_ordre()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with open(os.path.join(APP, ".studio-port"), "w") as f:
        f.write(str(port))
    threading.Thread(target=surveiller_fermeture, daemon=True).start()
    threading.Thread(target=modele_ia.preparer, daemon=True).start()
    if "--sans-fenetre" not in sys.argv:
        threading.Timer(1.0, ouvrir_fenetre, args=(f"http://127.0.0.1:{port}/",)).start()
    print(f"Studio Clips v{VERSION} sur http://127.0.0.1:{port}/", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
