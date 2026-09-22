const api = globalThis.browser ?? globalThis.chrome;
const champPort = document.getElementById("port");
const champJeton = document.getElementById("token");
const etat = document.getElementById("etat");

api.storage.local.get({ port: 8781, token: "" }).then(({ port, token }) => {
  champPort.value = port;
  champJeton.value = token;
});

document.getElementById("enregistrer").addEventListener("click", async () => {
  const port = Number(champPort.value) || 8781;
  const token = champJeton.value.trim();
  await api.storage.local.set({ port, token });

  try {
    const reponse = await fetch(`http://127.0.0.1:${port}/browser/ping`, {
      headers: { "X-Chronicle-Token": token },
    });
    const corps = await reponse.json().catch(() => ({}));
    if (reponse.ok) {
      etat.className = "ok";
      etat.textContent = `Connecte au tracker de ${corps.device_id}.`;
    } else {
      etat.className = "ko";
      etat.textContent = reponse.status === 403
        ? "Jeton refuse : verifier [browser] token dans config.toml."
        : `Reponse inattendue : HTTP ${reponse.status}.`;
    }
  } catch {
    etat.className = "ko";
    etat.textContent = "Tracker injoignable : est-il lance (python -m pc.tracker status) ?";
  }
});
