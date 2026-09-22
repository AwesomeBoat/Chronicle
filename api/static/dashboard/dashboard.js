/* Chronicle - dashboard de l'activite PC.
 *
 * Aucune dependance, aucun CDN : la page marche hors ligne, sur cette
 * machine. Les donnees viennent de /api/v1/pc/* (api/dashboard.py).
 *
 * Regles de la page (guide dataviz) :
 *  - une couleur par categorie, fixe : la couleur suit l'entite, jamais
 *    son rang ni le filtre ;
 *  - chaque graphique a son tableau (bouton "Tableau") ;
 *  - infobulles au survol ET au clavier, jamais seul acces a une valeur ;
 *  - tout texte venu des donnees passe par textContent (titres de
 *    fenetres, messages de commit : donnees non fiables).
 */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const JOURS_SEMAINE = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."];
const nf = new Intl.NumberFormat("fr-FR");

const etat = {
  machine: "all",
  debut: null,
  fin: null,
  jours: 7,
  jour: null,
  cleRequise: false,
  meta: null,
  resume: null,
  journee: null,
};

// ================================================================ outils

const $ = (selecteur, racine = document) => racine.querySelector(selecteur);

function el(tag, attributs = {}, ...enfants) {
  const noeud = document.createElement(tag);
  for (const [cle, valeur] of Object.entries(attributs)) {
    if (valeur === null || valeur === undefined || valeur === false) continue;
    if (cle === "class") noeud.className = valeur;
    else if (cle === "style") Object.assign(noeud.style, valeur);
    else noeud.setAttribute(cle, valeur === true ? "" : valeur);
  }
  for (const enfant of enfants.flat()) {
    if (enfant === null || enfant === undefined || enfant === false) continue;
    noeud.append(enfant instanceof Node ? enfant : document.createTextNode(String(enfant)));
  }
  return noeud;
}

function sv(tag, attributs = {}, style = {}) {
  const noeud = document.createElementNS(SVGNS, tag);
  for (const [cle, valeur] of Object.entries(attributs)) noeud.setAttribute(cle, valeur);
  Object.assign(noeud.style, style);
  return noeud;
}

function texteSvg(x, y, contenu, attributs = {}) {
  const t = sv("text", { x, y, ...attributs });
  t.textContent = contenu;
  return t;
}

function stockage(cle, valeur) {
  // Le stockage du navigateur peut etre bloque (navigation privee) : la
  // page doit marcher sans.
  try {
    if (valeur === undefined) return localStorage.getItem(cle);
    if (valeur === null) localStorage.removeItem(cle);
    else localStorage.setItem(cle, valeur);
  } catch (_) { /* sans stockage */ }
  return null;
}

// ================================================================ formats

function duree(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  const m = Math.round(minutes);
  if (m < 1) return minutes > 0 ? "< 1 min" : "0 min";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const reste = m % 60;
  return reste ? `${h} h ${String(reste).padStart(2, "0")}` : `${h} h`;
}

// Un volume en Mo, affiche en Go au-dela de 1 000 Mo.
function volume(mo) {
  if (mo >= 1000) return `${nf.format(+(mo / 1000).toFixed(1))} Go`;
  return `${nf.format(Math.round(mo))} Mo`;
}

function dureeAxe(minutes) {
  if (minutes === 0) return "0";
  if (minutes < 60) return `${Math.round(minutes)} min`;
  return `${+(minutes / 60).toFixed(1)} h`.replace(".", ",");
}

function dateDe(iso) {
  const [a, m, j] = iso.split("-").map(Number);
  return new Date(a, m - 1, j);
}

