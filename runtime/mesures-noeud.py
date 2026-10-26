#!/usr/bin/env python3
"""Ce qu'une passe a coute : son temps, ses tours de parole, ses jetons.

Une passe est une session `claude` (le meneur) plus ses sous-agents. On lit les
journaux de session, on somme les `usage` de chaque appel au modele, et on rend un
JSON par passe. Le nombre de TOURS compte les appels au modele : c'est lui qui
commande le cout, parce qu'a chaque tour on renvoie au modele tout le contexte
accumule.

Les journaux vivent sous le dossier de config du moteur de la run — il y en a
plusieurs (un par moteur de moteurs.json, plus `~/.claude`) : on cherche dans tous.

Usage : mesures-noeud.py <state-du-noeud.json>
Sortie : {"passes": [...], "total": {...}} sur stdout.
"""
import html
import re
import sys, os, json, glob, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import socle  # noqa: E402

CONFIG_DIRS = socle.config_dirs_connus()

# Tarifs Claude Opus 5, en dollars par million de jetons. Le forfait Max ne
# facture rien : ce chiffre sert d'ordre de grandeur comparable entre passes.
PRIX = {'lu': 0.50, 'ecrit': 6.25, 'neuf': 5.00, 'sortie': 25.00}


def journaux(sid):
    """Le journal du meneur et ceux de ses sous-agents, ou qu'ils soient."""
    out = []
    for base in CONFIG_DIRS:
        for p in glob.glob(f'{base}/projects/*/{sid}.jsonl'):
            out.append(p)
            out += sorted(glob.glob(f'{base}/projects/*/{sid}/subagents/*.jsonl'))
    return out


