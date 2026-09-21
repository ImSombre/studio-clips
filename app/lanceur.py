"""
Lanceur de Studio Clips (c'est lui qu'ouvre le raccourci du Bureau).

1. Regarde en ligne si une version plus récente existe (GitHub, releases).
2. Si oui : la télécharge et remplace les fichiers de l'appli (petite fenêtre de progression).
   Les projets, l'IA et l'installation Python ne sont jamais touchés.
3. Démarre l'IA locale (Ollama) si besoin, puis ouvre l'appli.

Sans internet ou si quoi que ce soit échoue, on ouvre simplement la version déjà installée.
Uniquement la bibliothèque standard : ce fichier doit marcher même si le reste est cassé.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

APP = os.path.dirname(os.path.abspath(__file__))
DEPOT = "ImSombre/studio-clips"   # remplacé à la publication
URL_VERSION = os.environ.get("STUDIO_MAJ_URL") or f"https://github.com/{DEPOT}/releases/latest/download/version.json"
GARDES = {".venv", "bin", "projets", ".fenetre", "studio.log", "lanceur.log", ".studio-port"}
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHE = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def journal(msg):
    try:
        with open(os.path.join(APP, "lanceur.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def version_locale():
    try:
        with open(os.path.join(APP, "studio.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([\d.]+)"', f.read(), re.M)
        return m.group(1) if m else "0"
    except OSError:
        return "0"


def plus_recente(a, b):
    """a > b ? (versions « 2.10 » > « 2.9 »)"""
    en_tuple = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))
    return en_tuple(a) > en_tuple(b)


def lire(url, timeout=6, progression=None):
    req = urllib.request.Request(url, headers={"User-Agent": "StudioClips"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        morceaux, recu = [], 0
        while True:
            bloc = r.read(65536)
            if not bloc:
                break
            morceaux.append(bloc)
            recu += len(bloc)
            if progression and total:
                progression(recu / total)
        return b"".join(morceaux)


def appliquer_mise_a_jour(infos, fenetre):
    url_zip = infos.get("zip") or URL_VERSION.rsplit("/", 1)[0] + "/studio-clips.zip"
    fenetre.etat(f"Téléchargement de la v{infos['version']}…", 0)
    donnees = lire(url_zip, timeout=30, progression=lambda f: fenetre.etat(None, int(f * 80)))

    fenetre.etat("Installation de la mise à jour…", 85)
    tmp = tempfile.mkdtemp(prefix="studio_maj_")
    try:
        zipfile.ZipFile(io.BytesIO(donnees)).extractall(tmp)
        racine = next((d for d, _, fichiers in os.walk(tmp) if "studio.py" in fichiers), None)
        if not racine:
            raise RuntimeError("archive de mise à jour invalide")
        anciens_besoins = _lire_texte(os.path.join(APP, "requirements.txt"))
        for nom in os.listdir(racine):
            if nom in GARDES:
                continue
            src, dst = os.path.join(racine, nom), os.path.join(APP, nom)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        if _lire_texte(os.path.join(APP, "requirements.txt")) != anciens_besoins:
            fenetre.etat("Installation des nouveaux composants…", 92)
            subprocess.run([_python("python.exe"), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-r", os.path.join(APP, "requirements.txt")], creationflags=NO_WIN, cwd=APP)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    journal(f"mise à jour installée : v{infos['version']}")
    fenetre.etat(f"Studio Clips v{infos['version']} est prêt !", 100)


def _lire_texte(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _python(nom):
    return os.path.join(APP, ".venv", "Scripts", nom)


def demarrer_ollama():
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2).close()
        return
    except Exception:  # noqa: BLE001 — pas lancé : on le démarre
        pass
    exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    if os.path.exists(exe):
        subprocess.Popen([exe, "serve"], creationflags=NO_WIN | DETACHE, close_fds=True)


def ouvrir_appli():
    exe = _python("pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    subprocess.Popen([exe, os.path.join(APP, "studio.py")], cwd=APP, creationflags=DETACHE, close_fds=True)


class Fenetre:
    """Petite fenêtre de progression, créée seulement s'il y a une mise à jour."""

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk.Tk()
        self.tk.title("Studio Clips")
        self.tk.configure(bg="#161412")
        self.tk.resizable(False, False)
        largeur, hauteur = 420, 150
        x = (self.tk.winfo_screenwidth() - largeur) // 2
        y = (self.tk.winfo_screenheight() - hauteur) // 2
        self.tk.geometry(f"{largeur}x{hauteur}+{x}+{y}")
        tk.Label(self.tk, text="● STUDIO CLIPS", fg="#ff4d2e", bg="#161412", font=("Segoe UI", 9, "bold")).pack(pady=(18, 4))
        self.texte = tk.Label(self.tk, text="Recherche de mises à jour…", fg="#f2ece3", bg="#161412", font=("Segoe UI", 11))
        self.texte.pack()
        style = ttk.Style(self.tk)
        style.theme_use("clam")
        style.configure("R.Horizontal.TProgressbar", troughcolor="#262220", background="#ff4d2e", bordercolor="#262220",
                        lightcolor="#ff4d2e", darkcolor="#ff4d2e")
        self.barre = ttk.Progressbar(self.tk, style="R.Horizontal.TProgressbar", length=340, maximum=100)
        self.barre.pack(pady=16)
        self._a_faire = None

    def etat(self, texte, pct):
        self._a_faire = (texte, pct)   # appliqué par la boucle Tk (jamais depuis un autre thread)

    def _rafraichir(self):
        if self._a_faire:
            texte, pct = self._a_faire
            if texte:
                self.texte.config(text=texte)
            if pct is not None:
                self.barre["value"] = pct
        self.tk.after(100, self._rafraichir)

    def lancer(self, travail):
        def fil():
            try:
                travail(self)
            except Exception as e:  # noqa: BLE001 — une mise à jour ratée ne doit jamais bloquer l'appli
                journal(f"mise à jour échouée : {e}")
                self.etat("Mise à jour impossible, ouverture de la version actuelle…", 100)
            self.tk.after(900, self.tk.destroy)
        threading.Thread(target=fil, daemon=True).start()
        self._rafraichir()
        self.tk.mainloop()


def main():
    threading.Thread(target=demarrer_ollama, daemon=True).start()
    infos = None
    if "OWNER/" not in URL_VERSION:
        try:
            infos = json.loads(lire(URL_VERSION, timeout=5))
        except Exception as e:  # noqa: BLE001 — pas d'internet, GitHub indisponible…
            journal(f"vérification impossible : {e}")
    locale = version_locale()
    if infos and infos.get("version") and plus_recente(infos["version"], locale):
        journal(f"mise à jour {locale} -> {infos['version']}")
        Fenetre().lancer(lambda f: appliquer_mise_a_jour(infos, f))
    ouvrir_appli()


if __name__ == "__main__":
    main()
