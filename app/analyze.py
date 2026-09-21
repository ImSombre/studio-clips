"""
Envoie la transcription horodatée à une IA qui tourne EN LOCAL sur ton PC
via Ollama (gratuit, aucune clé API, aucun compte, aucun paiement).
Elle repère les meilleurs passages pour TikTok.

IMPORTANT : les IA locales ont une "fenêtre de contexte" limitée, c'est-à-dire
une quantité de texte maximale qu'elles peuvent lire d'un coup. Pour une vidéo
de 20-30 minutes, la transcription complète dépasse largement cette limite.

La solution : on découpe la transcription en morceaux d'environ 5-8 minutes
de vidéo QUI SE CHEVAUCHENT (pour ne pas rater un bon passage coupé en deux
entre deux morceaux), on demande à l'IA d'analyser chaque morceau, puis on
rassemble les extraits trouvés, on supprime les doublons et on garde les
mieux notés.

Tout ce que renvoie l'IA est vérifié avant d'être utilisé : une IA locale
se trompe régulièrement de format (timestamps en texte, champ manquant,
extrait qui dépasse la fin de la vidéo...).

Ollama doit être installé et lancé sur ta machine (voir INSTALLATION.md).
"""

import json
import re
import requests


# Durée visée pour chaque clip, et tolérance acceptée (l'IA locale vise
# rarement pile) : ici on accepte de 50 à 100 secondes.
CLIP_MIN = 61            # plus d'1 minute : condition pour que TikTok rémunère la vidéo
CLIP_MAX = 90
TOLERANCE = 10

# Nombre maximum de clips générés (on garde les MIEUX NOTÉS par l'IA).
MAX_CLIPS = 10

# Deux extraits qui se recouvrent sur plus de cette part du plus court
# sont considérés comme des doublons (on garde le mieux noté).
OVERLAP_RATIO = 0.5

def _system_prompt(CLIP_MIN, CLIP_MAX):
    return f"""Tu es un monteur expert en contenu viral TikTok/Shorts.
On te donne UNE PORTION de la transcription horodatée d'une vidéo plus longue
(les timestamps correspondent à la vidéo complète, pas à cette portion).

Ta mission : repérer les MEILLEURS extraits à découper dans cette portion, avec
ces règles strictes :
- Chaque extrait doit durer entre {CLIP_MIN} et {CLIP_MAX} secondes (jamais moins, jamais plus).
- L'extrait doit commencer par une phrase accrocheuse (une question, une punchline,
  une affirmation surprenante) qui capte l'attention en 3 secondes.
- L'extrait doit se terminer sur un moment de tension, une question ouverte, ou un
  cliffhanger qui donne envie de regarder la suite.
- Ne choisis QUE des extraits avec un vrai contenu intéressant : anecdote, révélation,
  conseil concret, moment drôle ou émouvant. Ignore les passages plats ou hors-sujet.
- Si cette portion ne contient RIEN d'assez intéressant, renvoie une liste vide,
  c'est tout à fait normal et attendu pour certaines portions.
- Utilise UNIQUEMENT les timestamps réels présents dans le texte fourni,
  en secondes, sous forme de NOMBRES (ex : 12.5, pas "12.5s" ni "00:12").
- Donne à chaque extrait une note "score" de 1 à 10 (10 = potentiel viral énorme).
- Écris "title" et "reason" dans la MÊME LANGUE que la transcription, et adapte le ton du titre
  au type de contenu (humour, podcast, gaming, tuto, info, sport, histoire vraie…).
- Propose entre 0 et 3 extraits pour cette portion (n'invente rien, ne force rien).

Réponds UNIQUEMENT avec un JSON valide, aucun texte avant ou après, dans ce format exact :
{{
  "clips": [
    {{
      "start": 12.5,
      "end": 78.3,
      "title": "Titre court et accrocheur pour ce clip",
      "reason": "Pourquoi ce passage fonctionne bien pour TikTok",
      "score": 8
    }}
  ]
}}
"""

OLLAMA_URL = "http://localhost:11434/api/chat"
# Même taille de contexte pour TOUS les appels (analyse, chat, titres) : sinon Ollama
# recharge le modèle à chaque changement, ce qui prend des dizaines de secondes sur un petit PC.
NUM_CTX = 8192
# Un morceau de transcription ne doit pas dépasser la fenêtre de contexte (≈ 3 caractères / jeton).
CHARS_PAR_MORCEAU = 12000
OLLAMA_MODEL = "llama3.1"

