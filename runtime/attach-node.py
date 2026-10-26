#!/usr/bin/env python3
"""
Flux — rattacher un superviseur à un nœud dont la session tourne encore.

Cas d'usage : le process run-node.py a disparu (redémarrage du service, kill)
alors que la session tmux de l'agent, elle, est toujours vivante. Sans
superviseur, personne ne passera le gate de sortie ni n'écrira l'état : le
nœud resterait « en-cours » pour toujours. Ce script reprend la veille là où
elle s'est arrêtée — il ne relance AUCUN agent.

Usage : attach-node.py <workflow> <node> --workspace DIR [--session NAME]
"""
import sys, os, json, time, hashlib, argparse, pathlib, subprocess, importlib.util

FLUX = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('runnode', FLUX / 'runtime' / 'run-node.py')
rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)

ap = argparse.ArgumentParser()
ap.add_argument('workflow'); ap.add_argument('node')
ap.add_argument('--workspace', required=True)
ap.add_argument('--session', default=None)
a = ap.parse_args()

wf = rn.load_wf(a.workflow); node = rn.node_of(wf, a.node); ws = os.path.abspath(a.workspace)
ndir = FLUX / 'state' / wf['id'] / os.path.basename(ws) / a.node
sess = a.session or f'flux-{wf["id"]}-{a.node}'
out = ndir / 'run.out'
# dossier de la passe courante : c'est la que vit l'enregistrement des ecrans
try:
    _st = json.loads((ndir.parent / f'{a.node}.json').read_text(encoding='utf-8'))
    _n = (_st.get('passages') or [{}])[-1].get('n') or 1
except Exception:
    _n = 1
pdir = ndir / 'passes' / str(_n)
_passages = (_st.get('passages') if isinstance(_st, dict) else None) or []
pdir.mkdir(parents=True, exist_ok=True)

if subprocess.call(['tmux', 'has-session', '-t', sess], stderr=subprocess.DEVNULL) != 0:
    print(f'session « {sess} » absente — rien à rattacher', file=sys.stderr); sys.exit(3)
print(f'  ↻ superviseur rattaché à {sess}', file=sys.stderr)

t0 = time.time(); last_h = None; stable_since = time.time(); fin = 'session-morte'
while True:
    if subprocess.call(['tmux', 'has-session', '-t', sess], stderr=subprocess.DEVNULL) != 0:
        break
    snap = rn.capture(sess, full=True)
    if snap: rn.archiver_ecran(pdir, snap)
    if snap: out.write_text(snap, encoding='utf-8')
    if (ndir / 'DONE').exists() and time.time() - stable_since > getattr(rn, 'GRACE_S', 45):
        fin = 'sentinelle'; break
    h = hashlib.md5(snap.encode('utf-8', 'replace')).hexdigest()
    if h != last_h: last_h = h; stable_since = time.time()
    elif time.time() - stable_since > rn.IDLE_S: fin = f'ecran-fige-{rn.IDLE_S}s'; break
    if time.time() - t0 > rn.MAX_S: fin = 'plafond-temps'; break
    time.sleep(5)

snap = rn.capture(sess, full=True)
if snap: out.write_text(snap, encoding='utf-8')
subprocess.call(['tmux', 'kill-session', '-t', sess], stderr=subprocess.DEVNULL)

ok, errs = rn.gate((node.get('gate') or {}).get('validateur'), ws)
restants = [d for d in node.get('livrables', []) if not rn.present(ws, d)]
passage = {'n': 1, 'session': sess, 'motif': 'superviseur rattaché', 'fin': fin,
           'ended': rn.now(), 'duree_s': int(time.time() - t0),
           'verdict': 'vert' if (ok and not restants) else 'rouge',
           'erreurs': [] if (ok and not restants) else errs[:20]}
if ok and not restants:
    rn.write_state(wf, ws, a.node, statut='vert', ended=rn.now(), erreurs=[], passages=(_passages or [passage]))
    print('  ✓ GATE SORTIE — vert', file=sys.stderr); sys.exit(0)
rn.write_state(wf, ws, a.node, statut='rouge', ended=rn.now(), erreurs=errs[:20],
               livrables_manquants=restants, passages=(_passages or [passage]))
print(f'  ✗ GATE SORTIE — rouge ({len(errs)} contrat(s), {len(restants)} livrable(s))', file=sys.stderr)
sys.exit(1)