HORO = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def instant(s):
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def mesure(sid):
    # `parole` : le temps ou le MENEUR redige, tour par tour — entre le dernier
    # resultat qu'il a reçu et le message qu'il vient de finir. Ce qui reste de
    # la duree de la passe est de l'attente : ses sous-agents travaillent, ou un
    # outil tourne. La distinction compte : sur une passe de 26 min, le meneur
    # n'a parle que 8 min, et optimiser sa rediction n'aurait touche que ces 8-la.
    t = {'tours': 0, 'agents': 0, 'lu': 0, 'ecrit': 0, 'neuf': 0, 'sortie': 0,
         'ctx_max': 0, 'parole': 0.0, 'parole_sous': 0.0, 'outils': {}}
    # LE DETAIL TOUR PAR TOUR, pour le MENEUR seul : ses sous-agents ont leurs
    # propres journaux, et melanger deux fils paralleles dans une seule liste
    # produirait une chronologie qui n'existe nulle part.
    detail = []
    # CHAQUE SOUS-AGENT, un par un. Le meneur nomme celui qu'il lance dans le
    # resultat de son appel (`toolUseResult` : agentId + description) ; le
    # journal du sous-agent porte le meme `agentId`. C'est ce qui permet de dire
    # « Chasseur Energie & environnement » plutot que « agent-a2e951a7… ».
    noms, sous = {}, []
    fichiers = journaux(sid)
    t['agents'] = max(0, len([f for f in fichiers if '/subagents/' in f]))
    for f in fichiers:
        try:
            fh = open(f, errors='ignore')
        except OSError:
            continue
        # UN TOUR = UN `message.id`, PAS UNE LIGNE DE JOURNAL. Quand un agent
        # appelle plusieurs outils dans le meme tour, le journal ecrit une ligne
        # par appel — et RECOPIE le meme bloc `usage` dans chacune. Compter les
        # lignes comptait donc le meme tour autant de fois qu'il portait
        # d'appels. Mesure sur une run complete : 511 M annonces pour 221 M
        # reels, soit un facteur 2,3. Pire, l'erreur croissait avec le
        # GROUPAGE des appels : le tableau de bord punissait exactement le
        # comportement qu'on demande aux agents d'adopter.
        vus, vus_val = set(), {}
        # Un tour dont l'agent lance plusieurs outils s'ecrit sur PLUSIEURS
        # lignes qui partagent le meme `message.id` — et les noms d'outils sont
        # repartis entre elles. On garde donc l'index du tour pour completer sa
        # liste d'outils quand ses lignes suivantes arrivent.
        ou = {}
        meneur = '/subagents/' not in f
        moi = {'nom': None, 'id': None, 'tours': 0, 'parole': 0.0, 'lu': 0,
               'ecrit': 0, 'neuf': 0, 'sortie': 0, 'ctx_max': 0,
               'debut': None, 'fin': None, 'outils': {}, 'tours_detail': [],
               'fichiers': []}
        # Le carnet du journal courant : celui du meneur, ou celui du sous-agent.
        # Le tour par tour a la meme forme pour les deux — c'est ce qui permet de
        # servir le meme tableau a l'interieur de chaque agent.
        carnet = detail if meneur else moi['tours_detail']
        prec = None            # l'instant du dernier evenement vu dans CE journal
        with fh:
            for ligne in fh:
                if '"usage"' not in ligne:
                    # Un resultat d'outil ne coute rien, mais il DATE la fin de
                    # l'attente : sans lui, le tour suivant se croirait long de
                    # tout le temps passe dans l'outil.
                    if '"type":"user"' in ligne or '"type": "user"' in ligne:
                        h = HORO.search(ligne)
                        if h:
                            i = instant(h.group(1))
                            if i:
                                prec = i
                        if meneur and '"agentId"' in ligne and '"description"' in ligne:
                            try:
                                r = (json.loads(ligne).get('toolUseResult') or {})
                                if r.get('agentId'):
                                    noms[r['agentId']] = r.get('description') or ''
                            except Exception:
                                pass
                        elif not meneur and moi['id'] is None and '"agentId"' in ligne:
                            try:
                                moi['id'] = json.loads(ligne).get('agentId')
                            except Exception:
                                pass
                    continue
                try:
                    d = json.loads(ligne)
                except Exception:
                    continue
                msg = d.get('message') or {}
                u = msg.get('usage') or d.get('usage')
                if not isinstance(u, dict):
                    continue
                mid = msg.get('id')
                if mid:
                    if mid in vus:
                        noms_o = [c.get('name') for c in (msg.get('content') or [])
                                  if isinstance(c, dict) and c.get('type') == 'tool_use']
                        if mid in ou:
                            carnet[ou[mid]]['outils'] += noms_o
                            e = carnet[ou[mid]]
                            for c in (msg.get('content') or []):
                                if not (isinstance(c, dict) and c.get('type') == 'tool_use'):
                                    continue
                                ent = c.get('input') or {}
                                if not isinstance(ent, dict):
                                    continue
                                if c.get('name') == 'Skill':
                                    s = ent.get('skill') or ent.get('name')
                                    if s and str(s) not in e['skills']:
                                        e['skills'].append(str(s))
                                elif c.get('name') in ('Write', 'Edit', 'NotebookEdit'):
                                    fp = ent.get('file_path') or ent.get('notebook_path')
                                    if fp and str(fp) not in e['ecrits']:
                                        e['ecrits'].append(str(fp))
                                        if not meneur and str(fp) not in moi['fichiers']:
                                            moi['fichiers'].append(str(fp))
                        cible_o = t['outils'] if meneur else moi['outils']
                        for nm in noms_o:
                            cible_o[nm] = cible_o.get(nm, 0) + 1
                        # Le compte definitif arrive sur la DERNIERE ligne du
                        # message : on rattrape l'ecart plutot que de le perdre.
                        rat = {'lu': (u.get('cache_read_input_tokens', 0) or 0),
                               'ecrit': (u.get('cache_creation_input_tokens', 0) or 0),
                               'neuf': (u.get('input_tokens', 0) or 0),
                               'sortie': (u.get('output_tokens', 0) or 0)}
                        vu0 = vus_val.get(mid) or {}
                        for k, v in rat.items():
                            ecart = v - (vu0.get(k) or 0)
                            if ecart > 0:
                                vu0[k] = v
                                t[k] += ecart
                                if not meneur:
                                    moi[k] += ecart
                                if mid in ou:
                                    if k == 'sortie':
                                        carnet[ou[mid]]['sortie'] += ecart
                                    carnet[ou[mid]]['tok'] += ecart
                        vus_val[mid] = vu0
                        continue
                    vus.add(mid)
                i = instant(d.get('timestamp') or '')
                parle = None
                if i:
                    if prec:
                        parle = max(0.0, (i - prec).total_seconds())
                        t['parole' if meneur else 'parole_sous'] += parle
                    prec = i
                if len(carnet) < 400:
                    blocs = [c for c in (msg.get('content') or [])
                             if isinstance(c, dict) and c.get('type') == 'tool_use']
                    outils = [c.get('name') for c in blocs]
                    # CE TOUR A-T-IL APPELE UN SKILL, ECRIT UN FICHIER ? Les deux
                    # se LISENT, ils ne se devinent pas : un skill invoque est un
                    # appel de l'outil `Skill`, une ecriture est un `Write`/`Edit`
                    # avec son chemin. Aucune lecture semantique du texte, donc
                    # aucune interpretation a se tromper.
                    skills, ecrits = [], []
                    for c in blocs:
                        ent = c.get('input') or {}
                        if not isinstance(ent, dict):
                            continue
                        if c.get('name') == 'Skill':
                            s = ent.get('skill') or ent.get('name')
                            if s and s not in skills:
                                skills.append(str(s))
                        elif c.get('name') in ('Write', 'Edit', 'NotebookEdit'):
                            fp = ent.get('file_path') or ent.get('notebook_path')
                            if fp and fp not in ecrits:
                                ecrits.append(str(fp))
                    if not meneur:
                        for fp in ecrits:
                            if fp not in moi['fichiers']:
                                moi['fichiers'].append(fp)
                    carnet.append({
                        'skills': skills,
                        'ecrits': ecrits,
                        'n': len(carnet) + 1,
                        'parle': None if parle is None else round(parle),
                        'fin': i.strftime('%Y-%m-%dT%H:%M:%SZ') if i else None,
                        'tok': ((u.get('cache_read_input_tokens', 0) or 0)
                                + (u.get('cache_creation_input_tokens', 0) or 0)
                                + (u.get('input_tokens', 0) or 0)
                                + (u.get('output_tokens', 0) or 0)),
                        'ecrit': u.get('cache_creation_input_tokens', 0) or 0,
                        'sortie': u.get('output_tokens', 0) or 0,
                        'outils': outils})
                    if mid:
                        ou[mid] = len(carnet) - 1
                lu = u.get('cache_read_input_tokens', 0) or 0
                ec = u.get('cache_creation_input_tokens', 0) or 0
                nf = u.get('input_tokens', 0) or 0
                so = u.get('output_tokens', 0) or 0
                if mid:
                    vus_val[mid] = {'lu': lu, 'ecrit': ec, 'neuf': nf, 'sortie': so}
                if not meneur:
                    moi['tours'] += 1
                    moi['parole'] += (parle or 0)
                    moi['lu'] += lu; moi['ecrit'] += ec
                    moi['neuf'] += nf; moi['sortie'] += so
                    moi['ctx_max'] = max(moi['ctx_max'], lu + ec + nf)
                    if i:
                        if moi['debut'] is None:
                            moi['debut'] = i
                        moi['fin'] = i
                for c in (msg.get('content') or []):
                    if isinstance(c, dict) and c.get('type') == 'tool_use':
                        cible = t['outils'] if meneur else moi['outils']
                        cible[c.get('name')] = cible.get(c.get('name'), 0) + 1
                t['tours'] += 1
                t['lu'] += lu
                t['ecrit'] += ec
                t['neuf'] += nf
                t['sortie'] += so
                t['ctx_max'] = max(t['ctx_max'], lu + ec + nf)
        if not meneur and moi['tours']:
            moi['total'] = moi['lu'] + moi['ecrit'] + moi['neuf'] + moi['sortie']
            moi['cout'] = round(sum(moi[k] * PRIX[k] for k in PRIX) / 1e6, 3)
            moi['couts'] = {k: round(moi[k] * PRIX[k] / 1e6, 3) for k in PRIX}
            moi['parole'] = round(moi['parole'])
            moi['duree'] = (round((moi['fin'] - moi['debut']).total_seconds())
                            if moi['debut'] and moi['fin'] else None)
            moi['attente'] = (max(0, moi['duree'] - moi['parole'])
                              if moi['duree'] is not None else None)
            moi['debut'] = moi['debut'].strftime('%Y-%m-%dT%H:%M:%SZ') if moi['debut'] else None
            moi['fin'] = moi['fin'].strftime('%Y-%m-%dT%H:%M:%SZ') if moi['fin'] else None
            moi['outils'] = sorted(moi['outils'].items(), key=lambda x: -x[1])[:4]
            sous.append(moi)
    t['total'] = t['lu'] + t['ecrit'] + t['neuf'] + t['sortie']
    t['cout'] = round(sum(t[k] * PRIX[k] for k in PRIX) / 1e6, 2)
    # LE COUT POSTE PAR POSTE, calcule ICI parce que c'est ici que vit le tarif.
    # Recopier la grille dans la page en ferait deux, et elles divergeraient au
    # premier changement de prix.
    t['couts'] = {k: round(t[k] * PRIX[k] / 1e6, 3) for k in PRIX}
    t['outils'] = sorted(t['outils'].items(), key=lambda x: -x[1])[:4]
    for s in sous:
        # La description arrive ECHAPPEE en HTML dans le journal : sans ce
        # deballage, la console affiche « Chasseur Modeles &amp; capacites ».
        s['nom'] = html.unescape(noms.get(s['id']) or (s['id'] or '')[:14])
    sous.sort(key=lambda s: -(s.get('cout') or 0))
    t['sous_agents'] = sous

    def poser_attente(c):
        for a, b in zip(c, c[1:]):
            ia, ib = instant(a.get('fin') or ''), instant(b.get('fin') or '')
            a['attend'] = (max(0, round((ib - ia).total_seconds()) - (b.get('parle') or 0))
                           if ia and ib else None)
        if c:
            c[-1]['attend'] = None       # rien ne suit : on ne devine pas

    poser_attente(detail)
    for s in sous:
        poser_attente(s['tours_detail'])
    t['tours_detail'] = detail
    t['parole'] = round(t['parole'])
    t['parole_sous'] = round(t['parole_sous'])
    return t


