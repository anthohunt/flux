#!/usr/bin/env python3
"""
Flux — moteur `api` : un nœud « formulaire », joue par un appel direct.

Pas de terminal, pas d'outils. Le nœud recoit ses consignes (le texte du
graphe) et ses fichiers `entree`, et doit rendre UN livrable JSON conforme a
son `schema`. Deux fournisseurs :

  - Anthropic, via `claude -p --model <opus|sonnet|haiku> --output-format json`
    (pas de cle API sur cette machine : le compte Max du moteur de la run,
    CLAUDE_CONFIG_DIR pose depuis moteurs.json). Le texte est dans `result`,
    l'usage dans `usage`.
  - Ollama, via `POST /api/chat` (`stream: false`, `format: <schema>`,
    `options.num_ctx: 32768`). Jetons = prompt_eval_count / eval_count, duree =
    total_duration. Modele `ollama:<nom>`, URL dans moteurs.json (moteurs_api)
    ou dans `state/ollama-url` de la run.

La reponse est validee contre le schema (jsonschema). Trois essais ; puis la
passe est une PANNE (`fin: panne`, cause `sortie-invalide`), jamais un vert.

Usage direct (test) :
  api.py <workflow-id> <node-id> --workspace DIR [--modele m]
Usage runtime : jouer(wf, node, ws, npass, pdir) -> passe (dict)
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import socle  # noqa: E402

SKILLS = socle.skills_dir()
ESSAIS = 3
NUM_CTX = 32768


# ───────────────────────────────────────────────── prompt

def texte_consigne(c):
    """Une consigne du graphe : du texte tel quel, ou le SKILL.md d'un skill nomme."""
    if isinstance(c, dict):
        c = c.get('texte') or c.get('nom') or ''
    c = str(c)
    f = socle.fichier_skill(c)
    if re.fullmatch(r'[a-z0-9][a-z0-9._-]*', c) and f.exists():
        return f.read_text(encoding='utf-8')
    return c


def lire_entree(ws, chemin, plafond=200_000):
    p = pathlib.Path(ws) / chemin
    if p.is_dir():
        morceaux = []
        for f in sorted(p.rglob('*')):
            if f.is_file():
                morceaux.append('### %s\n%s' % (f.relative_to(ws),
                                                f.read_text(encoding='utf-8', errors='replace')))
        txt = '\n\n'.join(morceaux)
    elif p.exists():
        txt = p.read_text(encoding='utf-8', errors='replace')
    else:
        raise FileNotFoundError('entree absente : %s' % chemin)
    if len(txt) > plafond:
        txt = txt[:plafond] + '\n[… tronque a %d caracteres]' % plafond
    return txt


def charger_schema(node, ws, cas_dir=None):
    s = node.get('schema')
    if isinstance(s, dict):
        return s
    if not s:
        return None
    p = socle.resoudre_chemin(s, [socle.racine(), cas_dir, ws])
    if not p or not pathlib.Path(p).exists():
        raise FileNotFoundError('schema introuvable : %s' % s)
    return socle.lire_json(p)


def construire_prompt(node, ws, schema):
    L = ['# ' + str(node.get('name') or node.get('id')), '']
    if node.get('objectif'):
        L += ['## Objectif', str(node['objectif']), '']
    cons = node.get('consignes') or []
    if cons:
        L.append('## Consignes')
        for c in cons:
            L.append(texte_consigne(c))
            L.append('')
    if node.get('taches'):
        L.append('## Ce que tu dois faire')
        for i, t in enumerate(node['taches'], 1):
            L.append('%d. %s' % (i, t))
        L.append('')
    entrees = node.get('entree') or []
    if entrees:
        L.append('## Entrees')
        for e in entrees:
            L.append('### %s' % e)
            L.append('```')
            L.append(lire_entree(ws, e))
            L.append('```')
            L.append('')
    if schema is not None:
        L.append('## Format de reponse — OBLIGATOIRE')
        L.append('Reponds UNIQUEMENT par un JSON conforme a ce schema, sans texte avant ni '
                 'apres, sans commentaire :')
        L.append('```json')
        L.append(json.dumps(schema, ensure_ascii=False, indent=1))
        L.append('```')
    else:
        L.append('Reponds UNIQUEMENT par un objet JSON, sans texte autour.')
    return '\n'.join(L)


