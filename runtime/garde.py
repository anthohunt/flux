#!/usr/bin/env python3
"""
Flux — la garde : un petit modele qui surveille, et n'a que trois gestes (D4, § 3.4).

A chaque tick elle lit `state/<wf>/<run>/etat-compact.json` (ecrit par le
runtime : nœud courant, depuis quand, derniere passe, fin, note, tentatives,
session tmux vivante ?), pose une question courte a un modele leger, et attend
`{geste: continuer|relancer|rouge|alerter, motif}`. Puis elle applique :

  - continuer : rien ;
  - relancer  : le sequenceur relance le nœud courant (bornes de relances comprises) ;
  - rouge     : la derniere passe recoit `garde: {drapeau: "rouge", motif}` et
                le nœud le statut `rouge` ; le sequenceur decide de la suite ;
  - alerter   : notification (FLUX_NOTIF_URL, puis e-mail), meme mecanisme
                que l'ancien orchestrateur.

Elle ne touche jamais aux livrables ni aux transcripts : elle ne les voit pas.

Modele : `garde.modele` du graphe (`haiku` par defaut -> `claude -p` ; ou
`ollama:<nom>`). FLUX_GARDE_MODELE_CMD=<commande> remplace l'appel par une
commande locale qui lit le prompt sur stdin et rend le JSON (tests).

Boucle : `garde.py <wf> <ws> --boucle` = un processus lance par le sequenceur
au demarrage d'une run active, toutes les `garde.toutes_les_min` (defaut 10),
tue a la cloture. Pourquoi pas systemd : l'utilisateur `claude` n'a pas sudo
pour poser une unite systeme, et `systemctl --user` n'a pas de lingering ici
— un timer utilisateur mourrait avec la session ssh qui l'a cree. Un
processus detache dont le pid est ecrit dans run.json fait le travail, se
voit dans `ps`, et se tue par son pid.

Usage :
  garde.py <workflow-id> <workspace>              un tick
  garde.py <workflow-id> <workspace> --boucle     toutes les N minutes, tant que la run est active
  garde.py <workflow-id> <workspace> --dry-run    montre le prompt et la reponse, n'applique rien
"""
import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle  # noqa: E402
import orchestrateur_agent  # noqa: E402

GESTES = ('continuer', 'relancer', 'rouge', 'alerter')
SEQUENCEUR = socle.racine() / 'runtime' / 'sequenceur.py'


def log(wid, ws, msg):
    print('  garde · ' + msg, file=sys.stderr)
    try:
        with open(socle.dossier_run(wid, ws) / 'garde.log', 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (socle.now(), msg))
    except Exception:
        pass


def prompt_de(compact, wf):
    g = wf.get('garde') or {}
    c = compact.get('noeud_courant') or {}
    dp = c.get('derniere_passe') or {}
    L = ["Tu es la garde d'une run de workflow d'agents. Tu ne vois que l'etat ci-dessous. "
         "Tu ne touches a rien : tu choisis UN geste parmi quatre, et tu dis pourquoi en une phrase.",
         "",
         "Gestes : `continuer` (tout va bien, ou rien n'est anormal) · `relancer` (la passe "
         "courante est visiblement morte ou bloquee depuis trop longtemps et il reste des "
         "relances) · `rouge` (la passe a fini mais son resultat est manifestement mauvais — "
         "note sous le seuil, panne repetee) · `alerter` (un humain doit regarder : run bloquee, "
         "pannes en serie, relances epuisees, rien ne bouge depuis longtemps).",
         "",
         "Regles : une session tmux vivante avec une passe de moins de %d minutes -> continuer. "
         "Une passe sans fin, session morte -> relancer. Un statut de run `bloque` -> alerter. "
         "Dans le doute -> continuer : une relance inutile coute une passe entiere."
         % int(g.get('patience_min') or 90),
         "",
         "Etat :",
         "```json",
         json.dumps({'mode': compact.get('mode'), 'statut_run': compact.get('statut_run'),
                     'noeud_courant': c.get('noeud'), 'statut_noeud': c.get('statut'),
                     'passes_du_noeud': c.get('passes'),
                     'derniere_passe': dp, 'a': compact.get('a'),
                     'noeuds': [{'noeud': n['noeud'], 'statut': n['statut'], 'passes': n['passes']}
                                for n in compact.get('noeuds') or []]},
                    ensure_ascii=False, indent=1),
         "```",
         "",
         'Reponds UNIQUEMENT par un JSON : {"geste": "continuer|relancer|rouge|alerter", "motif": "<une phrase>"}']
    return '\n'.join(L)