def normaliser(x):
    """La passe en forme v2, quel que soit son age (socle.normaliser_passe)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import socle
        return socle.normaliser_passe(x)
    except Exception:
        return dict(x)


def ecoule(depuis):
    """Les secondes passees depuis un horodatage ISO, en UTC."""
    try:
        t = datetime.datetime.fromisoformat(depuis.replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return max(0, int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()))
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: mesures-noeud.py <state-du-noeud.json>')
    p = sys.argv[1]
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception as e:
        print(json.dumps({'passes': [], 'erreur': f'{type(e).__name__}: {e}'}))
        return

    passes, cumul = [], {'tours': 0, 'total': 0, 'duree': 0, 'cout': 0.0}
    for x0 in d.get('passages', []):
        x = normaliser(x0)
        sid = x.get('claude_session_id')
        if x.get('moteur') == 'api':
            # UNE PASSE API n'a pas de journal de session a sommer : ses mesures
            # sont ecrites par le moteur lui-meme, dans la passe (jetons, duree,
            # cout au tarif du modele, essais).
            ma = x.get('mesures') or {}
            m = {'tours': ma.get('essais', 1) or 1, 'agents': 0,
                 'lu': ma.get('jetons_cache_lu', 0) or 0, 'ecrit': ma.get('jetons_cache_ecrit', 0) or 0,
                 'neuf': ma.get('jetons_neuf', (ma.get('jetons_entree', 0) or 0)
                                 - (ma.get('jetons_cache_lu', 0) or 0) - (ma.get('jetons_cache_ecrit', 0) or 0)),
                 'sortie': ma.get('jetons_sortie', 0) or 0, 'ctx_max': ma.get('jetons_entree', 0) or 0,
                 'total': (ma.get('jetons_entree', 0) or 0) + (ma.get('jetons_sortie', 0) or 0),
                 'cout': round(ma.get('cout_usd', 0) or 0, 4), 'parole': 0, 'parole_sous': 0,
                 'outils': [], 'tours_detail': [], 'sous_agents': []}
        else:
            m = mesure(sid) if sid else {'tours': 0, 'agents': 0, 'total': 0, 'cout': 0,
                                         'lu': 0, 'ecrit': 0, 'neuf': 0, 'sortie': 0, 'ctx_max': 0}
        duree, en_cours = x.get('duree_s'), False
        if duree is None and x.get('started') and not x.get('ended'):
            # `duree_s` n'est ecrit qu'a la cloture du passage : sans ce calcul, une
            # passe affiche « 0 s » pendant toute sa duree, c'est-a-dire pendant le
            # seul moment ou on la regarde.
            duree, en_cours = ecoule(x['started']), True
        passes.append({
            'n': x.get('n'), 'debut': x.get('started'), 'fin': x.get('ended'),
            'duree': duree, 'en_cours': en_cours,
            'attente': (max(0, duree - m.get('parole', 0)) if duree is not None else None),
            # LE MOTEUR DE CETTE PASSE, et pas celui de la run aujourd'hui : une
            # run bascule de compte quand l'un atteint son quota, donc « qui a
            # paye la passe 3 » ne se lit nulle part si on n'affiche que le
            # moteur courant. Il est deja ecrit dans l'etat du nœud ; il
            # manquait seulement au chemin qui remonte vers la page.
            # v2 : `moteur` est le moteur (tmux|api), `compte` le compte qui a
            # paye. Avant la v2 le compte vivait dans `moteur` : normaliser()
            # relit les anciennes passes.
            'moteur': x.get('moteur'), 'compte': x.get('compte'), 'modele': x.get('modele'),
            'decision': x.get('decision'), 'declencheur': x.get('declencheur'), 'motif': x.get('motif'),
            'note': x.get('note'), 'garde': x.get('garde'),
            'entree': x.get('entree') or [], 'sortie_fichiers': x.get('sortie') or [],
            'verdict': x.get('verdict'), 'motif': x.get('motif'),
            # LA PANNE, posee par l'orchestrateur quand il constate qu'une passe
            # est tombee au lieu d'avoir ete refusee. Sans ce champ ici, la
            # console ne peut pas distinguer un accident d'un jugement.
            'panne': x.get('panne'),
            'sortie_de_passe': x.get('fin'), **m})
        cumul['tours'] += m['tours']
        cumul['total'] += m['total']
        cumul['duree'] += duree or 0
        cumul['cout'] += m['cout']
        if en_cours:
            cumul['en_cours'] = True
    cumul['cout'] = round(cumul['cout'], 2)
    cumul['passes'] = len(passes)
    print(json.dumps({'passes': passes, 'total': cumul}, ensure_ascii=False))


if __name__ == '__main__':
    main()
