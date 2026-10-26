#!/usr/bin/env bash
# C2 — session morte = panne, et le sequenceur relance puis bloque et alerte.
#
# Scenario, avec un FAUX claude (tests/fakes/claude) qui ecrit un livrable
# partiel puis attend qu'on le tue :
#   1. run-node.py joue un nœud tmux ; on tue sa session tmux par son NOM ;
#      -> rc=2, la passe porte `fin: panne`, le nœud n'est pas vert (statut panne) ;
#   2. le sequenceur (reveille par run-node) relance UNE fois (passe 2) ;
#      on tue aussi cette session ;
#   3. relances_max (1) epuise -> run.json.statut == bloque, ET une alerte
#      arrive sur FLUX_NOTIF_URL, intercepte par un faux serveur HTTP local ;
#   4. la garde, avec un faux modele qui repond « alerter », alerte aussi ;
#   5. un ecran qui affiche « usage limit reached » est une panne quota-atteint.
#
# Tout se passe dans un dossier temporaire (FLUX_STATE_DIR, FLUX_WORKFLOWS_DIR) ;
# les sessions tmux portent le prefixe test-flux- et sont tuees par leur nom.
set -uo pipefail
cd "$(dirname "$0")/.."
RACINE="$(pwd)"
ECHECS=0
ok()   { printf 'ok   %s\n' "$1"; }
rate() { printf 'RATE %s\n     %s\n' "$1" "$2"; ECHECS=$((ECHECS + 1)); }

TMP=$(mktemp -d)
# Un serveur tmux PRIVE au test : son environnement (dossiers temporaires) ne doit pas
# rester dans le serveur tmux de la machine, dont heriteraient les sessions suivantes.
export TMUX_TMPDIR="$TMP/tmux"; mkdir -p "$TMUX_TMPDIR"; unset TMUX
export FLUX_STATE_DIR="$TMP/state" FLUX_WORKFLOWS_DIR="$TMP/workflows"
export FLUX_CLAUDE_BIN="$RACINE/tests/fakes/claude"
# Un moteurs.json de test : une version epinglee, pour tester le refus (section 6).
python3 - "$RACINE/moteurs.example.json" "$TMP/moteurs.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d['claude_version'] = '2.1.241'
json.dump(d, open(sys.argv[2], 'w'))
PY
export FLUX_MOTEURS="$TMP/moteurs.json"
export FLUX_SESSION_PREFIXE="test-flux"
export FLUX_SANS_GARDE=1
export FLUX_GARDE_MODELE_CMD="$RACINE/tests/fakes/garde-alerter.sh"
export FLUX_ORCHESTRATEUR=regle   # les tests jugent le programme, pas l agent
mkdir -p "$TMP/workflows" "$TMP/runs"
PIDS=()
nettoyer() {
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^test-flux-' || true); do
    tmux kill-session -t "$s" 2>/dev/null || true
  done
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  tmux kill-server 2>/dev/null || true   # le serveur prive du test, pas celui de la machine
  rm -rf "$TMP"
}
trap nettoyer EXIT

# --- le faux serveur de notification ----------------------------------------
JOURNAL="$TMP/notifs.log"; : > "$JOURNAL"
python3 tests/fakes/notif-serveur.py "$JOURNAL" > "$TMP/notif.out" 2>&1 &
PIDS+=($!)
for i in $(seq 1 50); do grep -q '^PORT' "$TMP/notif.out" 2>/dev/null && break; sleep 0.1; done
PORT=$(sed -n 's/^PORT //p' "$TMP/notif.out")
[ -n "$PORT" ] || { rate "faux serveur de notification" "pas de port"; exit 1; }
export FLUX_NOTIF_URL="http://127.0.0.1:$PORT/notif"

# --- le graphe de test --------------------------------------------------------
cat > "$TMP/workflows/tf.json" <<EOF
{"id": "tf", "name": "Test session morte", "workspace_root": "$TMP/runs",
 "garde": {"toutes_les_min": 10, "modele": "haiku", "relances_max": 1},
 "nodes": [
  {"id": "etape", "n": 1, "type": "agent", "name": "Etape unique", "moteur": "tmux", "modele": "opus",
   "objectif": "Ecrire le livrable.", "taches": ["Ecrire livrable.txt"], "entree": [],
   "livrables": ["livrable.txt"], "juge": {"type": "presence"}, "relances_max": 1,
   "tools": ["Bash", "Read", "Write", "TodoWrite"], "suivant": []}
 ]}
EOF

WS=$(python3 runtime/new-run.py tf session-morte --mode production 2>"$TMP/newrun.err") \
  || { rate "new-run.py" "$(cat "$TMP/newrun.err")"; exit 1; }