def demander(prompt, modele, ws):
    cmd = os.environ.get('FLUX_GARDE_MODELE_CMD')
    if cmd:
        r = subprocess.run(cmd, shell=True, input=prompt, capture_output=True, text=True, timeout=120)
        return r.stdout
    sys.path.insert(0, str(socle.racine() / 'runtime'))
    from moteurs import api
    schema = {'type': 'object', 'required': ['geste', 'motif'], 'additionalProperties': False,
              'properties': {'geste': {'type': 'string', 'enum': list(GESTES)},
                             'motif': {'type': 'string'}}}
    texte, _ = api.appeler(prompt, modele, schema, ws)
    return texte


def lire_geste(txt):
    sys.path.insert(0, str(socle.racine() / 'runtime'))
    from moteurs import api
    try:
        d = api.extraire_json(txt)
    except Exception:
        return {'geste': 'continuer', 'motif': 'reponse illisible : ' + str(txt)[:120]}
    g = str(d.get('geste') or '').strip().lower()
    if g not in GESTES:
        return {'geste': 'continuer', 'motif': 'geste inconnu « %s »' % g}
    return {'geste': g, 'motif': str(d.get('motif') or '')[:300]}


def appliquer(wf, ws, compact, geste):
    wid = wf['id']
    c = compact.get('noeud_courant') or {}
    nid = c.get('noeud')
    g = geste['geste']
    if g == 'continuer':
        return
    if nid:
        _marquer_passe(wid, ws, nid, g, geste['motif'])
    if g == 'relancer' and nid:
        log(wid, ws, 'relance demandee sur %s : %s' % (nid, geste['motif']))
        _tuer_session_courante(wid, ws, nid)
        subprocess.Popen([sys.executable, str(SEQUENCEUR), wid, ws, '--noeud-fini', nid, '--sans-garde'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    elif g == 'rouge' and nid:
        log(wid, ws, 'rouge pose sur %s : %s' % (nid, geste['motif']))
        f = socle.dossier_run(wid, ws) / (nid + '.json')
        st = socle.lire_json(f, {}) or {}
        if st.get('statut') != 'en-cours':
            st['statut'] = 'rouge'
            st['updated'] = socle.now()
            socle.ecrire_json(f, st)
            subprocess.Popen([sys.executable, str(SEQUENCEUR), wid, ws, '--noeud-fini', nid, '--sans-garde'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    elif g == 'alerter':
        n = socle.noeud(wf, nid) if nid else None
        etape = '%s. %s' % (n.get('n'), n.get('name')) if n else (compact.get('statut_run') or 'run')
        ok = socle.notifier(wid, ws, etape, 'Garde : ' + geste['motif'])
        log(wid, ws, 'alerte %s : %s' % ('envoyee' if ok else 'NON envoyee', geste['motif']))


def _marquer_passe(wid, ws, nid, drapeau, motif):
    """Le drapeau de la garde sur la derniere passe du nœud — ce que la console montre."""
    f = socle.dossier_run(wid, ws) / (nid + '.json')
    st = socle.lire_json(f, {}) or {}
    ps = st.get('passages') or []
    if not ps:
        return
    ps[-1]['garde'] = {'drapeau': drapeau, 'motif': motif, 'a': socle.now()}
    st['passages'] = ps
    socle.ecrire_json(f, st)


def _tuer_session_courante(wid, ws, nid):
    """Une passe qu'on relance ne doit pas rester ouverte : sa session tmux est
    fermee PAR SON NOM, et son lanceur constate la fin (session morte = panne)."""
    st = socle.lire_json(socle.dossier_run(wid, ws) / (nid + '.json'), {}) or {}
    ps = st.get('passages') or []
    sess = ps[-1].get('session') if ps else None
    if sess:
        subprocess.call(['tmux', 'kill-session', '-t', sess], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)


def tick(wf, ws, dry=False):
    wid = wf['id']
    compact = socle.ecrire_etat_compact(wid, ws)
    modele = (wf.get('garde') or {}).get('modele') or 'haiku'
    # LA RONDE : le meme orchestrateur, reveille par le temps et non par une
    # fin d'etape. Ses gestes de ronde se lisent en gestes de garde :
    # attendre -> continuer, relancer -> relancer, bloquer -> alerter,
    # lancer/terminer -> on passe la main au sequenceur.
    if orchestrateur_agent.actif() and not os.environ.get('FLUX_GARDE_MODELE_CMD'):
        c = compact.get('noeud_courant') or {}
        faits = {'declencheur': 'ronde', 'noeud_courant': c.get('noeud'), 'statut_noeud': c.get('statut'),
                 'depuis_s': (c.get('derniere_passe') or {}).get('depuis_s'),
                 'regle': {'geste': 'attendre', 'noeud': c.get('noeud'), 'motif': 'ronde : rien d\'anormal'}}
        d = orchestrateur_agent.decider(wf, ws, faits, 'ronde')
        geste = {'geste': {'attendre': 'continuer', 'relancer': 'relancer', 'bloquer': 'alerter',
                           'lancer': 'continuer', 'terminer': 'continuer'}.get(d.get('geste'), 'continuer'),
                 'motif': d.get('motif') or ''}
        # GARDE-FOU DU PROGRAMME : une ronde ne relance pas une passe qui tourne
        # encore et n'a pas depasse la patience — sinon elle tue un appel en
        # cours (vu le 05/09 : un appel Ollama qui attendait son delai a ete
        # abattu par la ronde, et compte comme une panne de plus).
        dp = c.get('derniere_passe') or {}
        patience_s = 60 * float((wf.get('garde') or {}).get('patience_min') or 90)
        if geste['geste'] == 'relancer' and dp.get('session_vivante') and (dp.get('depuis_s') or 0) < patience_s:
            log(wid, ws, 'ronde : relance refusee, la passe tourne depuis %ss (< patience %ss)' % (int(dp.get('depuis_s') or 0), int(patience_s)))
            geste = {'geste': 'continuer', 'motif': 'passe en cours, on la laisse finir (' + geste['motif'] + ')'}
        if d.get('geste') in ('lancer', 'terminer'):
            subprocess.Popen([sys.executable, str(SEQUENCEUR), wid, ws, '--reprendre', '--sans-garde'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        prompt = prompt_de(compact, wf)
        try:
            txt = demander(prompt, modele, ws)
        except Exception as e:
            log(wid, ws, 'modele injoignable (%s) : continuer' % e)
            return {'geste': 'continuer', 'motif': 'modele injoignable'}
        geste = lire_geste(txt)
    log(wid, ws, '%s — %s' % (geste['geste'], geste['motif']))
    try:
        with open(socle.dossier_run(wid, ws) / 'garde.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps({'a': socle.now(), 'modele': modele, **geste,
                                'noeud': (compact.get('noeud_courant') or {}).get('noeud')},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass
    if dry:
        print(prompt)
        print(json.dumps(geste, ensure_ascii=False))
        return geste
    appliquer(wf, ws, compact, geste)
    return geste


def boucle(wf, ws):
    wid = wf['id']
    minutes = float((wf.get('garde') or {}).get('toutes_les_min') or 10)
    minutes = float(os.environ.get('FLUX_GARDE_MIN') or minutes)
    log(wid, ws, 'boucle toutes les %.1f min (pid %d)' % (minutes, os.getpid()))
    stop = {'v': False}

    def _stop(*_):
        stop['v'] = True
    signal.signal(signal.SIGTERM, _stop)
    while not stop['v']:
        t0 = time.time()
        run = socle.lire_run(wid, ws)
        if run.get('statut') in ('terminee', 'close'):
            log(wid, ws, 'run %s : la garde s\'eteint' % run['statut'])
            break
        if not (pathlib.Path(ws) / 'state' / 'autopilote').exists():
            log(wid, ws, 'autopilote eteint : la garde s\'eteint')
            break
        try:
            tick(wf, ws)
        except Exception as e:
            log(wid, ws, 'tick en erreur : %s' % e)
        reste = minutes * 60 - (time.time() - t0)
        while reste > 0 and not stop['v']:
            time.sleep(min(5, reste))
            reste -= 5
    try:
        if socle.lire_run(wid, ws).get('garde_pid') == os.getpid():
            socle.ecrire_run(wid, ws, garde_pid=None)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workflow')
    ap.add_argument('workspace')
    ap.add_argument('--boucle', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    wf = socle.charger_graphe(a.workflow)
    if not wf:
        print('workflow introuvable : ' + a.workflow, file=sys.stderr)
        return 3
    ws = os.path.abspath(a.workspace)
    if a.boucle:
        boucle(wf, ws)
        return 0
    g = tick(wf, ws, dry=a.dry_run)
    print(json.dumps(g, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
