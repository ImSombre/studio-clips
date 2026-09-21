"""
Lanceur de Studio Clips (c'est lui qu'ouvre le raccourci du Bureau).

1. Affiche tout de suite une petite fenêtre « Ouverture de Studio Clips… » (le débutant ne reclique pas).
2. Regarde en ligne si une version plus récente existe (GitHub, releases).
3. Si oui : la prépare À CÔTÉ, vérifie qu'elle démarre, puis remplace l'ancienne.
   Au moindre problème, l'ancienne version est remise en place : l'appli ne peut pas être cassée
   par une mise à jour. Les projets, l'IA et l'installation Python ne sont jamais touchés.
4. Démarre l'IA locale (Ollama) si besoin, puis ouvre l'appli.

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
import time
import urllib.request
import zipfile

APP = os.path.dirname(os.path.abspath(__file__))
DEPOT = "ImSombre/studio-clips"   # remplacé à la publication
URL_VERSION = os.environ.get("STUDIO_MAJ_URL") or f"https://github.com/{DEPOT}/releases/latest/download/version.json"
GARDES = {".venv", "bin", "projets", ".fenetre", "studio.log", "studio.log.ancien", "lanceur.log", ".studio-port",
          ".sauvegarde-maj", "__pycache__"}
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHE = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def journal(msg):
    try:
        with open(os.path.join(APP, "lanceur.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def version_locale(dossier=APP):
    try:
        with open(os.path.join(dossier, "studio.py"), encoding="utf-8") as f:
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
        if total and recu < total:
            raise IOError("téléchargement incomplet")
        return b"".join(morceaux)


def lire_avec_delai(url, delai):
    """Comme lire(), mais on n'attend jamais plus de `delai` secondes (la résolution DNS n'a pas de timeout)."""
    resultat = {}

    def fil():
        try:
            resultat["ok"] = lire(url, timeout=delai)
        except Exception as e:  # noqa: BLE001
            resultat["err"] = e
    t = threading.Thread(target=fil, daemon=True)
    t.start()
    t.join(delai + 1)
    if "ok" in resultat:
        return resultat["ok"]
    raise resultat.get("err") or TimeoutError("pas de réponse")


def _python(nom):
    return os.path.join(APP, ".venv", "Scripts", nom)


def _lire_texte(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def appliquer_mise_a_jour(infos, fenetre):
    url_zip = infos.get("zip") or URL_VERSION.rsplit("/", 1)[0] + "/studio-clips.zip"
    fenetre.etat(f"Téléchargement de la v{infos['version']}…", 0)
    donnees = lire(url_zip, timeout=30, progression=lambda f: fenetre.etat(None, int(f * 70)))

    fenetre.etat("Préparation de la mise à jour…", 75)
    tmp = tempfile.mkdtemp(prefix="studio_maj_")
    sauvegarde = os.path.join(APP, ".sauvegarde-maj")
    remplaces = []
    try:
        zipfile.ZipFile(io.BytesIO(donnees)).extractall(tmp)
        racine = next((d for d, _, fichiers in os.walk(tmp) if "studio.py" in fichiers), None)
        if not racine or not plus_recente(version_locale(racine), "0"):
            raise RuntimeError("archive de mise à jour invalide")

        # 1. On met de côté les fichiers actuels (pour pouvoir tout remettre si ça rate).
        shutil.rmtree(sauvegarde, ignore_errors=True)
        os.makedirs(sauvegarde)
        for nom in os.listdir(APP):
            if nom not in GARDES:
                src = os.path.join(APP, nom)
                (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, os.path.join(sauvegarde, nom))
        anciens_besoins = _lire_texte(os.path.join(APP, "requirements.txt"))

        # 2. On copie la nouvelle version.
        fenetre.etat("Installation de la mise à jour…", 82)
        for nom in os.listdir(racine):
            if nom in GARDES:
                continue
            src, dst = os.path.join(racine, nom), os.path.join(APP, nom)
            remplaces.append(nom)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        # 3. Nouveaux composants Python si la liste a changé.
        if _lire_texte(os.path.join(APP, "requirements.txt")) != anciens_besoins:
            fenetre.etat("Installation des nouveaux composants…", 90)
            r = subprocess.run([_python("python.exe"), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                "-r", os.path.join(APP, "requirements.txt")], creationflags=NO_WIN, cwd=APP)
            if r.returncode != 0:
                raise RuntimeError("installation des composants impossible")

        # 4. La nouvelle version doit au moins démarrer, sinon on revient en arrière.
        fenetre.etat("Vérification…", 96)
        r = subprocess.run([_python("python.exe"), "-c", "import studio"], cwd=APP, capture_output=True,
                           creationflags=NO_WIN, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("la nouvelle version ne démarre pas : " + r.stderr.decode("utf-8", "replace")[-400:])
    except Exception:
        # Retour arrière : on remet exactement l'ancienne version.
        if os.path.isdir(sauvegarde):
            for nom in remplaces:
                dst = os.path.join(APP, nom)
                if not os.path.exists(os.path.join(sauvegarde, nom)):
                    shutil.rmtree(dst, ignore_errors=True) if os.path.isdir(dst) else _supprimer(dst)
            for nom in os.listdir(sauvegarde):
                src, dst = os.path.join(sauvegarde, nom), os.path.join(APP, nom)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(sauvegarde, ignore_errors=True)
    journal(f"mise à jour installée : v{infos['version']}")
    fenetre.etat(f"Studio Clips v{infos['version']} est prêt !", 100)


def _supprimer(chemin):
    try:
        os.remove(chemin)
    except OSError:
        pass


def demarrer_ollama():
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2).close()
        return
    except Exception:  # noqa: BLE001 — pas lancé : on le démarre
        pass
    for exe in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe"),
                shutil.which("ollama") or ""):
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, "serve"], creationflags=NO_WIN | DETACHE, close_fds=True)
            return


def appli_deja_ouverte():
    try:
        with open(os.path.join(APP, ".studio-port")) as f:
            port = int(f.read().strip())
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            return bool(json.load(r).get("studio"))
    except Exception:  # noqa: BLE001
        return False


def ouvrir_appli():
    exe = _python("pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    subprocess.Popen([exe, os.path.join(APP, "studio.py")], cwd=APP, creationflags=DETACHE, close_fds=True)


_verrou_systeme = [None]


def seul_lanceur():
    """Double-clic répété sur le raccourci : un seul lanceur travaille, les autres s'arrêtent."""
    try:
        import ctypes
        _verrou_systeme[0] = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\StudioClipsLanceur")
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:  # noqa: BLE001
        return True


