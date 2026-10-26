#!/usr/bin/env python3
"""Lanceur commun des juges (design v2, section 3.3). Banc seulement.

    juge.py <script> <corrige> <sortie>... [--bareme N] [--cas DIR] [--workspace DIR]
    juge.py --noeud <graphe.json> <id-noeud> --cas DIR --workspace DIR

Un juge est un script SANS modele, appele `script <corrige> <sortie>...`, qui
ecrit sur stdout un JSON {score, bareme, lignes[]}. Ce lanceur resout les
chemins, l'execute, valide sa reponse et la rend telle quelle (JSON sur
stdout, exit 0). Tout ce qui empeche de noter — script introuvable, sortie
absente, JSON illisible, score hors bareme, bareme different de celui que le
graphe declare — est une PANNE : message clair sur stderr, JSON
{"panne": ..., "score": null, "bareme": N, "lignes": []} sur stdout, exit 2.
Une passe en banc qui n'a pas de note ne passe pas.

Conventions de chemins (le runtime les respecte, la console les affiche) :
  - `script`   : relatif a la RACINE DU DEPOT flux (parent de runtime/), ex.
                 runtime/juges/formulaire.py ;
  - `corrige`  : relatif au DOSSIER DU CAS de la run, cas/<cas>/ (--cas), ex.
                 golden-formulaire.json -> cas/mail-01/golden-formulaire.json ;
                 `-` pour un juge sans corrige (presence) ;
  - `sortie`   : relatif au WORKSPACE de la run (--workspace), ex. resultat.json ;
  - un chemin absolu est pris tel quel ; sans --cas ni --workspace, le
    dossier courant sert de base.

Le bareme declare par le graphe est passe au script en `--bareme=N` et dans
FLUX_BAREME ; un script a bareme fixe (formulaire : 20, actions : 10) doit
rendre ce meme bareme, sinon le graphe ment et la passe est en panne.

Forme --noeud : lit le noeud dans le graphe et applique sa declaration —
`juge: {type: presence}` (runtime/juges/presence.py sur tous les livrables),
`juge: {script, corrige, bareme}` (sur tous les livrables), ou `juges: [...]`
(chaque juge sur sa `sortie` ; la note rendue est la somme des scores sur la
somme des baremes, `lignes` prefixees par la sortie, et `juges[]` garde le
detail). C'est la fonction `juger_noeud()` que le runtime appelle en Python.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ICI = pathlib.Path(__file__).resolve().parent
RACINE = ICI.parent
PRESENCE = 'runtime/juges/presence.py'
BAREME_PRESENCE = 20
DELAI_S = 120


class JugeEnPanne(Exception):
    """Le juge n'a pas pu noter. Le message dit pourquoi, en une ligne."""


def _resoudre(chemin, base):
    p = pathlib.Path(chemin)
    if p.is_absolute():
        return p
    return (pathlib.Path(base) if base else pathlib.Path.cwd()) / p


def juger(script, corrige, sorties, bareme=None, cas=None, workspace=None, racine=RACINE, delai=DELAI_S):
    """Execute un juge et rend {score, bareme, lignes}. Leve JugeEnPanne."""
    if not sorties:
        raise JugeEnPanne('aucune sortie a juger')
    p_script = _resoudre(script, racine)
    if not p_script.is_file():
        raise JugeEnPanne('juge introuvable : %s (relatif a %s)' % (script, racine))
    if corrige and corrige != '-':
        p_corrige = _resoudre(corrige, cas)
        if not p_corrige.is_file():
            raise JugeEnPanne('corrige introuvable : %s (cherche dans %s)' % (corrige, cas or os.getcwd()))
    else:
        p_corrige = pathlib.Path('-')
    p_sorties = [_resoudre(s, workspace) for s in sorties]
    if p_script.name != pathlib.Path(PRESENCE).name:
        # presence.py juge lui-meme l'absence ; les autres juges ont besoin du fichier
        for s, p in zip(sorties, p_sorties):
            if not p.exists():
                raise JugeEnPanne('sortie absente : %s (cherche dans %s) — l\'etape n\'a pas rendu son livrable'
                                  % (s, workspace or os.getcwd()))
    cmd = [sys.executable, str(p_script), str(p_corrige)] + [str(p) for p in p_sorties]
    env = dict(os.environ)
    if bareme is not None:
        cmd.append('--bareme=%s' % bareme)
        env['FLUX_BAREME'] = str(bareme)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=delai, env=env)
    except subprocess.TimeoutExpired:
        raise JugeEnPanne('juge %s : plus de %d s sans reponse' % (script, delai))
    if r.returncode != 0:
        raise JugeEnPanne('juge %s : exit %d — %s' % (script, r.returncode, (r.stderr or r.stdout).strip()[-400:]))
    try:
        rep = json.loads(r.stdout)
    except ValueError:
        raise JugeEnPanne('juge %s : stdout n\'est pas du JSON — %r' % (script, r.stdout.strip()[:200]))
    return valider_reponse(rep, script, bareme)