function isoDe(date) {
  const deux = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${deux(date.getMonth() + 1)}-${deux(date.getDate())}`;
}

function decaler(iso, jours) {
  const d = dateDe(iso);
  d.setDate(d.getDate() + jours);
  return isoDe(d);
}

function jourCourt(iso) {
  return dateDe(iso).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
}

function jourLong(iso) {
  return dateDe(iso).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}

function heureMin(minutes) {
  const m = Math.max(0, Math.round(minutes));
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function ilYa(iso) {
  if (!iso) return null;
  const secondes = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secondes < 90) return "il y a moins de 2 min";
  if (secondes < 3600) return `il y a ${Math.round(secondes / 60)} min`;
  if (secondes < 172800) return `il y a ${Math.round(secondes / 3600)} h`;
  return `il y a ${Math.round(secondes / 86400)} j`;
}

// ================================================================ API

async function api(chemin, parametres = {}) {
  const url = new URL(chemin, location.origin);
  for (const [cle, valeur] of Object.entries(parametres)) {
    if (valeur !== null && valeur !== undefined) url.searchParams.set(cle, valeur);
  }
  const entetes = {};
  const cle = stockage("chronicle-api-key");
  if (cle) entetes["X-API-Key"] = cle;
  const reponse = await fetch(url, { headers: entetes, cache: "no-store" });
  if (reponse.status === 401) {
    if (!etat.cleRequise) demanderCle();
    etat.cleRequise = true;
    throw new Error("cle requise");
  }
  if (!reponse.ok) throw new Error(`HTTP ${reponse.status}`);
  return reponse.json();
}

function demanderCle() {
  const champ = el("input", { type: "password", placeholder: "Clé d'API (data/api_key.txt)", "aria-label": "Clé d'API" });
  const bouton = el("button", { type: "button" }, "Ouvrir le dashboard");
  bouton.addEventListener("click", () => {
    stockage("chronicle-api-key", champ.value.trim());
    location.reload();
  });
  $("#contenu").replaceChildren(el("section", { class: "carte cle-api" },
    el("h2", {}, "Clé d'API requise"),
    el("p", { class: "aide" }, "Ce dashboard est ouvert depuis une autre machine. Entrez la clé de Chronicle (fichier data/api_key.txt) : elle reste dans ce navigateur."),
    champ, bouton));
}

// ============================================================ categories

function categories() {
  return etat.meta ? etat.meta.categories : [];
}

function couleurCategorie(id) {
  const c = categories().find((x) => x.id === id);
  return c && c.slot ? `var(--series-${c.slot})` : "var(--other)";
}

function libelleCategorie(id) {
  const c = categories().find((x) => x.id === id);
  return c ? c.label.replace("Medias", "Médias").replace("Systeme", "Système") : id;
}

// =============================================================== infobulle

const infobulle = $("#infobulle");

function montrerInfobulle(evenement, contenu) {
  const { titre, lignes = [], note } = contenu;
  const enfants = [];
  if (titre) enfants.push(el("div", { class: "titre-ib" }, titre));
  for (const ligne of lignes) {
    const cle = el("span", { class: "cle-ib" });
    if (ligne.couleur) cle.append(el("i", { style: { background: ligne.couleur } }));
    cle.append(ligne.libelle);
    enfants.push(el("div", { class: "ligne-ib" }, cle, el("strong", {}, ligne.valeur)));
  }
  if (note) enfants.push(el("div", { class: "note-ib" }, note));
  infobulle.replaceChildren(...enfants);
  infobulle.hidden = false;

  let x;
  let y;
  if (evenement.type === "focus") {
    const r = evenement.target.getBoundingClientRect();
    x = r.left + r.width / 2;
    y = r.top;
  } else {
    x = evenement.clientX;
    y = evenement.clientY;
  }
  const largeur = infobulle.offsetWidth;
  const hauteur = infobulle.offsetHeight;
  let gauche = x + 14;
  if (gauche + largeur > window.innerWidth - 8) gauche = x - largeur - 14;
  let haut = y - hauteur - 12;
  if (haut < 8) haut = y + 16;
  infobulle.style.left = `${Math.max(8, gauche)}px`;
  infobulle.style.top = `${haut}px`;
}

function cacherInfobulle() {
  infobulle.hidden = true;
}

function lierInfobulle(cible, contenu) {
  const montrer = (e) => montrerInfobulle(e, contenu());
  cible.addEventListener("pointermove", montrer);
  cible.addEventListener("pointerleave", cacherInfobulle);
  cible.addEventListener("focus", montrer);
  cible.addEventListener("blur", cacherInfobulle);
}

// ============================================================= vue tableau

const tables = new Map();

function definirTable(carte, colonnes, lignes) {
  tables.set(carte, { colonnes, lignes });
  if (carte.dataset.vue === "table") afficherTable(carte);
}

function afficherTable(carte) {
  const donnees = tables.get(carte);
  const graphe = carte.querySelector(".graphe, #g-sante");
  let conteneur = carte.querySelector(".defile");
  if (!conteneur) {
    conteneur = el("div", { class: "defile" });
    carte.append(conteneur);
  }
  const table = el("table", { class: "table-vue" });
  table.append(el("thead", {}, el("tr", {}, donnees.colonnes.map((c) =>
    el("th", { class: c.nombre ? "n" : null, scope: "col" }, c.titre)))));
  const corps = el("tbody");
  for (const ligne of donnees.lignes) {
    corps.append(el("tr", {}, donnees.colonnes.map((c) =>
      el("td", { class: c.nombre ? "n" : null }, ligne[c.cle] ?? "—"))));
  }
  if (!donnees.lignes.length) corps.append(el("tr", {}, el("td", { colspan: donnees.colonnes.length }, "Aucune donnée.")));
  table.append(corps);
  conteneur.replaceChildren(table);
  graphe.hidden = true;
  for (const legende of carte.querySelectorAll(".legende")) legende.hidden = true;
  conteneur.hidden = false;
}

function basculerTable(carte) {
  const bouton = carte.querySelector(".bascule-table");
  if (carte.dataset.vue === "table") {
    carte.dataset.vue = "graphe";
    carte.querySelector(".graphe").hidden = false;
    for (const legende of carte.querySelectorAll(".legende")) legende.hidden = false;
    const conteneur = carte.querySelector(".defile");
    if (conteneur) conteneur.hidden = true;
    bouton.textContent = "Tableau";
    if (etat.resume) rendreTout();
  } else {
    carte.dataset.vue = "table";
    if (tables.has(carte)) afficherTable(carte);
    bouton.textContent = "Graphique";
  }
}

// ================================================================ echelles

function echelle(d0, d1, r0, r1) {
  const ecart = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) * (r1 - r0)) / ecart;
}

// Pas "ronds" pour des durees en minutes : 5 min ... 24 h.
const PAS_MINUTES = [1, 2, 5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 2880];

function graduations(maximum, cible = 4) {
  const pas = PAS_MINUTES.find((p) => maximum / p <= cible) || PAS_MINUTES[PAS_MINUTES.length - 1];
  const haut = Math.max(pas, Math.ceil(maximum / pas) * pas);
  const ticks = [];
  for (let v = 0; v <= haut + 1e-9; v += pas) ticks.push(v);
  return { haut, ticks };
}

function graduationsNombre(maximum, cible = 4) {
  if (maximum <= 0) return { haut: 1, ticks: [0, 1] };
  const brut = maximum / cible;
  const puissance = 10 ** Math.floor(Math.log10(brut));
  const pas = [1, 2, 2.5, 5, 10].map((f) => f * puissance).find((p) => maximum / p <= cible);
  const haut = Math.ceil(maximum / pas) * pas;
  const ticks = [];
  for (let v = 0; v <= haut + 1e-9; v += pas) ticks.push(+v.toFixed(6));
  return { haut, ticks };
}

function largeurDe(conteneur) {
  return Math.max(280, Math.floor(conteneur.getBoundingClientRect().width));
}

function vide(conteneur, message = "Aucune donnée sur cette période.") {
  conteneur.replaceChildren(el("div", { class: "vide" }, message));
}

// Colonne : bord de donnee arrondi (4 px), base carree.
function cheminColonne(x, y, l, h, rayon = 4) {
  const r = Math.min(rayon, l / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + l - r},${y} Q${x + l},${y} ${x + l},${y + r} L${x + l},${y + h} Z`;
}

// ======================================================= graphe : colonnes

function colonnesEmpilees(conteneur, options) {
  const { buckets, series, marqueur, etiquette, selection, surClic, titreInfobulle } = options;
  const largeur = largeurDe(conteneur);
  const hauteurTrace = 230;
  const marge = { haut: 12, droite: 12, bas: 30, gauche: 52 };
  const hauteur = hauteurTrace + marge.haut + marge.bas;
  const totaux = buckets.map((b) => series.reduce((s, x) => s + (b.valeurs[x.id] || 0), 0));
  const maximum = Math.max(1, ...totaux, ...buckets.map((b) => b.marqueur || 0));
  const { haut, ticks } = graduations(maximum);
  const y = echelle(0, haut, marge.haut + hauteurTrace, marge.haut);
  const bande = (largeur - marge.gauche - marge.droite) / buckets.length;
  const epaisseur = Math.max(3, Math.min(24, bande * 0.62));

  const svg = sv("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, height: hauteur, role: "img", "aria-label": options.resume });
  const grille = sv("g", { class: "grille" });
  for (const t of ticks) {
    grille.append(sv("line", { x1: marge.gauche, x2: largeur - marge.droite, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 }));
    grille.append(texteSvg(marge.gauche - 8, y(t) + 4, dureeAxe(t), { "text-anchor": "end" }));
  }
  svg.append(grille);

  const pasEtiquette = Math.max(1, Math.ceil(buckets.length / Math.max(4, Math.floor((largeur - 80) / 62))));

  buckets.forEach((b, i) => {
    const x0 = marge.gauche + i * bande;
    const cx = x0 + bande / 2;
    const groupe = sv("g", { class: "cible", tabindex: 0, role: "button" });
    groupe.append(sv("rect", { class: b.cle === selection ? "survol choisi" : "survol", x: x0 + 1, y: marge.haut, width: Math.max(1, bande - 2), height: hauteurTrace, rx: 4 }));

    let base = marge.haut + hauteurTrace;
    const presentes = series.filter((s) => (b.valeurs[s.id] || 0) > 0);
    presentes.forEach((s, rang) => {
      const valeur = b.valeurs[s.id];
      const h = y(0) - y(valeur);
      const dernier = rang === presentes.length - 1;
      // Espace de 2 px couleur surface entre deux segments empiles.
      const hVisible = dernier ? h : Math.max(0.5, h - 2);
      const hautSegment = base - h;
      if (dernier) {
        groupe.append(sv("path", { class: "marque", d: cheminColonne(cx - epaisseur / 2, hautSegment, epaisseur, Math.max(1, hVisible)) }, { fill: s.couleur }));
      } else {
        groupe.append(sv("rect", { class: "marque", x: cx - epaisseur / 2, y: hautSegment + (h - hVisible), width: epaisseur, height: hVisible }, { fill: s.couleur }));
      }
      base -= h;
    });

    if (marqueur && b.marqueur > 0) {
      groupe.append(sv("circle", { cx, cy: y(b.marqueur), r: bande < 14 ? 3.5 : 4.5, "stroke-width": bande < 14 ? 1.5 : 2 },
        { fill: "var(--ink-1)", stroke: "var(--surface-1)" }));
    }

    if (i % pasEtiquette === 0) {
      groupe.append(texteSvg(cx, hauteur - 10, etiquette(b), { "text-anchor": "middle", class: b.cle === selection ? "fort" : "" }));
    }

    lierInfobulle(groupe, () => ({
      titre: titreInfobulle(b),
      lignes: [
        ...series.filter((s) => (b.valeurs[s.id] || 0) > 0).reverse().map((s) => ({ libelle: s.libelle, valeur: duree(b.valeurs[s.id]), couleur: s.couleur })),
        { libelle: "Total au premier plan", valeur: duree(totaux[i]) },
        ...(marqueur ? [{ libelle: marqueur.libelle, valeur: duree(b.marqueur), couleur: "var(--ink-1)" }] : []),
      ],
    }));

    if (surClic) {
      groupe.addEventListener("click", () => surClic(b));
      groupe.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); surClic(b); } });
    }
    svg.append(groupe);
  });

  svg.append(sv("line", { class: "base", x1: marge.gauche, x2: largeur - marge.droite, y1: y(0) + 0.5, y2: y(0) + 0.5 }));
  conteneur.replaceChildren(svg);
}

