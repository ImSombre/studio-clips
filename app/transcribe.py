"""
Transcrit une vidéo en texte avec des horodatages précis, en utilisant
faster-whisper. Tourne 100% en local, gratuit.

On extrait d'abord l'audio proprement avec FFmpeg (en wav mono 16kHz)
avant de le donner à Whisper : certains fichiers vidéo (notamment
issus de captures d'écran ou de certains téléphones) ont des métadonnées
de durée mal formées qui font planter la lecture directe en silence
après quelques dizaines de secondes. Passer par un wav propre règle
ce problème dans la quasi-totalité des cas.

On demande aussi l'horodatage MOT PAR MOT : c'est ce qui permet d'afficher
des sous-titres courts (3-4 mots à la fois) façon TikTok au lieu de
phrases entières.
"""

import os
import re
import shutil
import subprocess
import tempfile

NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Phrases que Whisper « invente » sur de la musique ou du silence (génériques de sous-titrage).
HALLUCINATIONS = re.compile(
    r"amara\.org|sous-titr(es|age) (réalisés?|par|fait)|merci d'avoir regardé|abonnez-vous|"
    r"thanks? for watching|subtitles by|like and subscribe|продолжение следует|字幕", re.I)


def _executer(cmd):
    """FFmpeg/ffprobe : sortie lue en UTF-8 (un emoji dans le nom de fichier ne doit rien casser)."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WIN)


def get_video_duration(video_path: str):
    """Durée réelle de la vidéo en secondes (via ffprobe), ou None si illisible."""
    try:
        r = _executer(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", video_path])
        return float(r.stdout.strip())
    except (OSError, ValueError):
        return None


def a_du_son(video_path: str):
    """True / False, ou None si ffprobe ne sait pas répondre."""
    try:
        r = _executer(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                       "-of", "csv=p=0", video_path])
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def codec_video(video_path: str):
    """Nom du codec de la 1ʳᵉ piste vidéo (« h264 », « hevc »…) ou None."""
    try:
        r = _executer(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                       "-of", "csv=p=0", video_path])
        return r.stdout.strip().splitlines()[0].strip() or None
    except (OSError, IndexError):
        return None


def dossier_modeles():
    """Dossier des modèles Whisper. CTranslate2 lit mal les chemins avec accents :
    si le profil Windows en contient (« C:\\Users\\Élodie »), on range les modèles ailleurs."""
    profil = os.path.expanduser("~")
    if profil.isascii():
        return None   # emplacement par défaut (cache Hugging Face)
    racine = os.environ.get("ProgramData", r"C:\ProgramData")
    dossier = os.path.join(racine, "StudioClips", "whisper")
    os.makedirs(dossier, exist_ok=True)
    return dossier


def _extract_audio(video_path: str, tmp_dir: str, log, debut=None, fin=None) -> str:
    """Extrait l'audio de la vidéo (ou d'une partie) dans un fichier .wav temporaire propre."""
    tmp_wav = os.path.join(tmp_dir, "audio.wav")
    log("  Extraction de l'audio (FFmpeg)...")
    zone = (["-ss", f"{debut:.3f}"] if debut else []) + (["-to", f"{fin:.3f}"] if fin else [])
    try:
        result = _executer(["ffmpeg", "-y", *zone, "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
                            "-acodec", "pcm_s16le", tmp_wav])
    except FileNotFoundError:
        raise RuntimeError("FFmpeg est introuvable. Réinstalle Studio Clips avec Studio-Clips.exe.") from None
    if result.returncode != 0 or not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) < 1000:
        print("Extraction audio impossible :", result.stderr[-1500:], flush=True)
        if a_du_son(video_path) is False:
            raise RuntimeError("Cette vidéo n'a pas de son : je ne peux pas trouver de passages parlés dedans.")
        raise RuntimeError("Je n'arrive pas à lire le son de cette vidéo. Le fichier est peut-être abîmé : "
                           "essaie de le réexporter depuis l'appli d'origine.")
    log(f"  Audio extrait ({os.path.getsize(tmp_wav) / 1048576:.1f} Mo).")
    return tmp_wav


def transcribe_video(video_path: str, model_size: str = "small", log=print, device="cpu", compute="int8",
                     beam=None, debut=None, fin=None):
    """
    Transcrit la vidéo.

    Retourne (segments, texte_horodate, langue) :
        segments : liste de dicts {start, end, text, words: [{start, end, text}]}
        langue   : code de la langue détectée (« fr », « en »…)
    `log` peut lever une exception pour interrompre la transcription (bouton Arrêter).
    """
    tmp_dir = tempfile.mkdtemp(prefix="studio_audio_")
    decalage = float(debut or 0)   # transcription d'une partie : on remet les temps dans la vidéo entière
    try:
        audio_path = _extract_audio(video_path, tmp_dir, log, debut, fin)
        try:
            return _transcrire(audio_path, model_size, log, device, compute, beam, decalage)
        except Exception as e:  # noqa: BLE001
            # « Arrêter » cliqué, ou vrai problème de modèle : on ne relance surtout pas sur le processeur
            if device == "cpu" or type(e).__name__ == "Arret" or (isinstance(e, RuntimeError) and "prêt" in str(e)):
                raise
            # souci avec la carte graphique (pilote, mémoire…) : on refait tout sur le processeur
            print("Transcription sur carte graphique impossible, retour au processeur :", repr(e), flush=True)
            log("  La carte graphique n'a pas pu servir : transcription sur le processeur…")
            return _transcrire(audio_path, "small" if model_size != "base" else "base", log, "cpu", "int8", 1, decalage)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _transcrire(audio_path, model_size, log, device, compute, beam, decalage):
    from faster_whisper import WhisperModel

    log(f"  Chargement du modèle de transcription « {model_size} » ({'carte graphique' if device == 'cuda' else 'processeur'})...")
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute, download_root=dossier_modeles(),
                             cpu_threads=(os.cpu_count() or 4) if device == "cpu" else 0)
    except Exception as e:  # noqa: BLE001 — modèle absent et pas d'internet, disque plein…
        print("Chargement Whisper impossible :", repr(e), flush=True)
        if device == "cuda":
            raise
        raise RuntimeError("Le module de transcription n'est pas prêt. Il faut internet une fois pour le "
                           "télécharger : vérifie ta connexion puis clique sur Relancer.") from None

    log("  Transcription en cours...")
    if beam is None:
        beam = 5 if device == "cuda" else 1
    segments_raw, info = model.transcribe(
        audio_path, beam_size=beam, word_timestamps=True, vad_filter=True,
        # sans ça, Whisper peut répéter la même phrase en boucle sur de la musique
        condition_on_previous_text=False,
    )
    segments, lignes = [], []
    duree = info.duration or 0
    palier = 0
    for seg in segments_raw:   # générateur : la transcription se fait pendant cette boucle
            if duree:
                pct = int(min(seg.end / duree, 1) * 100)
                if pct >= palier + 5:
                    palier = pct - pct % 5
                    log(f"  ... {palier} % transcrit ({seg.end / 60:.0f} min sur {duree / 60:.0f})")
            text = seg.text.strip()
            if not text or getattr(seg, "no_speech_prob", 0) > 0.6 or HALLUCINATIONS.search(text):
                continue
            start, end = round(seg.start + decalage, 2), round(seg.end + decalage, 2)
            words = [{"start": round(w.start + decalage, 2), "end": round(w.end + decalage, 2), "text": w.word.strip()}
                     for w in (seg.words or []) if w.word.strip()]
            if segments and segments[-1]["text"] == text and start - segments[-1]["end"] < 2:
                continue   # même phrase répétée à la suite = hallucination
            segments.append({"start": start, "end": end, "text": text, "words": words})
            lignes.append(f"[{start:.2f}s -> {end:.2f}s] {text}")

    parle = segments[-1]["end"] - segments[0]["start"] if segments else 0
    log(f"  Langue détectée : {info.language} | parole détectée sur ~{parle:.0f}s")
    return segments, "\n".join(lignes), info.language or "fr"