# Nombre de lignes de transcription (environ une par phrase/segment de parole)
# regroupées dans chaque morceau envoyé à l'IA. ~120 lignes correspond en général
# à 5-8 minutes de vidéo, une taille sûre pour la fenêtre de contexte demandée.
LINES_PER_CHUNK = 120
# Lignes communes entre deux morceaux voisins (~1 minute) : un bon passage
# situé à la frontière apparaît en entier dans au moins un des deux.
CHUNK_OVERLAP = 25


def _call_ollama(user_message: str, model: str, log, system_prompt: str) -> list:
    """Un seul appel à Ollama pour un morceau de transcription. Retourne une liste de clips bruts."""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "format": "json",
                "stream": False,
                # Qwen3 : pas de « réflexion » à voix haute, réponse directe
                **({"think": False} if str(model).startswith("qwen3") else {}),
                "options": {"temperature": 0.3, "num_ctx": NUM_CTX},
            },
            timeout=600,
        )
    except requests.exceptions.ConnectionError:
        raise RuntimeError("L'IA ne répond pas (elle est peut-être en train de démarrer). "
                           "Attends une minute puis clique sur Relancer.") from None
    except requests.exceptions.Timeout:
        log("    (l'IA a mis plus de 10 minutes à répondre pour ce morceau, on l'ignore et on continue)")
        return None

    if response.status_code == 404:
        raise RuntimeError("L'IA n'est pas encore téléchargée. Attends la fin du téléchargement "
                           "(barre en bas à gauche de l'appli), puis clique sur Relancer.")
    if not response.ok:
        log(f"    (Ollama a renvoyé une erreur {response.status_code} pour ce morceau, on l'ignore)")
        print("Ollama erreur", response.status_code, response.text[:300], flush=True)
        return None

    try:
        raw_text = response.json()["message"]["content"].strip()
    except (ValueError, KeyError, TypeError):
        log("    (réponse d'Ollama illisible pour ce morceau, on l'ignore et on continue)")
        return None

    raw_text_clean = re.sub(r"^```json\s*|\s*```$", "", raw_text)
    match = re.search(r"\{.*\}", raw_text_clean, re.DOTALL)
    if match:
        raw_text_clean = match.group(0)

    try:
        data = json.loads(raw_text_clean)
    except json.JSONDecodeError:
        log("    (réponse non exploitable de l'IA pour ce morceau, on l'ignore et on continue)")
        return None

    if not isinstance(data, dict):
        return None
    clips = data.get("clips", [])
    return clips if isinstance(clips, list) else []


def _to_seconds(value) -> float:
    """Convertit ce que l'IA renvoie en secondes : 12.5, "12.5", "12.5s", "01:12", "0:01:12.5"."""
    if isinstance(value, bool):
        raise ValueError("booléen")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower().rstrip("s").strip().replace(",", ".")
        if ":" in text:
            total = 0.0
            for part in text.split(":"):
                total = total * 60 + float(part)
            return total
        return float(text)
    raise ValueError(f"type inattendu : {type(value).__name__}")


def _normalize_clip(clip, video_duration, log, clip_min=CLIP_MIN, clip_max=CLIP_MAX):
    """Vérifie et nettoie un extrait proposé par l'IA. Retourne None s'il est inutilisable."""
    if not isinstance(clip, dict):
        log(f"  Extrait ignoré (format inattendu) : {clip!r}")
        return None

    try:
        start = _to_seconds(clip["start"])
        end = _to_seconds(clip["end"])
    except (KeyError, ValueError, TypeError):
        log(f"  Extrait ignoré (timestamps absents ou illisibles) : {clip!r}")
        return None

    start = max(0.0, start)
    if video_duration:
        end = min(end, video_duration)

    title = str(clip.get("title") or "").strip() or "Extrait"
    reason = str(clip.get("reason") or "").strip()

    duration = end - start
    # Une IA locale vise rarement pile : on accepte large, puis on ramène
    # l'extrait dans la durée demandée (le studio recale ensuite la fin sur un mot).
    # Un petit modèle renvoie souvent juste la phrase-clé (3-7 s) : on la garde comme
    # POINT DE DÉPART et on étend le clip à la durée demandée, au lieu de le jeter.
    if duration <= 0:
        log(f"  Extrait ignoré (timestamps incohérents) : {title}")
        return None
    if duration > clip_max:
        end = start + clip_max
    elif duration < clip_min:
        end = start + (clip_min + clip_max) / 2
    if video_duration:
        end = min(end, video_duration)
        if end - start < clip_min:
            # passage proposé tout à la fin : on avance son début au lieu de le raccourcir
            start = max(0.0, end - (clip_min + clip_max) / 2)
            if end - start < min(clip_min * 0.5, 5):
                log(f"  Extrait ignoré (vidéo trop courte à cet endroit) : {title}")
                return None

    try:
        score = float(clip.get("score", 5))
    except (ValueError, TypeError):
        score = 5.0
    score = min(10.0, max(0.0, score))

    return {"start": round(start, 2), "end": round(end, 2), "title": title, "reason": reason, "score": score}