def valider_reponse(rep, script='?', bareme=None):
    if not isinstance(rep, dict):
        raise JugeEnPanne('juge %s : la reponse n\'est pas un objet' % script)
    for cle in ('score', 'bareme', 'lignes'):
        if cle not in rep:
            raise JugeEnPanne('juge %s : `%s` manquant dans la reponse' % (script, cle))
    score, b, lignes = rep['score'], rep['bareme'], rep['lignes']
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise JugeEnPanne('juge %s : score non numerique (%r)' % (script, score))
    if isinstance(b, bool) or not isinstance(b, (int, float)) or b <= 0:
        raise JugeEnPanne('juge %s : bareme invalide (%r)' % (script, b))
    if not (0 <= score <= b):
        raise JugeEnPanne('juge %s : score %s hors de [0, %s]' % (script, score, b))
    if not isinstance(lignes, list) or not all(isinstance(l, str) for l in lignes):
        raise JugeEnPanne('juge %s : `lignes` doit etre une liste de chaines' % script)
    if bareme is not None and float(b) != float(bareme):
        raise JugeEnPanne('juge %s : rend un bareme de %s, le graphe declare %s' % (script, b, bareme))
    return {'score': score, 'bareme': b, 'lignes': lignes}


def _declaration(j):
    """(script, corrige, bareme) depuis un objet juge du graphe."""
    if j.get('type') == 'presence':
        return PRESENCE, '-', j.get('bareme', BAREME_PRESENCE)
    return j['script'], j['corrige'], j['bareme']


def juger_noeud(noeud, cas=None, workspace=None, racine=RACINE):
    """Applique la declaration `juge` / `juges` d'un noeud. Rend la note de la
    passe : {score, bareme, lignes} (+ `juges[]` quand il y en a plusieurs)."""
    livrables = noeud.get('livrables') or []
    if 'juges' in noeud:
        total, bareme, lignes, detail = 0, 0, [], []
        for j in noeud['juges']:
            script, corrige, b = _declaration(j)
            sortie = j['sortie']
            rep = juger(script, corrige, [sortie], bareme=b, cas=cas, workspace=workspace, racine=racine)
            total += rep['score']
            bareme += rep['bareme']
            lignes.extend('[%s] %s' % (sortie, l) for l in rep['lignes'])
            detail.append({'sortie': sortie, 'script': script, **rep})
        return {'score': total, 'bareme': bareme, 'lignes': lignes, 'juges': detail}
    if 'juge' in noeud:
        script, corrige, b = _declaration(noeud['juge'])
        return juger(script, corrige, livrables, bareme=b, cas=cas, workspace=workspace, racine=racine)
    raise JugeEnPanne('noeud %s : ni `juge` ni `juges`' % noeud.get('id'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split('\n', 1)[1])
    ap.add_argument('args', nargs='*', help='<script> <corrige> <sortie>... ou, avec --noeud, <graphe.json> <id-noeud>')
    ap.add_argument('--noeud', action='store_true', help='juger un noeud d\'un graphe selon sa declaration juge/juges')
    ap.add_argument('--bareme', type=float, help='bareme declare par le graphe (le script doit rendre le meme)')
    ap.add_argument('--cas', help='dossier du cas de banc, base du corrige (cas/<cas>/)')
    ap.add_argument('--workspace', help='workspace de la run, base des sorties')
    a = ap.parse_args(argv)
    bareme = None
    if a.bareme is not None:
        bareme = int(a.bareme) if a.bareme == int(a.bareme) else a.bareme
    try:
        if a.noeud:
            if len(a.args) != 2:
                ap.error('--noeud attend <graphe.json> <id-noeud>')
            graphe = json.load(open(a.args[0], encoding='utf-8'))
            noeuds = [n for n in graphe.get('nodes', []) if n.get('id') == a.args[1]]
            if not noeuds:
                raise JugeEnPanne('noeud %s absent de %s' % (a.args[1], a.args[0]))
            rep = juger_noeud(noeuds[0], cas=a.cas, workspace=a.workspace)
        else:
            if len(a.args) < 3:
                ap.error('attend <script> <corrige> <sortie>...')
            rep = juger(a.args[0], a.args[1], a.args[2:], bareme=bareme, cas=a.cas, workspace=a.workspace)
    except JugeEnPanne as e:
        sys.stderr.write('PANNE %s\n' % e)
        print(json.dumps({'panne': str(e), 'score': None, 'bareme': bareme, 'lignes': []}, ensure_ascii=False))
        return 2
    print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
