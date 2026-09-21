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
import shutil
import subprocess
import tempfile

from faster_whisper import WhisperModel


def get_video_duration(video_path: str):
    """Durée réelle de la vidéo en secondes (via ffprobe), ou None si illisible."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError):
        return None


def _extract_audio(video_path: str, tmp_dir: str, log) -> str:
    """Extrait l'audio de la vidéo dans un fichier .wav temporaire propre."""
    # Dossier temporaire propre à ce traitement : deux lancements en même
    # temps ne se marchent plus dessus.
    tmp_wav = os.path.join(tmp_dir, "audio.wav")

    log("  Extraction de l'audio (FFmpeg)...")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",                # pas de vidéo
        "-ac", "1",            # mono
        "-ar", "16000",        # 16kHz, format attendu par Whisper
        "-acodec", "pcm_s16le",
        tmp_wav,
    ]
    result = subprocess.run(cmd, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True)

    if result.returncode != 0 or not os.path.exists(tmp_wav):
        log("  ERREUR lors de l'extraction audio :")
        log(f"  {result.stderr[-800:]}")
        raise RuntimeError("Échec de l'extraction audio, voir le journal ci-dessus.")

    size_mb = os.path.getsize(tmp_wav) / (1024 * 1024)
    log(f"  Audio extrait avec succès ({size_mb:.1f} Mo).")
    return tmp_wav


def transcribe_video(video_path: str, model_size: str = "small", log=print):
    """
    Transcrit la vidéo et retourne une liste de segments avec leurs
    horodatages de début/fin (et ceux de chaque mot), ainsi que le texte complet.

    Retourne:
        segments: liste de dicts {start, end, text, words: [{start, end, text}]}
        full_text_with_timestamps: string formatée pour l'IA
    """
    tmp_dir = tempfile.mkdtemp(prefix="tiktok_app_")
    try:
        audio_path = _extract_audio(video_path, tmp_dir, log)

        log(f"  Chargement du modèle Whisper '{model_size}'...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")

        log("  Transcription en cours (ça peut prendre plusieurs minutes)...")
        # PC modeste (modèle léger) : recherche simple, ~2x plus rapide.
        beam = 5 if model_size in ("small", "medium", "large-v3") else 1
        segments_raw, info = model.transcribe(
            audio_path, beam_size=beam, word_timestamps=True, vad_filter=True
        )

        segments = []
        full_text_lines = []
        duree = info.duration or 0
        palier = 0

        # segments_raw est un générateur : la transcription se fait pendant
        # cette boucle, donc AVANT de supprimer le fichier audio.
        for seg in segments_raw:
            if duree:
                pct = int(min(seg.end / duree, 1) * 100)
                if pct >= palier + 5:
                    palier = pct - pct % 5
                    log(f"  ... {palier} % transcrit ({seg.end / 60:.0f} min sur {duree / 60:.0f})")
            start = round(seg.start, 2)
            end = round(seg.end, 2)
            text = seg.text.strip()
            if not text:
                continue
            words = [
                {"start": round(w.start, 2), "end": round(w.end, 2), "text": w.word.strip()}
                for w in (seg.words or [])
                if w.word.strip()
            ]
            segments.append({"start": start, "end": end, "text": text, "words": words})
            full_text_lines.append(f"[{start:.2f}s -> {end:.2f}s] {text}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    full_text_with_timestamps = "\n".join(full_text_lines)

    total_duration = segments[-1]["end"] if segments else 0
    log(f"  Langue détectée : {info.language} | Durée parlée détectée : {total_duration:.0f}s")

    if total_duration < 70:
        log(
            f"  ATTENTION : seulement ~{total_duration:.0f}s de contenu parlé détecté. "
            "Si ta vidéo est plus longue que ça, il peut rester un souci sur le fichier "
            "source lui-même (essaie de le ré-exporter depuis l'app d'origine)."
        )

    return segments, full_text_with_timestamps