[ -f "$FLUX_STATE_DIR/tf/$(basename "$WS")/run.json" ] && ok "new-run.py ecrit run.json (mode production)" \
  || rate "run.json" "absent"
grep -q '^mode: production' "$WS/WORKSPACE.txt" && ok "WORKSPACE.txt porte mode:" || rate "WORKSPACE.txt mode:" "absent"
mkdir -p "$WS/state"; echo "$(date -Is)" > "$WS/state/autopilote"
echo partiel > "$WS/.faux-claude"
ETAT="$FLUX_STATE_DIR/tf/$(basename "$WS")/etape.json"
RUNJSON="$FLUX_STATE_DIR/tf/$(basename "$WS")/run.json"

attendre_session() {   # attendre_session <n> -> 0 si la session de la passe n vit
  local n="$1" i
  for i in $(seq 1 120); do
    tmux ls -F '#{session_name}' 2>/dev/null | grep -q "^test-flux-tf-.*-etape-p$n\$" && return 0
    sleep 0.5
  done
  return 1
}
session_de() { tmux ls -F '#{session_name}' 2>/dev/null | grep "^test-flux-tf-.*-etape-p$1\$" | head -1; }
attendre_fichier() {   # attendre_fichier <motif-python> <secondes>
  local i
  for i in $(seq 1 $(( $2 * 2 ))); do
    python3 - "$ETAT" "$1" <<'PY' && return 0
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
sys.exit(0 if eval(sys.argv[2], {}, {'d': d}) else 1)
PY
    sleep 0.5
  done
  return 1
}

# --- 1. premiere passe : session tuee -> panne --------------------------------
python3 runtime/run-node.py tf etape --workspace "$WS" > "$TMP/p1.log" 2>&1 &
RN1=$!
if attendre_session 1; then ok "passe 1 : session tmux ouverte ($(session_de 1))"; else rate "passe 1 : session" "jamais ouverte : $(cat "$TMP/p1.log")"; exit 1; fi
for i in $(seq 1 40); do [ -f "$WS/livrable.txt.partiel" ] && break; sleep 0.5; done
[ -f "$WS/livrable.txt.partiel" ] && ok "le faux agent a ecrit un livrable PARTIEL" || rate "livrable partiel" "absent"
tmux kill-session -t "$(session_de 1)"
wait $RN1; RC1=$?
[ "$RC1" -eq 2 ] && ok "run-node rc=2 apres session morte" || rate "rc run-node" "rc=$RC1 (attendu 2) : $(tail -5 "$TMP/p1.log")"
python3 - "$ETAT" <<'PY' && ok "passe 1 : fin=panne, verdict=panne, cause session-morte, statut du nœud = panne" || rate "passe 1 panne" "$(cat "$ETAT" | head -40)"
import json, sys
d = json.load(open(sys.argv[1])); p = d['passages'][0]
assert p['fin'] == 'panne', p['fin']
assert p['verdict'] == 'panne', p['verdict']
assert p['panne']['cause'] == 'session-morte', p['panne']
assert p['moteur'] == 'tmux' and p['compte'] and p['modele'] == 'opus', p
assert d['statut'] == 'panne', d['statut']
assert 'note' in p and p['note'] is None
PY

# --- 2. le sequenceur relance une fois ---------------------------------------
if attendre_session 2; then ok "sequenceur : relance -> passe 2 ouverte ($(session_de 2))"; else
  rate "relance passe 2" "aucune session p2 ; sequenceur.log : $(cat "$FLUX_STATE_DIR/tf/$(basename "$WS")/sequenceur.log" 2>/dev/null)"; exit 1; fi
attendre_fichier "len(d.get('passages',[]))==2 and d['statut']=='en-cours'" 10 \
  && ok "etat : 2 passes, nœud en-cours" || rate "etat passe 2" "$(cat "$ETAT")"
python3 - "$RUNJSON" <<'PY' && ok "run.json : statut en-cours, noeud_courant=etape" || rate "run.json en-cours" "$(cat "$RUNJSON")"
import json, sys
d = json.load(open(sys.argv[1])); assert d['statut'] == 'en-cours' and d['noeud_courant'] == 'etape', d
PY
sleep 2
tmux kill-session -t "$(session_de 2)"

# --- 3. relances epuisees -> bloque + alerte -----------------------------------
for i in $(seq 1 120); do
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('statut')=='bloque' else 1)" "$RUNJSON" 2>/dev/null && break
  sleep 0.5
