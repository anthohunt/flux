#!/usr/bin/env python3
"""
Flux — le socle commun du runtime v2.

Ce que run-node.py, le moteur api, le sequenceur et la garde partagent : ou
vivent les fichiers, la forme d'une passe, l'etat d'une run (`run.json`),
l'etat compact que lit la garde, le juge, la notification, les tarifs.

Rien ici ne decide : ce sont des lectures et des ecritures, tolerantes aux
formats anciens (une passe d'avant la v2 n'a ni `note` ni `garde`, et son
`moteur` est un identifiant de COMPTE, pas de moteur).

Variables d'environnement (tests et deploiements) :
  FLUX_RACINE           racine du depot (defaut : le parent de runtime/)
  FLUX_STATE_DIR        dossier d'etat (defaut : <racine>/state)
  FLUX_WORKFLOWS_DIR    dossier(s) des graphes, separes par « : » (defaut : <racine>/workflows)
  FLUX_RUNS_HOME        dossier sous lequel vivent les runs `<wf>-runs` (defaut : $HOME)
  FLUX_MOTEURS          fichier des moteurs (defaut : <racine>/moteurs.json, sinon moteurs.example.json)
  FLUX_SKILLS_DIR       dossier des skills injectes aux agents (defaut : <racine>/skills)
  FLUX_PIPELINE_RUNTIME outils de pipeline facultatifs (defaut : <skills>/pipeline-runtime)
  FLUX_VALIDATORS_PATH  dossiers de validateurs en plus, separes par « : »
  FLUX_NOTIF_URL        adresse HTTP a appeler pour une alerte (facultatif)
  FLUX_ALERTE_CMD       commande d'alerte de repli, recoit --sujet et --corps (facultatif)
  FLUX_CLAUDE_BIN       binaire claude (defaut : `claude` trouve dans le PATH)
  FLUX_IGNORER_VERSION  =1 : ne pas refuser une version de Claude Code differente
"""
import datetime
import glob
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse

_ICI = pathlib.Path(__file__).resolve().parent


def racine():
    return pathlib.Path(os.environ.get('FLUX_RACINE') or _ICI.parent)


def runs_home():
    """Le dossier sous lequel vivent les runs d'un graphe sans `workspace_root`."""
    return pathlib.Path(os.environ.get('FLUX_RUNS_HOME') or pathlib.Path.home())


def workspace_root(wf):
    """La racine des runs d'un graphe : `workspace_root` (`~` et variables
    d'environnement developpes), sinon `<FLUX_RUNS_HOME>/<id>-runs`."""
    r = (wf or {}).get('workspace_root')
    if r:
        return pathlib.Path(os.path.expandvars(os.path.expanduser(str(r))))
    return runs_home() / ('%s-runs' % (wf or {}).get('id', 'flux'))


def skills_dirs():
    """Les dossiers de skills (FLUX_SKILLS_DIR, separes par « : »), un sous-dossier par skill."""
    return _chemins('FLUX_SKILLS_DIR', racine() / 'skills')


def skills_dir():
    """Le premier dossier de skills."""
    return skills_dirs()[0]


def fichier_skill(nom):
    """Le SKILL.md du skill `nom`, cherche dans chaque dossier ; le premier gagne."""
    for d in skills_dirs():
        f = d / str(nom) / 'SKILL.md'
        if f.exists():
            return f
    return skills_dir() / str(nom) / 'SKILL.md'


def pipeline_runtime_dir():
    """Le dossier des outils de pipeline facultatifs (validateurs partages,
    rapport de run, rendu des transcripts, passe d'accents)."""
    return pathlib.Path(os.environ.get('FLUX_PIPELINE_RUNTIME') or (skills_dir() / 'pipeline-runtime'))


def claude_config_defaut():
    """Le dossier de config Claude Code quand aucun moteur n'en declare."""
    return os.environ.get('CLAUDE_CONFIG_DIR') or str(pathlib.Path.home() / '.claude')


