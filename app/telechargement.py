"""
Télécharge une vidéo à partir d'un lien (YouTube, rediffusion Twitch, TikTok, Kick, Instagram,
X/Twitter… plus de 1 000 sites) avec yt-dlp.

- On peut ne prendre qu'un morceau (« de 1:20:00 à 2:05:00 ») : indispensable pour une rediff de 4 h.
- On privilégie du MP4 H.264, lisible directement par le lecteur de l'appli.
- Au-delà de 1 h 30 sans morceau choisi, on se limite au 720p (taille et temps divisés par ~2).
- Toutes les erreurs sont traduites en phrases simples.
"""

import os
import re
import sys
import time

DOSSIER = os.path.join(os.path.expanduser("~"), "Videos", "Studio Clips")
LIMITE_1080P = 90 * 60


class Annule(Exception):
    pass


def est_un_lien(texte):
    return bool(re.fullmatch(r"https?://\S+", (texte or "").strip()))


def trouver_lien(texte):
    m = re.search(r"https?://\S+", texte or "")
    return m.group(0).rstrip(".,;)»\"'") if m else None


def lire_heure(texte):
    """« 1:20:00 », « 80:00 », « 4800 », « 1h20 », « 1h20m30s » -> secondes ; vide -> None."""
    t = (texte or "").strip().lower().replace(" ", "")
    if not t:
        return None
    m = re.fullmatch(r"(\d+)h(\d{1,2})", t)   # « 1h20 » = 1 h 20 min
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)(?:m|min))?(?:(\d+)s?)?", t)
    if m and any(m.groups()) and ("h" in t or "m" in t):
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    parts = t.split(":")
    if all(p.isdigit() for p in parts) and 1 <= len(parts) <= 3:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    raise ValueError(f"heure illisible : « {texte} » (écris par exemple 1:20:00)")


def trouver_deno():
    """YouTube exige un moteur JavaScript (Deno) pour que yt-dlp voie tous les formats.
    Il est installé avec l'appli (paquet Python « deno ») : on le cherche à côté de Python."""
    import glob
    import shutil
    dossier_py = os.path.dirname(sys.executable)
    for chemin in [os.path.join(dossier_py, "deno.exe"), os.path.join(dossier_py, "Scripts", "deno.exe"),
                   shutil.which("deno") or "",
                   *glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "**", "deno.exe"), recursive=True)[:1]]:
        if chemin and os.path.exists(chemin):
            return chemin
    return None