done
python3 - "$RUNJSON" "$ETAT" <<'PY' && ok "run.json.statut == bloque apres relances_max epuise" || rate "bloque" "$(cat "$RUNJSON"; cat "$FLUX_STATE_DIR/tf/$(basename "$WS")/sequenceur.log")"
import json, sys
r = json.load(open(sys.argv[1])); e = json.load(open(sys.argv[2]))
assert r['statut'] == 'bloque', r
assert r['bloque_sur'] == 'etape', r
assert len(e['passages']) == 2 and all(p['fin'] == 'panne' for p in e['passages']), e['passages']
assert e['statut'] == 'panne'
PY
for i in $(seq 1 20); do grep -q 'bloqu' "$JOURNAL" 2>/dev/null && break; sleep 0.5; done
grep -q 'bloqu' "$JOURNAL" && ok "alerte recue par le faux serveur (FLUX_NOTIF_URL) : $(head -1 "$JOURNAL" | cut -c1-90)…" \
  || rate "alerte bloque" "rien dans $JOURNAL"
if tmux ls -F '#{session_name}' 2>/dev/null | grep -q '^test-flux-tf-'; then rate "pas de 3e passe" "une session vit encore"; else ok "aucune 3e passe lancee"; fi
[ -f "$FLUX_STATE_DIR/tf/$(basename "$WS")/etat-compact.json" ] && ok "etat-compact.json ecrit pour la garde" || rate "etat-compact" "absent"

# --- 4. la garde alerte (faux modele) -------------------------------------------
N0=$(wc -l < "$JOURNAL")
python3 runtime/garde.py tf "$WS" > "$TMP/garde.out" 2>&1
grep -q '"geste": "alerter"' "$TMP/garde.out" && ok "garde : geste alerter lu du modele" || rate "garde geste" "$(cat "$TMP/garde.out")"
N1=$(wc -l < "$JOURNAL")
[ "$N1" -gt "$N0" ] && ok "garde : alerte recue par le faux serveur" || rate "garde alerte" "aucune requete de plus"
python3 - "$ETAT" <<'PY' && ok "garde : drapeau `garde` pose sur la derniere passe" || rate "drapeau garde" "$(cat "$ETAT")"
import json, sys
d = json.load(open(sys.argv[1])); g = d['passages'][-1].get('garde')
assert g and g['drapeau'] == 'alerter' and g['motif'], g
PY

# --- 5. mur de quota a l'ecran = panne quota-atteint ------------------------------
WS2=$(python3 runtime/new-run.py tf mur --mode production 2>/dev/null)
mkdir -p "$WS2/state"; echo mur > "$WS2/.faux-claude"
ETAT2="$FLUX_STATE_DIR/tf/$(basename "$WS2")/etape.json"
python3 runtime/run-node.py tf etape --workspace "$WS2" > "$TMP/p-mur.log" 2>&1
RCM=$?
python3 - "$ETAT2" <<'PY' && ok "mur « usage limit reached » a l'ecran -> panne quota-atteint (rc=$RCM)" || rate "mur quota" "rc=$RCM $(cat "$ETAT2" 2>/dev/null | head -30) $(tail -3 "$TMP/p-mur.log")"
import json, sys
d = json.load(open(sys.argv[1])); p = d['passages'][-1]
assert p['fin'] == 'panne' and p['panne']['cause'] == 'quota-atteint', p
assert d['statut'] == 'panne'
PY

# --- 6. version epinglee : un claude d'une autre version est refuse ---------------
WS3=$(python3 runtime/new-run.py tf version --mode production 2>/dev/null)
mkdir -p "$TMP/bin"; printf '#!/bin/sh\necho "9.9.9 (Claude Code)"\n' > "$TMP/bin/claude"; chmod +x "$TMP/bin/claude"
FLUX_CLAUDE_BIN="$TMP/bin/claude" python3 runtime/run-node.py tf etape --workspace "$WS3" > "$TMP/p-ver.log" 2>&1; RCV=$?
grep -q 'attendue' "$TMP/p-ver.log" && [ "$RCV" -eq 3 ] && ok "version differente refusee avec message clair (rc=3)" || rate "version" "rc=$RCV $(cat "$TMP/p-ver.log")"
FLUX_IGNORER_VERSION=1 FLUX_CLAUDE_BIN="$TMP/bin/claude" python3 runtime/run-node.py tf etape --workspace "$WS3" --dry-run > "$TMP/p-ver2.log" 2>&1
grep -q 'DRY-RUN' "$TMP/p-ver2.log" && ok "FLUX_IGNORER_VERSION=1 passe outre" || rate "ignorer version" "$(cat "$TMP/p-ver2.log")"

echo "---"
if [ "$ECHECS" -eq 0 ]; then echo "test_session_morte : VERT"; exit 0; else echo "test_session_morte : $ECHECS echec(s)"; exit 1; fi
