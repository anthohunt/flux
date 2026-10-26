#!/usr/bin/env python3
"""Un TEST = les runs de la suite declaree par le graphe (`test.suite`), jouees
A LA SUITE sur le meme cas, dans flux (par l'API de la console), et rangees
ensemble : state/<wf>/tests/<id>.json.

usage : jouer-test.py <workflow-id> <cas> [<id-du-test>] [--hote http://127.0.0.1:8901]

Chaque run de la suite s'ouvre en banc avec `--champ test=<id>` (run.json en
garde la trace), l'autopilote l'enchaine, et on attend qu'elle soit terminee
ou bloquee avant la suivante — les durees ne se marchent pas dessus, et la
console montre les quatre l'une apres l'autre.
"""
import json, os, pathlib, subprocess, sys, time, urllib.request, datetime

def _socle():
    sys.path.insert(0, str(FLUX / 'runtime'))
    import socle
    return socle


FLUX = pathlib.Path(__file__).resolve().parent.parent


def api(hote, chemin, methode='GET', corps=None):
    req = urllib.request.Request(hote + chemin, method=methode,
                                 data=json.dumps(corps).encode() if corps is not None else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or '{}')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    hote = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--hote=')), 'http://127.0.0.1:8901')
    if len(args) < 2:
        sys.exit(__doc__)
    wid, cas = args[0], args[1]
    tid = args[2] if len(args) > 2 else 'test-' + datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
    wf = json.loads(_socle().fichier_workflow(wid).read_text(encoding='utf-8'))
    suite = ((wf.get('test') or {}).get('suite')) or [{'titre': 'workflow', 'workflow': wid, 'modele': None}]
    dossier = FLUX / 'state' / wid / 'tests'; dossier.mkdir(parents=True, exist_ok=True)
    f = dossier / (tid + '.json')
    etat = {'id': tid, 'workflow': wid, 'cas': cas, 'debut': datetime.datetime.now().isoformat(timespec='seconds'),
            'suite': [{'titre': s.get('titre') or s.get('workflow'), 'workflow': s.get('workflow') or wid,
                       'modele': s.get('modele'), 'run': None, 'statut': 'a-venir'} for s in suite]}
    ecrire = lambda: f.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding='utf-8')
    ecrire()
    for i, s in enumerate(etat['suite']):
        w = s['workflow']
        corps = {'cas': cas, 'test': tid}
        if s['modele']:
            corps['modele'] = s['modele']
        try:
            rep = api(hote, f'/api/wf/{w}/nouvelle-run?mode=banc', 'POST', corps)
            run = rep['workspace']
        except Exception as e:
            s['statut'] = 'ouverture-refusee'; s['erreur'] = str(e)[:300]; ecrire(); continue
        s['run'] = run; s['statut'] = 'en-cours'; ecrire()
        g = json.loads(_socle().fichier_workflow(w).read_text(encoding='utf-8'))
        premier = g['nodes'][0]['id']
        api(hote, f'/api/wf/{w}/autopilote?on=1&ws={run}', 'POST')
        lance = None
        for essai in range(3):
            try:
                lance = api(hote, f'/api/wf/{w}/run/{premier}?ws={run}', 'POST')
            except Exception as e:
                lance = {'error': str(e)[:200]}
            if lance.get('started'):
                break
            time.sleep(10)
        t0 = time.time()
        while time.time() - t0 < 1200:
            time.sleep(5)
            try:
                st = json.loads((FLUX / 'state' / w / run / 'run.json').read_text(encoding='utf-8')).get('statut')
            except Exception:
                st = None
            if st in ('terminee', 'bloque', 'close'):
                break
        s['statut'] = st or 'delai-depasse'; ecrire()
    etat['fin'] = datetime.datetime.now().isoformat(timespec='seconds'); ecrire()
    print(tid)


if __name__ == '__main__':
    main()