class _Journal:
    """yt-dlp écrit dans ce journal plutôt que dans une console (il n'y en a pas)."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        print("yt-dlp :", msg, flush=True)

    def error(self, msg):
        print("yt-dlp ERREUR :", msg, flush=True)


def _message_clair(erreur):
    e = str(erreur).lower()
    if "unsupported url" in e:
        return "Ce lien n'est pas pris en charge. Colle le lien direct de la vidéo (pas celui d'une chaîne ou d'une recherche)."
    if "private" in e or "privée" in e:
        return "Cette vidéo est privée : je ne peux pas la télécharger."
    if "subscriber" in e or "sub-only" in e or "members" in e:
        return "Cette vidéo est réservée aux abonnés : je ne peux pas la télécharger."
    if "sign in" in e or "login" in e or "age" in e and "confirm" in e:
        return "Ce site demande d'être connecté pour voir cette vidéo : je ne peux pas la télécharger."
    if "not available in your country" in e or "geo" in e:
        return "Cette vidéo n'est pas disponible dans ton pays."
    if "404" in e or "does not exist" in e or "removed" in e or "unavailable" in e:
        return "Cette vidéo n'existe plus (supprimée ou lien incorrect)."
    if "live" in e and ("is live" in e or "upcoming" in e or "not yet" in e):
        return "Ce live n'est pas encore terminé : attends que la rediffusion soit disponible."
    if "no space" in e or "errno 28" in e:
        return "Le disque est plein : libère de la place puis réessaie."
    if "timed out" in e or "connection" in e or "network" in e or "resolve" in e:
        return "Problème de connexion internet pendant le téléchargement. Vérifie ta connexion puis clique sur Relancer."
    return "Je n'arrive pas à télécharger cette vidéo. Vérifie le lien, ou télécharge-la toi-même et choisis le fichier."


def telecharger(url, progression=lambda f, msg: None, arreter=lambda: False, debut=None, fin=None, ffmpeg=None):
    """Retourne (chemin_du_fichier, titre, duree). Lève RuntimeError (message clair) ou Annule."""
    import yt_dlp
    from yt_dlp.utils import DownloadError, download_range_func

    os.makedirs(DOSSIER, exist_ok=True)
    debut_dl = [time.time()]

    def suivi(d):
        if arreter():
            raise Annule()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            fait = d.get("downloaded_bytes") or 0
            if total:
                vitesse = d.get("speed") or 0
                reste = f", encore ~{max(1, int((total - fait) / vitesse / 60))} min" if vitesse else ""
                progression(min(fait / total, 1), f"{fait / 1048576:.0f} Mo sur {total / 1048576:.0f} Mo{reste}")
            elif d.get("fragment_count"):
                progression(min((d.get("fragment_index") or 0) / d["fragment_count"], 1), "")
        elif d.get("status") == "finished":
            progression(1, "Assemblage de la vidéo…")

    commun = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Journal(), "noplaylist": True,
              "windowsfilenames": True, "retries": 10, "fragment_retries": 10, "socket_timeout": 30}
    deno = trouver_deno()
    if deno:
        commun["js_runtimes"] = {"deno": {"path": deno}}
    if ffmpeg:
        commun["ffmpeg_location"] = ffmpeg
    try:
        with yt_dlp.YoutubeDL(commun) as ydl:
            infos = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise RuntimeError(_message_clair(e)) from None
    if infos.get("_type") == "playlist":
        entrees = [x for x in (infos.get("entries") or []) if x]
        if not entrees:
            raise RuntimeError("Ce lien est une liste vide : colle le lien d'une vidéo précise.")
        infos = entrees[0]
        url = infos.get("webpage_url") or infos.get("url") or url
    if infos.get("is_live") or infos.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("Ce live n'est pas encore terminé : attends que la rediffusion soit disponible, "
                           "puis colle le lien de la rediffusion.")
    duree = infos.get("duration")
    if duree and debut and debut >= duree:
        raise RuntimeError(f"Le début demandé dépasse la fin de la vidéo (elle dure {int(duree // 60)} min).")

    hauteur = 720 if (duree or 0) > LIMITE_1080P and not (debut or fin) else 1080
    suffixe = f" ({int((debut or 0) // 60)}-{int(fin // 60) if fin else 'fin'} min)" if (debut or fin) else ""
    options = {
        **commun,
        # MP4 H.264 + AAC en priorité : le lecteur de l'appli l'affiche sans conversion
        "format": (f"bv*[height<={hauteur}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={hauteur}]+ba/"
                   f"b[height<={hauteur}]/bv*+ba/b"),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(DOSSIER, f"%(title).80B [%(id)s]{suffixe}.%(ext)s"),
        "progress_hooks": [suivi],
        "concurrent_fragment_downloads": 4,
        "overwrites": False,
    }
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    if debut or fin:
        options["download_ranges"] = download_range_func(None, [(debut or 0, fin or float("inf"))])
        options["force_keyframes_at_cuts"] = True
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            resultat = ydl.process_ie_result(infos, download=True)
    except Annule:
        raise
    except DownloadError as e:
        if isinstance(getattr(e, "exc_info", [None, None])[1], Annule):
            raise Annule() from None
        raise RuntimeError(_message_clair(e)) from None
    except OSError as e:
        raise RuntimeError(_message_clair(e)) from None

    fichiers = [d.get("filepath") for d in (resultat.get("requested_downloads") or []) if d.get("filepath")]
    chemin = next((f for f in fichiers if os.path.exists(f)), None)
    if not chemin:
        raise RuntimeError("Le téléchargement s'est terminé sans fichier vidéo. Réessaie avec Relancer.")
    titre = resultat.get("title") or os.path.splitext(os.path.basename(chemin))[0]
    if debut or fin:
        duree = (min(fin, duree) if fin and duree else (fin or duree or 0)) - (debut or 0)
    print(f"Téléchargé en {time.time() - debut_dl[0]:.0f} s : {chemin}", file=sys.stdout, flush=True)
    return chemin, titre, duree
