// Chronicle PC tracker - extension navigateur (Chrome, Edge, Firefox).
//
// Role unique : dire au tracker local QUEL onglet est actif. Rien d'autre.
//
//   - n'envoie que l'onglet actif de la fenetre active ;
//   - n'envoie qu'a 127.0.0.1 (le tracker de CETTE machine) ;
//   - ne tourne jamais en navigation privee ("incognito": "not_allowed") ;
//   - retire le fragment (#...) avant l'envoi. Le tracker, lui, ne garde
//     par defaut que le DOMAINE (voir [browser] url_mode) ;
//   - ne lit jamais le contenu des pages.
//
// Le temps passe par page est calcule par le tracker, qui sait si le
// navigateur est vraiment au premier plan : l'extension ne mesure rien.

const api = globalThis.browser ?? globalThis.chrome;

function navigateur() {
  const ua = navigator.userAgent || "";
  if (ua.includes("Firefox/")) return "firefox";
  if (ua.includes("Edg/")) return "edge";
  if (ua.includes("OPR/")) return "opera";
  if (ua.includes("Vivaldi")) return "vivaldi";
  if (navigator.brave) return "brave";
  return "chrome";
}

const NAVIGATEUR = navigateur();
let dernier = "";

async function reglages() {
  return api.storage.local.get({ port: 8781, token: "" });
}

function sansFragment(url) {
  if (!url) return null;
  const position = url.indexOf("#");
  return position >= 0 ? url.slice(0, position) : url;
}

async function signaler(force = false) {
  const { port, token } = await reglages();
  if (!token) return;                       // pas encore configuree

  let fenetre;
  try {
    fenetre = await api.windows.getLastFocused({ populate: false });
  } catch {
    return;
  }
  if (!fenetre || fenetre.id === undefined) return;

  const [onglet] = await api.tabs.query({ active: true, windowId: fenetre.id });
  if (!onglet) return;

  const rapport = {
    browser: NAVIGATEUR,
    url: onglet.incognito ? null : sansFragment(onglet.url || onglet.pendingUrl),
    title: onglet.incognito ? null : (onglet.title || null),
    incognito: Boolean(onglet.incognito),
    tab_id: onglet.id,
  };

  const empreinte = JSON.stringify(rapport);
  if (!force && empreinte === dernier) return;

  try {
    const reponse = await fetch(`http://127.0.0.1:${port}/browser/tab`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Chronicle-Token": token },
      body: empreinte,
    });
    if (reponse.ok) dernier = empreinte;
  } catch {
    // Tracker arrete : rien a faire. Le prochain changement d'onglet (ou
    // l'alarme de la minute suivante) reessaiera.
  }
}

api.tabs.onActivated.addListener(() => signaler());
api.tabs.onUpdated.addListener((_id, changement, onglet) => {
  if (onglet.active && (changement.url || changement.title || changement.status === "complete")) {
    signaler();
  }
});
api.windows.onFocusChanged.addListener((id) => {
  if (id !== api.windows.WINDOW_ID_NONE) signaler();
});

// Une fois par minute : renvoie l'etat courant, meme inchange. Un tracker
// qui vient de redemarrer apprend ainsi l'onglet actif sans attendre un clic.
api.alarms.create("chronicle-pc", { periodInMinutes: 1 });
api.alarms.onAlarm.addListener((alarme) => {
  if (alarme.name === "chronicle-pc") signaler(true);
});

api.runtime.onStartup?.addListener(() => signaler(true));
api.runtime.onInstalled?.addListener(() => signaler(true));