def config_dirs_connus():
    """Tous les dossiers de config Claude ou chercher un journal de session :
    ceux des moteurs declares, puis le dossier par defaut."""
    out = []
    for m in charger_moteurs().get('moteurs') or []:
        if m.get('config_dir'):
            out.append(os.path.expanduser(m['config_dir']))
    for c in (claude_config_defaut(), str(pathlib.Path.home() / '.claude')):
        if c not in out:
            out.append(c)
    return out


def dossier_state():
    return pathlib.Path(os.environ.get('FLUX_STATE_DIR') or (racine() / 'state'))


# ── LE LOT DE MESURE EST CONFINE ────────────────────────────────────────────
# Un cas de mesure porte son corrige ; une run qui le joue produit, elle, du
# materiel qui LE TRAHIT : le mail recopie dans le workspace, et surtout la
# sortie du juge, qui imprime `attendu=<valeur du corrige>` ligne a ligne.
# Tant que ce materiel atterrissait sous `state/` et sous le workspace_root
# public du workflow, la console le publiait sans authentification et la
# sauvegarde restic l'emportait sur Google Drive : jouer un cas de mesure le
# brulait. Le confinement ne se demande donc pas en option, il SUIT LA RUN —
# un workspace ouvert sous la racine de mesure emmene son etat avec lui, quel
# que soit l'outil qui le reprend ensuite (run-node, sequenceur, garde, juge)
# et sans variable d'environnement a ne pas oublier.
def racine_mesure():
    return pathlib.Path(os.environ.get('FLUX_MESURE') or '/var/lib/flux-mesure')


def racine_runs_mesure():
    return racine_mesure() / 'runs'


def run_confinee(ws):
    """La run vit-elle sous la racine de mesure ? Alors son etat y reste."""
    try:
        p = pathlib.Path(ws).expanduser().resolve()
        r = racine_runs_mesure().expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return p == r or r in p.parents


def _chemins(var, defaut):
    v = os.environ.get(var)
    out = [pathlib.Path(os.path.expanduser(x)) for x in (v.split(':') if v else []) if x]
    return out or [defaut]


def dossiers_workflows():
    """Les dossiers de graphes, dans l'ordre (FLUX_WORKFLOWS_DIR, separes par « : »)."""
    return _chemins('FLUX_WORKFLOWS_DIR', racine() / 'workflows')


def dossier_workflows():
    """Le premier dossier de graphes (celui ou l'on ecrit)."""
    return dossiers_workflows()[0]


def fichier_workflow(wid):
    """Le fichier du graphe `wid`, cherche dans chaque dossier ; le premier gagne."""
    for d in dossiers_workflows():
        p = d / (str(wid) + '.json')
        if p.exists():
            return p
    return dossier_workflows() / (str(wid) + '.json')


def tous_les_graphes():
    """Tous les graphes visibles, un par identifiant (le premier dossier gagne)."""
    vus = {}
    for d in dossiers_workflows():
        for p in sorted(d.glob('*.json')) if d.is_dir() else []:
            vus.setdefault(p.stem, p)
    return [vus[k] for k in sorted(vus)]


def claude_bin():
    if os.environ.get('FLUX_CLAUDE_BIN'):
        return os.environ['FLUX_CLAUDE_BIN']
    import shutil
    return shutil.which('claude') or 'claude'


def now():
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec='seconds').replace('+00:00', 'Z'))


def instant(s):
    try:
        t = datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return t
    except Exception:
        return None


def ecoule_s(depuis):
    t = instant(depuis)
    if not t:
        return None
    return max(0, int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()))


def lire_json(p, defaut=None):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))
    except Exception:
        return defaut


def ecrire_json(p, d):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, p)


# ───────────────────────────────────────────────── graphe et moteurs

def charger_graphe(wid):
    p = fichier_workflow(wid)
    if not p.exists():
        return None
    return lire_json(p)


def noeuds(wf):
    return (wf or {}).get('nodes') or (wf or {}).get('noeuds') or []


def noeud(wf, nid):
    return next((n for n in noeuds(wf) if n.get('id') == nid), None)


def fichier_moteurs():
    """`moteurs.json` (reglages locaux, non suivi par git), sinon l'exemple du depot."""
    if os.environ.get('FLUX_MOTEURS'):
        return pathlib.Path(os.environ['FLUX_MOTEURS'])
    f = racine() / 'moteurs.json'
    return f if f.exists() else racine() / 'moteurs.example.json'


