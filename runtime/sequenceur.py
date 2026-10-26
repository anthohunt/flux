#!/usr/bin/env python3
"""
Flux — le sequenceur : le programme enchaine, personne ne decide (D4, § 3.5).

Remplace l'orchestrateur agent. Une etape vient de finir ; on lit sa derniere
passe et on applique UNE regle :

  - passe VERTE  -> on lance le nœud `suivant` ;
  - passe ROUGE ou PANNE -> on relance le meme nœud, jusqu'a `relances_max`
    (nœud, sinon `garde.relances_max` du graphe, sinon 2) ;
  - relances epuisees -> `run.json.statut = "bloque"`, et on alerte.

Verte, c'est : en banc, `note.score >= seuil` (le `seuil` du nœud, sinon le
bareme entier) ; en production, un verdict vert sans panne ; pour un nœud
juge par un nœud de verification (registre `verifications/etape-N`), le
registre conforme (pas de faute grave, score >= 95 %, ou plafond de passes
atteint sans grave).

Statut de run dans `state/<wf>/<run>/run.json` : en-cours | bloque | terminee | close.

L'autopilote et sa borne sont CONSERVES : le sequenceur ne fait rien si
`<workspace>/state/autopilote` est absent ; `jusqua=<id>` dans ce fichier
l'eteint une fois ce nœud franchi et valide.

La GARDE (garde.py) est lancee en boucle par le sequenceur au premier reveil
d'une run active, et tuee a la cloture. Pas de timer systemd : l'utilisateur
`claude` n'a pas sudo, et `systemctl --user` ne survit pas a une session ssh
sans lingering — un processus detache, dont le pid est dans run.json, fait le
meme travail et se voit dans `ps`.

Usage :
  sequenceur.py <workflow-id> <workspace> [--noeud-fini <id>] [--dry-run]
  sequenceur.py <workflow-id> <workspace> --reprendre        (relit l'etat, repart)
  sequenceur.py <workflow-id> <workspace> --clore            (statut close, garde tuee, ecrans purges)
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
import orchestrateur_agent  # noqa: E402  (l'agent en charge de la run)

RUNNER = socle.racine() / 'runtime' / 'run-node.py'
GARDE = socle.racine() / 'runtime' / 'garde.py'
SEUIL_QUALITE = 95          # registre de verification : meme seuil qu'avant
RELANCES_DEFAUT = 2
JOURNAL = 'sequenceur.log'


def log(wid, ws, msg):
    ligne = '%s %s\n' % (socle.now(), msg)
    print('  ' + msg, file=sys.stderr)
    try:
        with open(socle.dossier_run(wid, ws) / JOURNAL, 'a', encoding='utf-8') as f:
            f.write(ligne)
    except Exception:
        pass


# ───────────────────────────────────────────────── lectures

def etat_noeud(wid, ws, nid):
    return socle.lire_json(socle.dossier_run(wid, ws) / (nid + '.json'), {}) or {}


def lire_borne(toggle):
    try:
        for l in pathlib.Path(toggle).read_text(encoding='utf-8').splitlines():
            if l.strip().startswith('jusqua='):
                return l.strip().split('=', 1)[1].strip() or None
    except Exception:
        pass
    return None


def relances_max(wf, node):
    for v in (node.get('relances_max'), (wf.get('garde') or {}).get('relances_max')):
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return RELANCES_DEFAUT


def plafond_passes(wf, rang):
    for n in socle.noeuds(wf):
        if n.get('verification') and int(n.get('n') or 0) == rang:
            v = n.get('passes_max')
            return int(v) if v else None
    return None


def registre(ws, rang):
    return socle.lire_json(pathlib.Path(ws) / 'verifications' / ('etape-%d' % rang) /
                           'registre.json', None)


def etape_validee(wf, ws, rang):
    """Le registre du controle de ce rang valide-t-il l'etape ? (bool, motif)"""
    r = registre(ws, rang)
    if not isinstance(r, dict):
        return False, 'registre etape-%d absent ou illisible' % rang
    sc = r.get('score')
    if isinstance(sc, dict):
        graves = sc.get('fautes_graves') or 0
        if graves:
            return False, 'etape-%d : %d faute(s) grave(s)' % (rang, graves)
        p = sc.get('pourcentage')
        if p is not None:
            if p >= SEUIL_QUALITE:
                return True, 'etape-%d : %s %% >= %d %%' % (rang, p, SEUIL_QUALITE)
            plaf = plafond_passes(wf, rang)
            passe = 0
            try:
                passe = max([int(u.get('passe') or 0) for u in (r.get('unites') or {}).values()]
                            or [int(r.get('passe') or 0)])
            except Exception:
                pass
            if plaf and passe >= plaf:
                return True, 'etape-%d : %s %% mais plafond de %d passes atteint' % (rang, p, plaf)
            return False, 'etape-%d : %s %% < %d %%' % (rang, p, SEUIL_QUALITE)
    v = str(r.get('verdict') or '').lower()
    if v in ('conforme', 'vert', 'valide'):
        return True, 'etape-%d : verdict %s' % (rang, v)
    return False, 'etape-%d : verdict %s' % (rang, v or 'absent')


