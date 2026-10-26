#!/bin/bash
# Arrete l'autopilote AVANT qu'un nœud nomme ne demarre.
#
# Sert quand une etape franchit une frontiere qu'on ne veut pas franchir sans
# regarder — par exemple une etape qui envoie un message a quelqu'un d'autre :
# un envoi ne se declenche jamais dans le dos de son proprietaire.
#
# usage : borne-avant.sh <workflow> <run> <noeud-a-ne-pas-laisser-demarrer>
WF="$1"; R="$2"; STOP="$3"
FLUX="$(cd "$(dirname "$0")/.." && pwd)"
WS="${FLUX_RUNS_HOME:-$HOME}/$WF-runs/$R"
ST="${FLUX_STATE_DIR:-$FLUX/state}/$WF/$R"
LOG="$WS/state/borne-avant.log"
mkdir -p "$WS/state"
echo "$(date -Is) borne posee sur $R — arret avant « $STOP »" >> "$LOG"

eteindre() {
  if [ -f "$WS/state/autopilote" ]; then
    mv "$WS/state/autopilote" "$WS/state/autopilote.borne"
    echo "$(date -Is) autopilote eteint — $1" >> "$LOG"
  fi
  exit 0
}

while true; do
  sleep 20
  # Le nœud a ete lance : on coupe tout de suite pour que rien n enchaine derriere,
  # meme si sa session est deja partie. Mieux vaut une session a interrompre qu un
  # envoi qu on ne peut pas rattraper.
  [ -f "$ST/$STOP.json" ] && eteindre "le nœud « $STOP » a demarre"
  # Quelqu un a deja eteint : plus rien a garder
  [ -f "$WS/state/autopilote" ] || eteindre "autopilote deja eteint"
done
