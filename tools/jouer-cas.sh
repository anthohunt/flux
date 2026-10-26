#!/bin/bash
# Joue UNE run de banc du workflow mail, de bout en bout, et rend la note.
#   jouer-cas.sh <cas> <modele> [slug]
# Sortie : une ligne par nœud + la ligne TOTAL, lues dans state/.
set -u
CAS="$1"; MODELE="$2"; SLUG="${3:-feu}"
FLUX="$(cd "$(dirname "$0")/.." && pwd)"
cd "$FLUX" || exit 3
# Un cas vit dans cas/ (lot boucle) ou dans le dossier du lot de mesure, hors depot
# (decision D-b). L'identifiant reste le meme des deux cotes : c'est lui qui nomme la
# run et que les outils de note rapprochent du corrige. Seul --cas prend le chemin.
MESURE="${FLUX_CAS_MESURE:-/var/lib/flux-mesure/cas}"
case "$CAS" in
  */*) case "$CAS" in
         "$MESURE"/*) [ "${FLUX_MESURE_AUTORISEE:-0}" = 1 ] || {
             echo "refus : chemin du lot de MESURE sans FLUX_MESURE_AUTORISEE=1"; exit 4; } ;;
       esac
       CAS_ID=$(basename "$CAS"); CAS_DIR="$CAS" ;;
  *)   CAS_ID="$CAS"
       # Un identifiant NU ne resout JAMAIS vers le lot de mesure : c'est ce
       # repli qui a brule 7 cas le 2026-09-07. Jouer un cas de mesure exige
       # son chemin complet ET FLUX_MESURE_AUTORISEE=1, une intention ecrite.
       if   [ -d "cas/$CAS" ]; then CAS_DIR="$CAS"
       elif [ -d "$MESURE/$CAS" ]; then
         echo "refus : $CAS appartient au lot de MESURE. La boucle ne doit pas le voir."
         echo "        Pour une campagne de mesure deliberee :"
         echo "        FLUX_MESURE_AUTORISEE=1 $0 $MESURE/$CAS $MODELE"
         exit 4
       else echo "cas introuvable : cas/$CAS"; exit 3
       fi ;;
esac
[ -d "$CAS_DIR" ] || [ -d "cas/$CAS_DIR" ] || { echo "cas introuvable : $CAS_DIR"; exit 3; }
WS=$(python3 runtime/new-run.py mail "$SLUG-$CAS_ID-${MODELE//[:.]/-}" --mode banc --cas "$CAS_DIR" --champ "modele=$MODELE" 2>/dev/null | tail -1)
[ -d "$WS" ] || { echo "PANNE ouverture de run pour $CAS/$MODELE"; exit 3; }
echo "workspace : $WS"
T0=$(date +%s)
for N in remplir decider; do
  python3 runtime/run-node.py mail "$N" --workspace "$WS" >/dev/null 2>&1
  echo "  $N : rc=$?"
done
T1=$(date +%s)
RUN=$(basename "$WS")
python3 - "$RUN" "$CAS_ID" "$MODELE" $((T1-T0)) <<"PY"
import json, os, pathlib, sys
run, cas, modele, duree = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
d = pathlib.Path(os.environ.get("FLUX_STATE_DIR") or "state") / "mail" / run
tot = bar = 0; statuts = []
for n in ("remplir", "decider"):
    e = json.loads((d / (n + ".json")).read_text(encoding="utf-8"))
    p = e["passages"][-1]
    note = p.get("note") or {}
    s, b = note.get("score"), note.get("bareme")
    statuts.append("%s=%s" % (n, e["statut"]))
    if s is not None:
        tot += s; bar += b
    print("  %-9s %-8s note %s/%s  passes=%d" % (n, e["statut"], s, b, len(e["passages"])))
print("TOTAL %s %s : %d/%d · %s · %d s" % (cas, modele, tot, bar, " ".join(statuts), duree))
PY
