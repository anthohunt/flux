#!/usr/bin/env python3
"""Joue un LOT de cas sur un ou plusieurs MODELES, en mode banc, et ecrit la campagne.

    python3 tools/jouer-lot.py agenda --lot defi --modeles ollama:qwen3.5:4b,opus --nom descente-4b
    python3 tools/jouer-lot.py agenda --lot boucle --modeles ollama:qwen3.5:9b --essais 2

Un lot est un ensemble de cas declares dans cas/cas.json (`lot`: boucle | defi |
sentinelle ; le lot de mesure ne se joue pas d'ici). Chaque (cas, modele, essai)
est une run de banc ordinaire (new-run.py + run-node.py), donc visible dans la
console comme n'importe quelle run. Le resultat agrege va dans
state/<wf>/campagnes/<nom>.json : une ligne par run (cas, modele, note, duree,
jetons, cout, chemin de la run) et la moyenne par modele. C'est ce que la
console montre comme « campagne », et ce que la descente de modeles compare.

Le nom de la run est `<nom>-<cas>-<modele>` : la campagne se retrouve depuis
la run, et la run depuis la campagne.
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

def _socle():
    sys.path.insert(0, str(FLUX / 'runtime'))
    import socle
    return socle


FLUX = pathlib.Path(__file__).resolve().parent.parent


def cas_du_lot(lot):
    carte = json.loads((FLUX / 'cas' / 'cas.json').read_text(encoding='utf-8'))
    ids = sorted(c['id'] for c in carte['cas'] if c.get('lot') == lot)
    if not ids:
        sys.exit('aucun cas dans le lot %r' % lot)
    return ids


def slug(m):
    return re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')


def chaine(wf, depart):
    """Les ids des noeuds a jouer, du depart jusqu'au bout des `suivant`."""
    par_id = {n['id']: n for n in wf['nodes']}
    ids, cur = [], depart
    while cur and cur not in ids and cur in par_id:
        ids.append(cur)
        cur = (par_id[cur].get('suivant') or [None])[0]
    return ids


def preparer_moteur(modele):
    """Un modele `llamacpp:<nom>` se sert sur sa machine AVANT de jouer : `servir` verifie qu'il
    rentre dans la VRAM (rentre), le descend s'il manque et est au catalogue, bascule le
    llama-server et attend qu'il reponde. Rend None si tout va bien, sinon le message d'erreur
    (les runs de ce modele sont alors marquees `refus` sans etre jouees)."""
    if not modele.startswith('llamacpp:'):
        return None
    sys.path.insert(0, str(FLUX / 'runtime'))
    import socle
    cfg = (socle.charger_moteurs().get('moteurs_api') or {}).get('llamacpp') or {}
    hote = cfg.get('ssh')
    nom = modele.split(':', 1)[1]
    if not hote:
        return None   # pas de machine declaree : on joue le modele que le serveur sert deja
    print('moteur : servir %s sur %s …' % (nom, hote), flush=True)
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', hote, 'servir', nom],
                       capture_output=True, text=True, timeout=1800)
    sortie = (r.stdout + r.stderr).strip()
    print('  ' + sortie.replace('\n', '\n  '), flush=True)
    return None if r.returncode == 0 else ('servir %s : %s' % (nom, sortie[-300:]))


