#!/bin/sh
# ---------------------------------------------------------------------------
# Envoyer au PC.sh - Televerse les donnees de lecture de la Kindle vers le PC.
#
# A poser dans /mnt/us/documents/ sur la liseuse : le script apparait alors
# comme un item de la bibliotheque. Un appui dessus l'execute (jailbreak KMC,
# root via jb.so). Pour lui donner une icone, creer a cote le dossier
# "Envoyer au PC.sh.sdr/" contenant un "icon.png".
#
# Cote PC, lancer d'abord :   python main.py kindle-serve
# ---------------------------------------------------------------------------

# --- CONFIGURATION --- (kindle-serve affiche ces valeurs au demarrage)
PC_HOST=192.168.1.9
PC_PORT=8765
TOKEN=kindle
# ---------------------------------------------------------------------------

BASE="http://${PC_HOST}:${PC_PORT}"
US=/mnt/us
TMP=/tmp/kindle_envoi
LOG="${US}/documents/envoi_pc.log"

exec >>"$LOG" 2>&1
echo "===== $(date) ====="

# Notification a l'ecran si les outils du firmware sont disponibles.
notify() {
    echo "$1"
    [ -x /usr/bin/eips ] && /usr/bin/eips 2 2 "$1" 2>/dev/null
    command -v lipc-set-prop >/dev/null 2>&1 && \
        lipc-set-prop com.lab126.pillow interrogatePillow \
        "{\"clientParams\":{\"alertId\":\"infoAlert\",\"msg\":\"$1\"}}" 2>/dev/null
    return 0
}

fail() { notify "Echec : $1"; echo "ERREUR: $1"; exit 1; }

rm -rf "$TMP"; mkdir -p "$TMP" || fail "dossier temporaire"

# --- Connectivite ----------------------------------------------------------
notify "Connexion au PC..."
command -v lipc-set-prop >/dev/null 2>&1 && \
    lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null

i=0
while [ $i -lt 15 ]; do
    if curl -s -m 3 "${BASE}/ping" 2>/dev/null | grep -q pong; then break; fi
    i=$((i + 1)); sleep 1
done
[ $i -ge 15 ] && fail "PC injoignable (${PC_HOST}:${PC_PORT})"

# --- Copie des bases -------------------------------------------------------
# On copie AVANT d'envoyer : ces bases sont ouvertes en ecriture par le
# firmware. Les televerser a chaud donnerait des fichiers corrompus.
# ".backup" de sqlite3 fige un etat coherent ; cp est le repli.
copy_db() {
    src="$1"; dst="$2"
    [ -f "$src" ] || { echo "absent: $src"; return 1; }
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$src" ".backup '${TMP}/${dst}'" 2>/dev/null && return 0
    fi
    cp -f "$src" "${TMP}/${dst}" 2>/dev/null
}

copy_db /var/local/fmcache/fmcache.db fmcache.db
[ -f "${TMP}/fmcache.db" ] || copy_db "${US}/system/fmcache/fmcache.db" fmcache.db
copy_db "${US}/system/vocabulary/vocab.db" vocab.db

ANN=$(ls "${US}"/system/ksdk/.annotations/*/ksdk_annotation_v1.db 2>/dev/null | head -n 1)
[ -n "$ANN" ] && copy_db "$ANN" annotations.db

# Catalogue : les noms de fichiers portent l'ASIN de chaque livre.
ls "${US}/documents/Downloads/Items01/" > "${TMP}/catalog.txt" 2>/dev/null

# --- Envoi -----------------------------------------------------------------
sent=0
for f in fmcache.db vocab.db annotations.db catalog.txt; do
    [ -s "${TMP}/${f}" ] || { echo "saute (vide/absent): $f"; continue; }
    notify "Envoi ${f}..."
    if curl -s -m 120 -f -T "${TMP}/${f}" "${BASE}/u/${TOKEN}/${f}" >/dev/null 2>&1; then
        echo "envoye: $f ($(wc -c < "${TMP}/${f}") octets)"
        sent=$((sent + 1))
    else
        echo "echec envoi: $f"
    fi
done

[ $sent -eq 0 ] && fail "aucun fichier envoye"

# --- Cloture : le PC ingere -------------------------------------------------
RESP=$(curl -s -m 300 -X POST "${BASE}/done/${TOKEN}" 2>/dev/null)
echo "reponse serveur: $RESP"

rm -rf "$TMP"
notify "Termine : ${sent} fichier(s)"
echo "OK"
exit 0