// ================================================== graphe : chronologie

function fusionner(segments, ecart = 0.25) {
  const fusion = [];
  for (const s of segments) {
    const precedent = fusion[fusion.length - 1];
    if (precedent && s.s - precedent.e <= ecart) {
      precedent.e = Math.max(precedent.e, s.e);
      precedent.morceaux.push(s);
    } else {
      fusion.push({ s: s.s, e: s.e, morceaux: [s] });
    }
  }
  return fusion;
}

function chronologie(conteneur, jour, lanes, minutesJour) {
  const largeur = largeurDe(conteneur);
  const marge = { gauche: 138, droite: 14, haut: 6, bas: 26 };
  const ligne = 26;
  const epaisseur = 14;
  const trace = largeur - marge.gauche - marge.droite;
  const x = echelle(0, minutesJour, marge.gauche, marge.gauche + trace);
  const hauteur = marge.haut + lanes.length * ligne + marge.bas;
  const svg = sv("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, height: hauteur, role: "img", "aria-label": `Chronologie du ${jourLong(jour)}` });

  const grille = sv("g", { class: "grille" });
  const pasHeures = trace >= 300 ? 3 : 6;
  for (let h = 0; h <= minutesJour / 60 + 1e-9; h += pasHeures) {
    const px = Math.round(x(h * 60)) + 0.5;
    grille.append(sv("line", { x1: px, x2: px, y1: marge.haut, y2: hauteur - marge.bas }));
    grille.append(texteSvg(px, hauteur - 8, `${String(h).padStart(2, "0")} h`, { "text-anchor": "middle" }));
  }
  svg.append(grille);

  lanes.forEach((lane, rang) => {
    const y0 = marge.haut + rang * ligne;
    svg.append(texteSvg(marge.gauche - 12, y0 + ligne / 2 + 4, lane.libelle, { "text-anchor": "end", class: "moyen" }));
    const hauteurBarre = lane.fine ? 6 : epaisseur;
    for (const bloc of fusionner(lane.segments)) {
      const x0 = x(bloc.s);
      const l = Math.max(1.5, x(bloc.e) - x0);
      const groupe = sv("g", { class: "cible", tabindex: 0 });
      groupe.append(sv("rect", { x: x0, y: y0 + (ligne - Math.max(hauteurBarre, 18)) / 2, width: l, height: Math.max(hauteurBarre, 18) }, { fill: "transparent" }));
      groupe.append(sv("rect", { class: "marque", x: x0, y: y0 + (ligne - hauteurBarre) / 2, width: l, height: hauteurBarre, rx: Math.min(3, l / 2) }, { fill: lane.couleur }));
      lierInfobulle(groupe, () => lane.infobulle(bloc));
      svg.append(groupe);
    }
  });

  // "Maintenant", si la journee est aujourd'hui.
  if (jour === isoDe(new Date())) {
    const maintenant = new Date();
    const px = x(maintenant.getHours() * 60 + maintenant.getMinutes());
    svg.append(sv("line", { x1: px, x2: px, y1: marge.haut, y2: hauteur - marge.bas, "stroke-width": 1 }, { stroke: "var(--ink-3)" }));
  }
  conteneur.replaceChildren(svg);
}

// ================================================= graphe : barres (HTML)

function barresHorizontales(conteneur, elements, options = {}) {
  if (!elements.length) return vide(conteneur, options.vide);
  const maximum = Math.max(...elements.map((e) => e.valeur), 1e-9);
  const largeurValeur = Math.max(...elements.map((e) => Math.max(e.texte.length, (e.sousTexte || "").length))) + 1;
  const liste = el("div", { class: "barres" });
  for (const e of elements) {
    const piste = el("div", { class: "piste" });
    const part = (v) => `${Math.max(0.4, (Math.min(v, maximum) / maximum) * 100)}%`;
    if (e.partiel !== undefined) {
      // Pale : le total. Pleine : la part "reellement active", meme echelle.
      piste.append(el("div", { class: "barre pale", style: { width: part(e.valeur), background: e.couleur } }));
      if (e.partiel > 0) piste.append(el("div", { class: "barre", style: { width: part(Math.min(e.partiel, e.valeur)), background: e.couleur } }));
    } else {
      piste.append(el("div", { class: "barre", style: { width: part(e.valeur), background: e.couleur } }));
    }
    const rangee = el("div", { class: "rangee cible", tabindex: 0, style: { gridTemplateColumns: `minmax(64px, 30%) 1fr ${largeurValeur}ch` } },
      el("div", { class: "libelle-barre", title: e.libelle }, e.libelle),
      piste,
      el("div", { class: "valeur-barre" }, el("span", {}, e.texte), e.sousTexte ? el("small", {}, e.sousTexte) : null));
    lierInfobulle(rangee, () => e.infobulle);
    liste.append(rangee);
  }
  conteneur.replaceChildren(liste);
}

// ================================================== graphe : carte thermique

function carteThermique(conteneur, cellules) {
  const largeur = largeurDe(conteneur);
  const marge = { gauche: 40, droite: 8, haut: 4, bas: 22 };
  const cote = (largeur - marge.gauche - marge.droite) / 24;
  const hCellule = Math.min(22, Math.max(14, cote));
  const hauteur = marge.haut + 7 * hCellule + marge.bas;
  const valeurs = new Map(cellules.map((c) => [`${c.dow}-${c.hour}`, c.minutes]));
  const maximum = Math.max(1, ...cellules.map((c) => c.minutes));
  const svg = sv("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, height: hauteur, role: "img", "aria-label": "Minutes actives par jour de la semaine et par heure" });

  for (let j = 1; j <= 7; j++) {
    const y0 = marge.haut + (j - 1) * hCellule;
    svg.append(texteSvg(marge.gauche - 8, y0 + hCellule / 2 + 4, JOURS_SEMAINE[j - 1], { "text-anchor": "end" }));
    for (let h = 0; h < 24; h++) {
      const minutes = valeurs.get(`${j}-${h}`) || 0;
      const pas = minutes <= 0 ? 0 : Math.min(6, 1 + Math.floor((minutes / maximum) * 5.999));
      const groupe = sv("g", { class: "cible", tabindex: minutes > 0 ? 0 : -1 });
      groupe.append(sv("rect", { class: "marque", x: marge.gauche + h * cote + 1, y: y0 + 1, width: Math.max(1, cote - 2), height: hCellule - 2, rx: 2 },
        { fill: `var(--seq-${pas})` }));
      lierInfobulle(groupe, () => ({ titre: `${JOURS_SEMAINE[j - 1]} ${String(h).padStart(2, "0")} h – ${String(h + 1).padStart(2, "0")} h`, lignes: [{ libelle: "Actif", valeur: duree(minutes) }] }));
      svg.append(groupe);
    }
  }
  for (let h = 0; h < 24; h += cote * 3 >= 34 ? 3 : 6) {
    svg.append(texteSvg(marge.gauche + h * cote + cote / 2, hauteur - 6, `${String(h).padStart(2, "0")} h`, { "text-anchor": "middle" }));
  }
  const echelleLegende = el("div", { class: "legende", style: { alignItems: "center" } },
    el("span", {}, "moins"),
    ...[0, 1, 2, 3, 4, 5, 6].map((p) => el("i", { style: { background: `var(--seq-${p})`, borderRadius: "2px" } })),
    el("span", {}, `plus (max ${duree(maximum)} par créneau)`));
  conteneur.replaceChildren(svg, echelleLegende);
}

// ========================================================= graphe : ligne

function courbe(conteneur, points, options) {
  const largeur = largeurDe(conteneur);
  const hauteurTrace = 110;
  const marge = { haut: 22, droite: 12, bas: 24, gauche: 40 };
  const hauteur = marge.haut + hauteurTrace + marge.bas;
  const n = points.length;
  const x = (i) => marge.gauche + (n <= 1 ? 0.5 : i / (n - 1)) * (largeur - marge.gauche - marge.droite);
  const y = echelle(0, 100, marge.haut + hauteurTrace, marge.haut);
  const svg = sv("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, height: hauteur, role: "img", "aria-label": options.titre });
  svg.append(texteSvg(0, 12, options.titre, { class: "moyen" }));
  const grille = sv("g", { class: "grille" });
  for (const t of [0, 50, 100]) {
    grille.append(sv("line", { x1: marge.gauche, x2: largeur - marge.droite, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 }));
    grille.append(texteSvg(marge.gauche - 8, y(t) + 4, `${t} %`, { "text-anchor": "end" }));
  }
  svg.append(grille);

  // Une valeur manquante coupe la ligne : on ne relie pas deux mesures a
  // travers un trou.
  let chemin = "";
  let aire = "";
  let segment = [];
  const fermer = () => {
    if (segment.length) {
      aire += `M${segment[0][0]},${y(0)} ` + segment.map((p) => `L${p[0]},${p[1]}`).join(" ") + ` L${segment[segment.length - 1][0]},${y(0)} Z `;
      segment = [];
    }
  };
  points.forEach((p, i) => {
    if (p.valeur === null || p.valeur === undefined) { fermer(); return; }
    const point = [x(i), y(p.valeur)];
    chemin += `${segment.length ? "L" : "M"}${point[0]},${point[1]} `;
    segment.push(point);
  });
  fermer();
  svg.append(sv("path", { d: aire }, { fill: options.couleur, opacity: 0.1 }));
  svg.append(sv("path", { d: chemin, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, { fill: "none", stroke: options.couleur }));
  points.forEach((p, i) => {
    if (p.valeur !== null && p.valeur !== undefined && (n === 1 || i === n - 1 || points[i - 1]?.valeur == null)) {
      svg.append(sv("circle", { cx: x(i), cy: y(p.valeur), r: 4, "stroke-width": 2 }, { fill: options.couleur, stroke: "var(--surface-1)" }));
    }
  });

  // Reticule : suit le pointeur et s'aimante au point le plus proche.
  const reticule = sv("line", { y1: marge.haut, y2: marge.haut + hauteurTrace, "stroke-width": 1 }, { stroke: "var(--ink-3)", display: "none" });
  svg.append(reticule);
  const zone = sv("rect", { x: marge.gauche, y: marge.haut, width: largeur - marge.gauche - marge.droite, height: hauteurTrace }, { fill: "transparent" });
  zone.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * largeur;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - marge.gauche) / (largeur - marge.gauche - marge.droite)) * (n - 1))));
    reticule.setAttribute("x1", x(i));
    reticule.setAttribute("x2", x(i));
    reticule.style.display = "";
    montrerInfobulle(e, { titre: points[i].titre, lignes: [{ libelle: options.titre, valeur: points[i].valeur == null ? "—" : `${nf.format(points[i].valeur)} %`, couleur: options.couleur }] });
  });
  zone.addEventListener("pointerleave", () => { reticule.style.display = "none"; cacherInfobulle(); });
  svg.append(zone);
  svg.append(texteSvg(marge.gauche, hauteur - 6, points[0]?.etiquette || "", { "text-anchor": "start" }));
  svg.append(texteSvg(largeur - marge.droite, hauteur - 6, points[n - 1]?.etiquette || "", { "text-anchor": "end" }));
  return svg;
}