def jouer(wf, cas, modele, nom, noeuds):
    """Une run de banc : joue les noeuds dans l'ordre, dans le meme workspace ; rend le dict
    de resultat (note du DERNIER noeud, durees et jetons cumules, chemin)."""
    r = subprocess.run([sys.executable, str(FLUX / 'runtime' / 'new-run.py'), wf, '%s-%s-%s' % (nom, cas, slug(modele)),
                        '--mode', 'banc', '--cas', cas, '--champ', 'modele=%s' % modele],
                       capture_output=True, text=True)
    ws = (r.stdout.strip().splitlines() or [''])[-1]
    if not ws or not pathlib.Path(ws).is_dir():
        return {'cas': cas, 'modele': modele, 'statut': 'refus', 'erreur': (r.stdout + r.stderr)[-300:]}
    t0 = time.time()
    run = pathlib.Path(ws).name
    out = {'cas': cas, 'modele': modele, 'run': run, 'workspace': ws, 'noeuds': list(noeuds),
           'duree_s': 0.0, 'jetons_entree': 0, 'jetons_sortie': 0, 'cout_usd': 0.0, 'essais': 0}
    for noeud in noeuds:
        subprocess.run([sys.executable, str(FLUX / 'runtime' / 'run-node.py'), wf, noeud, '--workspace', ws],
                       capture_output=True, text=True)
        etat = FLUX / 'state' / wf / run / (noeud + '.json')
        try:
            e = json.loads(etat.read_text(encoding='utf-8'))
            p = e['passages'][-1]
            n = p.get('note') or {}
            mes = p.get('mesures') or {}
            out.update({'statut': e.get('statut'), 'fin': p.get('fin'), 'score': n.get('score'), 'bareme': n.get('bareme'),
                        'panne': (p.get('panne') or {}).get('cause')})
            out['duree_s'] = round(out['duree_s'] + (mes.get('duree_s') or 0), 1)
            for k in ('jetons_entree', 'jetons_sortie', 'cout_usd'):
                out[k] = out[k] + (mes.get(k) or 0)
            out['essais'] += len(p.get('essais') or [])
        except Exception as ex:
            out.update({'statut': 'illisible', 'erreur': str(ex)[:200]})
            break
        if out.get('panne') or out.get('statut') in ('bloque-entree', 'illisible'):
            break   # le noeud suivant n'aurait pas son entree
    out['duree_totale_s'] = round(time.time() - t0, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('wf')
    ap.add_argument('--lot', default='defi')
    ap.add_argument('--modeles', required=True, help='liste separee par des virgules')
    ap.add_argument('--essais', type=int, default=1)
    ap.add_argument('--nom', default=None)
    ap.add_argument('--noeud', default=None, help='id du noeud a jouer (defaut : le premier du graphe)')
    ap.add_argument('--cas', default=None, help='sous-ensemble de cas, separes par des virgules')
    a = ap.parse_args()
    wf = json.loads(_socle().fichier_workflow(a.wf).read_text(encoding='utf-8'))
    noeud = a.noeud or wf['nodes'][0]['id']
    noeuds = chaine(wf, noeud)
    modeles = [m.strip() for m in a.modeles.split(',') if m.strip()]
    cas = a.cas.split(',') if a.cas else cas_du_lot(a.lot)
    nom = a.nom or ('%s-%s' % (a.lot, dt.datetime.now().strftime('%m%d-%H%M')))
    dossier = FLUX / 'state' / a.wf / 'campagnes'
    dossier.mkdir(parents=True, exist_ok=True)
    fichier = dossier / (nom + '.json')
    camp = {'nom': nom, 'workflow': a.wf, 'noeud': noeuds[-1], 'noeuds': noeuds, 'lot': a.lot, 'cas': cas, 'modeles': modeles,
            'essais': a.essais, 'debut': dt.datetime.now().isoformat(timespec='seconds'), 'runs': [], 'fin': None}
    consignes = [wf['nodes'][[n['id'] for n in wf['nodes']].index(x)].get('consignes') or [] for x in noeuds]
    import hashlib
    camp['consignes_md5'] = hashlib.md5(json.dumps(consignes, ensure_ascii=False).encode()).hexdigest()[:12]

    def sauver():
        par = {}
        for r in camp['runs']:
            if r.get('score') is not None:
                par.setdefault(r['modele'], []).append(r)
        camp['bilan'] = {m: {'moyenne': round(statistics.mean(x['score'] for x in rs), 2),
                             'n': len(rs),
                             'pannes': sum(1 for r in camp['runs'] if r['modele'] == m and r.get('score') is None),
                             's_par_mail': round(statistics.mean(x['duree_s'] or 0 for x in rs), 1),
                             'jetons_entree': round(statistics.mean(x['jetons_entree'] or 0 for x in rs)),
                             'cout_usd': round(sum(x['cout_usd'] or 0 for x in rs), 4)}
                        for m, rs in par.items()}
        fichier.write_text(json.dumps(camp, ensure_ascii=False, indent=1), encoding='utf-8')

    sauver()
    for m in modeles:
        panne_moteur = preparer_moteur(m)
        for c in cas:
            for k in range(a.essais):
                if panne_moteur:
                    r = {'cas': c, 'modele': m, 'statut': 'refus', 'erreur': panne_moteur}
                else:
                    r = jouer(a.wf, c, m, nom if a.essais == 1 else '%s-e%d' % (nom, k + 1), noeuds)
                r['essai'] = k + 1
                camp['runs'].append(r)
                print('%-9s %-22s %s' % (c, m, r.get('score') if r.get('score') is not None else 'PANNE/' + str(r.get('panne') or r.get('statut'))), flush=True)
                sauver()
    camp['fin'] = dt.datetime.now().isoformat(timespec='seconds')
    sauver()
    print('\ncampagne :', fichier)
    for m, b in camp['bilan'].items():
        print('%-22s %6.2f /72  %2d runs  %d panne(s)  %5.1f s/mail' % (m, b['moyenne'], b['n'], b['pannes'], b['s_par_mail']))


if __name__ == '__main__':
    main()
