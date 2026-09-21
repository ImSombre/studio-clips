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
const jobsVus = new Set();

async function api(url, opts = {}) {
  const r = await fetch(url, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let m = `Erreur ${r.status}`;
    try { m = (await r.json()).erreur || m; } catch (_) { /* réponse non JSON */ }
    throw new Error(m);
  }
  return r.status === 204 ? null : r.json();
}

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

async function chargerProjets() {
  const liste = await api("/api/projets");
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
        <div class="projet-infos"><span class="badge ${p.etat}">${etats[p.etat] || p.etat}</span>
          <span class="mono">${p.clips} clip${p.clips > 1 ? "s" : ""}${p.exportes ? ` · ${p.exportes} exporté${p.exportes > 1 ? "s" : ""}` : ""}</span>
          <span>${new Date(p.cree).toLocaleDateString("fr-FR", { day: "numeric", month: "short" })}</span></div>
      </div>
      <button class="projet-suppr" data-suppr="${p.id}" title="Supprimer le projet" type="button">✕</button>
    </div>`).join("");
}

$("#liste-projets").addEventListener("click", async (e) => {
  const suppr = e.target.closest("[data-suppr]");
  if (suppr) {
    e.stopPropagation();
    if (confirm("Supprimer ce projet ? (tes clips déjà exportés ne sont pas touchés)")) {
      await api(`/api/projets/${suppr.dataset.suppr}`, { method: "DELETE" });
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
  $("#nom-video").textContent = "Choix en cours…";
  try {
    const { chemin } = await api("/api/choisir", { body: {} });
    if (chemin) {
      cheminChoisi = chemin;
      $("#nom-video").textContent = chemin.split(/[\\/]/).pop();
      $("#chemin-video").textContent = chemin;
      b.classList.add("choisie");
    } else if (!cheminChoisi) {
      $("#nom-video").textContent = "Choisir une vidéo";
    } else {
      $("#nom-video").textContent = cheminChoisi.split(/[\\/]/).pop();
    }
  } catch (err) { toast(err.message); $("#nom-video").textContent = "Choisir une vidéo"; }
  $("#btn-lancer").disabled = !cheminChoisi;
});

$("#puces-accueil").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const ta = $("#consigne");
  ta.value = ta.value.trim() ? `${ta.value.trim().replace(/[,.]$/, "")}, ${b.dataset.texte}` : b.dataset.texte;
  ta.focus();
});

$("#btn-lancer").addEventListener("click", async () => {
  if (!cheminChoisi) return;
  $("#btn-lancer").disabled = true;
  try {
    const { id } = await api("/api/projets", { body: { chemin: cheminChoisi, consigne: $("#consigne").value } });
    cheminChoisi = ""; $("#consigne").value = "";
    $("#nom-video").textContent = "Choisir une vidéo"; $("#chemin-video").textContent = "MP4, MOV, MKV, AVI…";
    $("#btn-choisir").classList.remove("choisie");
    ouvrirProjet(id);
  } catch (err) { toast(err.message); $("#btn-lancer").disabled = false; }
});

/* =================== STUDIO : ouverture / synchro =================== */
async function ouvrirProjet(id) {
  P = await api(`/api/projets/${id}`);
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
  try {
    const e = await api(`/api/projets/${P.id}/etat`);
    const avaitSegments = P.segments?.length;
    fusionner(e);
    if (!avaitSegments && e.etat === "pret") {
      const complet = await api(`/api/projets/${P.id}`);
      P.segments = complet.segments; P.duree = complet.duree;
      toutAfficher(true);
    }
  } catch (_) { /* serveur momentanément occupé : on réessaie */ }
  const occupe = P && (P.etat === "traitement" || P.jobs.some((j) => j.etat === "en_cours"));
  planifierPoll(occupe ? 1000 : 3500);
}

function fusionner(e) {
  const ancien = clip();
  const anciensIds = P.clips.map((c) => c.id).join();
  const garde = glisse || deplace || Object.keys(patchEnAttente).length ? ancien : null;
  Object.assign(P, { etat: e.etat, erreur: e.erreur, duree: e.duree ?? P.duree, jobs: e.jobs, source_existe: e.source_existe });
  P.clips = e.clips.map((c) => (garde && c.id === garde.id ? garde : c));
  const chatChange = (P.chat?.length || 0) !== e.chat.length;
  P.chat = e.chat;

  // Tâches terminées depuis la dernière synchro
  for (const j of e.jobs) {
    const cle = j.id + j.etat;
    if (jobsVus.has(cle)) continue;
    jobsVus.add(cle);
    if (j.etat === "fini" && j.type === "nouveau_clip" && j.resultat) { cur = j.resultat; toast("Nouveau clip trouvé"); }
    if (j.etat === "fini" && j.type === "export") toast("Clip exporté ✔");
    if (j.etat === "fini" && j.type === "apercu") rechargerVideo();
    if (j.etat === "erreur" && j.type !== "analyse") toast(`Problème : ${j.erreur}`);
  }
  if (!clip()) cur = P.clips[0]?.id || null;

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
    $(".clip-meta", el).innerHTML = `<span>${fmtCourt(c.fin - c.debut)}</span><span>★ ${Math.round(c.note)}</span>${c.exporte ? '<span class="ok">✔ exporté</span>' : ""}`;
    const pr = $(".clip-progression", el);
    pr.hidden = !job; if (job) $("i", pr).style.width = `${job.pct}%`;
    const vign = $(".clip-vignette", el), url = `/miniature/${P.id}/${c.id}?d=${c.debut}`;
    if (vign.dataset.url !== url) { vign.dataset.url = url; vign.style.backgroundImage = `url('${url}')`; }
  });
}

$("#liste-clips").addEventListener("click", (e) => {
  const b = e.target.closest(".clip"); if (!b || b.dataset.id === cur) return;
  envoyerPatchMaintenant();
  cur = b.dataset.id; libre = false; $("#idees").hidden = true;
  afficherClips(false); afficherMontage(true); afficherChat();
  const v = $("#lecteur"); v.currentTime = clip().debut; v.play().catch(() => {});
});

/* =================== traitement =================== */
function afficherTraitement() {
  const job = P.jobs.filter((j) => j.type === "analyse").pop();
  const enCours = P.etat === "traitement";
  const montrer = enCours || (!P.clips.length && P.etat !== "pret");
  $("#traitement").hidden = !montrer;
  $("#vide").hidden = montrer || P.clips.length > 0;
  if (!montrer) return;
  const pct = job ? job.pct : 0;
  $("#traitement-etape").textContent = enCours ? (job?.etape || "Démarrage…") : (P.etat === "erreur" ? "Le traitement a échoué" : "Traitement interrompu");
  $("#traitement-barre").style.width = `${pct}%`;
  $("#traitement-pct").textContent = `${pct} %`;
  $("#traitement-msg").textContent = job?.message || "";
  const etape = pct < 66 ? 1 : pct < 100 ? 2 : 3;
  $$("#traitement-etapes li").forEach((li) => {
    const n = +li.dataset.e;
    li.className = n < etape ? "faite" : n === etape && enCours ? "encours" : "";
  });
  const err = $("#traitement-erreur");
  err.hidden = !P.erreur || enCours; err.textContent = P.erreur || "";
  $("#btn-relancer").hidden = enCours;
}
$("#btn-relancer").addEventListener("click", async () => {
  await api(`/api/projets/${P.id}/relancer`, { body: {} });
  P.etat = "traitement"; afficherTraitement(); planifierPoll(300);
});

function afficherJobs() {
  const libelles = { export: "Export", nouveau_clip: "Recherche", apercu: "Aperçu", analyse: "Analyse", titres: "Titres" };
  $("#jobs-barre").innerHTML = P.jobs.filter((j) => j.etat === "en_cours" && j.type !== "analyse")
    .map((j) => `<span class="job-pilule">${libelles[j.type] || j.type}<i style="--p:${j.pct}%"></i>${j.pct}%</span>`).join("");
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
  if (!P.source_existe) {
    $("#apercu-msg").hidden = false;
    $("#apercu-msg").textContent = "Vidéo d'origine introuvable : elle a été déplacée ou renommée.";
  }
  calculerSousTitres();
  remplirReglages(c);
  if (bornesChangees) { calculerFenetre(); dessinerTimeline(); }
  else majZone();
  $("#chat-contexte").textContent = `Clip ${P.clips.indexOf(c) + 1} · ${fmtCourt(c.fin - c.debut)}`;
}

video.addEventListener("error", async () => {
  if (!P || !video.dataset.src) return;
  $("#apercu-msg").hidden = false;
  $("#apercu-msg").textContent = "Ce format ne se lit pas directement : je prépare un aperçu, un instant…";
  try { await api(`/api/projets/${P.id}/apercu`, { body: {} }); planifierPoll(500); } catch (_) { /* affiché via les tâches */ }
});
video.addEventListener("loadedmetadata", () => { const c = clip(); if (c && video.currentTime < 0.1) video.currentTime = c.debut; });
video.addEventListener("play", () => { ecran.classList.remove("pause"); $("#btn-play").classList.add("lecture"); });
video.addEventListener("pause", () => { ecran.classList.add("pause"); $("#btn-play").classList.remove("lecture"); });
ecran.classList.add("pause");

function basculerLecture() {
  const c = clip(); if (!c) return;
  if (video.paused) {
    if (video.currentTime < c.debut - 0.05 || video.currentTime >= c.fin - 0.05) { video.currentTime = c.debut; libre = false; }
    else libre = video.currentTime < c.debut || video.currentTime > c.fin;
    video.play().catch(() => {});
  } else video.pause();
}
$("#btn-play").addEventListener("click", basculerLecture);
$("#btn-grand-play").addEventListener("click", basculerLecture);

/* Sous-titres : même algorithme que montage.py */
function calculerSousTitres() {
  const c = clip(); groupes = [];
  if (!c || !P.segments) return;
  const mots = [];
  for (const s of P.segments) {
    if (s.end <= c.debut || s.start >= c.fin) continue;
    if (s.words?.length) s.words.forEach((w) => { if (w.end > c.debut && w.start < c.fin) mots.push(w); });
    else mots.push({ start: s.start, end: s.end, text: s.text });
  }
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
  const cle = `${i}|${actif}|${JSON.stringify(s)}|${ecran.clientHeight}|${fantome}`;
  if (cle === dernierRendu) return;
  dernierRendu = cle;
  el.classList.toggle("fantome", fantome);
  el.style.top = `${s.position}%`;
  el.style.fontFamily = `"${s.police}", Arial, sans-serif`;
  el.style.fontSize = `${s.taille * 0.92 * echelle}px`;
  el.style.color = s.couleur;
  el.style.webkitTextStroke = `${s.epaisseur * 2 * echelle}px ${s.contour}`;
  el.style.textShadow = `0 ${2 * echelle}px 0 rgba(0,0,0,.5)`;
  if (i < 0) { el.innerHTML = fantome ? `<span>${s.majuscules ? "SOUS-TITRES" : "Sous-titres"}</span>` : ""; return; }
  el.innerHTML = groupes[i].mots.map((w, k) => {
    const txt = echap(s.majuscules ? w.text.toLocaleUpperCase("fr-FR") : w.text);
    return `<span${s.surligne && k === actif ? ` style="color:${s.surligne}"` : ""}>${txt}</span>`;
  }).join(" ");
}

const fond = $("#fond"), ctxFond = fond.getContext("2d");
let dernierFond = 0;
function boucle(now) {
  requestAnimationFrame(boucle);
  const c = clip();
  if (!c || $("#montage").hidden) return;
  let t = video.currentTime;
  if (!video.paused && !libre && !glisse && t >= c.fin - 0.02) {
    if ($("#boucle").checked) { video.currentTime = c.debut; t = c.debut; }
    else { video.pause(); video.currentTime = c.debut; t = c.debut; }
  }
  if (libre && !video.paused && t >= c.debut && t < c.fin) libre = false;
  rendreSousTitre(t);
  $("#t-actuel").textContent = fmt(t - c.debut);
  $("#tl-tete").style.left = `${pos(t)}%`;
  if (c.cadrage.mode === "flou" && now - dernierFond > 90 && video.readyState >= 2 && video.videoWidth) {
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
  const marge = Math.max(8, (c.fin - c.debut) * 0.35);
  fenetre = [Math.max(0, c.debut - marge), Math.min(P.duree || c.fin + marge, c.fin + marge)];
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
  const z = $("#tl-zone");
  z.style.left = `${pos(c.debut)}%`;
  z.style.width = `${pos(c.fin) - pos(c.debut)}%`;
  $$("#tl-mots i").forEach((i) => i.classList.toggle("dedans", +i.dataset.t >= c.debut && +i.dataset.t < c.fin));
  $("#t-debut").textContent = fmt(c.debut);
  $("#t-fin").textContent = fmt(c.fin);
  $("#t-duree").textContent = fmt(c.fin - c.debut);
}

$("#tl-piste").addEventListener("pointerdown", (e) => {
  const c = clip(); if (!c) return;
  const p = e.target.closest(".poignee");
  if (p) {
    glisse = p.dataset.p;
    e.target.setPointerCapture(e.pointerId);
    video.pause();
    return;
  }
  const t = tempsA(e.clientX);
  libre = t < c.debut || t > c.fin;
  video.currentTime = t;
});
$("#tl-piste").addEventListener("pointermove", (e) => {
  if (!glisse) return;
  const c = clip(); const t = tempsA(e.clientX);
  if (glisse === "debut") c.debut = +Math.min(t, c.fin - DUREE_MIN).toFixed(2);
  else c.fin = +Math.max(t, c.debut + DUREE_MIN).toFixed(2);
  video.currentTime = glisse === "debut" ? c.debut : c.fin;
  calculerSousTitres(); majZone();
});
$("#tl-piste").addEventListener("pointerup", () => {
  if (!glisse) return;
  const c = clip(); glisse = null;
  planifierPatch({ debut: c.debut, fin: c.fin }, 0);
  calculerFenetre(); dessinerTimeline();
  video.currentTime = c.debut;
});

$(".bornes").addEventListener("click", (e) => {
  const b = e.target.closest("button"); const c = clip(); if (!b || !c) return;
  if (b.dataset.ici) {
    const t = video.currentTime;
    if (b.dataset.ici === "debut") c.debut = +Math.min(t, c.fin - DUREE_MIN).toFixed(2);
    else c.fin = +Math.max(t, c.debut + DUREE_MIN).toFixed(2);
  } else {
    const d = +b.dataset.d;
    if (b.dataset.borne === "debut") c.debut = +Math.max(0, Math.min(c.debut + d, c.fin - DUREE_MIN)).toFixed(2);
    else c.fin = +Math.min(P.duree || 1e9, Math.max(c.fin + d, c.debut + DUREE_MIN)).toFixed(2);
    video.currentTime = b.dataset.borne === "debut" ? c.debut : Math.max(c.debut, c.fin - 2);
  }
  calculerSousTitres(); majZone();
  planifierPatch({ debut: c.debut, fin: c.fin });
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
  $$("#r-surligne .pastille").forEach((b) => b.classList.toggle("choisie", b.dataset.v.toUpperCase() === (s.surligne || "").toUpperCase()));
  $$("#r-mots button").forEach((b) => b.classList.toggle("choisi", +b.dataset.v === s.mots));
  $("#r-maj").setAttribute("aria-pressed", String(!!s.majuscules));
  $("#r-maj").textContent = s.majuscules ? "AA" : "Aa";
  const cad = c.cadrage;
  $$("#r-cadrage button").forEach((b) => b.classList.toggle("choisi", b.dataset.v === cad.mode));
  ecran.classList.toggle("remplir", cad.mode === "remplir");
  video.style.objectPosition = `${50 + cad.decalage}% 50%`;
  $("#ligne-decalage").hidden = cad.mode !== "remplir";
  if (actif !== $("#r-decalage")) $("#r-decalage").value = cad.decalage;
  $("#v-decalage").textContent = cad.decalage > 0 ? `+${cad.decalage}` : cad.decalage;
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
  if (tx.partie) lignes.push(`<span class="te-partie">PARTIE ${tx.numero || c.numero || 1}</span>`);
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
  else if (c.exporte) box.innerHTML = `✔ Exporté — <a data-ouvrir>ouvrir le fichier</a>`;
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
  remplirReglages(c);
  planifierPatch({ cadrage: partiel });
}

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
    patchEnAttente[k] = typeof v === "object" && v ? { ...(patchEnAttente[k] || {}), ...v } : v;
  }
  patchEnAttente.__clip = cur;
  clearTimeout(minuteurPatch);
  minuteurPatch = setTimeout(envoyerPatchMaintenant, delai);
}
async function envoyerPatchMaintenant() {
  clearTimeout(minuteurPatch);
  const corps = patchEnAttente; patchEnAttente = {};
  const cid = corps.__clip; delete corps.__clip;
  if (!cid || !Object.keys(corps).length) return;
  try {
    const maj = await api(`/api/projets/${P.id}/clips/${cid}`, { method: "PATCH", body: corps });
    if (Object.keys(patchEnAttente).length) return;   // d'autres modifs arrivent : on garde l'état local
    const i = P.clips.findIndex((c) => c.id === cid);
    if (i >= 0) P.clips[i] = maj;
    if (cid === cur) { remplirReglages(maj); majZone(); }
    afficherClips(false);
  } catch (err) { toast(err.message); }
}

$("#btn-annuler").addEventListener("click", annulerModif);
async function annulerModif() {
  const c = clip(); if (!c) return;
  await envoyerPatchMaintenant();
  const maj = await api(`/api/projets/${P.id}/clips/${c.id}/annuler`, { body: {} });
  P.clips[P.clips.findIndex((x) => x.id === c.id)] = maj;
  calculerSousTitres(); afficherClips(false); afficherMontage(true);
  video.currentTime = maj.debut;
  toast("Modif annulée");
}
$("#btn-suppr").addEventListener("click", async () => {
  const c = clip(); if (!c || !confirm(`Supprimer le clip « ${c.titre} » ?`)) return;
  await api(`/api/projets/${P.id}/clips/${c.id}`, { method: "DELETE" });
  const i = P.clips.indexOf(c);
  P.clips.splice(i, 1);
  cur = (P.clips[i] || P.clips[i - 1])?.id || null;
  toutAfficher(true);
});
$("#btn-exporter").addEventListener("click", async () => {
  await envoyerPatchMaintenant();
  const job = await api(`/api/projets/${P.id}/clips/${cur}/exporter`, { body: {} });
  P.jobs.push(job); majEtatExport(clip()); afficherJobs(); planifierPoll(400);
});
$("#btn-tout").addEventListener("click", async () => {
  if (!P.clips.length) return;
  await envoyerPatchMaintenant();
  const nouveaux = await api(`/api/projets/${P.id}/exporter-tout`, { body: {} });
  P.jobs.push(...nouveaux); afficherJobs(); planifierPoll(400);
  toast(`${nouveaux.length} clip(s) en cours d'export`);
});
$("#btn-dossier").addEventListener("click", () => api(`/api/projets/${P.id}/ouvrir`, { body: {} }));
$("#btn-retour").addEventListener("click", async () => {
  await envoyerPatchMaintenant();
  clearTimeout(minuteurPoll);
  video.pause();
  P = null; cur = null;
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

async function envoyerMessage(texte) {
  texte = texte.trim();
  if (!texte || envoiChat || !P) return;
  await envoyerPatchMaintenant();
  const avant = clip() ? { debut: clip().debut, fin: clip().fin } : null;
  P.chat.push({ role: "moi", texte, cid: cur });
  envoiChat = true; $(".envoyer").disabled = true;
  afficherChat();
  try {
    const e = await api(`/api/projets/${P.id}/chat`, { body: { clip: cur, message: texte } });
    envoiChat = false;
    fusionner(e);
    afficherChat();
    if (e.propositions) afficherIdees(e.propositions);
    const c = clip();
    if (c && avant && (c.debut !== avant.debut || c.fin !== avant.fin)) {
      libre = false; video.currentTime = c.debut; video.play().catch(() => {});
    }
  } catch (err) {
    envoiChat = false; P.chat.push({ role: "ia", texte: `Oups : ${err.message}`, cid: cur }); afficherChat();
  }
  $(".envoyer").disabled = false;
  planifierPoll(500);
}

$("#form-chat").addEventListener("submit", (e) => {
  e.preventDefault();
  const ta = $("#message"); const t = ta.value; ta.value = ""; ta.style.height = "";
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
  const tag = document.activeElement?.tagName;
  if (tag === "TEXTAREA" || (tag === "INPUT" && document.activeElement.type !== "range" && document.activeElement.type !== "checkbox")) return;
  const c = clip(); if (!c) return;
  if (e.code === "Space") { e.preventDefault(); basculerLecture(); }
  else if (e.key === "i" || e.key === "I") $('[data-ici="debut"]').click();
  else if (e.key === "o" || e.key === "O") $('[data-ici="fin"]').click();
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); annulerModif(); }
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
    deplace = { quoi, y0: e.clientY, pos0: positionDe(quoi, c) };
    el.classList.add("deplace");
  });
  el.addEventListener("pointermove", (e) => {
    if (!deplace || deplace.quoi !== quoi) return;
    const c = clip();
    poserPosition(quoi, c, deplace.pos0 + ((e.clientY - deplace.y0) / ecran.clientHeight) * 100);
  });
  const lacher = () => {
    if (!deplace || deplace.quoi !== quoi) return;
    const c = clip(); deplace = null; el.classList.remove("deplace");
    planifierPatch(quoi === "sous" ? { style: { position: c.style.position } } : { texte: { position: c.texte.position } }, 0);
  };
  el.addEventListener("pointerup", lacher);
  el.addEventListener("pointercancel", lacher);
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
  } else if (e && e.message) {
    pastille.hidden = false; pastille.textContent = e.message;
  } else if (e) {
    pastille.hidden = true;
  }
  if (!e || e.etat === "verification" || e.etat === "telechargement") setTimeout(suivreIA, 1500);
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
addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));
addEventListener("resize", () => { dernierRendu = ""; rendreTitre(clip()); });

chargerProjets();
