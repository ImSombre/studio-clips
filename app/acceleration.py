"""
Accélération de la transcription par la carte graphique (NVIDIA).

- Au démarrage : si une carte NVIDIA est présente, on installe EN ARRIÈRE-PLAN les bibliothèques
  CUDA dont Whisper a besoin (cuBLAS + cuDNN 9, ~1 Go, une seule fois).
- Transcription : sur la carte graphique si tout est prêt (5 à 10× plus rapide, modèle plus précis),
  sinon sur le processeur. Au moindre problème avec la carte, retour automatique au processeur.
"""

import glob
import os
import subprocess
import sys
import sysconfig
import threading

NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
etat = {"gpu": None, "vram": 0, "etat": "verification", "message": ""}
_dll_activees = [False]


def detecter_nvidia():
    """(nom, mémoire en Mo) de la carte NVIDIA, ou None."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15, creationflags=NO_WIN)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        nom, memoire = r.stdout.strip().splitlines()[0].rsplit(",", 1)
        return nom.strip(), int(float(memoire))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _dossiers_cuda():
    racine = sysconfig.get_paths()["purelib"]
    return [d for d in glob.glob(os.path.join(racine, "nvidia", "*", "bin")) if os.path.isdir(d)]


def bibliotheques_presentes():
    fichiers = {os.path.basename(f).lower() for d in _dossiers_cuda() for f in glob.glob(os.path.join(d, "*.dll"))}
    return any(f.startswith("cublas64_12") for f in fichiers) and any(f.startswith("cudnn64_9") for f in fichiers)


def activer_dll():
    """Indique à Windows où trouver les DLL CUDA installées par pip."""
    if _dll_activees[0]:
        return
    for d in _dossiers_cuda():
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    _dll_activees[0] = True


def preparer(app_dir):
    """À lancer au démarrage, dans un thread. Ne fait rien hors de l'appli installée."""
    carte = detecter_nvidia()
    if not carte:
        etat.update(etat="absent")
        return
    etat.update(gpu=carte[0], vram=carte[1])
    if not os.path.isdir(os.path.join(app_dir, ".venv")):
        etat.update(etat="absent")
        return
    if not bibliotheques_presentes():
        etat.update(etat="installation", message=f"Accélération par la carte graphique ({carte[0]}) : installation, une seule fois…")
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        try:
            r = subprocess.run([exe, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"],
                               capture_output=True, creationflags=NO_WIN, timeout=3600)
            ok = r.returncode == 0 and bibliotheques_presentes()
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if not ok:
            etat.update(etat="erreur", message="")   # on reste sur le processeur, sans déranger l'utilisateur
            return
    activer_dll()
    etat.update(etat="pret", message="")


def choix_transcription(ram_go):
    """(modèle, appareil, précision, largeur de recherche) selon la machine."""
    if etat["etat"] == "pret":
        activer_dll()
        return ("large-v3-turbo" if etat["vram"] >= 5500 else "small"), "cuda", "int8_float16", 5
    # processeur : recherche simple (beam 1), ~2x plus rapide pour une qualité quasi identique
    return ("small" if ram_go >= 15 else "base"), "cpu", "int8", 1


def lancer_en_fond(app_dir):
    threading.Thread(target=preparer, args=(app_dir,), daemon=True).start()