def _overlaps_too_much(a, b) -> bool:
    shared = min(a["end"], b["end"]) - max(a["start"], b["start"])
    if shared <= 0:
        return False
    shortest = min(a["end"] - a["start"], b["end"] - b["start"])
    return shared > OVERLAP_RATIO * shortest


def _split_in_chunks(lines):
    """Morceaux qui se chevauchent, limités en lignes ET en caractères."""
    chunks, i = [], 0
    while i < len(lines):
        taille, j = 0, i
        while j < len(lines) and j - i < LINES_PER_CHUNK and (taille + len(lines[j]) < CHARS_PAR_MORCEAU or j == i):
            taille += len(lines[j]) + 1
            j += 1
        chunks.append(lines[i:j])
        if j >= len(lines):
            break
        i = max(i + 1, j - min(CHUNK_OVERLAP, (j - i) // 4))
    return chunks


def find_best_clips(
    full_text_with_timestamps: str,
    model: str = OLLAMA_MODEL,
    log=print,
    custom_instructions: str = "",
    video_duration=None,
    clip_min: float = CLIP_MIN,
    clip_max: float = CLIP_MAX,
    max_clips: int = MAX_CLIPS,
):
    """
    Découpe la transcription en morceaux, appelle l'IA locale sur chaque
    morceau, et rassemble les meilleurs extraits trouvés.

    custom_instructions : consignes en langage naturel données par l'utilisateur,
    transmises à chaque morceau.
    video_duration : durée réelle de la vidéo (s), pour ne jamais couper au-delà.
    log: fonction utilisée pour afficher les messages de progression.
    Retourne une liste de dicts {start, end, title, reason, score}, triée par ordre
    chronologique.
    """
    lines = [l for l in full_text_with_timestamps.split("\n") if l.strip()]
    if not lines:
        log("  Transcription vide, rien à analyser.")
        return []

    chunks = _split_in_chunks(lines)
    log(f"  Vidéo découpée en {len(chunks)} partie(s) pour l'analyse par l'IA locale.")

    # Vidéo plus courte que la durée demandée : on vise ce qu'elle permet.
    if video_duration and video_duration < clip_min + 5:
        clip_min, clip_max = max(5, video_duration * 0.5), video_duration
    system_prompt = _system_prompt(int(clip_min), int(clip_max))
    all_clips = []
    echecs = 0
    for idx, chunk_lines in enumerate(chunks, start=1):
        chunk_text = "\n".join(chunk_lines)
        user_message = f"Voici une portion de la transcription horodatée :\n\n{chunk_text}"
        if custom_instructions:
            user_message += (
                f"\n\nConsigne donnée par l'utilisateur, à respecter en priorité tant que "
                f"ça reste cohérent avec le format demandé : {custom_instructions}"
            )

        log(f"  Analyse de la partie {idx}/{len(chunks)}...")
        clips = _call_ollama(user_message, model, log, system_prompt)
        if clips is None:
            echecs += 1
            continue
        if clips:
            log(f"    -> {len(clips)} extrait(s) trouvé(s) dans cette partie.")
        all_clips.extend(clips)

    if echecs and echecs == len(chunks):
        raise RuntimeError("L'IA n'a pas réussi à analyser la vidéo (elle manque peut-être de mémoire). "
                           "Ferme les autres logiciels ouverts puis clique sur Relancer.")
    log(f"  Total avant filtrage : {len(all_clips)} extrait(s) sur l'ensemble de la vidéo.")

    valid_clips = [
        c for c in (_normalize_clip(c, video_duration, log, clip_min, clip_max) for c in all_clips) if c
    ]

    # Du mieux noté au moins bien noté ; à note égale, le plus tôt dans la vidéo.
    valid_clips.sort(key=lambda c: (-c["score"], c["start"]))

    kept = []
    for clip in valid_clips:
        if any(_overlaps_too_much(clip, other) for other in kept):
            log(f"  Doublon ignoré (recouvre un extrait mieux noté) : {clip['title']}")
            continue
        kept.append(clip)

    if len(kept) > max_clips:
        log(f"  Beaucoup d'extraits trouvés, on garde les {max_clips} mieux notés.")
        kept = kept[:max_clips]

    kept.sort(key=lambda c: c["start"])
    log(f"  -> {len(kept)} extrait(s) retenu(s) au total après filtrage.")
    return kept
