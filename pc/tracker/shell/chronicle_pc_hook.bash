# Chronicle PC tracker - hook bash (terminal_command). Git Bash et Linux.
#
# A charger depuis ~/.bashrc :
#     source /chemin/vers/Chronicle/pc/tracker/shell/chronicle_pc_hook.bash
#
# Apres chaque commande, ajoute UNE ligne dans le dossier de depot du
# tracker (bash-<pid>.tsv). Le tracker lit, REDIGE les secrets, supprime.
#
#   b2  debut  fin  code  pid  cwd  commande  shell  terminal
#
# Tabulations, retours a la ligne et barres obliques inverses sont
# echappes (\t \n \r \\) : une commande sur plusieurs lignes reste une
# ligne du fichier.
#
# RAPIDITE : aucun processus lance par commande, sauf UNE lecture de
# l'historique. Horloge par $EPOCHREALTIME (bash >= 5), expressions
# natives au lieu d'awk/sed. Sous Git Bash, chaque processus lance coute
# des dizaines de millisecondes : le prompt ne doit pas les payer.

if [ -n "${BASH_VERSION:-}" ] && [ -z "${__CHRONICLE_PC_HOOK:-}" ]; then
    __CHRONICLE_PC_HOOK=1

    if [ -n "${CHRONICLE_PC_HOME:-}" ]; then
        __chronicle_pc_spool="$CHRONICLE_PC_HOME/spool"
    elif [ -n "${LOCALAPPDATA:-}" ]; then
        __chronicle_pc_spool="$(cygpath -u "$LOCALAPPDATA" 2>/dev/null || echo "$LOCALAPPDATA")/ChroniclePC/spool"
    else
        __chronicle_pc_spool="${XDG_DATA_HOME:-$HOME/.local/share}/chronicle-pc/spool"
    fi

    __chronicle_pc_debut=""
    __chronicle_pc_numero=""
    # Rien n'est note avant le premier prompt : les commandes du .bashrc et
    # la derniere ligne de l'historique de la session precedente ne sont
    # pas des commandes tapees.
    __chronicle_pc_pret=""

    __chronicle_pc_maintenant() {
        # Certaines locales ecrivent la virgule decimale ("1758.123").
        if [ -n "${EPOCHREALTIME:-}" ]; then
            __chronicle_pc_t=${EPOCHREALTIME/,/.}
        else
            __chronicle_pc_t=$(date +%s)
        fi
    }

    __chronicle_pc_echapper() {
        local s=$1
        s=${s//\\/\\\\}
        s=${s//$'\t'/\\t}
        s=${s//$'\n'/\\n}
        s=${s//$'\r'/\\r}
        __chronicle_pc_e=$s
    }

    __chronicle_pc_preexec() {
        # Le piege DEBUG se declenche avant CHAQUE commande simple, prompt
        # compris : seul le premier declenchement apres un prompt compte.
        [ -z "$__chronicle_pc_pret" ] && return
        case "$BASH_COMMAND" in __chronicle_pc_*) return ;; esac
        [ -n "${COMP_LINE:-}" ] && return
        if [ -z "$__chronicle_pc_debut" ]; then
            __chronicle_pc_maintenant
            __chronicle_pc_debut=$__chronicle_pc_t
        fi
    }

    __chronicle_pc_precmd() {
        local code=$?
        local ligne numero commande cwd
        ligne=$(HISTTIMEFORMAT='' builtin history 1)
        if [[ $ligne =~ ^\ *([0-9]+)\*?\ +(.*)$ ]]; then
            numero=${BASH_REMATCH[1]}
            commande=${BASH_REMATCH[2]}
        fi
        if [ -z "$__chronicle_pc_pret" ]; then
            __chronicle_pc_pret=1
            __chronicle_pc_numero="$numero"
            __chronicle_pc_debut=""
            return $code
        fi
        # Meme numero d'historique qu'au dernier prompt : aucune commande
        # nouvelle (Entree a vide, ou morceau de PROMPT_COMMAND).
        if [ -n "$__chronicle_pc_debut" ] && [ -n "$numero" ] && [ "$numero" != "$__chronicle_pc_numero" ]; then
            __chronicle_pc_numero="$numero"
            __chronicle_pc_maintenant
            __chronicle_pc_echapper "$PWD"; cwd=$__chronicle_pc_e
            __chronicle_pc_echapper "$commande"; commande=$__chronicle_pc_e
            [ -d "$__chronicle_pc_spool" ] || mkdir -p "$__chronicle_pc_spool" 2>/dev/null
            printf 'b2\t%s\t%s\t%s\t%s\t%s\t%s\tbash\t%s\n' \
                "$__chronicle_pc_debut" "$__chronicle_pc_t" "$code" "$$" \
                "$cwd" "$commande" "${TERM_PROGRAM:-${TERM:-}}" \
                >> "$__chronicle_pc_spool/bash-$$.tsv" 2>/dev/null
        fi
        __chronicle_pc_debut=""
        return $code
    }

    trap '__chronicle_pc_preexec' DEBUG
    PROMPT_COMMAND="__chronicle_pc_precmd${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
fi