// ================================================ graphe : colonnes groupees

function colonnesGroupees(conteneur, buckets, series, etiquette, titreInfobulle) {
  const largeur = largeurDe(conteneur);
  const hauteurTrace = 150;
  const marge = { haut: 10, droite: 12, bas: 28, gauche: 52 };
  const hauteur = marge.haut + hauteurTrace + marge.bas;
  const maximum = Math.max(0, ...buckets.flatMap((b) => series.map((s) => b[s.cle] || 0)));
  if (maximum <= 0) return vide(conteneur);
  const { haut, ticks } = graduationsNombre(maximum);
  const y = echelle(0, haut, marge.haut + hauteurTrace, marge.haut);
  const bande = (largeur - marge.gauche - marge.droite) / buckets.length;
  const epaisseur = Math.max(2, Math.min(12, (bande * 0.7 - 2) / series.length));
  const svg = sv("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, height: hauteur, role: "img", "aria-label": "Trafic réseau par période" });
  const grille = sv("g", { class: "grille" });
  for (const t of ticks) {
    grille.append(sv("line", { x1: marge.gauche, x2: largeur - marge.droite, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 }));
    grille.append(texteSvg(marge.gauche - 8, y(t) + 4, volume(t), { "text-anchor": "end" }));
  }
  svg.append(grille);
  const pasEtiquette = Math.max(1, Math.ceil(buckets.length / Math.max(4, Math.floor((largeur - 80) / 62))));
  buckets.forEach((b, i) => {
    const x0 = marge.gauche + i * bande;
    const groupe = sv("g", { class: "cible", tabindex: 0 });
    groupe.append(sv("rect", { class: "survol", x: x0 + 1, y: marge.haut, width: Math.max(1, bande - 2), height: hauteurTrace, rx: 4 }));
    const debut = x0 + bande / 2 - (series.length * epaisseur + (series.length - 1) * 2) / 2;
    series.forEach((s, k) => {
      const v = b[s.cle] || 0;
      if (v <= 0) return;
      const h = Math.max(1, y(0) - y(v));
      groupe.append(sv("path", { class: "marque", d: cheminColonne(debut + k * (epaisseur + 2), y(0) - h, epaisseur, h, 3) }, { fill: s.couleur }));
    });
    if (i % pasEtiquette === 0) groupe.append(texteSvg(x0 + bande / 2, hauteur - 9, etiquette(b), { "text-anchor": "middle" }));
    lierInfobulle(groupe, () => ({ titre: titreInfobulle(b), lignes: series.map((s) => ({ libelle: s.libelle, valeur: b[s.cle] == null ? "—" : volume(b[s.cle]), couleur: s.couleur })) }));
    svg.append(groupe);
  });
  svg.append(sv("line", { class: "base", x1: marge.gauche, x2: largeur - marge.droite, y1: y(0) + 0.5, y2: y(0) + 0.5 }));
  conteneur.replaceChildren(svg);
}

// =============================================================== legendes

function legende(conteneur, entrees) {
  conteneur.replaceChildren(...entrees.map((e) =>
    el("span", {}, el("i", { class: e.forme || null, style: { background: e.couleur } }), e.libelle)));
}

// ================================================================= rendus

function etiquetteBucket(b) {
  if (etat.resume.range.bucket === "hour") return `${b.cle} h`;
  if (etat.resume.range.days > 14) return dateDe(b.cle).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  return jourCourt(b.cle);
}

function titreBucket(b) {
  if (etat.resume.range.bucket === "hour") {
    return `${jourLong(etat.debut)}, ${b.cle} h – ${String(Number(b.cle) + 1).padStart(2, "0")} h`;
  }
  return jourLong(b.cle);
}

function delta(courant, precedent, format) {
  if (!etat.resume.previous) return null;
  const jours = etat.resume.range.days;
  const periode = jours === 1 ? "la veille" : `les ${jours} jours précédents`;
  if (!precedent && !courant) return null;
  if (!precedent) return `aucune donnée ${periode}`;
  const ecart = courant - precedent;
  const fleche = ecart > 0 ? "▲" : ecart < 0 ? "▼" : "=";
  return `${fleche} ${format(Math.abs(ecart))} vs ${periode}`;
}

function rendreKpis() {
  const k = etat.resume.kpis;
  const p = etat.resume.previous || {};
  const ratio = k.premier_plan_min > 0 ? Math.min(1, k.actif_min / k.premier_plan_min) : 0;
  const parHeure = k.actif_min > 0 ? k.changements / (k.actif_min / 60) : 0;

  const hero = el("div", { class: "tuile hero" },
    el("div", { class: "libelle" }, "Temps réellement actif"),
    el("div", { class: "valeur" }, duree(k.actif_min)),
    el("div", { class: "detail" }, `sur ${duree(k.premier_plan_min)} au premier plan · ${Math.round(ratio * 100)} % d'usage réel`),
    el("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(ratio * 100), "aria-label": "Part du temps au premier plan réellement active" },
      el("span", { style: { width: `${ratio * 100}%` } })),
    el("div", { class: "delta" }, delta(k.actif_min, p.actif_min, duree)));

  const tuile = (libelle, valeur, detail, variation) => el("div", { class: "tuile" },
    el("div", { class: "libelle" }, libelle), el("div", { class: "valeur" }, valeur),
    detail ? el("div", { class: "detail" }, detail) : null,
    variation ? el("div", { class: "delta" }, variation) : null);

  $("#kpis").replaceChildren(
    hero,
    tuile("Au premier plan", duree(k.premier_plan_min), "une fenêtre devant soi", delta(k.premier_plan_min, p.premier_plan_min, duree)),
    tuile("Changements de contexte", nf.format(k.changements), k.actif_min > 0 ? `≈ ${nf.format(Math.round(parHeure))} par heure active` : "passages d'une app à une autre", delta(k.changements, p.changements, (v) => nf.format(v))),
    tuile("Inactif", duree(k.inactif_min), "pauses de 2 min et plus", delta(k.inactif_min, p.inactif_min, duree)),
    tuile("Commits", nf.format(k.commits), `+${nf.format(k.lignes_plus)} / −${nf.format(k.lignes_moins)} lignes`, delta(k.commits, p.commits, (v) => nf.format(v))),
    tuile("PC en veille", duree(k.veille_min), `session verrouillée : ${duree(k.verrouille_min)}`, null),
    tuile("Commandes de terminal", nf.format(k.commandes), `${nf.format(k.fichiers)} fichiers modifiés · ${nf.format(k.touches)} touches`, null));
}

function seriesCategories() {
  return categories().map((c) => ({ id: c.id, libelle: libelleCategorie(c.id), couleur: couleurCategorie(c.id) }));
}

function rendrePeriode() {
  const r = etat.resume;
  const carte = $("#carte-periode");
  const conteneur = $("#g-periode");
  $("#titre-periode").textContent = r.range.bucket === "hour"
    ? `Temps au premier plan, par heure · ${jourLong(etat.debut)}`
    : "Temps au premier plan, par jour";

  const series = seriesCategories();
  const buckets = r.buckets.map((b) => ({ cle: b.key, valeurs: b.categories, marqueur: b.actif_min }));
  const utilisees = series.filter((s) => buckets.some((b) => (b.valeurs[s.id] || 0) > 0));
  legende($("#legende-periode"), [
    ...utilisees.map((s) => ({ libelle: s.libelle, couleur: s.couleur })),
    { libelle: "Actif (clavier, souris)", couleur: "var(--ink-1)", forme: "point" },
  ]);

  const total = buckets.reduce((s, b) => s + Object.values(b.valeurs).reduce((a, v) => a + v, 0) + (b.marqueur || 0), 0);
  if (total <= 0) {
    vide(conteneur);
  } else {
    colonnesEmpilees(conteneur, {
      buckets, series,
      marqueur: { libelle: "Actif (clavier, souris)" },
      etiquette: etiquetteBucket, titreInfobulle: titreBucket,
      selection: r.range.bucket === "day" ? etat.jour : null,
      surClic: r.range.bucket === "day" ? (b) => choisirJour(b.cle) : null,
      resume: `Temps au premier plan par ${r.range.bucket === "hour" ? "heure" : "jour"} et par catégorie`,
    });
  }

  definirTable(carte,
    [{ cle: "quand", titre: r.range.bucket === "hour" ? "Heure" : "Jour" }, ...utilisees.map((s) => ({ cle: s.id, titre: s.libelle, nombre: true })), { cle: "total", titre: "Total", nombre: true }, { cle: "actif", titre: "Actif", nombre: true }],
    buckets.map((b) => ({
      quand: r.range.bucket === "hour" ? `${b.cle} h` : jourLong(b.cle),
      ...Object.fromEntries(utilisees.map((s) => [s.id, duree(b.valeurs[s.id] || 0)])),
      total: duree(Object.values(b.valeurs).reduce((a, v) => a + v, 0)),
      actif: duree(b.marqueur),
    })));
}

const ETATS = [
  { id: "media", libelle: "Lecture média", couleur: "var(--series-7)" },
  { id: "idle", libelle: "Inactif", couleur: "var(--state-idle)" },
  { id: "locked", libelle: "Verrouillé", couleur: "var(--state-locked)" },
  { id: "sleep", libelle: "Veille", couleur: "var(--state-sleep)" },
  { id: "tracker", libelle: "Collecte active", couleur: "var(--state-tracker)", fine: true },
];

function rendreJournee() {
  const j = etat.journee;
  const carte = $("#carte-journee");
  const conteneur = $("#g-journee");
  $("#titre-journee").textContent = jourLong(j.day);
  $("#jour-apres").disabled = j.day >= isoDe(new Date());

  const lanes = [];
  const lignesTable = [];
  for (const c of [...categories()]) {
    const segments = j.lanes[c.id];
    if (!segments || !segments.length) continue;
    lanes.push({
      libelle: libelleCategorie(c.id), couleur: couleurCategorie(c.id), segments,
      infobulle: (bloc) => {
        const parApp = new Map();
        let actif = 0;
        let long = null;
        for (const m of bloc.morceaux) {
          parApp.set(m.app_name, (parApp.get(m.app_name) || 0) + (m.e - m.s));
          actif += m.active_s || 0;
          if (!long || m.e - m.s > long.e - long.s) long = m;
        }
        const apps = [...parApp.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
        return {
          titre: `${libelleCategorie(c.id)} · ${heureMin(bloc.s)} → ${heureMin(bloc.e)}`,
          lignes: [
            { libelle: "Au premier plan", valeur: duree(bloc.e - bloc.s), couleur: couleurCategorie(c.id) },
            { libelle: "Actif", valeur: duree(actif / 60) },
            ...apps.map(([nom, min]) => ({ libelle: nom, valeur: duree(min) })),
          ],
          note: long && long.title ? `« ${long.title.slice(0, 120)} »` : null,
        };
      },
    });
    for (const s of segments) {
      lignesTable.push({ lane: libelleCategorie(c.id), debut: heureMin(s.s), fin: heureMin(s.e), duree: duree(s.e - s.s), quoi: s.app_name, titre: s.title || "" });
    }
  }
  for (const e of ETATS) {
    const segments = j.lanes[e.id];
    if (!segments || !segments.length) continue;
    lanes.push({
      ...e, segments,
      infobulle: (bloc) => {
        const m = bloc.morceaux[0];
        const lignes = [{ libelle: "Durée", valeur: duree(bloc.e - bloc.s), couleur: e.couleur }];
        let note = null;
        if (e.id === "media") note = [m.title, m.artist].filter(Boolean).join(" · ") || m.player;
        if (e.id === "sleep" && m.state) note = m.state === "hibernate" ? "Hibernation" : "Veille";
        return { titre: `${e.libelle} · ${heureMin(bloc.s)} → ${heureMin(bloc.e)}`, lignes, note };
      },
    });
    for (const s of segments) {
      lignesTable.push({ lane: e.libelle, debut: heureMin(s.s), fin: heureMin(s.e), duree: duree(s.e - s.s), quoi: s.player || s.state || s.reason || "", titre: [s.title, s.artist].filter(Boolean).join(" · ") });
    }
  }

  if (!lanes.length) vide(conteneur, "Aucune donnée ce jour-là.");
  else chronologie(conteneur, j.day, lanes, j.minutes || 1440);

  lignesTable.sort((a, b) => a.debut.localeCompare(b.debut));
  definirTable(carte, [
    { cle: "debut", titre: "Début" }, { cle: "fin", titre: "Fin" }, { cle: "duree", titre: "Durée", nombre: true },
    { cle: "lane", titre: "Ligne" }, { cle: "quoi", titre: "Application / état" }, { cle: "titre", titre: "Titre" },
  ], lignesTable);
}

function rendreApps() {
  const apps = etat.resume.apps;
  legende($("#legende-apps"), [
    { libelle: "Au premier plan", couleur: "var(--ink-3)", forme: "pale" },
    { libelle: "Réellement actif", couleur: "var(--ink-3)" },
  ]);
  barresHorizontales($("#g-apps"), apps.map((a) => ({
    libelle: a.app_name, valeur: a.minutes, partiel: a.actif_min, couleur: couleurCategorie(a.category),
    texte: duree(Math.max(a.minutes, 0.05)), sousTexte: `${duree(a.actif_min)} actif`,
    infobulle: {
      titre: `${a.app_name} · ${libelleCategorie(a.category)}`,
      lignes: [
        { libelle: "Au premier plan", valeur: duree(a.minutes), couleur: couleurCategorie(a.category) },
        { libelle: "Réellement actif", valeur: duree(a.actif_min) },
        { libelle: "Fenêtres", valeur: nf.format(a.fenetres) },
      ],
    },
  })), { vide: "Aucune application au premier plan sur cette période." });
  definirTable($("#carte-apps"), [
    { cle: "app", titre: "Application" }, { cle: "cat", titre: "Catégorie" }, { cle: "pp", titre: "Premier plan", nombre: true },
    { cle: "actif", titre: "Actif", nombre: true }, { cle: "fenetres", titre: "Fenêtres", nombre: true },
  ], apps.map((a) => ({ app: a.app_name, cat: libelleCategorie(a.category), pp: duree(a.minutes), actif: duree(a.actif_min), fenetres: nf.format(a.fenetres) })));
}

function rendreSites() {
  const sites = etat.resume.domains;
  barresHorizontales($("#g-sites"), sites.map((s) => ({
    libelle: s.domain, valeur: s.minutes, couleur: "var(--series-2)",
    texte: duree(s.minutes),
    infobulle: { titre: s.domain, lignes: [{ libelle: "Temps", valeur: duree(s.minutes), couleur: "var(--series-2)" }, { libelle: "Pages", valeur: nf.format(s.pages) }] },
  })), { vide: "Aucune page web : l'extension navigateur est-elle installée ? (python -m pc.tracker extension)" });
  definirTable($("#carte-sites"), [{ cle: "d", titre: "Domaine" }, { cle: "m", titre: "Temps", nombre: true }, { cle: "p", titre: "Pages", nombre: true }],
    sites.map((s) => ({ d: s.domain, m: duree(s.minutes), p: nf.format(s.pages) })));
}

function rendreRythme() {
  const cellules = etat.resume.heatmap;
  if (!cellules.length) vide($("#g-rythme"));
  else carteThermique($("#g-rythme"), cellules);
  definirTable($("#carte-rythme"), [{ cle: "j", titre: "Jour" }, { cle: "h", titre: "Heure" }, { cle: "m", titre: "Actif", nombre: true }],
    [...cellules].sort((a, b) => a.dow - b.dow || a.hour - b.hour).map((c) => ({ j: JOURS_SEMAINE[c.dow - 1], h: `${String(c.hour).padStart(2, "0")} h`, m: duree(c.minutes) })));
}

function pastille(categorie) {
  return el("span", { class: "pastille", style: { background: couleurCategorie(categorie) } });
}

function rendreTransitions() {
  const t = etat.resume.transitions;
  const conteneur = $("#g-transitions");
  if (!t.length) {
    vide(conteneur, "Aucun changement d'application sur cette période.");
  } else {
    conteneur.replaceChildren(el("table", { class: "liste" }, el("tbody", {}, t.map((x) => el("tr", {},
      el("td", {}, pastille(x.from_category), x.from),
      el("td", { class: "secondaire" }, "→"),
      el("td", {}, pastille(x.to_category), x.to),
      el("td", { class: "n" }, `× ${nf.format(x.n)}`))))));
  }
  definirTable($("#carte-transitions"), [{ cle: "de", titre: "De" }, { cle: "vers", titre: "Vers" }, { cle: "n", titre: "Fois", nombre: true }],
    t.map((x) => ({ de: x.from, vers: x.to, n: nf.format(x.n) })));
}

function rendreGit() {
  const c = etat.resume.commits;
  const conteneur = $("#g-git");
  const quand = (iso) => {
    const d = new Date(iso);
    return `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  };
  if (!c.length) {
    vide(conteneur, "Aucun commit sur cette période.");
  } else {
    conteneur.replaceChildren(el("table", { class: "liste" }, el("tbody", {}, c.slice(0, 10).map((x) => el("tr", {},
      el("td", { class: "secondaire", style: { whiteSpace: "nowrap" } }, quand(x.when)),
      el("td", {}, el("div", {}, x.repo), el("div", { class: "secondaire" }, x.branch || "")),
      el("td", { class: "message" }, x.message || ""),
      el("td", { class: "n" }, el("span", { class: "plus" }, `+${nf.format(x.insertions ?? 0)}`), " ", el("span", { class: "moins" }, `−${nf.format(x.deletions ?? 0)}`)))))));
  }
  definirTable($("#carte-git"), [
    { cle: "q", titre: "Quand" }, { cle: "r", titre: "Dépôt" }, { cle: "b", titre: "Branche" }, { cle: "m", titre: "Message" },
    { cle: "p", titre: "+", nombre: true }, { cle: "s", titre: "−", nombre: true }, { cle: "h", titre: "Commit" },
  ], c.map((x) => ({ q: quand(x.when), r: x.repo, b: x.branch, m: x.message, p: x.insertions, s: x.deletions, h: x.hash })));
}

function rendreTerminal() {
  const r = etat.resume;
  const conteneur = $("#g-terminal");
  const bloc = (titre, liste, videMsg) => {
    const zone = el("div", { style: { marginBottom: "14px" } }, el("div", { class: "secondaire", style: { color: "var(--ink-2)", fontSize: "12.5px", marginBottom: "6px" } }, titre));
    const graphe = el("div");
    zone.append(graphe);
    // Les barres ont besoin d'une largeur : on attache avant de dessiner.
    return { zone, dessiner: () => barresHorizontales(graphe, liste.map((x) => ({
      libelle: x.label, valeur: x.n, couleur: "var(--series-1)", texte: nf.format(x.n),
      infobulle: { titre: x.label, lignes: [{ libelle: titre, valeur: nf.format(x.n), couleur: "var(--series-1)" }] },
    })), { vide: videMsg }) };
  };
  const programmes = bloc("Programmes lancés dans le terminal", r.programs, "Aucune commande : hook de terminal installé ? (python -m pc.tracker shell-hook install)");
  const extensions = bloc("Types de fichiers modifiés", r.extensions, "Aucune modification de fichier suivie.");
  conteneur.replaceChildren(programmes.zone, extensions.zone);
  programmes.dessiner();
  extensions.dessiner();
  definirTable($("#carte-terminal"), [{ cle: "type", titre: "Type" }, { cle: "l", titre: "Élément" }, { cle: "n", titre: "Nombre", nombre: true }],
    [...r.programs.map((x) => ({ type: "Programme", l: x.label, n: nf.format(x.n) })), ...r.extensions.map((x) => ({ type: "Fichier", l: x.label, n: nf.format(x.n) }))]);
}

function rendreMachine() {
  const m = etat.resume.machine;
  const conteneur = $("#g-machine");
  if (!m.some((x) => x.cpu !== null || x.ram !== null)) {
    vide(conteneur);
  } else {
    const points = (cle) => m.map((x) => ({ valeur: x[cle], titre: titreBucket({ cle: x.key }), etiquette: etiquetteBucket({ cle: x.key }) }));
    conteneur.replaceChildren(
      courbe(conteneur, points("cpu"), { titre: "Processeur (moyenne)", couleur: "var(--series-7)" }),
      courbe(conteneur, points("ram"), { titre: "Mémoire vive (moyenne)", couleur: "var(--series-7)" }));
  }
  definirTable($("#carte-machine"), [{ cle: "q", titre: "Période" }, { cle: "c", titre: "CPU %", nombre: true }, { cle: "r", titre: "RAM %", nombre: true }],
    m.map((x) => ({ q: titreBucket({ cle: x.key }), c: x.cpu == null ? "—" : nf.format(x.cpu), r: x.ram == null ? "—" : nf.format(x.ram) })));
}

function rendreReseau() {
  const m = etat.resume.machine;
  const series = [
    { cle: "down_mb", libelle: "Reçu", couleur: "var(--pair-a)" },
    { cle: "up_mb", libelle: "Envoyé", couleur: "var(--pair-b)" },
  ];
  legende($("#legende-reseau"), series.map((s) => ({ libelle: s.libelle, couleur: s.couleur })));
  colonnesGroupees($("#g-reseau"), m.map((x) => ({ ...x, cle: x.key })), series, etiquetteBucket, titreBucket);
  definirTable($("#carte-reseau"), [{ cle: "q", titre: "Période" }, { cle: "d", titre: "Reçu (Mo)", nombre: true }, { cle: "u", titre: "Envoyé (Mo)", nombre: true }],
    m.map((x) => ({ q: titreBucket({ cle: x.key }), d: x.down_mb == null ? "—" : nf.format(x.down_mb), u: x.up_mb == null ? "—" : nf.format(x.up_mb) })));
}

function statut(niveau, texte) {
  const icones = { good: "●", warning: "▲", critical: "✕" };
  const libelles = { good: "OK", warning: "Attention", critical: "Problème" };
  return el("span", {}, el("span", { class: `icone ${niveau}`, "aria-hidden": "true" }, icones[niveau]), " ", el("strong", {}, libelles[niveau]), " · ", texte);
}

function rendreSante() {
  const h = etat.resume.health;
  const derniere = h.last_event;
  const age = derniere ? (Date.now() - new Date(derniere).getTime()) / 3600000 : null;
  const niveau = age === null ? "critical" : age < 36 ? "good" : "warning";
  const lignes = [
    el("tr", {}, el("td", {}, statut(niveau, derniere ? `dernière donnée ${ilYa(derniere)}` : "aucune donnée reçue"))),
    el("tr", {}, el("td", {}, `Collecte active ${duree(h.tracked_min)} sur la période${h.coverage_pct != null ? ` (${nf.format(h.coverage_pct)} % du temps écoulé)` : ""}, en ${nf.format(h.runs)} lancement(s) du tracker.`)),
  ];
  for (const e of h.errors) {
    lignes.push(el("tr", {}, el("td", {},
      statut(e.status === "error" ? "critical" : "warning", `${e.collector} : ${e.status}`),
      el("div", { class: "secondaire" }, `${new Date(e.when).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" })} · ${e.detail || ""}`))));
  }
  if (niveau !== "good") {
    lignes.push(el("tr", {}, el("td", { class: "secondaire" },
      "Pour relancer la collecte : python -m pc.tracker status, puis python -m pc.tracker start (ou install).")));
  }
  $("#g-sante").replaceChildren(el("table", { class: "liste" }, el("tbody", {}, lignes)));
}

function rendreEtat() {
  const machines = etat.meta.devices.filter((d) => etat.machine === "all" || d.device === etat.machine);
  const dernier = machines.map((d) => d.last_batch_at).filter(Boolean).sort().pop();
  const age = dernier ? (Date.now() - new Date(dernier).getTime()) / 3600000 : null;
  const niveau = age === null ? "critical" : age < 36 ? "good" : "warning";
  const icones = { good: "●", warning: "▲", critical: "✕" };
  $("#etat").replaceChildren(el("span", { class: `icone ${niveau}`, "aria-hidden": "true" }, icones[niveau]),
    dernier ? `Dernier lot reçu ${ilYa(dernier)}` : "Aucun lot reçu");
}

function rendreTout() {
  cacherInfobulle();
  rendreEtat();
  rendreKpis();
  rendrePeriode();
  rendreApps();
  rendreSites();
  rendreRythme();
  rendreTransitions();
  rendreGit();
  rendreTerminal();
  rendreMachine();
  rendreReseau();
  rendreSante();
  if (etat.journee) rendreJournee();
}

// ============================================================== chargement

async function chargerJournee() {
  etat.journee = await api("/api/v1/pc/day", { device: etat.machine, day: etat.jour });
  rendreJournee();
}

function memoriserFiltres() {
  const params = new URLSearchParams();
  if (etat.machine !== "all") params.set("machine", etat.machine);
  const preset = document.querySelector('.presets button[aria-pressed="true"]');
  if (preset) params.set("jours", preset.dataset.jours);
  else { params.set("debut", etat.debut); params.set("fin", etat.fin); }
  const adresse = params.toString() ? `?${params}` : location.pathname;
  try { history.replaceState(null, "", adresse); } catch (_) { /* adresse inchangee */ }
}

async function charger({ discret = false } = {}) {
  if (etat.cleRequise) return;
  memoriserFiltres();
  const principal = $("#contenu");
  if (!discret) principal.classList.add("rechargement");
  try {
    const [meta, resume] = await Promise.all([
      api("/api/v1/pc/meta"),
      api("/api/v1/pc/summary", { device: etat.machine, start: etat.debut, end: etat.fin }),
    ]);
    etat.meta = meta;
    etat.resume = resume;
    if (!etat.jour || etat.jour < etat.debut || etat.jour > etat.fin) etat.jour = etat.fin;
    etat.journee = await api("/api/v1/pc/day", { device: etat.machine, day: etat.jour });
    rendreTout();
  } catch (erreur) {
    if (erreur.message !== "cle requise") {
      $("#etat").replaceChildren(el("span", { class: "icone critical" }, "✕"), ` Chronicle injoignable (${erreur.message})`);
    }
  } finally {
    principal.classList.remove("rechargement");
  }
}

function choisirJour(iso) {
  etat.jour = iso;
  rendrePeriode();
  chargerJournee();
}

function appliquerPreset(jours) {
  etat.jours = jours;
  etat.fin = isoDe(new Date());
  etat.debut = decaler(etat.fin, -(jours - 1));
  etat.jour = etat.fin;
  $("#f-debut").value = etat.debut;
  $("#f-fin").value = etat.fin;
  for (const b of document.querySelectorAll(".presets button")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.jours) === jours));
  }
}