def charger_moteurs():
    return lire_json(fichier_moteurs(), {}) or {}


# Tarifs de repli, en dollars par million de jetons, si moteurs.json n'en
# porte pas : Opus (5), Sonnet (4.5), Haiku (4.5), au tarif public.
TARIFS_DEFAUT = {
    'opus':   {'entree': 5.00, 'sortie': 25.00, 'cache_ecrit': 6.25, 'cache_lu': 0.50},
    'sonnet': {'entree': 3.00, 'sortie': 15.00, 'cache_ecrit': 3.75, 'cache_lu': 0.30},
    'haiku':  {'entree': 1.00, 'sortie': 5.00,  'cache_ecrit': 1.25, 'cache_lu': 0.10},
}


def tarif(modele):
    """Le tarif du modele : {entree, sortie, cache_ecrit, cache_lu} en $/M. Ollama = 0."""
    if str(modele).startswith('ollama:'):
        return {'entree': 0.0, 'sortie': 0.0, 'cache_ecrit': 0.0, 'cache_lu': 0.0}
    t = (charger_moteurs().get('tarifs') or {}).get(modele) or TARIFS_DEFAUT.get(modele)
    if not t:
        t = TARIFS_DEFAUT['opus']
    return t


def cout_usd(modele, neuf=0, sortie=0, cache_ecrit=0, cache_lu=0):
    t = tarif(modele)
    return round((neuf * t['entree'] + sortie * t['sortie'] + cache_ecrit * t['cache_ecrit']
                  + cache_lu * t['cache_lu']) / 1e6, 5)


def moteur_api_llamacpp_url(ws=None):
    """L'adresse du llama-server (le moteur du paquet) :  de la run, env, moteurs.json."""
    if ws:
        try:
            u = (pathlib.Path(ws) / 'state' / 'llamacpp-url').read_text(encoding='utf-8').strip()
            if u:
                return u.rstrip('/')
        except Exception:
            pass
    u = os.environ.get('FLUX_LLAMACPP_URL')
    if u:
        return u.rstrip('/')
    cfg = (charger_moteurs().get('moteurs_api') or {}).get('llamacpp') or {}
    return (cfg.get('url') or 'http://127.0.0.1:8080').rstrip('/')


def moteur_api_ollama_url(ws=None):
    """L'adresse d'Ollama : parametre de run (`state/ollama-url`), puis moteurs.json, puis env."""
    if ws:
        try:
            u = (pathlib.Path(ws) / 'state' / 'ollama-url').read_text(encoding='utf-8').strip()
            if u:
                return u.rstrip('/')
        except Exception:
            pass
    u = os.environ.get('FLUX_OLLAMA_URL')
    if u:
        return u.rstrip('/')
    cfg = (charger_moteurs().get('moteurs_api') or {}).get('ollama') or {}
    return (cfg.get('url') or 'http://127.0.0.1:11434').rstrip('/')


def config_dir_anthropic(ws=None):
    """Le dossier de config Claude pour un appel `claude -p` : celui du moteur de la run."""
    cfg = charger_moteurs()
    table = {m.get('id'): m for m in cfg.get('moteurs') or []}
    choix = None
    if ws:
        try:
            choix = (pathlib.Path(ws) / 'state' / 'moteur').read_text(encoding='utf-8').strip()
        except Exception:
            pass
    m = table.get(choix) or table.get(cfg.get('defaut')) or {}
    api = (cfg.get('moteurs_api') or {}).get('anthropic') or {}
    cd = m.get('config_dir') or api.get('config_dir')
    cd = os.path.expanduser(cd) if cd else claude_config_defaut()
    return cd, (m.get('id') or cfg.get('defaut') or 'defaut')


