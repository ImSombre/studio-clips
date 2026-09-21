"""
Choix et préparation de l'IA locale (Ollama).

Le modèle est choisi selon la mémoire du PC, puis téléchargé AUTOMATIQUEMENT
au lancement de l'appli s'il manque (barre de progression dans l'interface).
Tant qu'il n'est pas prêt, on se rabat sur un modèle déjà installé.
"""

import ctypes
import json
import threading

import requests

OLLAMA = "http://localhost:11434"

# Qwen3 comprend bien mieux les consignes en français que llama3.x à taille égale.
PREFERENCES = ["qwen3:8b", "qwen3:4b", "qwen3:1.7b", "llama3.1", "llama3.2", "llama3.2:1b"]

etat = {"modele": None, "voulu": None, "etat": "verification", "pct": 0, "message": ""}


def ram_go():
    class MEMOIRE(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        m = MEMOIRE()
        m.dwLength = ctypes.sizeof(MEMOIRE)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1024 ** 3
    except Exception:  # noqa: BLE001 — hors Windows : on suppose un PC moyen
        return 8.0


def modele_voulu():
    ram = ram_go()
    if ram >= 15:
        return "qwen3:8b"
    if ram >= 7:
        return "qwen3:4b"
    return "qwen3:1.7b"


def installes():
    r = requests.get(f"{OLLAMA}/api/tags", timeout=5)
    r.raise_for_status()
    noms = set()
    for m in r.json().get("models", []):
        nom = m.get("name", "")
        noms.add(nom)
        if nom.endswith(":latest"):
            noms.add(nom[:-7])
    return noms


def actif():
    """Modèle à utiliser maintenant (le meilleur déjà installé)."""
    return etat["modele"] or etat["voulu"] or modele_voulu()


def _choisir_parmi(presents):
    voulu = etat["voulu"]
    if voulu in presents:
        return voulu
    # Pas encore le bon : le meilleur déjà présent, sans dépasser la taille voulue
    ordre = PREFERENCES[PREFERENCES.index(voulu):] if voulu in PREFERENCES else PREFERENCES
    for nom in ordre + PREFERENCES:
        if nom in presents:
            return nom
    return None


def _demarrer_ollama():
    """Relance le moteur d'IA s'il ne tourne pas (installé pour l'utilisateur ou pour tout le PC)."""
    import os
    import shutil
    import subprocess
    for exe in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe"),
                shutil.which("ollama") or ""):
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             close_fds=True)
            return True
    return False


def preparer():
    """À lancer au démarrage, dans un thread : télécharge le modèle voulu s'il manque.
    Si Ollama ne répond pas, on le relance et on réessaie en arrière-plan, sans jamais abandonner."""
    etat["voulu"] = modele_voulu()
    essai = 0
    while True:
        try:
            presents = installes()
            break
        except Exception:  # noqa: BLE001 — Ollama pas (encore) démarré
            essai += 1
            if essai == 5:
                _demarrer_ollama()
            if essai >= 30:
                etat.update(etat="erreur", message="L'IA démarre… (si ce message reste, redémarre le PC)")
            threading.Event().wait(2 if essai < 30 else 15)
    etat["message"] = ""
    etat["modele"] = _choisir_parmi(presents)
    if etat["voulu"] in presents:
        etat.update(etat="pret", pct=100)
        return

    etat.update(etat="telechargement", pct=0, message=f"Téléchargement de la nouvelle IA ({etat['voulu']})")
    try:
        with requests.post(f"{OLLAMA}/api/pull", json={"model": etat["voulu"], "stream": True},
                           stream=True, timeout=(10, 600)) as r:
            for ligne in r.iter_lines():
                if not ligne:
                    continue
                d = json.loads(ligne)
                if d.get("error"):
                    raise RuntimeError(d["error"])
                if d.get("total"):
                    etat["pct"] = int(d.get("completed", 0) / d["total"] * 100)
        etat.update(modele=etat["voulu"], etat="pret", pct=100, message="")
    except Exception as e:  # noqa: BLE001 — sans internet on garde l'ancien modèle
        etat.update(etat="pret" if etat["modele"] else "erreur",
                    message=f"Nouvelle IA non téléchargée ({e}). J'utilise {etat['modele'] or 'aucune IA'}.")


def attendre_pret(pendant=None):
    """Bloque tant que le téléchargement tourne (appelé par l'analyse d'une vidéo).
    La simple vérification ne peut pas bloquer plus de 2 minutes : on utilise alors ce qui est installé."""
    if etat["etat"] in ("verification", "erreur"):
        # un modèle est déjà installé ? alors rien à attendre : on s'en sert tout de suite
        try:
            presents = installes()
        except Exception:  # noqa: BLE001 — Ollama ne répond pas encore : on attend comme avant
            presents = set()
        choisi = _choisir_parmi(presents) if presents else None
        if choisi:
            etat.update(modele=choisi, etat="pret", pct=100, message="")
    attente_verif = 0
    while etat["etat"] == "telechargement" or (etat["etat"] in ("verification", "erreur") and attente_verif < 180):
        if pendant:
            pendant(etat)
        if etat["etat"] != "telechargement":
            attente_verif += 1   # Ollama démarre : on lui laisse 3 minutes avant d'essayer quand même
        threading.Event().wait(1)