function relacherPresets() {
  for (const b of document.querySelectorAll(".presets button")) b.setAttribute("aria-pressed", "false");
}

function appliquerTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  $("#f-theme").textContent = `Thème : ${{ light: "clair", dark: "sombre" }[theme] || "auto"}`;
}

async function demarrer() {
  appliquerTheme(stockage("chronicle-theme"));
  $("#f-theme").addEventListener("click", () => {
    const suivant = { auto: "light", light: "dark", dark: "auto" }[stockage("chronicle-theme") || "auto"];
    stockage("chronicle-theme", suivant === "auto" ? null : suivant);
    appliquerTheme(suivant);
    rendreTout();
  });

  for (const carte of document.querySelectorAll(".carte")) {
    const bouton = carte.querySelector(".bascule-table");
    if (bouton) bouton.addEventListener("click", () => basculerTable(carte));
  }

  for (const b of document.querySelectorAll(".presets button")) {
    b.addEventListener("click", () => { appliquerPreset(Number(b.dataset.jours)); charger(); });
  }

  const dates = () => {
    const debut = $("#f-debut").value;
    const fin = $("#f-fin").value;
    if (!debut || !fin || debut > fin) return;
    etat.debut = debut;
    etat.fin = fin;
    relacherPresets();
    charger();
  };
  $("#f-debut").addEventListener("change", dates);
  $("#f-fin").addEventListener("change", dates);

  $("#jour-avant").addEventListener("click", () => {
    etat.jour = decaler(etat.jour, -1);
    if (etat.jour < etat.debut) { etat.debut = etat.jour; $("#f-debut").value = etat.debut; relacherPresets(); charger(); } else choisirJour(etat.jour);
  });
  $("#jour-apres").addEventListener("click", () => {
    if (etat.jour >= isoDe(new Date())) return;
    etat.jour = decaler(etat.jour, 1);
    if (etat.jour > etat.fin) { etat.fin = etat.jour; $("#f-fin").value = etat.fin; relacherPresets(); charger(); } else choisirJour(etat.jour);
  });

  const params = new URLSearchParams(location.search);
  const dateValide = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v || "");
  if (dateValide(params.get("debut")) && dateValide(params.get("fin")) && params.get("debut") <= params.get("fin")) {
    appliquerPreset(7);
    etat.debut = params.get("debut");
    etat.fin = params.get("fin");
    etat.jour = etat.fin;
    $("#f-debut").value = etat.debut;
    $("#f-fin").value = etat.fin;
    relacherPresets();
  } else {
    appliquerPreset([1, 7, 30, 90].includes(Number(params.get("jours"))) ? Number(params.get("jours")) : 7);
  }

  const meta = await api("/api/v1/pc/meta").catch(() => null);
  if (meta) {
    const choix = $("#f-machine");
    choix.replaceChildren(el("option", { value: "all" }, "Toutes les machines"),
      ...meta.devices.map((d) => el("option", { value: d.device }, d.device)));
    const demandee = params.get("machine");
    if (demandee && meta.devices.some((d) => d.device === demandee)) etat.machine = demandee;
    else if (meta.devices.length === 1) etat.machine = meta.devices[0].device;
    choix.value = etat.machine;
    choix.addEventListener("change", () => { etat.machine = choix.value; charger(); });
  }
  await charger();

  // Redessiner a la bonne largeur, sans recharger les donnees.
  let attente = null;
  new ResizeObserver(() => {
    clearTimeout(attente);
    attente = setTimeout(() => { if (etat.resume) rendreTout(); }, 150);
  }).observe($("#contenu"));

  // Rafraichissement discret chaque minute si la periode inclut aujourd'hui.
  setInterval(() => { if (etat.fin >= isoDe(new Date()) && !document.hidden) charger({ discret: true }); }, 60000);
}

demarrer();
