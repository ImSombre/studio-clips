"""
Studio Clips — lance l'appli : un petit serveur local + une fenêtre (Edge en mode appli).

Tout reste sur le PC : la vidéo n'est jamais envoyée sur internet.
Les projets sont rangés dans le dossier « projets » à côté de ce fichier.
"""

import copy
import json
import os
import re
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
    _log = open(os.path.join(APP, "studio.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

from flask import Flask, jsonify, request, send_file, abort  # noqa: E402

import assistant  # noqa: E402
import lanceur  # noqa: E402
import modele_ia  # noqa: E402
import montage  # noqa: E402
from analyze import find_best_clips  # noqa: E402
from transcribe import transcribe_video, get_video_duration  # noqa: E402

VERSION = "2.6"
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FORMATS_LISIBLES = (".mp4", ".m4v", ".webm", ".mov")

app = Flask(__name__, static_folder=os.path.join(APP, "static"), static_url_path="/static")
verrou = threading.RLock()
verrou_export = threading.Lock()   # un seul encodage à la fois : un PC modeste ne suit pas plus
jobs = {}
cache = {}


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
            with open(chemin, encoding="utf-8") as f:
                cache[pid] = json.load(f)
        return cache[pid]


def sauver(projet):
    with verrou:
        dossier = _dossier(projet["id"])
        os.makedirs(dossier, exist_ok=True)
        tmp = os.path.join(dossier, "projet.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(projet, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(dossier, "projet.json"))
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
    v["jobs"] = [j for j in jobs.values() if j["projet"] == p["id"]]
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
    jobs[jid] = job

    def corps():
        try:
            fonction(job)
            job["etat"], job["pct"] = "fini", 100
        except Exception as e:  # noqa: BLE001 — tout échec doit remonter dans l'interface
            traceback.print_exc()
            job["etat"], job["erreur"] = "erreur", str(e)
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
    duree = get_video_duration(source) or 1
    cmd = ["ffmpeg", "-y", "-i", source, "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", sortie + ".tmp.mp4"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=NO_WIN)
    except FileNotFoundError:
        raise RuntimeError("FFmpeg est introuvable : relance INSTALLER.bat.") from None
    for ligne in proc.stdout:
        if ligne.startswith("out_time_us="):
            try:
                t = int(ligne.split("=")[1]) / 1e6
                job["pct"] = int(pct_debut + (pct_fin - pct_debut) * min(1, t / duree))
            except ValueError:
                pass
    if proc.wait() != 0:
        raise RuntimeError("Impossible de préparer l'aperçu de cette vidéo.")
    os.replace(sortie + ".tmp.mp4", sortie)


def job_analyse(pid):
    def travail(job):
        p = charger(pid)
        p["etat"], p["erreur"] = "traitement", None
        sauver(p)
        try:
            if not p["source"].lower().endswith(FORMATS_LISIBLES) and not os.path.exists(_chemin_apercu(pid)):
                _fabriquer_apercu(pid, p["source"], job, 0, 8)

            if not p.get("segments"):
                job["etape"] = "Transcription de la vidéo"

                def log_transcription(msg):
                    job["message"] = msg.strip()
                    m = re.search(r"(\d+) % transcrit", msg)
                    if m:
                        job["pct"] = 8 + int(int(m.group(1)) * 0.57)
                job["pct"] = 8
                # Modèle de transcription selon la puissance du PC (base = ~3x plus rapide que small)
                taille_whisper = "small" if modele_ia.ram_go() >= 15 else "base"
                segments, _ = transcribe_video(p["source"], model_size=taille_whisper, log=log_transcription)
                p["segments"] = segments
                p["duree"] = get_video_duration(p["source"]) or (segments[-1]["end"] if segments else 0)
                sauver(p)

            job["pct"] = 66
            modele_ia.attendre_pret(lambda e: job.update(etape=f"Téléchargement de l'IA ({e['pct']} %)"))
            job["etape"] = "L'IA choisit les meilleurs passages"

            def log_analyse(msg):
                job["message"] = msg.strip()
                m = re.search(r"partie (\d+)/(\d+)", msg)
                if m:
                    job["pct"] = 66 + int(32 * (int(m.group(1)) - 1) / int(m.group(2)))

            bornes = assistant.fourchette_duree(p.get("consigne", ""))
            options = {"clip_min": bornes[0], "clip_max": bornes[1]} if bornes else {}
            trouves = find_best_clips(
                _texte_horodate(p["segments"]), model=modele_ia.actif(), log=log_analyse,
                custom_instructions=p.get("consigne", ""),
                video_duration=p.get("duree"), **options,
            )
            p["clips"] = [nouveau_clip(c, p["segments"]) for c in trouves]
            p["etat"] = "pret"
            if p["clips"]:
                dire(p, f"J'ai trouvé {len(p['clips'])} clip(s). Clique sur un clip pour le regarder, "
                        "puis dis-moi ce que tu veux changer : « coupe les 3 premières secondes », "
                        "« sous-titres en jaune plus gros », « mets-le en plein écran »…")
            else:
                dire(p, "Je n'ai trouvé aucun passage assez fort. Essaie « trouve-moi un passage drôle » "
                        "ou relance avec une autre consigne.")
            sauver(p)
        except Exception as e:
            p["etat"], p["erreur"] = "erreur", str(e)
            sauver(p)
            raise
    return lancer_job(pid, "analyse", travail, etape="Démarrage")


def job_nouveau_clip(pid, consigne):
    def travail(job):
        p = charger(pid)
        job["etape"] = "L'IA cherche un nouveau passage"

        def log(msg):
            job["message"] = msg.strip()
            m = re.search(r"partie (\d+)/(\d+)", msg)
            if m:
                job["pct"] = int(95 * (int(m.group(1)) - 1) / int(m.group(2)))

        bornes = assistant.fourchette_duree(consigne) or assistant.fourchette_duree(p.get("consigne", ""))
        options = {"clip_min": bornes[0], "clip_max": bornes[1]} if bornes else {}
        modele_ia.attendre_pret()
        trouves = find_best_clips(_texte_horodate(p["segments"]), model=modele_ia.actif(), log=log,
                                  custom_instructions=consigne,
                                  video_duration=p.get("duree"), max_clips=5, **options)
        existants = p["clips"]

        def recouvre(c):
            return any(min(c["end"], e["fin"]) - max(c["start"], e["debut"]) > 0.5 * (c["end"] - c["start"])
                       for e in existants)
        libres = sorted((c for c in trouves if not recouvre(c)), key=lambda c: -c["score"])
        if not libres:
            dire(p, "Je n'ai pas trouvé de nouveau passage qui colle. Essaie de préciser (un sujet, un moment…).")
        else:
            c = nouveau_clip(libres[0], p["segments"])
            p["clips"].append(c)
            p["clips"].sort(key=lambda x: x["debut"])
            job["resultat"] = c["id"]
            dire(p, f"Trouvé : « {c['titre']} » (note {c['note']:.0f}/10). {c['raison']}".strip(), c["id"])
        sauver(p)
    return lancer_job(pid, "nouveau_clip", travail, etape="Recherche")


def job_export(pid, cid):
    def travail(job):
        job["etape"] = "En attente de l'export précédent"
        with verrou_export:
            p = charger(pid)
            c = clip_de(p, cid)
            job["etape"] = f"Export de « {c['titre']} »"
            sortie_dir = os.path.join(os.path.dirname(p["source"]), "clips_tiktok")
            os.makedirs(sortie_dir, exist_ok=True)
            nom = "".join(ch for ch in c["titre"] if ch.isalnum() or ch in " -_").strip()[:50] or "clip"
            sortie = montage.chemin_unique(os.path.join(sortie_dir, f"{nom}.mp4"))

            def prog(pct):
                job["pct"] = pct
            a_exporter = copy.deepcopy(c)
            a_exporter["numero"] = p["clips"].index(c) + 1
            montage.exporter_clip(p["source"], p["segments"], a_exporter, sortie, prog)
            c["exporte"] = sortie
            job["resultat"] = sortie
            taille = montage.dimensions(sortie)
            format_ = f" — {taille[0]}×{taille[1]}" + (" (format TikTok ✔)" if taille and taille[0] < taille[1] else "") if taille else ""
            dire(p, f"Clip « {c['titre']} » exporté{format_}, {c['fin'] - c['debut']:.0f} s. "
                    "Il est dans le dossier clips_tiktok, à côté de ta vidéo.", cid)
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
        try:
            p = charger(pid)
        except Exception:  # noqa: BLE001 — un projet abîmé ne doit pas bloquer l'accueil
            continue
        if p["etat"] == "traitement" and not any(j["projet"] == pid and j["etat"] == "en_cours" for j in jobs.values()):
            p["etat"] = "interrompu"
        res.append({"id": pid, "nom": p["nom"], "cree": p["cree"], "etat": p["etat"],
                    "clips": len(p["clips"]), "exportes": sum(1 for c in p["clips"] if c.get("exporte")),
                    "premier": p["clips"][0]["id"] if p["clips"] else None})
    res.sort(key=lambda x: x["cree"], reverse=True)
    return jsonify(res)


@app.post("/api/choisir")
def choisir_video():
    code = (
        "import tkinter as tk; from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = filedialog.askopenfilename(title='Choisis ta vidéo', filetypes=[('Vidéos', "
        "'*.mp4 *.mov *.mkv *.avi *.m4v *.webm'), ('Tous les fichiers', '*.*')])\n"
        "print(p or '')"
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    r = subprocess.run([exe, "-c", code], capture_output=True, text=True, encoding="utf-8", env=env,
                       creationflags=NO_WIN)
    chemin = r.stdout.strip()
    return jsonify({"chemin": os.path.normpath(chemin) if chemin else ""})


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
    sauver(p)
    job_analyse(pid)
    return jsonify({"id": pid})


@app.get("/api/projets/<pid>")
def lire_projet(pid):
    return jsonify(vue_projet(charger(pid)))


@app.delete("/api/projets/<pid>")
def supprimer_projet(pid):
    import shutil
    with verrou:
        charger(pid)
        cache.pop(pid, None)
        shutil.rmtree(_dossier(pid), ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/projets/<pid>/relancer")
def relancer(pid):
    d = request.get_json(silent=True) or {}
    p = charger(pid)
    if "consigne" in d:
        p["consigne"] = d["consigne"]
    sauver(p)
    job_analyse(pid)
    return jsonify({"ok": True})


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
    if os.path.exists(_chemin_apercu(pid)) or any(
            j["projet"] == pid and j["type"] == "apercu" and j["etat"] == "en_cours" for j in jobs.values()):
        return jsonify({"ok": True})
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
    if not os.path.exists(chemin) and not montage.miniature(p["source"], instant, chemin):
        abort(404)
    return send_file(chemin, max_age=3600)


@app.patch("/api/projets/<pid>/clips/<cid>")
def modifier_clip(pid, cid):
    d = request.get_json(force=True)
    with verrou:
        p = charger(pid)
        c = clip_de(p, cid)
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
        assistant.appliquer(p, c, actions)
        c["exporte"] = c.get("exporte") if not actions else None
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
            c["exporte"] = None
        sauver(p)
        return jsonify(vue_projet(p, avec_segments=False))


@app.post("/api/projets/<pid>/clips/<cid>/annuler")
def annuler_clip(pid, cid):
    with verrou:
        p = charger(pid)
        c = clip_de(p, cid)
        assistant.annuler(c)
        sauver(p)
        return jsonify(vue_clip(c, p))


@app.delete("/api/projets/<pid>/clips/<cid>")
def supprimer_clip(pid, cid):
    with verrou:
        p = charger(pid)
        clip_de(p, cid)
        p["clips"] = [c for c in p["clips"] if c["id"] != cid]
        sauver(p)
    return jsonify({"ok": True})


@app.post("/api/projets/<pid>/clips/<cid>/exporter")
def exporter(pid, cid):
    clip_de(charger(pid), cid)
    return jsonify(job_export(pid, cid))


@app.post("/api/projets/<pid>/exporter-tout")
def exporter_tout(pid):
    p = charger(pid)
    return jsonify([job_export(pid, c["id"]) for c in p["clips"]])


@app.post("/api/projets/<pid>/chat")
def chat(pid):
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()[:1000]
    if not message:
        return jsonify({"erreur": "Message vide."}), 400
    p = charger(pid)
    cid = d.get("clip")
    with verrou:
        p.setdefault("chat", []).append({"role": "moi", "texte": message, "cid": cid, "t": time.time()})
        sauver(p)
    if not p["clips"] or not cid:
        # Pas de clip ouvert : seule la recherche d'un passage a du sens.
        job_nouveau_clip(pid, message)
        with verrou:
            dire(p, "Je fouille la vidéo pour te trouver un passage, ça peut prendre quelques minutes.")
            sauver(p)
        return jsonify(vue_projet(p, avec_segments=False))

    c = clip_de(p, cid)
    historique = [h for h in p["chat"][:-1] if h.get("cid") == cid]
    # L'IA peut mettre du temps : on travaille sur une copie, puis on applique d'un coup.
    copie = copy.deepcopy(c)
    reponse, speciales, _, actions = assistant.traiter_message(p, copie, message, historique)
    with verrou:
        empreinte = lambda x: json.dumps([x.get(k) for k in ("debut", "fin", "style", "cadrage", "titre")])
        avant = empreinte(c)
        c.clear()
        c.update(copie)
        if "annuler" in speciales:
            reponse = (reponse + " " if reponse else "") + (
                "J'ai annulé la dernière modif." if assistant.annuler(c) else "Il n'y a rien à annuler.")
        if empreinte(c) != avant:
            c["exporte"] = None   # le MP4 déjà fabriqué ne correspond plus
        if "tous" in speciales:
            # Même demande appliquée aux autres clips (sauf ce qui n'a de sens que pour un seul).
            communes = [a for a in actions if isinstance(a, dict) and a.get("type") in
                        ("couper_debut", "couper_fin", "duree", "sous_titres", "cadrage", "texte_ecran")]
            for autre in p["clips"]:
                if autre is not c and communes:
                    assistant.appliquer(p, autre, copy.deepcopy(communes))
                    autre["exporte"] = None
        dire(p, reponse.strip() or "C'est noté.", cid)
        sauver(p)
    for s in speciales:
        if s == "exporter":
            job_export(pid, cid)
        elif isinstance(s, tuple) and s[0] == "nouveau_clip":
            job_nouveau_clip(pid, s[1] or message)
    return jsonify(vue_projet(p, avec_segments=False))


@app.post("/api/projets/<pid>/ouvrir")
def ouvrir(pid):
    d = request.get_json(silent=True) or {}
    p = charger(pid)
    cible = os.path.join(os.path.dirname(p["source"]), "clips_tiktok")
    if d.get("clip"):
        cible = clip_de(p, d["clip"]).get("exporte") or cible
    if os.path.exists(cible):
        os.startfile(cible)  # noqa: S606 — chemin issu du projet, jamais du client
    return jsonify({"ok": True})


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
    if any(j["etat"] == "en_cours" for j in jobs.values()):
        return jsonify({"erreur": "Attends la fin du travail en cours (export ou analyse)."}), 409
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    subprocess.Popen([exe, os.path.join(APP, "lanceur.py")], cwd=APP, creationflags=lanceur.DETACHE, close_fds=True)
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
        occupe = any(j["etat"] == "en_cours" for j in jobs.values())
        if dernier_ping[0] and time.time() - dernier_ping[0] > 150 and not occupe:
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


def main():
    port = port_deja_ouvert()
    if port:  # déjà lancé : on rouvre juste la fenêtre
        ouvrir_fenetre(f"http://127.0.0.1:{port}/")
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with open(os.path.join(APP, ".studio-port"), "w") as f:
        f.write(str(port))
    threading.Thread(target=surveiller_fermeture, daemon=True).start()
    threading.Thread(target=modele_ia.preparer, daemon=True).start()
    if "--sans-fenetre" not in sys.argv:
        threading.Timer(1.0, ouvrir_fenetre, args=(f"http://127.0.0.1:{port}/",)).start()
    print(f"Studio Clips sur http://127.0.0.1:{port}/", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