def noeud_juge_par(wf, verif):
    """Le nœud que ce nœud de verification juge : meme rang, pas verification."""
    return next((n for n in socle.noeuds(wf)
                 if not n.get('verification') and n.get('n') == verif.get('n')), None)


def a_une_verification(wf, node):
    return any(n.get('verification') and n.get('n') == node.get('n') and n is not node
               for n in socle.noeuds(wf))



def marquer_fait(wid, ws, node):
    """Le nœud juge vert par le sequenceur porte le statut `fait`."""
    try:
        f = socle.dossier_run(wid, ws) / (node['id'] + '.json')
        st = json.loads(f.read_text(encoding='utf-8'))
        if st.get('statut') != 'panne':
            st['statut'] = 'fait'
            st['fait_a'] = socle.now()
            f.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        log(wid, ws, 'marquer_fait %s : %s' % (node.get('id'), e))


def juger_fin(wf, ws, node, mode):
    """La derniere passe de ce nœud est-elle verte ? Rend (verdict, motif) avec
    verdict dans {vert, rouge, panne, en-cours, attente}."""
    st = etat_noeud(wf['id'], ws, node['id'])
    if st.get('statut') in ('attente-humain', 'attente-local'):
        return 'attente', 'nœud ' + node.get('type', '') + ' : attend son livrable'
    ps = st.get('passages') or []
    if node.get('type') in ('human', 'local'):
        return ('vert', 'livrables presents') if st.get('statut') == 'vert' else \
            ('attente', 'statut ' + str(st.get('statut')))
    if not ps:
        return 'en-cours', 'aucune passe'
    p = socle.normaliser_passe(ps[-1])
    if not p.get('ended'):
        sess = p.get('session')
        if sess and subprocess.call(['tmux', 'has-session', '-t', sess], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL) == 0:
            return 'en-cours', 'session vivante'
        if p.get('moteur') == 'api' and st.get('statut') == 'en-cours':
            return 'en-cours', 'appel api en cours'   # le moteur api ecrit toujours sa fin
        return 'panne', 'passe sans fin et session absente'
    if p.get('est_panne') or p.get('verdict') == 'panne':
        return 'panne', 'panne : ' + str((p.get('panne') or {}).get('cause') or p.get('fin'))
    # Un nœud de VERIFICATION rend son verdict dans le registre de l'etape :
    # « vert » veut dire « l'etape jugee passe », « rouge » « elle repart ».
    if node.get('verification'):
        if p.get('verdict') != 'vert':
            return 'rouge', 'verification sans ses livrables (' + str(p.get('verdict')) + ')'
        rang = node.get('n')
        if rang is not None:
            ok, motif = etape_validee(wf, ws, int(rang))
            return ('vert' if ok else 'refus'), motif
        return 'vert', 'verification rendue'
    ok, motif = socle.passe_verte(node, p, mode)
    if ok:
        return 'vert', motif
    return 'rouge', motif


# ───────────────────────────────────────────────── gestes