def attendre_fin_processus(pid, delai=15):
    """Après « Installer la mise à jour » depuis l'appli : on attend qu'elle soit bien fermée."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
        if h:
            ctypes.windll.kernel32.WaitForSingleObject(h, int(delai * 1000))
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        time.sleep(3)


class Fenetre:
    """Petite fenêtre d'attente / de progression (Tk)."""

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk.Tk()
        self.tk.title("Studio Clips")
        self.tk.configure(bg="#161412")
        self.tk.resizable(False, False)
        self.tk.attributes("-topmost", True)
        largeur, hauteur = 420, 150
        x = (self.tk.winfo_screenwidth() - largeur) // 2
        y = (self.tk.winfo_screenheight() - hauteur) // 2
        self.tk.geometry(f"{largeur}x{hauteur}+{x}+{y}")
        tk.Label(self.tk, text="● STUDIO CLIPS", fg="#ff4d2e", bg="#161412", font=("Segoe UI", 9, "bold")).pack(pady=(18, 4))
        self.texte = tk.Label(self.tk, text="Ouverture de Studio Clips…", fg="#f2ece3", bg="#161412", font=("Segoe UI", 11))
        self.texte.pack()
        style = ttk.Style(self.tk)
        style.theme_use("clam")
        style.configure("R.Horizontal.TProgressbar", troughcolor="#262220", background="#ff4d2e", bordercolor="#262220",
                        lightcolor="#ff4d2e", darkcolor="#ff4d2e")
        self.barre = ttk.Progressbar(self.tk, style="R.Horizontal.TProgressbar", length=340, maximum=100,
                                     mode="indeterminate")
        self.barre.pack(pady=16)
        self.barre.start(12)
        self._a_faire = None

    def etat(self, texte, pct):
        self._a_faire = (texte, pct)   # appliqué par la boucle Tk (jamais depuis un autre thread)

    def _rafraichir(self):
        if self._a_faire:
            texte, pct = self._a_faire
            self._a_faire = None
            if texte:
                self.texte.config(text=texte)
            if pct is not None:
                if str(self.barre["mode"]) != "determinate":
                    self.barre.stop()
                    self.barre.config(mode="determinate")
                self.barre["value"] = pct
        self.tk.after(100, self._rafraichir)

    def lancer(self, travail, delai_fermeture=600):
        def fil():
            try:
                travail(self)
            except Exception as e:  # noqa: BLE001 — une mise à jour ratée ne doit jamais bloquer l'appli
                journal(f"mise à jour échouée : {e}")
                self.etat("Mise à jour impossible pour l'instant, ouverture de la version actuelle…", 100)
            self.tk.after(delai_fermeture, self.tk.destroy)
        threading.Thread(target=fil, daemon=True).start()
        self._rafraichir()
        self.tk.mainloop()


class SansFenetre:
    def etat(self, texte, pct):
        pass

    def lancer(self, travail, delai_fermeture=0):
        try:
            travail(self)
        except Exception as e:  # noqa: BLE001
            journal(f"mise à jour échouée : {e}")


def main():
    if not seul_lanceur():
        return   # un autre lanceur s'en occupe déjà
    if "--apres-fermeture" in sys.argv:
        attendre_fin_processus(sys.argv[sys.argv.index("--apres-fermeture") + 1])
    threading.Thread(target=demarrer_ollama, daemon=True).start()

    if appli_deja_ouverte():
        ouvrir_appli()   # l'appli tourne : elle rouvre juste sa fenêtre, on ne met rien à jour sous ses pieds
        return

    def travail(fenetre):
        if "OWNER/" in URL_VERSION:
            return
        try:
            infos = json.loads(lire_avec_delai(URL_VERSION, 6))
        except Exception as e:  # noqa: BLE001 — pas d'internet, GitHub indisponible…
            journal(f"vérification impossible : {e}")
            return
        locale = version_locale()
        if infos.get("version") and plus_recente(infos["version"], locale):
            journal(f"mise à jour {locale} -> {infos['version']}")
            appliquer_mise_a_jour(infos, fenetre)

    def travail_puis_ouvrir(fenetre):
        try:
            travail(fenetre)
        finally:
            ouvrir_appli()   # la petite fenêtre reste encore 3 s, le temps que l'appli s'affiche
            fenetre.etat("Ouverture de Studio Clips…", None)

    try:
        fenetre = Fenetre()
    except Exception as e:  # noqa: BLE001 — Tk indisponible : on travaille sans fenêtre
        journal(f"fenêtre d'attente impossible : {e}")
        fenetre = SansFenetre()
    fenetre.lancer(travail_puis_ouvrir, delai_fermeture=3000)


if __name__ == "__main__":
    main()
