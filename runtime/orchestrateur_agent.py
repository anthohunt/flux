#!/usr/bin/env python3
"""L'ORCHESTRATEUR : l'agent en charge de la run.

Un agent en charge de gerer la run : il est reveille a chaque fin d'etape (par le
sequenceur) et a chaque ronde (par la garde), il voit le graphe, l'etat de
la run et les faits que le programme a etablis (verdict de l'etape, relances
deja faites, ce que la regle ferait), et il DECIDE : lancer tel nœud,
relancer, terminer, bloquer (et alerter), ou attendre.

Le programme garde deux roles : etablir les faits (une session morte est
une panne, une note se lit dans la passe) et EXECUTER la decision — il ne
lance que des nœuds qui existent, et refuse une relance au-dela du double du
plafond, pour qu'une boucle ne tourne pas a vide.

Chaque reveil est une PASSE de l'orchestrateur, rangee dans
state/<wf>/<run>/orchestrateur.json avec ses mesures (jetons, cout, duree)
et sa decision : la console la montre dans la frise et le tableau, comme
toute passe.

Modele : `orchestrateur.modele` du graphe, sinon `garde.modele`, sinon
sonnet. `FLUX_ORCHESTRATEUR=regle` : pas d'agent, la regle decide seule
(les tests) ; `FLUX_ORCHESTRATEUR_CMD` : une commande qui joue l'agent
(prompt sur stdin, JSON sur stdout).
"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle  # noqa: E402

GESTES = ('lancer', 'relancer', 'terminer', 'bloquer', 'attendre')


def modele_de(wf):
    return ((wf.get('orchestrateur') or {}).get('modele')
            or (wf.get('garde') or {}).get('modele') or 'sonnet')


def actif():
    return os.environ.get('FLUX_ORCHESTRATEUR', '').lower() not in ('regle', '0', 'non')


def _graphe_resume(wf):
    out = []
    for n in socle.noeuds(wf):
        out.append({'noeud': n['id'], 'n': n.get('n'), 'nom': n.get('name'),
                    'suivant': n.get('suivant') or [], 'verification': bool(n.get('verification')),
                    'relances_max': n.get('relances_max')})
    return out


def prompt_de(wf, compact, faits, declencheur):
    L = ["Tu es l'ORCHESTRATEUR d'une run de workflow d'agents : tu es en charge de la run, "
         "de son debut a sa fin. Le programme a etabli les faits ci-dessous ; toi, tu decides "
         "de la suite. Tu ne modifies aucun livrable.",
         "",
         "Gestes possibles :",
         "- `lancer` <noeud> : lancer ce nœud (le suivant, en general — ou un autre si tu as une raison)",
         "- `relancer` <noeud> : rejouer un nœud dont la derniere passe est mauvaise ou morte",
         "- `terminer` : la run est finie (le dernier nœud est vert)",
         "- `bloquer` : rien de raisonnable a faire sans un humain ; on alerte l'operateur",
         "- `attendre` : une passe tourne encore, on ne fait rien",
         "",
         "Declencheur : %s" % declencheur,
         "",
         "Le graphe (les nœuds, dans l'ordre) :",
         "```json", json.dumps(_graphe_resume(wf), ensure_ascii=False), "```",
         "",
         "L'etat de la run :",
         "```json", json.dumps({k: compact.get(k) for k in ('mode', 'statut_run', 'noeud_courant', 'noeuds', 'a')},
                              ensure_ascii=False), "```",
         "",
         "Les faits etablis par le programme sur l'etape qui vient de finir :",
         "```json", json.dumps(faits, ensure_ascii=False), "```",
         "",
         "Regles de bon sens : un verdict vert -> lancer le suivant, ou terminer s'il n'y en a pas. "
         "Une panne ou un rouge -> relancer, sauf si les relances sont epuisees (alors bloquer). "
         "Une note sous le bareme n'est pas forcement un echec : en banc, elle SE MESURE ; "
         "on ne relance une etape notee que si sa note est visiblement mauvaise ET qu'il reste des relances. "
         "En cours ou attente -> attendre. Dans le doute, suis ce que la regle propose.",
         "",
         'Reponds UNIQUEMENT par un JSON : {"geste": "lancer|relancer|terminer|bloquer|attendre", '
         '"noeud": "<id ou null>", "motif": "<une phrase, en francais, pour la console>"}']
    return '\n'.join(L)


def demander(prompt, modele, ws):
    cmd = os.environ.get('FLUX_ORCHESTRATEUR_CMD')
    if cmd:
        r = subprocess.run(cmd, shell=True, input=prompt, capture_output=True, text=True, timeout=180)
        return r.stdout, {'jetons_entree': 0, 'jetons_sortie': 0, 'cout_usd': 0.0, 'duree_s': 0, 'fournisseur': 'commande'}
    from moteurs import api
    schema = {'type': 'object', 'required': ['geste', 'motif'], 'additionalProperties': False,
              'properties': {'geste': {'type': 'string', 'enum': list(GESTES)},
                             'noeud': {'type': ['string', 'null']},
                             'motif': {'type': 'string'}}}
    return api.appeler(prompt, modele, schema, ws)


def lire_decision(txt):
    from moteurs import api
    try:
        d = api.extraire_json(txt)
    except Exception:
        return None
    g = str(d.get('geste') or '').strip().lower()
    if g not in GESTES:
        return None
    return {'geste': g, 'noeud': (d.get('noeud') or None), 'motif': str(d.get('motif') or '')[:300]}


def _passe(wid, ws, modele, declencheur, faits, decision, mesures, fin, duree, erreur=None):
    """Range le reveil dans orchestrateur.json, comme une passe api."""
    f = socle.dossier_run(wid, ws) / 'orchestrateur.json'
    st = socle.lire_json(f, {}) or {}
    ps = st.get('passages') or []
    n = (ps[-1].get('n') if ps else 0) or len(ps)
    p = {'n': n + 1, 'moteur': 'api', 'modele': modele, 'compte': (mesures or {}).get('compte'),
         'started': faits.get('_a') or socle.now(), 'ended': socle.now(), 'fin': fin,
         'verdict': 'vert' if fin == 'sentinelle' else 'panne', 'declencheur': declencheur,
         'motif': (decision or {}).get('motif') or (erreur or ''),
         'decision': decision, 'proposition_regle': faits.get('regle'),
         'entree': [], 'sortie': [], 'note': None, 'garde': None,
         'mesures': {**{'jetons_entree': 0, 'jetons_sortie': 0, 'cout_usd': 0.0}, **(mesures or {}),
                     'duree_s': round(duree, 2), 'modele': modele}}
    if erreur:
        p['erreurs'] = [erreur]
    ps.append(p)
    st.update({'node': 'orchestrateur', 'statut': 'fait', 'passages': ps, 'updated': socle.now()})
    socle.ecrire_json(f, st)
    return p


def decider(wf, ws, faits, declencheur='fin-d-etape'):
    """Rend la decision de l'agent {geste, noeud, motif, par: 'agent'|'regle'}.
    Sans agent (FLUX_ORCHESTRATEUR=regle, ou panne du modele), rend la
    proposition de la regle, marquee comme telle — et l'ecrit aussi comme
    passe, pour que la console montre qui a decide."""
    wid = wf['id']
    regle = faits.get('regle') or {}
    if not actif():
        return {**regle, 'par': 'regle'}
    modele = modele_de(wf)
    compact = socle.ecrire_etat_compact(wid, ws)
    prompt = prompt_de(wf, compact, faits, declencheur)
    t0 = time.time()
    faits['_a'] = socle.now()
    try:
        txt, mes = demander(prompt, modele, ws)
        decision = lire_decision(txt)
        if not decision:
            raise RuntimeError('reponse illisible : ' + str(txt)[:160])
        _passe(wid, ws, modele, declencheur, faits, decision, mes, 'sentinelle', time.time() - t0)
        return {**decision, 'par': 'agent'}
    except Exception as e:
        _passe(wid, ws, modele, declencheur, faits, None, None, 'panne', time.time() - t0, erreur=str(e)[:300])
        return {**regle, 'par': 'regle', 'motif': (regle.get('motif') or '') + ' (orchestrateur en panne : %s)' % str(e)[:120]}
