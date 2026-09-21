/* Studio Clips — interface. L'aperçu (cadrage + sous-titres) est calculé ici en direct ;
   le serveur ne fabrique le MP4 qu'à l'export. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const POLICES = ["Arial Black", "Impact", "Segoe UI Black", "Verdana", "Trebuchet MS", "Comic Sans MS"];
const COULEURS = ["#FFFFFF", "#FFE14D", "#FF3B30", "#34E07A", "#2EE6F0", "#FF5FA2", "#FF8A1F", "#A974FF", "#000000"];
const MAX_S_SOUS_TITRE = 1.6, PAUSE_COUPURE = 0.6, DUREE_MIN = 3;

let P = null;            // projet ouvert (avec la transcription)
let cur = null;          // id du clip ouvert
let groupes = [];        // sous-titres du clip ouvert (temps absolus)
let fenetre = [0, 1];    // portion de vidéo affichée sur la timeline
let libre = false;       // lecture hors du clip (après un clic hors zone)
let glisse = null;       // poignée en cours de déplacement
let envoiChat = false;
let minuteurPoll = null, minuteurPatch = null, patchEnAttente = {};
let patchEnVol = 0;      // PATCH partis mais pas encore revenus : une synchro ne doit pas les écraser
let echecsServeur = 0;
const jobsVus = new Set();
const enCours = new Set();   // actions déjà lancées (anti double-clic)

async function api(url, opts = {}) {
  let r;
  try {
    r = await fetch(url, {
      method: opts.method || (opts.body ? "POST" : "GET"),
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (_) {
    serveurMuet();
    throw new Error("Studio Clips ne répond pas.");
  }
  serveurRepond();
  if (!r.ok) {
    let m = r.status === 404 ? "Ce clip ou ce projet n'existe plus." : "Quelque chose n'a pas marché, réessaie.";
    try { m = (await r.json()).erreur || m; } catch (_) { /* réponse non JSON */ }
    throw new Error(m);
  }
  return r.status === 204 ? null : r.json();
}

/* Serveur arrêté (PC en veille, plantage…) : on le dit clairement au lieu de paraître figé. */
function serveurMuet() {
  echecsServeur++;
  if (echecsServeur >= 3) $("#hors-ligne").hidden = false;
}
function serveurRepond() {
  echecsServeur = 0;
  $("#hors-ligne").hidden = true;
}

/* Une seule exécution à la fois d'une même action (double-clic, touche maintenue…). */
async function uneFois(cle, fonction) {
  if (enCours.has(cle)) return;
  enCours.add(cle);
  try { await fonction(); } catch (err) { toast(err.message); } finally { enCours.delete(cle); }
}

addEventListener("unhandledrejection", (e) => { e.preventDefault(); toast(e.reason?.message || "Quelque chose n'a pas marché."); });

function toast(texte) {
  const t = $("#toast");
  t.textContent = texte;
  t.classList.add("visible");
  clearTimeout(toast.m);
  toast.m = setTimeout(() => t.classList.remove("visible"), 2600);
}

const fmt = (s) => {
  const neg = s < 0; s = Math.abs(s);
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${neg ? "−" : ""}${String(m).padStart(2, "0")}:${r.toFixed(1).padStart(4, "0")}`;
};
const fmtCourt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const echap = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clip = () => P?.clips.find((c) => c.id === cur) || null;

/* =================== ACCUEIL =================== */
let cheminChoisi = "";

async function chargerProjets(essai = 0) {
  let liste;
  try { liste = await api("/api/projets"); } catch (_) {
    // au tout premier lancement le serveur peut mettre quelques secondes : on réessaie
    if (essai < 10) setTimeout(() => chargerProjets(essai + 1), 1000);
    return;
  }
  $("#nb-projets").textContent = liste.length ? `${liste.length}` : "";
  const box = $("#liste-projets");
  if (!liste.length) {
    box.innerHTML = `<div class="liste-vide">Pas encore de projet.<br>Choisis une vidéo pour commencer.</div>`;
    return;
  }
  const etats = { pret: "prêt", traitement: "en cours", erreur: "erreur", interrompu: "interrompu" };
  box.innerHTML = liste.map((p, i) => `
    <div class="projet" role="button" tabindex="0" data-id="${p.id}" style="animation-delay:${i * 40}ms">
      <div class="projet-vignette" style="${p.premier ? `background-image:url('/miniature/${p.id}/${p.premier}')` : ""}"></div>
      <div style="min-width:0">
        <div class="projet-nom">${echap(p.nom)}</div>
        <div class="projet-infos"><span class="badge ${p.etat}">${p.progression ? `${p.progression.pct} %` : (etats[p.etat] || p.etat)}</span>
          <span class="mono">${p.clips} clip${p.clips > 1 ? "s" : ""}${p.exportes ? ` · ${p.exportes} exporté${p.exportes > 1 ? "s" : ""}` : ""}</span>
          <span>${new Date(p.cree).toLocaleDateString("fr-FR", { day: "numeric", month: "short" })}</span></div>
        ${p.progression ? `<div class="projet-progression"><i style="width:${p.progression.pct}%"></i></div>
          <div class="projet-etape">${echap(p.progression.etape)}${p.progression.message ? " — " + echap(p.progression.message) : ""}</div>` : ""}
      </div>
      <button class="projet-suppr" data-suppr="${p.id}" title="Supprimer le projet" type="button">✕</button>
    </div>`).join("");
  // un projet travaille en arrière-plan : l'accueil suit sa progression en direct
  clearTimeout(chargerProjets.minuteur);
  if (liste.some((p) => p.progression)) {
    chargerProjets.minuteur = setTimeout(() => { if (!$("#accueil").hidden) chargerProjets(); }, 2000);
  }
}

$("#liste-projets").addEventListener("click", async (e) => {
  const suppr = e.target.closest("[data-suppr]");
  if (suppr) {
    e.stopPropagation();
    if (confirm("Supprimer ce projet ? (tes clips déjà exportés ne sont pas touchés)")) {
      try { await api(`/api/projets/${suppr.dataset.suppr}`, { method: "DELETE" }); } catch (err) { toast(err.message); }
      chargerProjets();
    }
    return;
  }
  const carte = e.target.closest(".projet");
  if (carte) ouvrirProjet(carte.dataset.id);
});
$("#liste-projets").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("projet")) ouvrirProjet(e.target.dataset.id);
});

$("#btn-choisir").addEventListener("click", async () => {
  const b = $("#btn-choisir");
  if (b.disabled) return;
  b.disabled = true;
  $("#nom-video").textContent = "Choix en cours…";
  try {
    const { chemin } = await api("/api/choisir", { body: {} });
    if (chemin) {
      cheminChoisi = chemin;
      $("#lien").value = ""; $("#plage").hidden = true; $(".zone-lien").classList.remove("choisie", "invalide");
      $("#nom-video").textContent = chemin.split(/[\\/]/).pop();
      $("#chemin-video").textContent = chemin;
      b.classList.add("choisie");
    } else if (!cheminChoisi) {
      $("#nom-video").textContent = "Choisir une vidéo";
    } else {
      $("#nom-video").textContent = cheminChoisi.split(/[\\/]/).pop();
    }
  } catch (err) { toast(err.message); $("#nom-video").textContent = "Choisir une vidéo"; }
  b.disabled = false;
  majBoutonLancer();
});

/* Lien d'une vidéo en ligne (YouTube, Twitch, TikTok…) : alternative au fichier */
const lienValide = (t) => /^https?:\/\/\S+\.\S+/.test((t || "").trim());
function majBoutonLancer() {
  const lien = $("#lien").value.trim();
  $("#plage").hidden = !(lienValide(lien) || cheminChoisi);
  $("#btn-lancer").disabled = !(cheminChoisi || lienValide(lien));
  $(".zone-lien").classList.toggle("choisie", lienValide(lien));
  $(".zone-lien").classList.toggle("invalide", !!lien && !lienValide(lien));
}
$("#lien").addEventListener("input", () => {
  if ($("#lien").value.trim() && cheminChoisi) {   // on colle un lien : il remplace le fichier choisi
    cheminChoisi = ""; $("#nom-video").textContent = "Choisir une vidéo";
    $("#chemin-video").textContent = "MP4, MOV, MKV, AVI…"; $("#btn-choisir").classList.remove("choisie");
  }
  majBoutonLancer();
});
$("#lien").addEventListener("keydown", (e) => { if (e.key === "Enter" && !$("#btn-lancer").disabled) $("#btn-lancer").click(); });

$("#puces-accueil").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const ta = $("#consigne");
  ta.value = ta.value.trim() ? `${ta.value.trim().replace(/[,.]$/, "")}, ${b.dataset.texte}` : b.dataset.texte;
  ta.focus();
});

$("#btn-lancer").addEventListener("click", async () => {
  const lien = $("#lien").value.trim();
  if (!cheminChoisi && !lienValide(lien)) return;
  $("#btn-lancer").disabled = true;
  try {
    const corps = lienValide(lien) && !cheminChoisi
      ? { lien, debut: $("#lien-de").value, fin: $("#lien-a").value, consigne: $("#consigne").value }
      : { chemin: cheminChoisi, debut: $("#lien-de").value, fin: $("#lien-a").value, consigne: $("#consigne").value };
    const { id } = await api("/api/projets", { body: corps });
    cheminChoisi = ""; $("#consigne").value = "";
    $("#lien").value = ""; $("#lien-de").value = ""; $("#lien-a").value = ""; majBoutonLancer();
    $("#nom-video").textContent = "Choisir une vidéo"; $("#chemin-video").textContent = "MP4, MOV, MKV, AVI…";
    $("#btn-choisir").classList.remove("choisie");
    ouvrirProjet(id);
  } catch (err) { toast(err.message); $("#btn-lancer").disabled = false; }
});

/* =================== STUDIO : ouverture / synchro =================== */
async function ouvrirProjet(id) {
  let charge;
  try { charge = await api(`/api/projets/${id}`); } catch (err) { toast(`Impossible d'ouvrir ce projet : ${err.message}`); return; }
  P = charge;
  envoiChat = false; $(".envoyer").disabled = false;
  patchEnAttente = {}; $("#idees").hidden = true;
  P.jobs.forEach((j) => jobsVus.add(j.id + j.etat));
  cur = P.clips[0]?.id || null;
  $("#accueil").hidden = true; $("#studio").hidden = false;
  $("#titre-projet").textContent = P.nom;
  const v = $("#lecteur");
  v.removeAttribute("src"); v.dataset.src = "";
  $("#apercu-msg").hidden = true;
  toutAfficher(true);
  planifierPoll(200);
}

