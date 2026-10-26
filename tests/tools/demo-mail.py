#!/usr/bin/env python3
"""Monte (ou demonte) la DEMO « traiter un mail » pour les captures de la console v2.

La console ne rend que ce que le graphe et les passes declarent : pour montrer
la note, le drapeau de la garde, l'entree → sortie, le mode et le cas de banc,
il faut un graphe qui les porte et des passes v2 qui les ecrivent. Aucun moteur
api n'existe encore au moment de T7/T8 : cette demo FABRIQUE l'etat a la main,
au format du contrat v2 (§ 3.2 du design), sans rien jouer.

  demo-mail.py monter    -> workflows/mail-demo.json (copie de tests/fixtures/mail-demo/),
                            deux runs (banc, production) sous ~/mail-demo-runs/,
                            leur etat sous state/mail-demo/ ; imprime les noms des runs
  demo-mail.py demonter  -> retire tout ce qui precede (cas/ crees par « valider » compris,
                            s'ils portent l'origine de la demo)

Rien de ceci n'est commite : workflows/mail-demo.json est retire au demontage,
state/ est ignore par git, les workspaces vivent hors du depot.
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FLUX = Path(__file__).resolve().parent.parent.parent
FIX = FLUX / 'tests' / 'fixtures' / 'mail-demo'
WF = FLUX / 'workflows' / 'mail-demo.json'
ROOT = Path.home() / 'mail-demo-runs'
STATE = FLUX / 'state' / 'mail-demo'
CAS = FLUX / 'cas' / 'mail-01'


def iso(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def passe(n, t0, secs, verdict, note, garde, entree, sortie, modele, moteur_type, tokens):
    t1 = t0 + timedelta(seconds=secs)
    etat = {'n': n, 'started': iso(t0), 'ended': iso(t1), 'duree_s': secs, 'fin': 'sentinelle',
            'verdict': verdict, 'motif': 'premier lancement' if n == 1 else 'relance par la garde',
            'moteur': moteur_type}
    v2 = {'n': n, 'started': iso(t0), 'ended': iso(t1), 'fin': 'sentinelle', 'moteur': moteur_type,
          'modele': modele, 'mesures': {'jetons_entree': tokens[0], 'jetons_sortie': tokens[1],
                                        'duree_s': secs, 'cout_usd': tokens[2]},
          'entree': entree, 'sortie': sortie, 'note': note, 'garde': garde}
    return etat, v2


def ecrire_run(nom, mode, cas, t0, passes_par_noeud):
    ws = ROOT / nom
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'WORKSPACE.txt').write_text(
        f'cas: {cas or ""}\ntimestamp: {nom}\nbase: {ws}\ncreated_at: {iso(t0)}\nmode: {mode}\n')
    shutil.copy(CAS / 'mail.md', ws / 'mail.md')
    shutil.copy(CAS / 'golden-formulaire.json', ws / 'resultat.json')
    (ws / 'actions.json').write_text(json.dumps(json.load(open(CAS / 'golden-actions.json')), indent=2) + '\n')
    st = STATE / nom
    st.mkdir(parents=True, exist_ok=True)
    (st / 'run.json').write_text(json.dumps({'mode': mode, 'statut': 'terminee', 'ouvert': iso(t0),
                                             'champs': {'cas': cas}}, indent=2) + '\n')
    for nid, passes in passes_par_noeud.items():
        (st / nid / 'passes').mkdir(parents=True, exist_ok=True)
        passages = []
        for etat, v2 in passes:
            passages.append(etat)
            (st / nid / 'passes' / f'{etat["n"]}.json').write_text(json.dumps(v2, indent=2, ensure_ascii=False) + '\n')
        dernier = passages[-1]
        (st / f'{nid}.json').write_text(json.dumps({
            'statut': 'fait' if dernier.get('verdict') == 'vert' else 'rouge', 'node': nid,
            'started': passages[0]['started'], 'ended': dernier['ended'],
            'passage_courant': dernier['n'], 'passages': passages, 'erreurs': []}, indent=2) + '\n')
    return ws


def monter():
    shutil.copy(FIX / 'workflow.json', WF)
    t = datetime(2026, 9, 4, 15, 2, tzinfo=timezone.utc)
    # RUN 1 — banc, mail-01 : le petit modele rate deux fois sur la date, Opus reussit
    banc = ecrire_run('mail-mail-01-2026-09-04-150200', 'banc', 'mail-01', t, {
        'formulaire': [
            passe(1, t + timedelta(seconds=60), 38, 'rouge',
                  {'score': 6.5, 'bareme': 20, 'detail': '5 champs faux : rdv_date garde la date obsolete du 1er message'},
                  {'drapeau': 'rouge', 'motif': 'date obsolète gardée — relance'},
                  ['mail.md'], ['resultat.json'], 'ollama:qwen3:4b', 'api', (9800, 2100, 0)),
            passe(2, t + timedelta(seconds=120), 41, 'rouge',
                  {'score': 8.5, 'bareme': 20, 'detail': '4 champs faux : rdv_date, rdv_heure, confirmation_date_limite, cout'},
                  {'drapeau': 'rouge', 'motif': 'date obsolète gardée — relances épuisées, passage à opus'},
                  ['mail.md'], ['resultat.json'], 'ollama:qwen3:4b', 'api', (11200, 2600, 0)),
            passe(3, t + timedelta(seconds=240), 22, 'vert',
                  {'score': 20, 'bareme': 20, 'detail': '20 champs justes sur 20'},
                  {'drapeau': 'vert', 'motif': 'rien à signaler'},
                  ['mail.md'], ['resultat.json'], 'opus', 'api', (12400, 2900, 0.11)),
        ],
        'decider': [
            passe(1, t + timedelta(seconds=300), 25, 'vert',
                  {'score': 9, 'bareme': 10, 'detail': '5 actions, 1 date à un jour près'},
                  {'drapeau': 'vert', 'motif': 'rien à signaler'},
                  ['resultat.json'], ['actions.json'], 'opus', 'api', (7100, 1900, 0.07)),
        ],
    })
    # RUN 2 — production, mail neuf : pas de note, la relecture humaine juge
    t2 = t + timedelta(minutes=40)
    prod = ecrire_run('mail-neuf-2026-09-04-154200', 'production', None, t2, {
        'formulaire': [passe(1, t2 + timedelta(seconds=30), 24, 'vert', None,
                             {'drapeau': 'vert', 'motif': 'rien à signaler'},
                             ['mail.md'], ['resultat.json'], 'opus', 'api', (12100, 2800, 0.10))],
        'decider': [passe(1, t2 + timedelta(seconds=90), 21, 'vert', None,
                          {'drapeau': 'vert', 'motif': 'rien à signaler'},
                          ['resultat.json'], ['actions.json'], 'opus', 'api', (7000, 1800, 0.07))],
    })
    print(banc.name)
    print(prod.name)


def demonter():
    if WF.exists():
        WF.unlink()
    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    for d in (FLUX / 'cas').glob('*'):
        f = d / 'cas.json'
        try:
            if json.loads(f.read_text()).get('workflow') == 'mail-demo':
                shutil.rmtree(d)
                print('retire', d)
        except Exception:
            pass
    print('demo retiree')


if __name__ == '__main__':
    {'monter': monter, 'demonter': demonter}[sys.argv[1] if len(sys.argv) > 1 else 'monter']()