def lancer_noeud(wid, ws, nid):
    """run-node.py, detache : il vit sa vie, et reveille le sequenceur a la fin."""
    try:
        subprocess.Popen([sys.executable, str(RUNNER), wid, nid, '--workspace', ws],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception as e:
        log(wid, ws, 'lancement de %s impossible : %s' % (nid, e))
        return False


def lancer_garde(wf, ws):
    """La boucle de garde, une par run active. Pid dans run.json."""
    wid = wf['id']
    run = socle.lire_run(wid, ws)
    pid = run.get('garde_pid')
    if pid and _vivant(pid):
        return pid
    if not GARDE.exists():
        return None
    try:
        p = subprocess.Popen([sys.executable, str(GARDE), wid, ws, '--boucle'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as e:
        log(wid, ws, 'garde non lancee : %s' % e)
        return None
    socle.ecrire_run(wid, ws, garde_pid=p.pid, garde_depuis=socle.now())
    log(wid, ws, 'garde lancee (pid %d)' % p.pid)
    return p.pid


def tuer_garde(wid, ws):
    run = socle.lire_run(wid, ws)
    pid = run.get('garde_pid')
    if pid and _vivant(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)        # CE pid, jamais un kill en bloc
            log(wid, ws, 'garde arretee (pid %s)' % pid)
        except Exception:
            pass
    if pid:
        socle.ecrire_run(wid, ws, garde_pid=None)


def _vivant(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def verrou(wid, ws):
    """Une seule instance par run : deux sequenceurs lanceraient deux fois le
    meme nœud. Le verrou est un fichier avec le pid ; un pid mort le libere."""
    f = socle.dossier_run(wid, ws) / 'sequenceur.lock'
    try:
        autre = int(f.read_text().strip())
        if autre != os.getpid() and _vivant(autre):
            return None
    except Exception:
        pass
    f.write_text(str(os.getpid()))
    return f


def bloquer(wf, ws, node, motif):
    wid = wf['id']
    socle.ecrire_run(wid, ws, statut='bloque', bloque_sur=node['id'], bloque_motif=motif,
                     bloque_a=socle.now())
    tuer_garde(wid, ws)
    socle.ecrire_etat_compact(wid, ws)
    log(wid, ws, 'BLOQUE sur %s : %s' % (node['id'], motif))
    ok = socle.notifier(wid, ws, '%s. %s' % (node.get('n'), node.get('name') or node['id']),
                        'Run bloquee : ' + motif + '. Rien ne tourne, la suite attend ta decision.')
    log(wid, ws, 'alerte ' + ('envoyee' if ok else 'NON envoyee'))
    return ok


def terminer(wf, ws, node):
    wid = wf['id']
    socle.ecrire_run(wid, ws, statut='terminee', terminee_a=socle.now(), noeud_courant=None)
    tuer_garde(wid, ws)
    socle.ecrire_etat_compact(wid, ws)
    log(wid, ws, 'TERMINEE apres %s' % node['id'])
    socle.notifier(wid, ws, '%s. %s' % (node.get('n'), node.get('name') or node['id']),
                   'Run terminee : le dernier nœud est vert.')


def clore(wf, ws):
    wid = wf['id']
    tuer_garde(wid, ws)
    n = socle.purger_ecrans(wid, ws)
    socle.ecrire_run(wid, ws, statut='close', close_a=socle.now())
    try:
        (pathlib.Path(ws) / 'state' / 'autopilote').unlink()
    except Exception:
        pass
    log(wid, ws, 'CLOSE (%d ecrans purges)' % n)


def borne_atteinte(wf, ws, fini, borne):
    """Le nœud fini est-il la borne (validee) ou au-dela ? Memes regles qu'avant :
    on compare des rangs, et au meme rang on ne s'arrete qu'apres la
    verification qui valide."""
    rangs = {n.get('id'): n.get('n') for n in socle.noeuds(wf)}
    rb = rangs.get(borne)
    if rb is None and str(borne).isdigit():
        rb = int(borne)
    rf = rangs.get(fini)
    if rb is None or rf is None:
        return False
    if rf > rb:
        return True
    nf = socle.noeud(wf, fini)
    if rf == rb and nf and nf.get('verification'):
        return etape_validee(wf, ws, int(rb))[0]
    if rf == rb and nf and not a_une_verification(wf, nf):
        return juger_fin(wf, ws, nf, socle.mode_run(wf['id'], ws))[0] == 'vert'
    return False


def eteindre_sur_borne(wf, ws, borne, fini):
    wid = wf['id']
    toggle = pathlib.Path(ws) / 'state' / 'autopilote'
    try:
        toggle.unlink()
    except Exception:
        pass
    socle.ecrire_run(wid, ws, borne_atteinte=borne, autopilote=False)
    tuer_garde(wid, ws)
    log(wid, ws, 'borne atteinte (%s) — autopilote eteint' % borne)
    n = socle.noeud(wf, borne) or {}
    socle.notifier(wid, ws, '%s. %s' % (n.get('n'), n.get('name') or borne),
                   "Validee par son controle. L'autopilote s'est eteint : la suite attend ta decision.")


# ───────────────────────────────────────────────── la decision

def decider(wf, ws, fini, dry=False):
    """Rend ce qui a ete fait : {'geste': ..., 'noeud': ...}."""
    wid = wf['id']
    mode = socle.mode_run(wid, ws)
    run = socle.lire_run(wid, ws)
    if run.get('statut') in ('close', 'terminee'):
        log(wid, ws, 'run %s — rien a faire' % run['statut'])
        return {'geste': 'rien', 'motif': run['statut']}
    if fini is None:
        fini = run.get('noeud_courant') or _dernier_noeud_touche(wf, ws)
        if not fini:
            premier = socle.noeuds(wf)[0]
            log(wid, ws, 'aucun nœud joue : on lance le premier, %s' % premier['id'])
            if not dry:
                socle.ecrire_run(wid, ws, statut='en-cours', noeud_courant=premier['id'])
                lancer_noeud(wid, ws, premier['id'])
            return {'geste': 'lancer', 'noeud': premier['id']}
    node = socle.noeud(wf, fini)
    if not node:
        log(wid, ws, 'nœud inconnu : %s' % fini)
        return {'geste': 'rien', 'motif': 'nœud inconnu'}
    verdict, motif = juger_fin(wf, ws, node, mode)
    log(wid, ws, '%s : %s (%s)' % (fini, verdict, motif))

    # ── LA REGLE PROPOSE ─────────────────────────────────────────────────────
    # Ce que le programme ferait seul. C'est un FAIT donne a l'orchestrateur,
    # et ce qui s'applique si l'agent est absent.
    regle, cible, faites, maxi = None, node, 0, 0
    if verdict in ('en-cours', 'attente'):
        regle = {'geste': 'attendre', 'noeud': fini, 'motif': motif}
    elif verdict == 'vert':
        suivants = node.get('suivant') or []
        regle = ({'geste': 'terminer', 'noeud': fini, 'motif': 'dernier nœud vert'} if not suivants
                 else {'geste': 'lancer', 'noeud': suivants[0], 'motif': '%s vert -> suivant' % fini})
    else:
        # ROUGE, PANNE ou REFUS (verification qui refuse l'etape) : relancer le
        # nœud qui doit refaire le travail — lui-meme, ou l'etape que le
        # controle refuse.
        if verdict == 'refus':
            cible = noeud_juge_par(wf, node) or node
        st = etat_noeud(wid, ws, cible['id'])
        passages = st.get('passages') or []
        if verdict == 'refus':
            faites = len(passages)
            maxi = plafond_passes(wf, int(cible.get('n') or 0)) or relances_max(wf, cible)
        else:
            # les passes ratees D'AFFILEE : une passe verte remet le compteur a zero
            for x in reversed(passages):
                if socle.passe_verte(cible, x, mode)[0]:
                    break
                faites += 1
            maxi = relances_max(wf, cible)
        if faites - 1 >= maxi:
            regle = {'geste': 'bloquer', 'noeud': cible['id'],
                     'motif': '%s apres %d passe(s) ratee(s) d\'affilee (relances_max %d) — %s' % (cible['id'], faites, maxi, motif)}
        else:
            regle = {'geste': 'relancer', 'noeud': cible['id'],
                     'motif': '%s : %s (echec %d, relances_max %d)' % (cible['id'], motif, faites, maxi)}

    faits = {'etape_finie': fini, 'verdict': verdict, 'motif': motif, 'mode': mode,
             'echecs_d_affilee': faites, 'relances_max': maxi, 'regle': regle,
             'note': (socle.normaliser_passe((etat_noeud(wid, ws, fini).get('passages') or [{}])[-1]).get('note')),
             'suivants': node.get('suivant') or []}

    # ── L'ORCHESTRATEUR DECIDE ───────────────────────────────────────────────
    decision = orchestrateur_agent.decider(wf, ws, faits, 'fin-d-etape') if not dry else {**regle, 'par': 'regle'}
    g, nid_d = decision.get('geste'), decision.get('noeud')
    par = decision.get('par', 'regle')
    # Garde-fous du programme : un nœud qui existe, jamais plus du double du plafond de relances.
    if g in ('lancer', 'relancer') and not socle.noeud(wf, nid_d or ''):
        log(wid, ws, 'orchestrateur : nœud inconnu « %s », on suit la regle' % nid_d)
        decision, g, nid_d, par = {**regle, 'par': 'regle'}, regle['geste'], regle.get('noeud'), 'regle'
    if g == 'relancer' and maxi and faites - 1 >= 2 * maxi:
        log(wid, ws, 'orchestrateur : relance refusee, double du plafond atteint -> bloquer')
        decision, g, par = {'geste': 'bloquer', 'noeud': cible['id'], 'motif': 'double du plafond de relances atteint'}, 'bloquer', 'programme'
    log(wid, ws, 'decision (%s) : %s %s — %s' % (par, g, nid_d or '', decision.get('motif') or ''))

    # ── LE PROGRAMME EXECUTE ─────────────────────────────────────────────────
    if g == 'attendre':
        return {'geste': 'attendre', 'noeud': fini, 'motif': decision.get('motif'), 'par': par}
    if g == 'terminer':
        if not dry:
            marquer_fait(wid, ws, node)
            terminer(wf, ws, node)
        return {'geste': 'terminer', 'noeud': fini, 'par': par}
    if g == 'lancer':
        if not dry:
            if run.get('statut') == 'bloque':
                socle.ecrire_run(wid, ws, statut='en-cours', bloque_sur=None, bloque_motif=None)
            if verdict == 'vert':
                marquer_fait(wid, ws, node)
            socle.ecrire_run(wid, ws, statut='en-cours', noeud_courant=nid_d)
            lancer_noeud(wid, ws, nid_d)
        return {'geste': 'lancer', 'noeud': nid_d, 'par': par}
    if g == 'bloquer':
        n_b = socle.noeud(wf, nid_d or '') or cible
        if not dry:
            bloquer(wf, ws, n_b, decision.get('motif') or motif)
        return {'geste': 'bloquer', 'noeud': n_b['id'], 'motif': decision.get('motif'), 'par': par}
    # relancer
    n_r = socle.noeud(wf, nid_d or '') or cible
    passages = etat_noeud(wid, ws, n_r['id']).get('passages') or []
    if not dry:
        socle.ecrire_run(wid, ws, statut='en-cours', noeud_courant=n_r['id'])
        lancer_noeud(wid, ws, n_r['id'])
    log(wid, ws, 'relance %s (passe %d ; %d echec(s) d\'affilee, relances_max %d)'
        % (n_r['id'], len(passages) + 1, faites, maxi))
    return {'geste': 'relancer', 'noeud': n_r['id'], 'passe': len(passages) + 1,
            'echecs_d_affilee': faites, 'par': par}


def _dernier_noeud_touche(wf, ws):
    d = socle.dossier_run(wf['id'], ws)
    meilleur, t = None, None
    for n in socle.noeuds(wf):
        f = d / (n['id'] + '.json')
        if f.exists():
            m = f.stat().st_mtime
            if t is None or m > t:
                meilleur, t = n['id'], m
    return meilleur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workflow')
    ap.add_argument('workspace')
    ap.add_argument('--noeud-fini', default=None)
    ap.add_argument('--reprendre', action='store_true')
    ap.add_argument('--clore', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--sans-garde', action='store_true', help='ne lance pas la boucle de garde')
    a = ap.parse_args()

    wid, ws = a.workflow, os.path.abspath(a.workspace)
    wf = socle.charger_graphe(wid)
    if not wf:
        print('workflow introuvable : ' + wid, file=sys.stderr)
        return 3
    if not os.path.isdir(ws):
        print('workspace introuvable : ' + ws, file=sys.stderr)
        return 3

    if a.clore:
        clore(wf, ws)
        return 0

    toggle = pathlib.Path(ws) / 'state' / 'autopilote'
    if not toggle.exists():
        print('  autopilote éteint — rien à faire', file=sys.stderr)
        socle.ecrire_etat_compact(wid, ws)
        return 0

    v = verrou(wid, ws)
    if v is None:
        print('  un sequenceur tourne déjà sur cette run — on ne double pas', file=sys.stderr)
        return 0
    try:
        borne = lire_borne(toggle)
        if borne and a.noeud_fini and borne_atteinte(wf, ws, a.noeud_fini, borne):
            if not a.dry_run:
                eteindre_sur_borne(wf, ws, borne, a.noeud_fini)
            return 0
        if not a.dry_run and not a.sans_garde and not os.environ.get('FLUX_SANS_GARDE'):
            lancer_garde(wf, ws)
        res = decider(wf, ws, a.noeud_fini, dry=a.dry_run)
        print(json.dumps(res, ensure_ascii=False))
        socle.ecrire_etat_compact(wid, ws)
        return 0
    finally:
        try:
            v.unlink()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