function planifierPoll(ms) {
  clearTimeout(minuteurPoll);
  minuteurPoll = setTimeout(poll, ms);
}

async function poll() {
  if (!P) return;
  const pid = P.id;
  try {
    const e = await api(`/api/projets/${pid}/etat`);
    if (P?.id !== pid) return;            // on a changé de projet entre-temps
    serveurRepond();
    const manqueSegments = P.segments === undefined;
    fusionner(e);
    if (manqueSegments && e.etat === "pret") {
      const complet = await api(`/api/projets/${pid}`);
      if (P?.id !== pid) return;
      P.segments = complet.segments || []; P.duree = complet.duree;
      toutAfficher(true);
    }
  } catch (_) { /* compté par api() : au bout de 3 échecs le bandeau « arrêté » s'affiche */ }
  if (P?.id !== pid) return;
  const occupe = P && (P.etat === "traitement" || P.jobs.some((j) => j.etat === "en_cours"));
  planifierPoll(occupe ? 1000 : 3500);
}

function fusionner(e) {
  if (!P || (e.id && e.id !== P.id)) return;   // réponse d'un autre projet : on l'ignore
  const ancien = clip();
  const anciensIds = P.clips.map((c) => c.id).join();
  const garde = glisse || deplace || patchEnVol || Object.keys(patchEnAttente).length ? ancien : null;
  Object.assign(P, { etat: e.etat, erreur: e.erreur, duree: e.duree ?? P.duree, jobs: e.jobs, source_existe: e.source_existe,
                     nom: e.nom ?? P.nom, lien: e.lien ?? P.lien });
  $("#titre-projet").textContent = P.nom;   // un projet créé depuis un lien prend le vrai titre une fois téléchargé
  P.clips = e.clips.map((c) => (garde && c.id === garde.id ? garde : c));
  const dernier = (l) => (l?.length ? `${l.length}|${l[l.length - 1].t}` : "");
  const chatChange = dernier(P.chat) !== dernier(e.chat);
  P.chat = e.chat;

  // Tâches terminées depuis la dernière synchro
  for (const j of e.jobs) {
    const cle = j.id + j.etat;
    if (jobsVus.has(cle)) continue;
    jobsVus.add(cle);
    if (j.etat === "fini" && j.type === "nouveau_clip" && j.resultat && !glisse && !deplace) {
      envoyerPatchMaintenant(); cur = j.resultat; apresChangementDeClip(); toast("Nouveau clip trouvé");
    }
    if (j.etat === "fini" && j.type === "export") toast("Clip exporté ✔");
    if (j.etat === "fini" && j.type === "apercu") rechargerVideo();
    if (j.etat === "fini" && j.type === "vision") dernierDessin = "";
    if (j.etat === "erreur" && j.type !== "analyse") toast(`Problème : ${j.erreur}`);
  }
  if (!clip()) { cur = P.clips[0]?.id || null; apresChangementDeClip(); }

  const nouveau = clip();
  const bornesChangees = !ancien || !nouveau || ancien.id !== nouveau.id || ancien.debut !== nouveau.debut || ancien.fin !== nouveau.fin;
  afficherClips(anciensIds !== P.clips.map((c) => c.id).join());
  afficherTraitement();
  afficherJobs();
  afficherMontage(bornesChangees);
  if (chatChange) afficherChat();
}

function toutAfficher(bornes) {
  afficherClips(true);
  afficherTraitement();
  afficherJobs();
  afficherMontage(bornes);
  afficherChat();
}

/* =================== liste des clips =================== */
function afficherClips(reconstruire) {
  $("#nb-clips").textContent = P.clips.length || "";
  const box = $("#liste-clips");
  if (reconstruire) {
    box.innerHTML = P.clips.map((c, i) => `
      <button class="clip" type="button" data-id="${c.id}" style="animation-delay:${i * 35}ms">
        <div class="clip-vignette"><span class="num">${i + 1}</span></div>
        <div style="min-width:0"><div class="clip-titre"></div><div class="clip-meta"></div><div class="clip-progression" hidden><i></i></div></div>
      </button>`).join("");
  }
  P.clips.forEach((c) => {
    const el = box.querySelector(`[data-id="${c.id}"]`); if (!el) return;
    el.classList.toggle("actif", c.id === cur);
    $(".clip-titre", el).textContent = c.titre;
    const job = P.jobs.find((j) => j.clip === c.id && j.type === "export" && j.etat === "en_cours");
    const duree = P.segments ? dureeMontee(calculerBlocs(c)) : c.fin - c.debut;
    const court = duree < 60 ? `<span class="court" title="TikTok ne rémunère que les vidéos de plus d'1 minute">moins d'1 min</span>` : "";
    $(".clip-meta", el).innerHTML = `<span>${fmtCourt(duree)}</span><span>★ ${Math.round(c.note)}</span>${court}${c.exporte ? '<span class="ok">✔ exporté</span>' : ""}`;
    const pr = $(".clip-progression", el);
    pr.hidden = !job; if (job) $("i", pr).style.width = `${job.pct}%`;
    const vign = $(".clip-vignette", el), url = `/miniature/${P.id}/${c.id}?d=${c.debut}`;
    if (vign.dataset.url !== url) { vign.dataset.url = url; vign.style.backgroundImage = `url('${url}')`; }
  });
}

/* Tout changement de clip passe par ici : modifs envoyées au BON clip, état remis à zéro. */
function apresChangementDeClip() {
  libre = false; glisse = null; iPassage = 0;
  if (typeof deplace !== "undefined") deplace = null;
  $("#idees").hidden = true;
  const c = clip();
  if (c && video.readyState >= 1) { try { video.currentTime = c.debut; } catch (_) { /* vidéo pas prête */ } }
}

$("#liste-clips").addEventListener("click", (e) => {
  const b = e.target.closest(".clip"); if (!b || b.dataset.id === cur) return;
  envoyerPatchMaintenant();
  cur = b.dataset.id;
  apresChangementDeClip();
  afficherClips(false); afficherMontage(true); afficherChat();
  video.play().catch(() => {});
});

/* =================== traitement =================== */
function afficherTraitement() {
  const job = P.jobs.filter((j) => j.type === "analyse").pop();
  const enCours = P.etat === "traitement";
  const montrer = enCours || (!P.clips.length && P.etat !== "pret");
  $("#traitement").hidden = !montrer;
  $("#vide").hidden = montrer || P.clips.length > 0;
  // bouton du bandeau : il reste visible quand le projet est prêt (donc AVANT le retour ci-dessous)
  $("#btn-refaire").hidden = enCours || !P.clips.length || P.source_existe === false;
  if (!montrer) return;
  const pct = job ? (job.etape_pct ?? job.pct) : 0;   // pendant un téléchargement : sa propre progression
  $("#traitement-etape").textContent = enCours ? (job?.etape || "Démarrage…") : (P.etat === "erreur" ? "Le traitement a échoué" : "Traitement interrompu");
  $("#traitement-barre").style.width = `${pct}%`;
  $("#traitement-pct").textContent = `${pct} %`;
  $("#traitement-msg").textContent = job?.message || "";
  const etape = pct < 66 ? 1 : pct < 100 ? 2 : 3;
  const telechargement = job?.etape_pct !== undefined && job?.etape_pct !== null;
  $("#traitement-etapes li[data-e=\"1\"]").textContent = telechargement ? "Téléchargement de la vidéo" : "Transcription de la vidéo";
  $$("#traitement-etapes li").forEach((li) => {
    const n = +li.dataset.e;
    const e = telechargement ? 1 : etape;
    li.className = n < e ? "faite" : n === e && enCours ? "encours" : "";
  });
  const err = $("#traitement-erreur");
  err.hidden = !P.erreur || enCours; err.textContent = P.erreur || "";
  $("#btn-relancer").hidden = enCours;
  $("#btn-arreter").hidden = !enCours;
  $("#btn-retrouver-t").hidden = enCours || P.source_existe !== false;
}
$("#btn-relancer").addEventListener("click", () => uneFois("relancer", async () => {
  const pid = P.id;
  await api(`/api/projets/${pid}/relancer`, { body: {} });
  if (P?.id !== pid) return;
  P.etat = "traitement"; P.erreur = null; afficherTraitement(); planifierPoll(300);
}));
/* « Refaire les clips » : rechoisit les passages avec la version actuelle de l'appli */
$("#btn-refaire").addEventListener("click", () => uneFois("refaire", async () => {
  if (!P) return;
  const n = P.clips?.length || 0;
  if (n && !confirm(`Refaire les ${n} clip(s) avec la nouvelle version ?\n\n`
      + "• les passages sont rechoisis (au moins 1 minute chacun)\n"
      + "• le cadrage intelligent est appliqué\n"
      + "• tes réglages actuels (titres, sous-titres, morceaux) seront perdus\n\n"
      + "La vidéo n'est pas re-transcrite : c'est rapide.")) return;
  const pid = P.id;
  await api(`/api/projets/${pid}/relancer`, { body: { refaire: true } });
  if (P?.id !== pid) return;
  P.etat = "traitement"; P.erreur = null; P.clips = []; cur = null;
  afficherClips(true); afficherTraitement(); planifierPoll(300);
}));
$("#btn-arreter").addEventListener("click", () => uneFois("arreter", async () => {
  if (!confirm("Arrêter le traitement en cours ? Tu pourras le relancer plus tard.")) return;
  await api(`/api/projets/${P.id}/arreter`, { body: {} });
  toast("Arrêt demandé…"); planifierPoll(500);
}));