# ───────────────────────────────────────────────── extraction et validation

def extraire_json(txt):
    """L'objet JSON d'une reponse : tel quel, dans un bloc ```json, ou le premier {...}."""
    if txt is None:
        raise ValueError('reponse vide')
    if isinstance(txt, (dict, list)):
        return txt
    t = str(txt).strip()
    # qwen3 et consorts : un bloc de pensee avant la reponse
    t = re.sub(r'<think>.*?</think>', '', t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r'```(?:json)?\s*(.*?)```', t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    i, j = t.find('{'), t.rfind('}')
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError('aucun JSON dans la reponse')


def valider(obj, schema):
    if schema is None:
        if not isinstance(obj, (dict, list)):
            raise ValueError('la reponse n\'est pas un objet JSON')
        return
    import jsonschema
    jsonschema.validate(obj, schema)


# ───────────────────────────────────────────────── fournisseurs

def appel_anthropic(prompt, modele, ws=None, timeout=900):
    """`claude -p` : rend (texte, mesures). Sans outil, sans MCP."""
    cfg_dir, compte = socle.config_dir_anthropic(ws)
    env = dict(os.environ)
    env['CLAUDE_CONFIG_DIR'] = cfg_dir
    env.pop('CLAUDECODE', None)
    cmd = [socle.claude_bin(), '-p', prompt, '--model', modele, '--output-format', 'json',
           '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                       cwd='/tmp')
    duree = time.time() - t0
    if r.returncode != 0 and not (r.stdout or '').strip():
        raise RuntimeError('claude -p rc=%d : %s' % (r.returncode, (r.stderr or '')[-400:]))
    try:
        d = json.loads(r.stdout)
    except Exception:
        raise RuntimeError('claude -p : sortie non JSON : %s' % (r.stdout or '')[-300:])
    if d.get('is_error'):
        raise RuntimeError('claude -p en erreur : %s' % str(d.get('result'))[:300])
    u = d.get('usage') or {}
    neuf = u.get('input_tokens', 0) or 0
    cw = u.get('cache_creation_input_tokens', 0) or 0
    cr = u.get('cache_read_input_tokens', 0) or 0
    sortie = u.get('output_tokens', 0) or 0
    mesures = {'jetons_entree': neuf + cw + cr, 'jetons_sortie': sortie,
               'jetons_neuf': neuf, 'jetons_cache_ecrit': cw, 'jetons_cache_lu': cr,
               'duree_s': round(duree, 2),
               'duree_api_s': round((d.get('duration_api_ms') or 0) / 1000, 2),
               'cout_usd': socle.cout_usd(modele, neuf, sortie, cw, cr),
               'cout_annonce_usd': d.get('total_cost_usd'),
               'fournisseur': 'anthropic', 'compte': compte,
               'claude_session_id': d.get('session_id')}
    return d.get('result'), mesures


AFFICHE = False   # vrai quand le moteur tourne dans une fenetre (tmux) que la console montre


def dire(txt='', fin='\n'):
    """Ecrit sur la fenetre du moteur, quand il y en a une."""
    if AFFICHE:
        sys.stdout.write(str(txt) + fin)
        sys.stdout.flush()


def appel_ollama(prompt, modele, schema, ws=None, timeout=900):
    """`POST /api/chat` : rend (texte, mesures). `format` porte le schema."""
    url = socle.moteur_api_ollama_url(ws) + '/api/chat'
    corps = {'model': modele, 'stream': bool(AFFICHE),
             'messages': [{'role': 'user', 'content': prompt}],
             'options': {'num_ctx': NUM_CTX, 'temperature': 0},
             'think': False}
    if schema is not None:
        corps['format'] = schema
    req = urllib.request.Request(url, data=json.dumps(corps).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if AFFICHE:
                # EN DIRECT : chaque morceau s'ecrit a l'ecran des qu'il arrive,
                # le dernier porte les compteurs.
                contenu, d = [], {}
                for ligne in resp:
                    try:
                        m = json.loads(ligne.decode('utf-8'))
                    except Exception:
                        continue
                    bout = (m.get('message') or {}).get('content') or ''
                    if bout:
                        contenu.append(bout); dire(bout, fin='')
                    if m.get('done'):
                        d = m
                dire()
                d['message'] = {'content': ''.join(contenu)}
            else:
                d = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError('ollama HTTP %s : %s' % (e.code, e.read()[:300]))
    except Exception as e:
        raise RuntimeError('ollama injoignable (%s) : %s' % (url, e))
    duree = time.time() - t0
    if d.get('error'):
        raise RuntimeError('ollama : %s' % d['error'])
    mesures = {'jetons_entree': d.get('prompt_eval_count', 0) or 0,
               'jetons_sortie': d.get('eval_count', 0) or 0,
               'duree_s': round(duree, 2),
               'duree_api_s': round((d.get('total_duration') or 0) / 1e9, 2),
               'cout_usd': 0.0, 'fournisseur': 'ollama', 'url': url}
    return (d.get('message') or {}).get('content'), mesures



def appel_llamacpp(prompt, modele, schema, ws=None, timeout=900):
    """`POST /completion` d'un llama-server : LE moteur du paquet Mail Agenda, joue a l'identique.

    Prompt ChatML ecrit en clair avec un bloc de pensee VIDE (Qwen3 hybride ne
    reflechit pas), sortie contrainte par `json_schema`. Le serveur n'heberge
    qu'un modele : on verifie par /props que c'est bien celui demande ; sinon on
    demande a la machine qui l'heberge de le servir (`ssh <moteurs_api.llamacpp.ssh> servir <nom>` :
    `rentre` verifie qu'il tient dans la VRAM, `descendre` le telecharge s'il est au
    catalogue, le service bascule) et on reverifie — panne franche si ce n'est
    toujours pas lui, plutot qu'une mesure sur le mauvais modele."""
    base = socle.moteur_api_llamacpp_url(ws)

    def modele_servi():
        try:
            with urllib.request.urlopen(base + '/props', timeout=10) as r:
                props = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            raise RuntimeError('llama-server injoignable (%s) : %s' % (base, e))
        return str(props.get('model_path') or (props.get('default_generation_settings') or {}).get('model') or '')

    def correspond(chemin):
        attendu = re.sub(r'[^a-z0-9]', '', modele.lower())
        return not attendu or attendu in re.sub(r'[^a-z0-9]', '', chemin.lower())

    chemin = modele_servi()
    if not correspond(chemin):
        hote = ((socle.charger_moteurs().get('moteurs_api') or {}).get('llamacpp') or {}).get('ssh')
        if not hote:
            raise RuntimeError('llama-server sert %s, pas %s (pose moteurs_api.llamacpp.ssh '
                               'pour le faire basculer)' % (chemin or '?', modele))
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', hote, 'servir', modele],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError('llama-server sert %s, pas %s, et `servir` a refuse : %s'
                               % (chemin or '?', modele, (r.stdout + r.stderr).strip()[-300:]))
        chemin = modele_servi()
        if not correspond(chemin):
            raise RuntimeError('llama-server sert %s, pas %s (meme apres servir)' % (chemin or '?', modele))
    brut = ('<|im_start|>user\n' + prompt + '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')
    corps = {'prompt': brut, 'temperature': 0, 'n_predict': 4096, 'cache_prompt': True}
    if schema is not None:
        corps['json_schema'] = schema
    req = urllib.request.Request(base + '/completion', data=json.dumps(corps).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError('llama-server HTTP %s : %s' % (e.code, e.read()[:300]))
    except Exception as e:
        raise RuntimeError('llama-server injoignable (%s) : %s' % (base, e))
    tm = d.get('timings') or {}
    mesures = {'jetons_entree': d.get('tokens_evaluated', 0) or 0, 'jetons_sortie': d.get('tokens_predicted', 0) or 0,
               'duree_s': round(time.time() - t0, 2),
               'duree_api_s': round(((tm.get('prompt_ms') or 0) + (tm.get('predicted_ms') or 0)) / 1000, 2),
               'jetons_par_s': round(tm.get('predicted_per_second') or 0, 1), 'arret': d.get('stop_type'),
               'cout_usd': 0.0, 'fournisseur': 'llamacpp', 'url': base, 'modele_serveur': chemin}
    return d.get('content') or '', mesures


def appeler(prompt, modele, schema, ws=None):
    if str(modele).startswith('ollama:'):
        return appel_ollama(prompt, modele.split(':', 1)[1], schema, ws)
    if str(modele).startswith('llamacpp:'):
        return appel_llamacpp(prompt, modele.split(':', 1)[1], schema, ws)
    return appel_anthropic(prompt, modele, ws)


# ───────────────────────────────────────────────── la passe

def jouer(wf, node, ws, npass, pdir, modele=None, essais=ESSAIS, mode=None):
    """Joue le nœud et rend la passe (dict, forme v2). Ecrit le livrable.

    Ne note pas : la note est posee par run-node.py apres, comme pour tmux.
    """
    wid = wf.get('id')
    pdir = pathlib.Path(pdir)
    pdir.mkdir(parents=True, exist_ok=True)
    modele = modele or node.get('modele') or 'sonnet'
    livrables = list(node.get('livrables') or [])
    passe = {'n': npass, 'session': None, 'claude_session_id': None,
             'moteur': 'api', 'compte': None, 'modele': modele,
             'started': socle.now(), 'motif': 'premier lancement' if npass == 1 else 'relance',
             'entree': list(node.get('entree') or []), 'sortie': livrables,
             'note': None, 'garde': None, 'mesures': None, 'essais': []}
    t0 = time.time()
    cumul = {'jetons_entree': 0, 'jetons_sortie': 0, 'cout_usd': 0.0}
    journal = []
    dire('flux · moteur api · %s · %s · modele %s' % (wid, node.get('id'), modele))
    dire('─' * 78)
    try:
        schema = charger_schema(node, ws, socle.dossier_cas(wid, ws))
        prompt = construire_prompt(node, ws, schema)
        (pdir / 'prompt.md').write_text(prompt, encoding='utf-8')
        dire('CE QUE LE MODELE RECOIT (%d caracteres) :' % len(prompt)); dire()
        dire(prompt if len(prompt) <= 6000 else prompt[:6000] + '\n[… %d caracteres de plus, dans prompt.md]' % (len(prompt) - 6000))
        dire(); dire('─' * 78)
    except Exception as e:
        passe.update({'ended': socle.now(), 'fin': 'panne', 'verdict': 'panne',
                      'duree_s': int(time.time() - t0), 'erreurs': [str(e)],
                      'panne': {'cause': 'entree-manquante', 'constat': str(e),
                                'releve_a': socle.now(), 'releve_par': 'moteur-api'}})
        (pdir / 'run.out').write_text('PANNE avant l\'appel : %s\n' % e, encoding='utf-8')
        return passe
    obj, derniere_erreur = None, None
    for essai in range(1, essais + 1):
        ligne = {'essai': essai, 'a': socle.now()}
        dire('APPEL %d/%d → %s …' % (essai, essais, modele)); dire()
        try:
            texte, mes = appeler(prompt, modele, schema, ws)
            if not str(modele).startswith(('ollama:', 'llamacpp:')):
                dire(str(texte or ''))    # claude -p rend d'un bloc : on l'ecrit a l'arrivee
            dire(); dire('… %.1f s · %d jetons entree · %d jetons sortie' % (
                mes.get('duree_s') or 0, mes.get('jetons_entree') or 0, mes.get('jetons_sortie') or 0))
            ligne['mesures'] = mes
            for k in ('jetons_entree', 'jetons_sortie', 'cout_usd'):
                cumul[k] += mes.get(k) or 0
            if mes.get('claude_session_id') and not passe.get('claude_session_id'):
                passe['claude_session_id'] = mes['claude_session_id']
            if mes.get('compte'):
                passe['compte'] = mes['compte']
            (pdir / ('reponse-%d.txt' % essai)).write_text(str(texte or ''), encoding='utf-8')
            obj = extraire_json(texte)
            valider(obj, schema)
            ligne['ok'] = True
            passe['essais'].append(ligne)
            journal.append('essai %d : conforme' % essai)
            dire('✓ reponse conforme au schema')
            break
        except Exception as e:
            derniere_erreur = '%s: %s' % (type(e).__name__, str(e)[:400])
            dire('✗ ' + derniere_erreur)
            ligne.update({'ok': False, 'erreur': derniere_erreur})
            passe['essais'].append(ligne)
            journal.append('essai %d : %s' % (essai, derniere_erreur))
            obj = None
    mesures = {**cumul, 'cout_usd': round(cumul['cout_usd'], 5),
               'duree_s': round(time.time() - t0, 2), 'essais': len(passe['essais']),
               'modele': modele}
    passe['mesures'] = mesures
    passe['ended'] = socle.now()
    passe['duree_s'] = int(time.time() - t0)
    if obj is None:
        passe.update({'fin': 'panne', 'verdict': 'panne', 'erreurs': [derniere_erreur or '?'],
                      'panne': {'cause': 'sortie-invalide', 'constat': derniere_erreur or '?',
                                'releve_a': socle.now(), 'releve_par': 'moteur-api'}})
    else:
        # UN LIVRABLE : le document entier. PLUSIEURS : le document porte une
        # cle par livrable, sans extension (`resultat` -> resultat.json), et
        # chaque cle s'ecrit dans son fichier (convention du contrat v2).
        for l in livrables:
            p = pathlib.Path(ws) / l
            p.parent.mkdir(parents=True, exist_ok=True)
            cle = pathlib.Path(l).stem
            contenu = obj if len(livrables) == 1 else (obj.get(cle) if isinstance(obj, dict) else None)
            if contenu is None and len(livrables) > 1:
                journal.append('livrable %s : cle « %s » absente du document rendu' % (l, cle))
                continue
            p.write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding='utf-8')
        passe.update({'fin': 'sentinelle', 'verdict': 'vert', 'erreurs': []})
    dire('─' * 78)
    dire('FIN : %s · %.1f s · %d jetons entree · %d jetons sortie · %.4f $ · livrables : %s'
         % (passe['fin'], mesures['duree_s'], mesures['jetons_entree'], mesures['jetons_sortie'],
            mesures['cout_usd'], ', '.join(livrables)))
    (pdir / 'run.out').write_text(
        'moteur api · modele %s · %s\n%s\nfin : %s · %.1f s · %d jetons entree · %d jetons sortie · %.4f $\n'
        % (modele, passe['started'], '\n'.join(journal), passe['fin'], mesures['duree_s'],
           mesures['jetons_entree'], mesures['jetons_sortie'], mesures['cout_usd']),
        encoding='utf-8')
    return passe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workflow')
    ap.add_argument('node')
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--modele', default=None)
    ap.add_argument('--passe', type=int, default=1)
    ap.add_argument('--affiche', action='store_true', help='raconte l appel a l ecran (fenetre tmux)')
    ap.add_argument('--passe-json', default=None, help='ecrit la passe dans ce fichier au lieu de stdout')
    a = ap.parse_args()
    global AFFICHE
    AFFICHE = bool(a.affiche)
    wf = socle.charger_graphe(a.workflow)
    if not wf:
        sys.exit('workflow introuvable : ' + a.workflow)
    node = socle.noeud(wf, a.node)
    if not node:
        sys.exit('nœud inconnu : ' + a.node)
    pdir = socle.dossier_run(wf['id'], a.workspace) / a.node / 'passes' / str(a.passe)
    p = jouer(wf, node, a.workspace, a.passe, pdir, modele=a.modele)
    if a.passe_json:
        pathlib.Path(a.passe_json).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding='utf-8')
    else:
        print(json.dumps(p, ensure_ascii=False, indent=1))
    return 0 if p.get('fin') != 'panne' else 2


if __name__ == '__main__':
    sys.exit(main())