def verifier_version_claude():
    """La version de Claude Code est-elle celle que moteurs.json epingle ?

    Rend (ok, message). Une version differente est REFUSEE : le moteur tmux lit
    l'ecran d'une TUI, et une TUI change d'une version a l'autre. On passe outre
    en dev avec FLUX_IGNORER_VERSION=1, et le message le dit.
    """
    attendue = str(charger_moteurs().get('claude_version') or '').strip()
    if not attendue:
        return True, 'aucune version epinglee dans moteurs.json'
    try:
        out = subprocess.run([claude_bin(), '--version'], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception as e:
        return False, 'claude --version injoignable (%s) : %s' % (claude_bin(), e)
    import re
    m = re.search(r'\d+\.\d+\.\d+', out or '')
    vue = m.group(0) if m else (out or '').strip()
    if vue == attendue:
        return True, 'Claude Code %s (epinglee)' % vue
    msg = ('Claude Code %s installee, %s attendue (moteurs.json:claude_version). '
           'Le moteur tmux lit l\'ecran d\'une TUI qui change entre deux versions : '
           'mets a jour l\'epingle apres avoir teste, ou pose FLUX_IGNORER_VERSION=1 '
           'pour passer outre en developpement.' % (vue, attendue))
    if os.environ.get('FLUX_IGNORER_VERSION') == '1':
        return True, 'ATTENTION — ' + msg + ' (ignore : FLUX_IGNORER_VERSION=1)'
    return False, msg


# ───────────────────────────────────────────────── run.json

STATUTS_RUN = ('en-cours', 'bloque', 'terminee', 'close')


def dossier_run(wid, ws):
    nom = os.path.basename(os.path.normpath(ws))
    if run_confinee(ws):
        # 0700 a chaque etage : la console (qui ne lit que <racine>/state) n'y
        # voit rien, et rien d'autre que `claude` ne peut meme traverser.
        d = racine_runs_mesure() / 'state' / str(wid) / nom
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        for e in (d, d.parent, d.parent.parent):
            try:
                os.chmod(e, 0o700)
            except OSError:
                pass
        return d
    d = dossier_state() / str(wid) / nom
    d.mkdir(parents=True, exist_ok=True)
    return d


def chemin_run_json(wid, ws):
    return dossier_run(wid, ws) / 'run.json'


def lire_run(wid, ws):
    """L'etat de la run. Une run d'avant la v2 n'a pas de run.json : on la lit
    comme une production en cours, sans cas — c'est ce qu'elle etait."""
    d = lire_json(chemin_run_json(wid, ws), None)
    if not isinstance(d, dict):
        d = {'workflow': wid, 'run': os.path.basename(os.path.normpath(ws)),
             'workspace': os.path.abspath(ws), 'mode': 'production', 'cas': None,
             'statut': 'en-cours', 'herite': True}
    d.setdefault('mode', 'production')
    d.setdefault('statut', 'en-cours')
    d.setdefault('cas', None)
    return d


def ecrire_run(wid, ws, **champs):
    d = lire_run(wid, ws)
    d.pop('herite', None)
    d.update(champs)
    d['mis_a_jour'] = now()
    ecrire_json(chemin_run_json(wid, ws), d)
    return d


def mode_run(wid, ws):
    return lire_run(wid, ws).get('mode') or 'production'


def parametres_run(wid, ws):
    """Les parametres de la run : run.json `parametres`, completes par WORKSPACE.txt."""
    d = dict(lire_run(wid, ws).get('parametres') or {})
    try:
        for l in open(os.path.join(ws, 'WORKSPACE.txt'), encoding='utf-8'):
            if ':' in l:
                k, v = l.split(':', 1)
                d.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return d


def modele_du_noeud(wid, ws, node, wf=None):
    """Le modele du nœud, `{param}` substitue par le parametre de run du meme
    nom (defaut du `parametres_run` du graphe s'il manque). None si le nœud
    n'en declare pas."""
    m = node.get('modele')
    if not m:
        return None
    import re
    if '{' not in str(m):
        return m
    prm = parametres_run(wid, ws)
    defauts = {p.get('id'): p.get('defaut') for p in ((wf or charger_graphe(wid) or {}).get('parametres_run') or [])}

    def _sub(mm):
        k = mm.group(1)
        v = prm.get(k) or defauts.get(k)
        return str(v) if v else mm.group(0)
    out = re.sub(r'\{(\w+)\}', _sub, str(m))
    return out if '{' not in out else None


# ───────────────────────────────────────────────── passes

COMPTES_CONNUS = ('perso', 'defaut')


def normaliser_passe(x):
    """Une passe, dans la forme v2, quel que soit son age.

    Ne modifie pas `x` : rend une copie completee. Les champs v2 absents
    valent None/[] ; un `moteur` qui est un identifiant de compte (avant la
    v2) devient `compte`, et le moteur vaut `tmux`.
    """
    p = dict(x or {})
    m = p.get('moteur')
    if m not in ('tmux', 'api'):
        comptes = tuple(mm.get('id') for mm in (charger_moteurs().get('moteurs') or [])) \
            or COMPTES_CONNUS
        if m in comptes or m in COMPTES_CONNUS:
            p.setdefault('compte', m)
            p['moteur'] = 'tmux'
        elif m is None:
            p['moteur'] = 'tmux' if p.get('session') or p.get('claude_session_id') else None
    p.setdefault('compte', None)
    p.setdefault('modele', None)
    p.setdefault('entree', [])
    p.setdefault('sortie', [])
    p.setdefault('note', None)
    p.setdefault('garde', None)
    p.setdefault('mesures', None)
    p.setdefault('panne', None)
    # Une passe close sur `session-morte` avant la v2 portait un verdict vert :
    # en lecture, elle est une panne (D5). On ne reecrit pas le fichier.
    if p.get('fin') in ('session-morte', 'lanceur-disparu', 'panne'):
        p['est_panne'] = True
        if not p.get('panne'):
            p['panne'] = {'cause': p.get('fin') if p.get('fin') != 'panne' else 'inconnue',
                          'constat': 'passe close sur %s (lecture v2)' % p.get('fin'),
                          'releve_par': 'lecture'}
    else:
        p['est_panne'] = bool(p.get('panne')) and p.get('verdict') != 'vert'
    return p


def passe_verte(node, passe, mode):
    """La passe est-elle verte au sens du sequenceur ? Rend (bool, motif).

    - une panne n'est jamais verte ;
    - en banc, il faut une note, et note.score >= seuil (le `seuil` du nœud,
      sinon le bareme entier) ;
    - en production, verdict vert (livrables presents) suffit.
    """
    p = normaliser_passe(passe)
    if p.get('est_panne') or p.get('verdict') == 'panne':
        return False, 'panne : ' + str((p.get('panne') or {}).get('cause') or p.get('fin'))
    if p.get('verdict') != 'vert':
        return False, 'verdict ' + str(p.get('verdict'))
    if mode == 'banc' and node.get('juge') is not None:
        n = p.get('note')
        if not isinstance(n, dict) or n.get('score') is None:
            return False, 'banc : passe sans note'
        bareme = n.get('bareme') or (node.get('juge') or {}).get('bareme') or 0
        seuil = node.get('seuil')
        if seuil is None:
            seuil = bareme
        try:
            ok = float(n['score']) >= float(seuil)
        except Exception:
            return False, 'note illisible'
        return ok, 'note %s/%s (seuil %s)' % (n['score'], bareme, seuil)
    return True, 'vert'


# ───────────────────────────────────────────────── juge

def resoudre_chemin(candidat, bases):
    """Un chemin declare par le graphe : absolu tel quel, sinon le premier qui
    existe parmi `bases`, sinon dans la premiere base.

    Conventions du contrat v2 : `juge.script` et `schema` relatifs a la RACINE
    du depot ; `juge.corrige` relatif a `cas/<cas>/` ; `entree`, `livrables`,
    `juges[].sortie` relatifs au workspace.
    """
    if not candidat:
        return None
    c = pathlib.Path(candidat)
    if c.is_absolute():
        return c
    bases = [pathlib.Path(b) for b in bases if b]
    for base in bases:
        if (base / c).exists():
            return base / c
    return (bases[0] / c) if bases else c


def chemin_racine(candidat, ws=None):
    return resoudre_chemin(candidat, [racine(), ws])


def dossier_cas(wid, ws):
    cas = lire_run(wid, ws).get('cas')
    if not cas:
        return None
    p = pathlib.Path(cas)
    if p.is_absolute():
        return p
    return racine() / 'cas' / cas


def _juge_module():
    """`runtime/juge.py` du contrat v2 (juger_noeud, JugeEnPanne), s'il est la.
    FLUX_JUGE_DIR pointe un autre dossier (tests contre un autre chantier)."""
    import importlib.util
    for d in (os.environ.get('FLUX_JUGE_DIR'), str(racine() / 'runtime')):
        if not d:
            continue
        f = pathlib.Path(d) / 'juge.py'
        if not f.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location('flux_juge', str(f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, 'juger_noeud'):
                return mod
        except Exception:
            continue
    return None


def noter(wid, node, ws, sorties=None, pdir=None):
    """Lance le juge du nœud et rend la note {score, bareme, lignes[, juges[]]}.

    Contrat (design § 3.3, chantier contrat) : `from juge import juger_noeud,
    JugeEnPanne` ; `juger_noeud(noeud, cas=<cas/<id>>, workspace=<ws>)` rend
    la note a ecrire telle quelle dans `note` ; `JugeEnPanne` -> passe en
    panne. `juge.type == presence` -> runtime/juges/presence.py. Un nœud peut
    porter `juge` ou `juges: [{script, corrige, bareme, sortie}]`.

    Tant que runtime/juge.py n'est pas la (chantier parallele), un repli
    applique les memes conventions : script relatif a la racine, corrige au
    cas, sortie au workspace, `--bareme=N` et FLUX_BAREME au script, JSON
    {score, bareme, lignes} attendu sur stdout.

    Leve RuntimeError si le juge plante : en banc, une etape non notee est une
    panne, pas un vert par defaut.
    """
    cas_dir = dossier_cas(wid, ws)
    mod = _juge_module()
    if mod is not None:
        try:
            note = mod.juger_noeud(node, cas=str(cas_dir) if cas_dir else None,
                                   workspace=str(ws), racine=racine())
        except TypeError:
            note = mod.juger_noeud(node, cas=str(cas_dir) if cas_dir else None, workspace=str(ws))
        except mod.JugeEnPanne as e:
            raise RuntimeError(str(e))
        note = dict(note)
        note.setdefault('type', 'presence' if (node.get('juge') or {}).get('type') == 'presence' else 'script')
        note['lanceur'] = 'runtime/juge.py'
        if pdir:
            _ecrire_juge_out(pdir, note)
        return note
    # ── repli
    if 'juges' in node:
        total, bareme, lignes, detail = 0, 0, [], []
        for j in node['juges']:
            rep = _juger_un(j, [j['sortie']], ws, cas_dir, pdir)
            total += rep['score']
            bareme += rep['bareme']
            lignes.extend('[%s] %s' % (j['sortie'], l) for l in rep['lignes'])
            detail.append({'sortie': j['sortie'], 'script': j.get('script'), **rep})
        note = {'score': total, 'bareme': bareme, 'lignes': lignes, 'juges': detail, 'type': 'script'}
    elif node.get('juge') is not None:
        j = node['juge']
        rep = _juger_un(j, sorties or list(node.get('livrables') or []), ws, cas_dir, pdir)
        note = {**rep, 'type': 'presence' if j.get('type') == 'presence' else 'script'}
    else:
        raise RuntimeError('nœud %s : ni `juge` ni `juges`' % node.get('id'))
    note['lanceur'] = 'socle (repli)'
    if pdir:
        _ecrire_juge_out(pdir, note)
    return note


def _ecrire_juge_out(pdir, note):
    try:
        (pathlib.Path(pdir) / 'juge.out').write_text(json.dumps(note, ensure_ascii=False, indent=1),
                                                     encoding='utf-8')
    except Exception:
        pass


def _juger_un(j, sorties, ws, cas_dir, pdir=None):
    """Un juge declare, sur ses sorties. Rend {score, bareme, lignes} ou leve."""
    juges_dir = racine() / 'runtime' / 'juges'
    if j.get('type') == 'presence':
        script, corrige, bareme = juges_dir / 'presence.py', '-', j.get('bareme', 20)
    else:
        script = resoudre_chemin(j.get('script'), [racine(), cas_dir, ws])
        corrige = resoudre_chemin(j.get('corrige'), [cas_dir, racine(), ws]) if j.get('corrige') else None
        bareme = j.get('bareme')
        if not script or not pathlib.Path(script).exists():
            raise RuntimeError('juge introuvable : %s (relatif a %s)' % (j.get('script'), racine()))
        if j.get('corrige') and not pathlib.Path(corrige).exists():
            raise RuntimeError('corrige introuvable : %s (cherche dans %s)' % (j.get('corrige'), cas_dir))
        corrige = str(corrige) if corrige else '-'
    if not sorties:
        raise RuntimeError('aucune sortie a juger')
    sorties_abs = [str(pathlib.Path(ws) / s) for s in sorties]
    if pathlib.Path(script).name != 'presence.py':
        for s in sorties_abs:
            if not os.path.exists(s):
                raise RuntimeError('sortie absente : %s — l\'etape n\'a pas rendu son livrable' % s)
    cmd = [sys.executable, str(script), str(corrige)] + sorties_abs
    env = dict(os.environ)
    if bareme is not None:
        cmd.append('--bareme=%s' % bareme)
        env['FLUX_BAREME'] = str(bareme)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError('juge %s : plus de 120 s sans reponse' % script)
    except Exception as e:
        raise RuntimeError('juge injoignable : %s' % e)
    if r.returncode != 0:
        raise RuntimeError('juge %s : exit %d — %s' % (script, r.returncode,
                                                       (r.stderr or r.stdout or '')[-300:]))
    note = _lire_note(r.stdout or '')
    if note is None:
        raise RuntimeError('juge %s : stdout n\'est pas une note — %r' % (script, (r.stdout or '')[-200:]))
    if bareme is not None and note.get('bareme') not in (None, 0) and float(note['bareme']) != float(bareme):
        raise RuntimeError('juge %s : rend un bareme de %s, le graphe declare %s'
                           % (script, note['bareme'], bareme))
    if note.get('bareme') in (None, 0):
        note['bareme'] = bareme
    if not isinstance(note.get('score'), (int, float)) or isinstance(note.get('score'), bool):
        raise RuntimeError('juge %s : score non numerique (%r)' % (script, note.get('score')))
    return note


def _lire_note(txt):
    """{score, bareme, lignes} depuis un stdout : JSON en dernier objet, ou « SCORE s/t »."""
    import re
    lignes = txt.strip().splitlines()
    for l in reversed(lignes):
        l = l.strip()
        if l.startswith('{'):
            try:
                d = json.loads(l)
                if isinstance(d, dict) and 'score' in d:
                    return {'score': d.get('score'), 'bareme': d.get('bareme'),
                            'lignes': d.get('lignes') or d.get('detail') or []}
            except Exception:
                pass
    try:
        d = json.loads(txt)
        if isinstance(d, dict) and 'score' in d:
            return {'score': d.get('score'), 'bareme': d.get('bareme'),
                    'lignes': d.get('lignes') or d.get('detail') or []}
    except Exception:
        pass
    m = re.search(r'SCORE\s+([\d.]+)\s*/\s*([\d.]+)', txt)
    if m:
        s, t = float(m.group(1)), float(m.group(2))
        return {'score': int(s) if s == int(s) else s, 'bareme': int(t) if t == int(t) else t,
                'lignes': [l for l in lignes if not l.startswith('SCORE')]}
    return None


# ───────────────────────────────────────────────── etat compact (garde)

def ecrire_etat_compact(wid, ws, **extra):
    """L'etat qu'un petit modele peut lire d'un coup : nœud courant, depuis
    quand, derniere passe, fin, note, tentatives, session vivante. Ecrit par
    le runtime a chaque evenement, et relu par la garde a chaque tick."""
    d = dossier_run(wid, ws)
    run = lire_run(wid, ws)
    wf = charger_graphe(wid) or {}
    etats = []
    courant = None
    for n in noeuds(wf):
        st = lire_json(d / (n['id'] + '.json'), {}) or {}
        ps = [normaliser_passe(x) for x in (st.get('passages') or [])]
        der = ps[-1] if ps else None
        e = {'noeud': n['id'], 'n': n.get('n'), 'nom': n.get('name'),
             'statut': st.get('statut', 'a-faire'),
             'passes': len(ps),
             'derniere_passe': None}
        if der:
            sess = der.get('session')
            vivante = bool(sess) and subprocess.call(
                ['tmux', 'has-session', '-t', sess], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL) == 0
            e['derniere_passe'] = {
                'n': der.get('n'), 'moteur': der.get('moteur'), 'modele': der.get('modele'),
                'started': der.get('started'), 'ended': der.get('ended'),
                'depuis_s': ecoule_s(der.get('started')),
                'fin': der.get('fin'), 'verdict': der.get('verdict'),
                'panne': (der.get('panne') or {}).get('cause') if der.get('panne') else None,
                'note': der.get('note') and {'score': der['note'].get('score'),
                                             'bareme': der['note'].get('bareme')},
                'garde': der.get('garde'),
                'session_vivante': vivante,
            }
        if st.get('statut') == 'en-cours':
            courant = e
        etats.append(e)
    # Rien en cours (run bloquee, entre deux nœuds) : le nœud courant est
    # celui sur lequel la run est arretee — c'est de lui que la garde parle.
    if courant is None:
        nid_run = run.get('bloque_sur') or run.get('noeud_courant')
        courant = next((e for e in etats if e['noeud'] == nid_run), None)
    compact = {'workflow': wid, 'run': run.get('run'), 'mode': run.get('mode'),
               'statut_run': run.get('statut'), 'a': now(),
               'noeud_courant': courant, 'noeuds': etats, **extra}
    ecrire_json(d / 'etat-compact.json', compact)
    return compact


# ───────────────────────────────────────────────── notification

def notif_url():
    return os.environ.get('FLUX_NOTIF_URL', '')


def notifier(wid, ws, etape, detail, sujet=None):
    """Previent l'operateur : une adresse HTTP (FLUX_NOTIF_URL, parametres
    dans l'adresse), sinon la commande FLUX_ALERTE_CMD (--sujet, --corps).
    Rend True si un canal a repondu ; sans aucun des deux, rien n'est envoye."""
    nom = os.path.basename(os.path.normpath(ws))
    sujet = sujet or (nom.rsplit('-', 3)[0] if nom.count('-') >= 3 else nom)
    q = urllib.parse.urlencode({'veille': sujet, 'etape': str(etape), 'detail': str(detail),
                                'workflow': wid, 'run': nom})
    if notif_url():
        try:
            subprocess.run(['curl', '-s', '-o', '/dev/null', '-m', '8', '-f', notif_url() + '?' + q],
                           timeout=15, check=True)
            return True
        except Exception:
            pass
    cmd = os.environ.get('FLUX_ALERTE_CMD')
    if not cmd:
        return False
    try:
        subprocess.run(cmd.split() + [
                        '--sujet', 'Flux — ' + str(etape),
                        '--corps', 'Run ' + nom + ' (' + wid + ') : ' + str(detail)],
                       timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ───────────────────────────────────────────────── ecrans

ECRANS_MAX = 200


def tourner_ecrans(pdir, garder=ECRANS_MAX):
    """Ne garde que les `garder` derniers ecrans d'une passe (index compris)."""
    d = pathlib.Path(pdir) / 'ecrans'
    idx_f = d / 'index.json'
    idx = lire_json(idx_f, [])
    if not isinstance(idx, list) or len(idx) <= garder:
        return 0
    a_jeter = idx[:-garder]
    for e in a_jeter:
        try:
            (d / ('%05d.ans' % int(e.get('n')))).unlink()
        except Exception:
            pass
    ecrire_json(idx_f, idx[-garder:])
    return len(a_jeter)


def purger_ecrans(wid, ws):
    """A la cloture d'une run : plus d'ecrans du tout, run.out reste."""
    n = 0
    for d in glob.glob(str(dossier_run(wid, ws) / '*' / 'passes' / '*' / 'ecrans')):
        for f in glob.glob(os.path.join(d, '*')):
            try:
                os.unlink(f)
                n += 1
            except Exception:
                pass
        try:
            os.rmdir(d)
        except Exception:
            pass
    return n