function afficherJobs() {
  const libelles = { export: "Export", nouveau_clip: "Recherche", apercu: "Aperçu", analyse: "Analyse", titres: "Titres", vision: "Image", assembler: "Assemblage" };
  const actifs = P.jobs.filter((j) => j.etat === "en_cours" && j.type !== "analyse");
  // regroupés par type : 30 exports = une seule pastille « Export 3/30 »
  const parType = {};
  for (const j of actifs) (parType[j.type] ||= []).push(j);
  $("#jobs-barre").innerHTML = Object.entries(parType).map(([type, liste]) => {
    const j = liste.find((x) => x.pct > 0) || liste[0];
    const compte = liste.length > 1 ? ` ×${liste.length}` : "";
    return `<span class="job-pilule">${libelles[type] || type}${compte}<i style="--p:${j.pct}%"></i>${j.pct}%</span>`;
  }).join("");
}

/* =================== montage : lecteur =================== */
const video = $("#lecteur");
const ecran = $("#ecran-video");

function rechargerVideo() {
  video.dataset.src = "";
  $("#apercu-msg").hidden = true;
  afficherMontage(true);
}

function afficherMontage(bornesChangees) {
  const c = clip();
  $("#montage").hidden = !c || !$("#traitement").hidden;
  if (!c) { video.pause(); return; }
  if (P.segments?.length && video.dataset.src !== P.id) {
    video.dataset.src = P.id;
    video.src = `/media/${P.id}?v=${Date.now()}`;
    video.currentTime = c.debut;
  }
  if (P.source_existe === false) {
    afficherMessageVideo("Vidéo d'origine introuvable : elle a été déplacée, renommée ou la clé USB est débranchée.", true);
  } else if ($("#apercu-msg").dataset.introuvable) {
    $("#apercu-msg").hidden = true; delete $("#apercu-msg").dataset.introuvable;
  }
  const bloque = P.source_existe === false;
  $("#btn-exporter").title = $("#btn-tout").title = bloque ? "Vidéo d'origine introuvable" : "";
  calculerSousTitres();
  remplirReglages(c);
  if (bornesChangees) { calculerFenetre(); dessinerTimeline(); }
  else majZone();
  $("#chat-contexte").textContent = `Clip ${P.clips.indexOf(c) + 1} · ${fmtCourt(dureeMontee(blocs))}`;
}

function afficherMessageVideo(texte, introuvable = false) {
  const m = $("#apercu-msg");
  m.hidden = false;
  m.innerHTML = echap(texte) + (introuvable ? ` <button type="button" class="bouton-contour petit" data-retrouver>Retrouver la vidéo</button>` : "");
  if (introuvable) m.dataset.introuvable = "1"; else delete m.dataset.introuvable;
}

async function retrouverVideo() {
  await uneFois("retrouver", async () => {
    const pid = P.id;
    const e = await api(`/api/projets/${pid}/retrouver`, { body: {} });
    if (P?.id !== pid || !e || e.ok === false) return;
    fusionner(e);
    delete $("#apercu-msg").dataset.introuvable;
    rechargerVideo();
    toast("Vidéo retrouvée ✔");
  });
}
document.addEventListener("click", (e) => { if (e.target.closest("[data-retrouver]")) retrouverVideo(); });

let apercuTente = null;   // on ne demande l'aperçu qu'une fois par projet
async function demanderApercu() {
  if (!P || P.source_existe === false) return;
  if (apercuTente === P.id) {
    afficherMessageVideo("Cette vidéo ne peut pas s'afficher ici, mais l'export fonctionne quand même.");
    return;
  }
  apercuTente = P.id;
  afficherMessageVideo("Ce format ne se lit pas directement : je prépare un aperçu, un instant…");
  try {
    const r = await api(`/api/projets/${P.id}/apercu`, { body: {} });
    if (r?.existe) rechargerVideo(); else planifierPoll(500);
  } catch (err) { afficherMessageVideo(err.message, P.source_existe === false); }
}
video.addEventListener("error", () => { if (P && video.dataset.src) demanderApercu(); });
// vidéo HEVC (iPhone) : le son passe mais l'image reste noire, sans erreur -> on bascule sur l'aperçu
video.addEventListener("loadeddata", () => { if (P && video.videoWidth === 0 && video.videoHeight === 0) demanderApercu(); });
video.addEventListener("loadedmetadata", () => { const c = clip(); if (c && video.currentTime < 0.1) video.currentTime = c.debut; });
video.addEventListener("ended", () => {
  const c = clip(); if (!c) return;
  video.currentTime = c.debut;
  if ($("#boucle").checked) video.play().catch(() => {});
});
video.addEventListener("play", () => { ecran.classList.remove("pause"); $("#btn-play").classList.add("lecture"); });
video.addEventListener("pause", () => { ecran.classList.add("pause"); $("#btn-play").classList.remove("lecture"); });
ecran.classList.add("pause");

function basculerLecture() {
  const c = clip(); if (!c) return;
  if (video.paused) {
    const fin = Number.isFinite(video.duration) ? Math.min(c.fin, video.duration) : c.fin;
    if (libre) { /* écoute libre hors du clip : on repart d'où on est */ }
    else if (video.ended || video.currentTime < c.debut - 0.05 || video.currentTime >= fin - 0.05) video.currentTime = blocs[0]?.[0] ?? c.debut;
    video.play().catch(() => {});
  } else video.pause();
}
$("#btn-play").addEventListener("click", basculerLecture);
$("#btn-grand-play").addEventListener("click", basculerLecture);

/* =================== Montage automatique (mêmes calculs que montage.py) =================== */
const FPS = 30, TROU_MIN = 0.45, MARGE = 0.12, PLAN_MAX = 6.0, DUREE_ACCROCHE = 2.5;
const CYCLE_ZOOMS = [1.0, 1.12, 1.04, 1.16];
const HESITATIONS = new Set(["euh", "heu", "euuh", "hum", "hmm", "hmmm", "humm", "uh", "um", "uhm", "erm"]);
const MOTS_VIDES = new Set(("alors aussi avait avant avec cette comme comment dans depuis donc elle elles encore entre est-ce " +
  "faire fait jamais juste leurs mais même moins notre nous parce pendant peut plus pour pourquoi quand quelque sans sont sous " +
  "tout toute toutes tous très trop vais votre vous about after again because before being could their there these thing think " +
  "those would really right").split(" "));
const normMot = (t) => (t || "").toLowerCase().replace(/[^\p{L}\p{N}_]/gu, "");
const montageDe = (c) => ({ coupes: false, zooms: true, anim: true, accroche: true, barre: true, transitions: true, ...(c?.montage || {}) });
const FONDU = 0.25;   // fondu entre deux morceaux assemblés (même valeur que montage.py)
/* Un clip peut assembler PLUSIEURS morceaux de la vidéo. Un seul, par défaut. */
const passagesDe = (c) => (c?.passages?.length ? c.passages : [[c.debut, c.fin]]).map(([a, b]) => [+a, +b]);
let iPassage = 0, jonctions = [];
function passageCourant(c) { const ps = passagesDe(c); return ps[Math.min(iPassage, ps.length - 1)]; }
function ecrirePassage(c, i, a, b) {
  const ps = passagesDe(c);
  ps[i] = [+(+a).toFixed(2), +(+b).toFixed(2)];
  c.passages = ps; c.debut = ps[0][0]; c.fin = ps[ps.length - 1][1];
}
function envoyerPassages(c, delai = 400) {
  // toujours la liste complète : envoyer « début/fin » sur un clip à plusieurs morceaux serait ambigu
  planifierPatch({ passages: passagesDe(c) }, delai);
}
let blocs = [], plans = [];

function motsDuPassage(debut, fin) {
  const mots = [];
  for (const s of P.segments || []) {
    if (s.end <= debut || s.start >= fin) continue;
    if (s.words?.length) s.words.forEach((w) => { if (w.end > debut && w.start < fin) mots.push(w); });
    else mots.push({ start: s.start, end: s.end, text: s.text });
  }
  return mots;
}

/* Blocs gardés de tout le clip (tous ses morceaux) ; `jonctions` = là où deux morceaux se rejoignent. */
function calculerBlocs(c) {
  const res = []; const j = [];
  for (const [a, b] of passagesDe(c)) {
    const part = blocsDunPassage(c, a, b);
    if (res.length && part.length) j.push([res[res.length - 1][1], part[0][0]]);
    res.push(...part);
  }
  jonctions = j;
  return res;
}
function blocsDunPassage(c, debut, fin) {
  if (!montageDe(c).coupes || !P.segments) return [[debut, fin]];
  const c2 = { debut, fin };
  const mots = motsDuPassage(debut, fin).filter((w) => !HESITATIONS.has(normMot(w.text)));
  if (!mots.length) return [[debut, fin]];
  return blocsDesMots(mots, c2);
}
function blocsDesMots(mots, c) {
  const bl = []; let finMot = null;
  for (const w of mots) {
    if (bl.length && w.start - finMot <= TROU_MIN) bl[bl.length - 1][1] = Math.min(c.fin, w.end + MARGE);
    else bl.push([Math.max(c.debut, w.start - MARGE), Math.min(c.fin, w.end + MARGE)]);
    finMot = w.end;
  }
  const res = [];
  for (let [a, b] of bl) {
    a = c.debut + Math.floor((a - c.debut) * FPS) / FPS;
    b = Math.min(c.fin, c.debut + Math.ceil((b - c.debut) * FPS) / FPS);
    if (res.length && a <= res[res.length - 1][1]) res[res.length - 1][1] = Math.max(res[res.length - 1][1], b);
    else if (b - a >= 0.2) res.push([a, b]);
  }
  return res.length ? res : [[c.debut, c.fin]];
}
const dureeMontee = (bl) => bl.reduce((s, [a, b]) => s + (b - a), 0);
function versSortie(t, bl) {
  let total = 0;
  for (const [a, b] of bl) {
    if (t < a) return total;
    if (t <= b) return total + (t - a);
    total += b - a;
  }
  return total;
}
function calculerPlans(bl, m) {
  const res = []; let image = 0, k = 0;
  for (const [a, b] of bl) {
    const n = Math.round((b - a) * FPS);
    for (const taille of n <= PLAN_MAX * FPS ? [n] : [Math.floor(n / 2), n - Math.floor(n / 2)]) {
      res.push([image, image + taille, m.zooms ? CYCLE_ZOOMS[k % CYCLE_ZOOMS.length] : 1]);
      image += taille; k++;
    }
  }
  return res;
}
function zoomA(tSortie) {
  const image = Math.floor(tSortie * FPS);
  const p = plans.find(([a, b]) => image < b) || plans[plans.length - 1];
  return p ? p[2] : 1;
}
function motCle(mots) {
  let meilleur = -1, score = 0;
  mots.forEach((w, i) => {
    const n = normMot(w.text);
    if (MOTS_VIDES.has(n) || n.length < 5) return;
    const s = n.length + (/\d/.test(n) ? 3 : 0) + (w.text.endsWith("!") ? 2 : 0);
    if (s > score) { meilleur = i; score = s; }
  });
  return meilleur;
}
function preparerMontage() {
  const c = clip(); if (!c) { blocs = []; plans = []; return; }
  blocs = calculerBlocs(c);
  plans = calculerPlans(blocs, montageDe(c));
}

/* Accroche + barre + zoom, dessinés à chaque image de l'aperçu */
function rendreHabillage(c, tSortie) {
  const m = montageDe(c), echelle = ecran.clientHeight / 1920;
  const z = m.zooms && !c.cadrages?.length ? zoomA(tSortie) : 1;   // avec un plan de cadrage, les zooms y sont déjà
  $("#calque").style.transform = z !== 1 ? `scale(${z})` : "";
  const acc = $("#accroche"), texte = ((c.texte?.contenu || "").trim() || c.titre || "").toLocaleUpperCase("fr-FR");
  const voir = m.accroche && texte && tSortie < DUREE_ACCROCHE && !libre;
  if (voir && acc.hidden) { acc.hidden = false; acc.style.animation = "none"; void acc.offsetWidth; acc.style.animation = ""; }
  if (!voir) acc.hidden = true;
  if (voir && acc.textContent !== texte) acc.textContent = texte;
  if (voir) { acc.style.fontSize = `${96 * 0.92 * echelle}px`; acc.style.webkitTextStroke = `${18 * echelle}px #000`; }
  const barre = $("#barre-prog");
  barre.hidden = !m.barre;
  if (m.barre) {
    barre.style.height = `${14 * echelle}px`;
    $("i", barre).style.width = `${Math.min(100, (tSortie / Math.max(dureeMontee(blocs), 0.1)) * 100)}%`;
  }
}

/* Interrupteurs « Montage auto » */
$("#r-montage").addEventListener("click", (e) => {
  const b = e.target.closest("[data-m]"); const c = clip(); if (!b || !c) return;
  const cle = b.dataset.m, valeur = !montageDe(c)[cle];
  c.montage = { ...montageDe(c), [cle]: valeur };
  calculerSousTitres(); remplirReglages(c); majZone(); afficherClips(false);
  planifierPatch({ montage: { [cle]: valeur } });
});

/* Sous-titres : même algorithme que montage.py */
function calculerSousTitres() {
  const c = clip(); groupes = [];
  preparerMontage();
  if (!c || !P.segments) return;
  const mots = motsDuPassage(c.debut, c.fin);
  const maxMots = c.style.mots;
  let g = [];
  for (const m of mots) {
    if (g.length && (g.length >= maxMots || m.end - g[0].start > MAX_S_SOUS_TITRE || m.start - g[g.length - 1].end > PAUSE_COUPURE || /[.?!]$/.test(g[g.length - 1].text))) {
      groupes.push(g); g = [];
    }
    g.push(m);
  }
  if (g.length) groupes.push(g);
  groupes = groupes.map((gr) => ({ debut: Math.max(gr[0].start, c.debut), fin: Math.min(gr[gr.length - 1].end, c.fin), mots: gr }))
    .filter((x) => x.fin - x.debut >= 0.05);
  for (let i = 0; i + 1 < groupes.length; i++) groupes[i].fin = Math.min(groupes[i].fin, groupes[i + 1].debut);
  dernierRendu = "";
}

let dernierRendu = "";
function rendreSousTitre(t) {
  const c = clip(), el = $("#soustitre");
  if (!c) return;
  const s = c.style;
  const echelle = ecran.clientHeight / 1920;
  const i = groupes.findIndex((g) => t >= g.debut && t < g.fin);
  let actif = -1;
  if (i >= 0) {
    const mots = groupes[i].mots;
    for (let k = 0; k < mots.length; k++) if (t >= (k === 0 ? groupes[i].debut : mots[k].start)) actif = k;
  }
  const fantome = i < 0 && video.paused;   // à l'arrêt, un repère reste visible pour pouvoir le déplacer
  const anim = montageDe(c).anim;
  const cle = `${i}|${actif}|${JSON.stringify(s)}|${ecran.clientHeight}|${fantome}|${anim}`;
  if (cle === dernierRendu) return;
  const nouveauGroupe = dernierRendu.split("|")[0] !== String(i);
  dernierRendu = cle;
  if (anim && nouveauGroupe && i >= 0) { el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); }
  el.classList.toggle("fantome", fantome);
  el.style.top = `${s.position}%`;
  el.style.fontFamily = `"${s.police}", Arial, sans-serif`;
  el.style.fontSize = `${s.taille * 0.92 * echelle}px`;
  el.style.color = s.couleur;
  el.style.webkitTextStroke = `${s.epaisseur * 2 * echelle}px ${s.contour}`;
  el.style.textShadow = `0 ${2 * echelle}px 0 rgba(0,0,0,.5)`;
  if (i < 0) { el.innerHTML = fantome ? `<span>${s.majuscules ? "SOUS-TITRES" : "Sous-titres"}</span>` : ""; return; }
  const iCle = anim ? motCle(groupes[i].mots) : -1;
  el.innerHTML = groupes[i].mots.map((w, k) => {
    const txt = echap(s.majuscules ? w.text.toLocaleUpperCase("fr-FR") : w.text);
    if (s.surligne && s.surligne !== "aucun" && k === actif) return `<span style="color:${s.surligne}">${txt}</span>`;
    return `<span${k === iCle ? ' class="cle"' : ""}>${txt}</span>`;
  }).join(" ");
}

/* =================== Cadrage intelligent : le même plan que l'export (vision.py) ===================
   Chaque plan : « rect » (un recadrage 9:16), « flou » (contenu entier sur fond flouté)
   ou « partage » (l'info en haut, le visage en bas). La vidéo ne sert que de source d'images. */
const rendu = $("#rendu"), ctxRendu = rendu.getContext("2d");
const tampon = document.createElement("canvas"); tampon.width = 72; tampon.height = 128;
const ctxTampon = tampon.getContext("2d");
const ST_DEFAUT = 68, TI_DEFAUT = 11;
let dernierDessin = "";
function planA(c, t) {
  const pl = c?.cadrages;
  if (!pl?.length) return null;
  return pl.find((p) => t >= p.de - 0.02 && t < p.a) || (t < pl[0].de ? pl[0] : pl[pl.length - 1]);
}
function rendreCadrage(c, t) {
  const plan = planA(c, t);
  ecran.classList.toggle("canvas", !!plan);
  if (!plan || video.readyState < 2 || !video.videoWidth) return plan;
  const W = Math.round(ecran.clientWidth * Math.min(2, window.devicePixelRatio || 1)), H = Math.round(W * 16 / 9);
  const cle = `${video.currentTime}|${JSON.stringify(plan)}|${W}`;
  if (cle === dernierDessin && video.paused) return plan;
  dernierDessin = cle;
  if (rendu.width !== W || rendu.height !== H) { rendu.width = W; rendu.height = H; }
  const vw = video.videoWidth, vh = video.videoHeight;
  const src = (r) => [r[0] * vw, r[1] * vh, Math.max(1, r[2] * vw), Math.max(1, r[3] * vh)];
  ctxRendu.fillStyle = "#000"; ctxRendu.fillRect(0, 0, W, H);
  if (plan.type === "partage") {
    ctxRendu.drawImage(video, ...src(plan.r), 0, 0, W, H / 2);
    ctxRendu.drawImage(video, ...src(plan.r2), 0, H / 2, W, H / 2);
    ctxRendu.fillRect(0, H / 2 - H * 3 / 1920, W, H * 6 / 1920);
  } else if (plan.type === "flou") {
    let fw = vw, fh = vw * 16 / 9;
    if (fh > vh) { fh = vh; fw = vh * 9 / 16; }
    ctxTampon.drawImage(video, (vw - fw) / 2, (vh - fh) / 2, fw, fh, 0, 0, 72, 128);   // petite image agrandie = flou
    ctxRendu.filter = "blur(4px) brightness(.8)";
    ctxRendu.drawImage(tampon, -W * 0.06, -H * 0.06, W * 1.12, H * 1.12);
    ctxRendu.filter = "none";
    const [sx, sy, sw, sh] = src(plan.r), k = Math.min(W / sw, H / sh);
    ctxRendu.drawImage(video, sx, sy, sw, sh, (W - sw * k) / 2, (H - sh * k) / 2, sw * k, sh * k);
  } else {
    ctxRendu.drawImage(video, ...src(plan.r), 0, 0, W, H);
  }
  return plan;
}
/* Fondu au noir juste avant / juste après une jonction entre deux morceaux (comme à l'export) */
function rendreFondu(c, t) {
  if (!montageDe(c).transitions || !jonctions.length) return;
  let noir = 0;
  for (const [finA, debutB] of jonctions) {
    if (t <= finA && finA - t < FONDU) noir = Math.max(noir, 1 - (finA - t) / FONDU);
    if (t >= debutB && t - debutB < FONDU) noir = Math.max(noir, 1 - (t - debutB) / FONDU);
  }
  if (noir <= 0.01) return;
  const cv = $("#rendu"), ctx = cv.getContext("2d");
  ctx.fillStyle = `rgba(0,0,0,${Math.min(1, noir).toFixed(3)})`;
  ctx.fillRect(0, 0, cv.width, cv.height);
  dernierDessin = "";   // l'image suivante doit être redessinée
}

/* Le texte s'écarte de l'info montrée (si la personne n'a pas placé le texte elle-même) */
function placerTextesSelonPlan(c, plan) {
  const st = plan?.st != null && c.style.position === ST_DEFAUT ? plan.st : c.style.position;
  $("#soustitre").style.top = `${st}%`;
  const tx = c.texte || {}, pos = tx.position ?? TI_DEFAUT;
  if (tx.titre || tx.partie) $("#titre-ecran").style.top = `${plan?.ti != null && pos === TI_DEFAUT ? plan.ti : pos}%`;
}

const fond = $("#fond"), ctxFond = fond.getContext("2d");
let dernierFond = 0;
function boucle(now) {
  requestAnimationFrame(boucle);
  const c = clip();
  if (!c || $("#montage").hidden) return;
  let t = video.currentTime;
  const premier = blocs.length ? blocs[0][0] : c.debut;
  const finClip = blocs.length ? blocs[blocs.length - 1][1] : c.fin;
  const finReelle = Number.isFinite(video.duration) ? Math.min(finClip, video.duration) : finClip;
  if (!video.paused && !libre && !glisse && t >= finReelle - 0.02) {
    if ($("#boucle").checked) { video.currentTime = premier; t = premier; }
    else { video.pause(); video.currentTime = premier; t = premier; }
  } else if (!video.paused && !libre && !glisse && (montageDe(c).coupes || blocs.length > 1)) {
    // l'aperçu saute les blancs, comme le fera l'export
    const suivant = blocs.find(([a, b]) => t < b);
    if (suivant && t < suivant[0] - 0.04) { video.currentTime = suivant[0]; t = suivant[0]; }
  }
  if (libre && !video.paused && t >= c.debut && t < c.fin) libre = false;
  rendreSousTitre(t);
  const tSortie = versSortie(t, blocs);
  rendreHabillage(c, tSortie);
  const plan = rendreCadrage(c, t);
  if (plan) rendreFondu(c, t);
  if (!deplace) placerTextesSelonPlan(c, plan);
  $("#t-actuel").textContent = fmt(libre ? t - c.debut : tSortie);
  $("#tl-tete").style.left = `${pos(t)}%`;
  if (c.cadrage.mode === "flou" && !c.cadrages?.length && now - dernierFond > 90 && video.readyState >= 2 && video.videoWidth) {
    dernierFond = now;
    const vw = video.videoWidth, vh = video.videoHeight, r = 9 / 16;
    let sw = vw, sh = vw / r;
    if (sh > vh) { sh = vh; sw = vh * r; }
    ctxFond.drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, fond.width, fond.height);
  }
}
requestAnimationFrame(boucle);

/* =================== timeline =================== */
function calculerFenetre() {
  const c = clip(); if (!c) return;
  const ps = passagesDe(c), a0 = ps[0][0], b0 = ps[ps.length - 1][1];
  const marge = Math.max(8, (b0 - a0) * 0.35);
  fenetre = [Math.max(0, a0 - marge), Math.min(P.duree || b0 + marge, b0 + marge)];
}
const pos = (t) => ((t - fenetre[0]) / (fenetre[1] - fenetre[0])) * 100;
const tempsA = (clientX) => {
  const r = $("#tl-piste").getBoundingClientRect();
  return fenetre[0] + Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * (fenetre[1] - fenetre[0]);
};

function dessinerTimeline() {
  const c = clip(); if (!c) return;
  const [a, b] = fenetre, dur = b - a;
  const pas = [1, 2, 5, 10, 15, 30, 60, 120].find((p) => dur / p <= 9) || 300;
  let grads = "";
  for (let t = Math.ceil(a / pas) * pas; t <= b; t += pas) grads += `<span style="left:${pos(t)}%">${fmtCourt(t)}</span>`;
  $("#tl-graduations").innerHTML = grads;
  let mots = "";
  let n = 0;
  for (const s of P.segments || []) {
    if (s.end < a || s.start > b) continue;
    for (const w of s.words?.length ? s.words : [{ start: s.start, end: s.end }]) {
      if (w.end < a || w.start > b || n++ > 900) continue;
      mots += `<i data-t="${w.start}" style="left:${pos(w.start)}%;width:${Math.max(0.15, pos(w.end) - pos(w.start) - 0.1)}%"></i>`;
    }
  }
  $("#tl-mots").innerHTML = mots;
  majZone();
}

function majZone() {
  const c = clip(); if (!c) return;
  const ps = passagesDe(c);
  if (iPassage >= ps.length) iPassage = ps.length - 1;
  const [da, fa] = ps[iPassage];
  const z = $("#tl-zone");
  z.style.left = `${pos(da)}%`;
  z.style.width = `${pos(fa) - pos(da)}%`;
  // les autres morceaux du clip, en clair : un clic dessus les sélectionne
  $("#tl-autres").innerHTML = ps.map(([a, b], i) => i === iPassage ? ""
    : `<i data-i="${i}" style="left:${pos(a)}%;width:${Math.max(0.5, pos(b) - pos(a))}%"></i>`).join("");
  $$("#tl-mots i").forEach((i) => i.classList.toggle("dedans", ps.some(([a, b]) => +i.dataset.t >= a && +i.dataset.t < b)));
  $("#t-debut").textContent = fmt(da);
  $("#t-fin").textContent = fmt(fa);
  // durée du clip MONTÉ (blancs retirés) et blancs coupés hachurés sur la timeline
  const bl = calculerBlocs(c);
  $("#t-duree").textContent = fmt(dureeMontee(bl));
  const saut = (a, b) => jonctions.some(([x, y]) => Math.abs(x - a) < 0.05 && Math.abs(y - b) < 0.05);
  let coupes = "";
  for (let k = 0; k + 1 < bl.length; k++) {
    if (saut(bl[k][1], bl[k + 1][0])) continue;   // saut entre deux morceaux : pas une coupe de blanc
    coupes += `<i style="left:${pos(bl[k][1])}%;width:${pos(bl[k + 1][0]) - pos(bl[k][1])}%"></i>`;
  }
  $("#tl-coupes").innerHTML = coupes;
  majMorceaux(c, ps);
}

/* Bande « Morceaux » : un bouton par morceau assemblé, + pour en ajouter un */
function majMorceaux(c, ps) {
  const box = $("#morceaux");
  box.hidden = false;
  box.innerHTML = ps.map(([a, b], i) =>
    `<button type="button" class="morceau${i === iPassage ? " choisi" : ""}" data-i="${i}">` +
    `<b>${i + 1}</b> ${fmtCourt(a)} → ${fmtCourt(b)}` +
    (ps.length > 1 ? `<span class="x" data-sup="${i}" title="Enlever ce morceau">✕</span>` : "") +
    `</button>`).join("") +
    `<button type="button" class="morceau ajout" data-ajout="1" title="Ajouter un morceau de la vidéo à ce clip">＋ Ajouter un morceau</button>` +
    (ps.length > 1 ? `<span class="morceaux-aide">fondu entre chaque morceau</span>` : "");
}

$("#morceaux").addEventListener("click", (e) => {
  const c = clip(); if (!c) return;
  const sup = e.target.closest("[data-sup]");
  if (sup) {
    const ps = passagesDe(c);
    if (ps.length < 2) return;
    ps.splice(+sup.dataset.sup, 1);
    c.passages = ps; c.debut = ps[0][0]; c.fin = ps[ps.length - 1][1];
    iPassage = Math.min(iPassage, ps.length - 1);
    calculerSousTitres(); calculerFenetre(); dessinerTimeline(); afficherClips(false);
    envoyerPassages(c, 0);
    return;
  }
  if (e.target.closest("[data-ajout]")) {
    const ps = passagesDe(c);
    const t = video.currentTime, max = P.duree || t + 20;
    let a = Math.max(0, Math.min(t, max - DUREE_MIN));
    for (const [x, y] of ps) if (a >= x - 0.01 && a < y) a = y + 0.05;   // déjà dans un morceau : on démarre après
    let b = Math.min(max, a + 15);
    for (const [x] of ps) if (x > a) { b = Math.min(b, x - 0.5); break; }   // et on s'arrête avant le suivant
    if (b - a < DUREE_MIN) { toast("Pas la place d'ajouter un morceau ici"); return; }
    ps.push([+a.toFixed(2), +b.toFixed(2)]);
    ps.sort((u, v) => u[0] - v[0]);
    c.passages = ps; c.debut = ps[0][0]; c.fin = ps[ps.length - 1][1];
    iPassage = ps.findIndex(([x]) => Math.abs(x - a) < 0.001);
    calculerSousTitres(); calculerFenetre(); dessinerTimeline(); afficherClips(false);
    envoyerPassages(c, 0);
    video.currentTime = a;
    return;
  }
  const b = e.target.closest("[data-i]");
  if (b) { iPassage = +b.dataset.i; majZone(); video.currentTime = passageCourant(c)[0]; }
});
$("#tl-autres").addEventListener("pointerdown", (e) => {
  const b = e.target.closest("[data-i]"); const c = clip();
  if (b && c) { iPassage = +b.dataset.i; majZone(); video.currentTime = passageCourant(c)[0]; }
});

$("#tl-piste").addEventListener("pointerdown", (e) => {
  const c = clip(); if (!c) return;
  const p = e.target.closest(".poignee");
  if (p) {
    glisse = p.dataset.p;
    glisse_depart = { passages: JSON.stringify(passagesDe(c)), clip: c.id };
    e.target.setPointerCapture(e.pointerId);
    video.pause();
    return;
  }
  const t = tempsA(e.clientX);
  libre = !passagesDe(c).some(([a, b]) => t >= a && t <= b);
  video.currentTime = t;
});
$("#tl-piste").addEventListener("pointermove", (e) => {
  if (!glisse) return;
  const c = clip(); const t = tempsA(e.clientX);
  const [a, b] = passageCourant(c);
  if (glisse === "debut") ecrirePassage(c, iPassage, Math.min(t, b - DUREE_MIN), b);
  else ecrirePassage(c, iPassage, a, Math.max(t, a + DUREE_MIN));
  video.currentTime = glisse === "debut" ? passageCourant(c)[0] : passageCourant(c)[1];
  calculerSousTitres(); majZone();
});
let glisse_depart = null;
function finGlisse() {
  if (!glisse) return;
  const c = clip(); glisse = null;
  if (!c) return;
  // un simple clic sans déplacement ne doit rien envoyer (ni effacer « exporté », ni remplir l'historique)
  if (glisse_depart && glisse_depart.clip === c.id && glisse_depart.passages !== JSON.stringify(passagesDe(c))) {
    envoyerPassages(c, 0);
  }
  glisse_depart = null;
  calculerFenetre(); dessinerTimeline();
  video.currentTime = passageCourant(c)[0];
}
$("#tl-piste").addEventListener("pointerup", finGlisse);
$("#tl-piste").addEventListener("pointercancel", finGlisse);
$("#tl-piste").addEventListener("lostpointercapture", finGlisse);

$(".bornes").addEventListener("click", (e) => {
  const b = e.target.closest("button"); const c = clip(); if (!b || !c) return;
  const [a, b0] = passageCourant(c);
  if (b.dataset.ici) {
    const t = video.currentTime;
    if (b.dataset.ici === "debut") ecrirePassage(c, iPassage, Math.min(t, b0 - DUREE_MIN), b0);
    else ecrirePassage(c, iPassage, a, Math.max(t, a + DUREE_MIN));
  } else {
    const d = +b.dataset.d;
    if (b.dataset.borne === "debut") ecrirePassage(c, iPassage, Math.max(0, Math.min(a + d, b0 - DUREE_MIN)), b0);
    else ecrirePassage(c, iPassage, a, Math.min(P.duree || 1e9, Math.max(b0 + d, a + DUREE_MIN)));
    const [na, nb] = passageCourant(c);
    video.currentTime = b.dataset.borne === "debut" ? na : Math.max(na, nb - 2);
  }
  calculerSousTitres(); majZone();
  envoyerPassages(c);
});

/* =================== réglages =================== */
$("#r-police").innerHTML = POLICES.map((p) => `<option style="font-family:'${p}'">${p}</option>`).join("");
$("#r-couleur").innerHTML = COULEURS.map((c) => `<button type="button" class="pastille" data-v="${c}" style="background:${c}" aria-label="${c}"></button>`).join("");
$("#r-surligne").innerHTML = `<button type="button" class="pastille aucun" data-v="" title="Sans karaoké" aria-label="Aucun"></button>` +
  COULEURS.slice(1, 8).map((c) => `<button type="button" class="pastille" data-v="${c}" style="background:${c}" aria-label="${c}"></button>`).join("");
$("#r-mots").innerHTML = [1, 2, 3, 4, 5].map((n) => `<button type="button" data-v="${n}">${n}</button>`).join("");

function remplirReglages(c) {
  const actif = document.activeElement;
  if (actif !== $("#titre-clip")) $("#titre-clip").value = c.titre;
  $("#note-clip").textContent = `★ ${Math.round(c.note)}/10`;
  $("#raison-clip").textContent = c.raison || "";
  const s = c.style;
  $("#r-police").value = s.police;
  if (actif !== $("#r-taille")) $("#r-taille").value = s.taille;
  $("#v-taille").textContent = s.taille;
  if (actif !== $("#r-position")) $("#r-position").value = s.position;
  $("#v-position").textContent = `${Math.round(s.position)}%`;
  $$("#r-couleur .pastille").forEach((b) => b.classList.toggle("choisie", b.dataset.v.toUpperCase() === s.couleur.toUpperCase()));
  const surligne = s.surligne === "aucun" ? "" : (s.surligne || "");
  $$("#r-surligne .pastille").forEach((b) => b.classList.toggle("choisie", b.dataset.v.toUpperCase() === surligne.toUpperCase()));
  $$("#r-mots button").forEach((b) => b.classList.toggle("choisi", +b.dataset.v === s.mots));
  $("#r-maj").setAttribute("aria-pressed", String(!!s.majuscules));
  $("#r-maj").textContent = s.majuscules ? "AA" : "Aa";
  const cad = c.cadrage;
  $$("#r-cadrage button").forEach((b) => b.classList.toggle("choisi", b.dataset.v === cad.mode));
  const auto = cad.auto !== false;
  $("#r-auto").setAttribute("aria-pressed", String(auto));
  $("#aide-auto").textContent = auto
    ? "Suit le visage et montre les infos affichées (article, capture…) ; écran partagé quand il y a les deux. Choix ci-dessous = quand il n'y a ni l'un ni l'autre."
    : "Cadrage fixe sur tout le clip.";
  ecran.classList.toggle("remplir", cad.mode === "remplir");
  video.style.objectPosition = `${50 + cad.decalage}% 50%`;
  $("#ligne-decalage").hidden = cad.mode !== "remplir";
  if (actif !== $("#r-decalage")) $("#r-decalage").value = cad.decalage;
  $("#v-decalage").textContent = cad.decalage > 0 ? `+${cad.decalage}` : cad.decalage;
  const mt = montageDe(c);
  $$("#r-montage [data-m]").forEach((b) => b.setAttribute("aria-pressed", String(!!mt[b.dataset.m])));
  const tx = c.texte || {};
  $("#r-titre").setAttribute("aria-pressed", String(!!tx.titre));
  $("#r-partie").setAttribute("aria-pressed", String(!!tx.partie));
  $("#r-partie").textContent = `Partie ${c.numero ?? "N"}`;
  if (actif !== $("#r-contenu")) $("#r-contenu").value = tx.contenu || "";
  $("#r-contenu").placeholder = `(${c.titre})`;
  if (actif !== $("#r-num")) $("#r-num").value = tx.numero ?? "";
  $("#ligne-tpos").hidden = $("#ligne-contenu").hidden = $("#ligne-num").hidden = !(tx.titre || tx.partie);
  if (actif !== $("#r-tpos")) $("#r-tpos").value = tx.position ?? 11;
  $("#v-tpos").textContent = `${Math.round(tx.position ?? 11)}%`;
  rendreTitre(c);
  $("#btn-annuler").disabled = !c.peut_annuler;
  majEtatExport(c);
}

/* Titre incrusté (même rendu que la boîte « Titre » de montage.py) */
function rendreTitre(c) {
  const el = $("#titre-ecran"), tx = c?.texte || {};
  if (el.querySelector(".te-edit")) return;   // édition en cours : on ne réécrit pas par-dessus
  if (!c || !(tx.titre || tx.partie)) { el.innerHTML = ""; return; }
  const echelle = ecran.clientHeight / 1920;
  el.style.top = `${tx.position ?? 11}%`;
  el.style.fontSize = `${(tx.taille || 56) * 0.92 * echelle}px`;
  const lignes = [];
  if (tx.partie) lignes.push(`<span class="te-partie">PARTIE ${echap(tx.numero || c.numero || 1)}</span>`);
  if (tx.titre) lignes.push(echap((tx.contenu || "").trim() || c.titre));
  el.innerHTML = `<span class="te-bloc">${lignes.join("<br>")}</span>`;
}

function changerTexte(partiel) {
  const c = clip(); if (!c) return;
  c.texte = { ...(c.texte || {}), ...partiel };
  remplirReglages(c);
  planifierPatch({ texte: partiel });
}
$("#r-contenu").addEventListener("change", (e) => changerTexte({ contenu: e.target.value.trim() }));
$("#r-contenu").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("#r-num").addEventListener("change", (e) => changerTexte({ numero: e.target.value ? +e.target.value : null }));
$("#r-titre").addEventListener("click", () => changerTexte({ titre: !clip().texte?.titre }));
$("#r-partie").addEventListener("click", () => changerTexte({ partie: !clip().texte?.partie }));
$("#r-tpos").addEventListener("input", (e) => changerTexte({ position: +e.target.value }));
$("#btn-tous").addEventListener("click", async () => {
  const c = clip(); if (!c) return;
  await envoyerPatchMaintenant();
  const e = await api(`/api/projets/${P.id}/clips/${c.id}/appliquer-a-tous`, { body: {} });
  fusionner(e);
  toast(`Style appliqué aux ${P.clips.length} clips`);
});

function majEtatExport(c) {
  const job = P.jobs.find((j) => j.clip === c.id && j.type === "export" && j.etat === "en_cours");
  const box = $("#export-etat");
  $("#btn-exporter").disabled = !!job;
  if (job) box.textContent = `${job.etape}… ${job.pct} %`;
  else if (c.exporte) box.innerHTML = `✔ Exporté — <button type="button" class="lien" data-ouvrir>ouvrir le fichier</button>`;
  else box.textContent = "";
}
$("#export-etat").addEventListener("click", (e) => {
  if (e.target.closest("[data-ouvrir]")) api(`/api/projets/${P.id}/ouvrir`, { body: { clip: cur } });
});

function changerStyle(partiel) {
  const c = clip(); if (!c) return;
  Object.assign(c.style, partiel);
  if ("mots" in partiel) calculerSousTitres();
  dernierRendu = "";
  remplirReglages(c);
  planifierPatch({ style: partiel });
}
function changerCadrage(partiel) {
  const c = clip(); if (!c) return;
  Object.assign(c.cadrage, partiel);
  if (c.cadrage.auto === false || "auto" in partiel) c.cadrages = [];   // l'ancien plan ne vaut plus : le serveur renvoie le nouveau
  remplirReglages(c);
  dernierDessin = "";
  planifierPatch({ cadrage: { auto: c.cadrage.auto !== false, ...partiel } });
}
$("#r-auto").addEventListener("click", () => changerCadrage({ auto: clip()?.cadrage.auto === false }));

$("#r-police").addEventListener("change", (e) => changerStyle({ police: e.target.value }));
$("#r-taille").addEventListener("input", (e) => changerStyle({ taille: +e.target.value }));
$("#r-position").addEventListener("input", (e) => changerStyle({ position: +e.target.value }));
$("#r-couleur").addEventListener("click", (e) => { const b = e.target.closest(".pastille"); if (b) changerStyle({ couleur: b.dataset.v }); });
$("#r-surligne").addEventListener("click", (e) => { const b = e.target.closest(".pastille"); if (b) changerStyle({ surligne: b.dataset.v || "aucun" }); });
$("#r-mots").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) changerStyle({ mots: +b.dataset.v }); });
$("#r-maj").addEventListener("click", () => changerStyle({ majuscules: !clip().style.majuscules }));
$("#r-cadrage").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) changerCadrage({ mode: b.dataset.v }); });
$("#r-decalage").addEventListener("input", (e) => changerCadrage({ decalage: +e.target.value }));
$("#titre-clip").addEventListener("change", (e) => { const c = clip(); if (c && e.target.value.trim()) { c.titre = e.target.value.trim(); afficherClips(false); rendreTitre(c); planifierPatch({ titre: c.titre }, 0); } });
$("#titre-clip").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });

/* Envoi groupé des modifs (évite une requête à chaque cran de curseur) */
function planifierPatch(partiel, delai = 400) {
  for (const [k, v] of Object.entries(partiel)) {
    // une liste (les morceaux du clip) remplace l'ancienne ; un objet (style, cadrage…) se complète
    patchEnAttente[k] = typeof v === "object" && v && !Array.isArray(v) ? { ...(patchEnAttente[k] || {}), ...v } : v;
  }
  patchEnAttente.__clip = cur;
  clearTimeout(minuteurPatch);
  minuteurPatch = setTimeout(envoyerPatchMaintenant, delai);
}
async function envoyerPatchMaintenant(essai = 0) {
  clearTimeout(minuteurPatch);
  const corps = patchEnAttente; patchEnAttente = {};
  const cid = corps.__clip; delete corps.__clip;
  if (!P || !cid || !Object.keys(corps).length) return;
  const pid = P.id;
  patchEnVol++;
  try {
    const maj = await api(`/api/projets/${pid}/clips/${cid}`, { method: "PATCH", body: corps });
    if (P?.id !== pid || Object.keys(patchEnAttente).length) return;   // autre projet, ou d'autres modifs arrivent
    const i = P.clips.findIndex((c) => c.id === cid);
    if (i >= 0) P.clips[i] = maj;
    if (cid === cur) { remplirReglages(maj); majZone(); }
    afficherClips(false);
  } catch (err) {
    if (/n'existe plus/.test(err.message)) return;   // clip supprimé entre-temps : rien à garder
    if (P?.id === pid && essai < 3) {
      // on remet la modif dans la file (sans écraser une modif plus récente) et on réessaie
      for (const [k, v] of Object.entries(corps)) {
        if (!(k in patchEnAttente)) patchEnAttente[k] = v;
        else if (typeof v === "object" && v) patchEnAttente[k] = { ...v, ...patchEnAttente[k] };
      }
      patchEnAttente.__clip ||= cid;
      clearTimeout(minuteurPatch);
      minuteurPatch = setTimeout(() => envoyerPatchMaintenant(essai + 1), 1500 * (essai + 1));
    } else toast("Une modification n'a pas pu être enregistrée.");
  } finally { patchEnVol--; }
}

$("#btn-annuler").addEventListener("click", annulerModif);
function annulerModif() {
  return uneFois("annuler", async () => {
    const c = clip(); if (!c || !c.peut_annuler) return;
    const pid = P.id;
    await envoyerPatchMaintenant();
    const maj = await api(`/api/projets/${pid}/clips/${c.id}/annuler`, { body: {} });
    if (P?.id !== pid) return;
    const i = P.clips.findIndex((x) => x.id === c.id);
    if (i >= 0) P.clips[i] = maj;
    calculerSousTitres(); afficherClips(false); afficherMontage(true);
    if (cur === c.id) video.currentTime = maj.debut;
    toast(maj.annule ? "Modif annulée" : "Rien à annuler");
  });
}
$("#btn-suppr").addEventListener("click", () => uneFois("suppr", async () => {
  const c = clip(); if (!c || !confirm(`Supprimer le clip « ${c.titre} » ?`)) return;
  const pid = P.id, id = c.id;
  if (patchEnAttente.__clip === id) { clearTimeout(minuteurPatch); patchEnAttente = {}; }
  await api(`/api/projets/${pid}/clips/${id}`, { method: "DELETE" });
  if (P?.id !== pid) return;
  const i = P.clips.findIndex((x) => x.id === id);
  if (i >= 0) P.clips.splice(i, 1);
  cur = (P.clips[Math.max(0, i)] || P.clips[i - 1])?.id || null;
  apresChangementDeClip();
  toutAfficher(true);
}));
$("#btn-exporter").addEventListener("click", () => uneFois("exporter", async () => {
  if (P.source_existe === false) { toast("La vidéo d'origine est introuvable : clique sur « Retrouver la vidéo »."); return; }
  $("#btn-exporter").disabled = true;
  await envoyerPatchMaintenant();
  const job = await api(`/api/projets/${P.id}/clips/${cur}/exporter`, { body: {} });
  if (!P.jobs.some((j) => j.id === job.id)) P.jobs.push(job);
  majEtatExport(clip()); afficherJobs(); planifierPoll(400);
}).finally(() => { if (clip()) majEtatExport(clip()); }));
$("#btn-tout").addEventListener("click", () => uneFois("tout", async () => {
  if (!P.clips.length) return;
  if (P.source_existe === false) { toast("La vidéo d'origine est introuvable : clique sur « Retrouver la vidéo »."); return; }
  await envoyerPatchMaintenant();
  const nouveaux = await api(`/api/projets/${P.id}/exporter-tout`, { body: {} });
  P.jobs.push(...nouveaux.filter((n) => !P.jobs.some((j) => j.id === n.id))); afficherJobs(); planifierPoll(400);
  toast(nouveaux.length ? `${nouveaux.length} clip(s) en cours d'export` : "Tous les clips sont déjà exportés ✔");
}));
$("#btn-dossier").addEventListener("click", () => uneFois("dossier", () => api(`/api/projets/${P.id}/ouvrir`, { body: {} })));
$("#btn-retour").addEventListener("click", async () => {
  await envoyerPatchMaintenant();
  clearTimeout(minuteurPoll);
  video.pause();
  P = null; cur = null; envoiChat = false; $(".envoyer").disabled = false;
  $("#studio").hidden = true; $("#accueil").hidden = false;
  chargerProjets();
});

/* =================== chat =================== */
function afficherChat() {
  const fil = $("#fil");
  const msgs = (P.chat || []).filter((m) => !m.cid || m.cid === cur);
  fil.innerHTML = msgs.map((m) => `<div class="bulle ${m.role}${m.role === "ia" && !m.cid ? " globale" : ""}">${echap(m.texte)}</div>`).join("") +
    (envoiChat ? `<div class="bulle ia ecrit"><i></i><i></i><i></i></div>` : "");
  if (!msgs.length && !envoiChat) {
    fil.innerHTML = `<div class="bulle ia globale">Je suis ton assistant de montage. Dis-moi ce que tu veux changer sur ce clip, je le fais en direct.</div>`;
  }
  fil.scrollTop = fil.scrollHeight;
}

function chatDisponible() {
  if (!P) return false;
  if (P.etat === "traitement") { toast("Attends la fin de l'analyse de la vidéo, puis je suis à toi."); return false; }
  if (envoiChat) { toast("Je réponds encore au message précédent…"); return false; }
  return true;
}

async function envoyerMessage(texte) {
  texte = texte.trim();
  if (!texte || !chatDisponible()) return false;
  const pid = P.id, cid = cur;
  await envoyerPatchMaintenant();
  const avant = clip() ? { debut: clip().debut, fin: clip().fin } : null;
  P.chat.push({ role: "moi", texte, cid, t: Date.now() / 1000 });
  envoiChat = true; $(".envoyer").disabled = true;
  afficherChat();
  try {
    const e = await api(`/api/projets/${pid}/chat`, { body: { clip: cid, message: texte } });
    if (P?.id !== pid) return true;       // on a quitté le projet entre-temps
    envoiChat = false;
    fusionner(e);
    afficherChat();
    if (cur !== cid) { toast("L'assistant a répondu sur un autre clip."); return true; }
    if (e.propositions) afficherIdees(e.propositions);
    const c = clip();
    if (c && avant && (c.debut !== avant.debut || c.fin !== avant.fin)) {
      libre = false; video.currentTime = c.debut; video.play().catch(() => {});
    }
  } catch (err) {
    if (P?.id !== pid) return true;
    envoiChat = false; P.chat.push({ role: "ia", texte: err.message, cid, t: Date.now() / 1000 }); afficherChat();
  } finally {
    if (P?.id === pid) { envoiChat = false; $(".envoyer").disabled = false; planifierPoll(500); }
  }
  return true;
}

$("#form-chat").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ta = $("#message"); const t = ta.value;
  if (!t.trim() || !chatDisponible()) return;   // le texte reste dans la zone si on ne peut pas l'envoyer
  ta.value = ""; ta.style.height = "";
  envoyerMessage(t);
});
$("#message").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#form-chat").requestSubmit(); }
});
$("#message").addEventListener("input", (e) => { e.target.style.height = ""; e.target.style.height = `${Math.min(140, e.target.scrollHeight)}px`; });
$("#puces-chat").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) envoyerMessage(b.textContent); });

/* =================== clavier =================== */
document.addEventListener("keydown", (e) => {
  if (!P || $("#studio").hidden) return;
  const el = document.activeElement, tag = el?.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || el?.isContentEditable
      || (tag === "INPUT" && el.type !== "range" && el.type !== "checkbox")) return;
  const c = clip(); if (!c) return;
  if (e.code === "Space" && tag !== "BUTTON") { e.preventDefault(); basculerLecture(); }
  else if (tag === "BUTTON" || e.ctrlKey || e.altKey || e.metaKey) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.repeat) { e.preventDefault(); annulerModif(); }
  }
  else if (e.key === "i" || e.key === "I") $('[data-ici="debut"]').click();
  else if (e.key === "o" || e.key === "O") $('[data-ici="fin"]').click();
});

/* =================== déplacer le titre / les sous-titres à la souris =================== */
let deplace = null;   // { quoi: "sous" | "titre", y0, pos0 }
function positionDe(quoi, c) { return quoi === "sous" ? c.style.position : (c.texte?.position ?? 11); }
function poserPosition(quoi, c, pos) {
  if (quoi === "sous") { c.style.position = Math.round(Math.max(8, Math.min(92, pos))); dernierRendu = ""; }
  else { c.texte = { ...(c.texte || {}), position: Math.round(Math.max(3, Math.min(60, pos))) }; }
  remplirReglages(c);
  rendreSousTitre(video.currentTime);
}
for (const [id, quoi] of [["#soustitre", "sous"], ["#titre-ecran", "titre"]]) {
  const el = $(id);
  el.title = "Glisse pour déplacer · molette pour changer la taille";
  el.addEventListener("pointerdown", (e) => {
    const c = clip(); if (!c || !el.textContent.trim() || e.target.closest(".te-edit")) return;
    e.preventDefault(); e.stopPropagation();
    el.setPointerCapture(e.pointerId);
    deplace = { quoi, y0: e.clientY, pos0: positionDe(quoi, c), clip: c.id };
    el.classList.add("deplace");
  });
  el.addEventListener("pointermove", (e) => {
    if (!deplace || deplace.quoi !== quoi) return;
    const c = clip();
    poserPosition(quoi, c, deplace.pos0 + ((e.clientY - deplace.y0) / ecran.clientHeight) * 100);
  });
  const lacher = () => {
    if (!deplace || deplace.quoi !== quoi) return;
    const c = clip(), depart = deplace; deplace = null; el.classList.remove("deplace");
    if (!c || c.id !== depart.clip || positionDe(quoi, c) === depart.pos0) return;   // simple clic : rien à envoyer
    planifierPatch(quoi === "sous" ? { style: { position: c.style.position } } : { texte: { position: c.texte.position } }, 0);
  };
  el.addEventListener("pointerup", lacher);
  el.addEventListener("pointercancel", lacher);
  el.addEventListener("lostpointercapture", lacher);
  el.addEventListener("wheel", (e) => {
    const c = clip(); if (!c || !el.textContent.trim()) return;
    e.preventDefault();
    const pas = e.deltaY < 0 ? 4 : -4;
    if (quoi === "sous") changerStyle({ taille: Math.max(40, Math.min(140, c.style.taille + pas)) });
    else changerTexte({ taille: Math.max(30, Math.min(110, (c.texte?.taille || 56) + pas)) });
  }, { passive: false });
}

api("/api/version").then(v => $$("[data-version]").forEach(e => { e.textContent = "v" + v.version; }))
  .catch(() => {});

/* Titres proposés par l'IA d'après ce qui est dit dans le clip */
function afficherIdees(titres) {
  const box = $("#idees");
  box.hidden = false;
  box.innerHTML = titres.map((t) => `<button type="button" class="idee">${echap(t)}</button>`).join("") +
    `<button type="button" class="idee autre" data-autre>↻ D'autres idées</button>`;
}
async function chercherIdees() {
  const c = clip(); if (!c) return;
  const box = $("#idees");
  box.hidden = false;
  box.innerHTML = `<span class="doux petit-texte">L'IA lit le clip et cherche des titres…</span>`;
  try {
    const { titres } = await api(`/api/projets/${P.id}/clips/${c.id}/titres`, { body: {} });
    if (clip()?.id === c.id) afficherIdees(titres);
  } catch (err) { box.innerHTML = `<span class="doux petit-texte">${echap(err.message)}</span>`; }
}
$("#btn-idees").addEventListener("click", chercherIdees);
$("#idees").addEventListener("click", (e) => {
  const b = e.target.closest(".idee"); if (!b) return;
  if (b.dataset.autre !== undefined) { chercherIdees(); return; }
  const c = clip(); if (!c) return;
  c.titre = b.textContent;
  $("#titre-clip").value = c.titre;
  $$("#idees .idee").forEach((x) => x.classList.toggle("choisie", x === b));
  afficherClips(false); rendreTitre(c);
  planifierPatch({ titre: c.titre }, 0);
  toast("Titre appliqué");
});
$("#btn-titrer-tous").addEventListener("click", async () => {
  if (!P?.clips.length) return;
  if (!confirm(`Donner un nouveau titre aux ${P.clips.length} clips, d'après ce qui est dit dedans ?`)) return;
  await envoyerPatchMaintenant();
  const job = await api(`/api/projets/${P.id}/titrer-tous`, { body: {} });
  P.jobs.push(job); afficherJobs(); planifierPoll(400);
  toast("Je cherche un titre pour chaque clip…");
});

/* Double-clic sur le titre dans la vidéo : on le retape sur place */
$("#titre-ecran").addEventListener("dblclick", (e) => {
  const c = clip(); if (!c || !c.texte?.titre) return;
  e.preventDefault(); e.stopPropagation();
  const el = $("#titre-ecran");
  const champ = document.createElement("input");
  champ.className = "te-edit";
  champ.value = (c.texte.contenu || "").trim() || c.titre;
  el.innerHTML = "";
  el.appendChild(champ);
  champ.focus(); champ.select();
  let fini = false;
  const valider = (garder) => {
    if (fini) return;
    fini = true;
    const texte = champ.value.trim();
    champ.remove();          // sinon le garde-fou de rendreTitre bloque le réaffichage
    dernierRendu = "";
    if (garder && texte) changerTexte({ contenu: texte === c.titre ? "" : texte });
    else remplirReglages(c);
  };
  champ.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") valider(true);
    if (ev.key === "Escape") valider(false);
  });
  champ.addEventListener("blur", () => valider(true));
});

/* =================== état de l'IA (téléchargement automatique) =================== */
async function suivreIA() {
  let e = null;
  try { e = await api("/api/ia"); } catch (_) { /* serveur en train de démarrer */ }
  const pastille = $("#ia-etat");
  if (e && e.etat === "telechargement") {
    pastille.hidden = false;
    pastille.innerHTML = `<span class="rec petit"></span> Téléchargement de la nouvelle IA… <b class="mono">${e.pct} %</b>`;
  } else if (e && e.acceleration?.etat === "installation") {
    pastille.hidden = false;
    pastille.innerHTML = `<span class="rec petit"></span> ${echap(e.acceleration.message)}`;
  } else if (e && e.message) {
    pastille.hidden = false; pastille.textContent = e.message;
  } else if (e) {
    pastille.hidden = true;
  }
  if (!e || e.etat === "verification" || e.etat === "telechargement" || ["verification", "installation"].includes(e.acceleration?.etat)) {
    setTimeout(suivreIA, 3000);
  }
}
suivreIA();

/* =================== mises à jour =================== */
async function verifierMaj() {
  try {
    const m = await api("/api/maj");
    const b = $("#maj-dispo");
    b.hidden = !m.disponible;
    if (m.disponible) b.innerHTML = `<span class="rec petit"></span> Mise à jour <b>v${m.distante}</b> disponible · <u>Installer</u>`;
  } catch (_) { /* hors ligne */ }
}
$("#maj-dispo").addEventListener("click", async () => {
  try {
    await api("/api/maj/installer", { body: {} });
    $("#maj-dispo").innerHTML = "Mise à jour en cours… Studio Clips va se rouvrir tout seul.";
    setTimeout(() => window.close(), 2500);
  } catch (err) { toast(err.message); }
});
verifierMaj(); setInterval(verifierMaj, 30 * 60 * 1000);

/* =================== vie de la fenêtre =================== */
const ping = () => fetch("/api/ping", { method: "POST" }).catch(() => {});
ping(); setInterval(ping, 20000);
addEventListener("pagehide", () => {
  // dernière retouche pas encore partie : on l'envoie avant de fermer
  const corps = { ...patchEnAttente }; const cid = corps.__clip; delete corps.__clip;
  if (P && cid && Object.keys(corps).length) {
    navigator.sendBeacon(`/api/projets/${P.id}/clips/${cid}`, new Blob([JSON.stringify(corps)], { type: "application/json" }));
  }
  navigator.sendBeacon("/api/bye");
});
addEventListener("resize", () => { dernierRendu = ""; rendreTitre(clip()); });

chargerProjets();
