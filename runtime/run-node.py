#!/usr/bin/env python3
"""
Flux — exécuteur d'UN nœud de workflow.

Le graphe (`workflows/<wf>.json`) est la vérité exécutable. Ce script prend un
nœud et l'exécute seul :

  1. GATE D'ENTRÉE — les livrables des nœuds amont existent-ils ? sinon BLOQUÉ.
  2. BRIEF — construit le prompt de l'agent depuis le nœud : objectif, tâches
     (check-list obligatoire), livrables attendus, consignes assemblées en prompt permanent.
  3. SPAWN — lance l'agent (`claude -p`) avec la whitelist d'outils du nœud.
  4. GATE DE SORTIE — les livrables du nœud sont-ils là ? sinon ROUGE. Une
     pipeline peut ajouter un validateur (`gate.validateur`) qui juge aussi
     leur contenu ; sans lui, ce jugement appartient au nœud de vérification.
  5. ÉTAT — écrit tout dans state/<wf>/<run>/<node>.json (statut, timestamps, logs).

Un nœud `human` ne spawn rien : il attend son livrable (produit par un humain).

Usage :
  run-node.py <workflow-id> <node-id> [--workspace DIR] [--dry-run]

Codes : 0 = vert · 2 = gate d'entrée bloqué · 1 = gate de sortie rouge · 3 = erreur.
"""
import sys
import shlex, os, re, json, glob, subprocess, argparse, pathlib, datetime, uuid, hashlib, unicodedata
import time as _tm

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle                                   # le socle commun v2 (run.json, passes, juge)

FLUX = socle.racine()
SKILLS = socle.skills_dir()
PIPELINE_RUNTIME = socle.pipeline_runtime_dir()
# FLUX_VALIDATE : un autre lanceur de controle, appele `<script> <id> <workspace>`
# (0 = vert, 2 = rouge, erreurs numerotees sur stderr).
VALIDATE = pathlib.Path(os.environ.get('FLUX_VALIDATE') or (FLUX / 'runtime' / 'validate.py'))

# Géométrie du terminal des agents. La console web ouvre un xterm.js de la MÊME
# taille : ce que montre l'UI est au pixel près ce que montrerait `tmux attach`.
# la meme conf pour toutes les sessions : c'est elle qui rend la molette
# utilisable dans la console attachee
TMUXCONF = str(FLUX / 'tmux.conf')
TERM_COLS, TERM_ROWS = 160, 45
IDLE_S = 300      # écran figé 5 min sans sentinelle = l'agent a rendu la main
GRACE_S = 45      # après DONE, on attend que l'écran se fige avant de couper
MAX_S  = 6 * 3600 # plafond dur par nœud

def now(): return socle.now()

def archiver_ecran(pdir, snap, etat={}):
    """Garde une trace de chaque ecran distinct, horodatee.

    `run.out` ne contient que le DERNIER ecran : tout ce qui a defile avant est
    perdu. Ici chaque ecran different devient un fichier `ecrans/<seq>.ans`, et
    `ecrans/index.json` en donne la chronologie. C'est ce qui permet de rejouer
    une session apres coup, exactement comme on la voyait en direct.
    """
    import hashlib, json as _json, time as _time
    if not snap:
        return
    d = pathlib.Path(pdir) / 'ecrans'
    h = hashlib.md5(snap.encode('utf-8', 'replace')).hexdigest()
    cle = str(d)
    if etat.get(cle) == h:
        return                                     # rien n'a bouge depuis le dernier
    etat[cle] = h
    d.mkdir(parents=True, exist_ok=True)
    idx_f = d / 'index.json'
    try:
        idx = _json.loads(idx_f.read_text(encoding='utf-8'))
    except Exception:
        idx = []
    seq = len(idx) + 1
    (d / ('%05d.ans' % seq)).write_text(snap, encoding='utf-8')
    idx.append({'n': seq, 'ts': _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())})
    idx_f.write_text(_json.dumps(idx), encoding='utf-8')

def capture(sess, full=False):
    """Le pane tel quel, séquences ANSI comprises (-e) : couleurs, cadres, spinners."""
    cmd = ['tmux', 'capture-pane', '-p', '-e', '-t', sess, '-S', '-3000' if full else '-200']
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ''

def load_wf(wid):
    p = socle.fichier_workflow(wid)
    if not p.exists(): sys.exit(f'workflow introuvable: {p}')
    return json.loads(p.read_text(encoding='utf-8'))

def node_of(wf, nid):
    n = next((x for x in wf['nodes'] if x['id'] == nid), None)
    if not n: sys.exit(f'nœud « {nid} » absent du workflow « {wf["id"]} »')
    return n

def resolve_ws(wf, explicit=None):
    """Workspace actif : le plus récent sous workspace_root contenant WORKSPACE.txt."""
    if explicit: return os.path.abspath(explicit)
    root = str(socle.workspace_root(wf))
    cands = [d for d in glob.glob(os.path.join(root, '*')) if os.path.isfile(os.path.join(d, 'WORKSPACE.txt'))]
    if not cands: sys.exit(f'aucun workspace sous {root} (lance le nœud de préparation d\'abord)')
    return max(cands, key=os.path.getmtime)

def present(ws, pattern):
    """Un livrable est là ? (fichier non vide, dossier non vide, ou motif glob qui matche)"""
    p = os.path.join(ws, pattern)
    if pattern.endswith('/'):
        return os.path.isdir(p) and bool(os.listdir(p))
    if '*' in pattern:
        return any(os.path.exists(g) for g in glob.glob(p))
    return os.path.isfile(p) and os.path.getsize(p) > 0

def champs_run(ws):
    """Les champs du WORKSPACE.txt — owner, subject, ce que l'ouverture y a ecrit."""
    d = {}
    try:
        for l in open(os.path.join(ws, 'WORKSPACE.txt'), encoding='utf-8'):
            if ':' in l:
                k, v = l.split(':', 1)
                d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def matiere_presente(chemin):
    """Ce chemin porte-t-il au moins un fichier non vide ?

    Recursif pour un dossier : une arborescence de dossiers vides ne compte pas
    comme de la matiere. C'est exactement le cas qu'on veut distinguer — un silo
    tout juste cree porte ses dossiers, pas encore ses fichiers.
    """
    if os.path.isfile(chemin):
        return os.path.getsize(chemin) > 0
    if os.path.isdir(chemin):
        for racine, _dirs, fichiers in os.walk(chemin):
            for f in fichiers:
                try:
                    if os.path.getsize(os.path.join(racine, f)) > 0:
                        return True
                except OSError:
                    pass
        return False
    return any(os.path.getsize(g) > 0 for g in glob.glob(chemin) if os.path.isfile(g))


DOSSIER_RAPPORTS = 'rapports-de-passe'


def rapport_de_passe_simple(nid, npass):
    """Le double simple du rapport — le seul que la console affiche.

    Le rapport technique reste entier, pour l'agent d'analyse. Mais personne ne
    lit six paragraphes pour savoir si une passe s'est bien passee : ce fichier
    est ecrit A PART, dans une langue simple, sans jargon, et c'est lui
    seul que flux montre dans sa bulle de passe.
    """
    return f'{DOSSIER_RAPPORTS}/{nid}-p{npass}-simple.md'


def rapport_de_passe(nid, npass):
    """Le rapport que TOUT agent doit rendre a la fin de sa passe.

    Le transcript dit tout, donc il ne dit rien : lire 3 000 lignes pour savoir
    ce qu'une passe a fait n'est a la portee de personne, humain ou agent. Le
    rapport est ce qu'on lit A LA PLACE — et le transcript reste dessous, pour
    quand on veut la preuve.

    Un par PASSE, ecrit par le meneur, avec une ligne par sous-agent : c'est la
    maille de la console (une ligne de la chronologie = une passe = un rapport).
    """
    return f'{DOSSIER_RAPPORTS}/{nid}-p{npass}.md'


def livrables_exiges(node, ws):
    """Les livrables reellement exiges de ce nœud, et ceux dont il est dispense.

    Un livrable declare dans `livrables_conditionnels` n'est exige que si sa
    MATIERE existe : sans matiere, il n'y a rien a produire, et un gate qui
    refuse pour ca tranche contre le travail reel.

    Rend (exiges, dispenses) — dispenses = [{chemin, motif}], ecrit dans l'etat.
    """
    cond = node.get('livrables_conditionnels') or {}
    champs = champs_run(ws) if cond else {}
    exiges, dispenses = [], []
    for d in (node.get('livrables') or []):
        c = cond.get(d)
        if not c:
            exiges.append(d)
            continue
        source = c['exige_si_present']
        for k, v in champs.items():
            source = source.replace('{' + k + '}', v)
        if matiere_presente(source):
            exiges.append(d)
        else:
            dispenses.append({'chemin': d, 'source': source,
                              'motif': c.get('sinon') or 'aucune matiere a ' + source})
    return exiges, dispenses


def upstream(wf, nid):
    return [n for n in wf['nodes'] if nid in (n.get('suivant') or [])]

def state_path(wf, ws, nid):
    return socle.dossier_run(wf['id'], ws) / f'{nid}.json'

def write_state(wf, ws, nid, **kw):
    p = state_path(wf, ws, nid)
    st = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    st.update(kw); st['node'] = nid; st['updated'] = now()
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
    return st

def decisions_du_proprietaire(ws):
    """Les décisions posées par l'opérateur dans `<workspace>/state/DECISION-*.md`.

    Une vérification qui escalade une question — retirer un livrable, accepter
    une dégradation, arbitrer une source — attend une réponse qui n'est pas de
    son ressort. Ces fichiers la portent, et le brief les recopie en entier :
    c'est le seul canal qui atteint l'agent à coup sûr, une session tmux ne
    lisant un message qu'entre deux tours.
    """
    try:
        fs = sorted(glob.glob(os.path.join(ws, 'state', 'DECISION-*.md')))
        return [(os.path.basename(f), open(f, encoding='utf-8').read().strip())
                for f in fs if os.path.getsize(f) > 0]
    except Exception:
        return []


def rapport_de_verification(wf, ws, nid):
    """Le rapport du contrôle qui juge ce nœud, s'il existe et s'il le juge.

    Le graphe apparie un nœud et son contrôle par le rang `n` : le nœud de
    verification qui porte le meme `n` juge ce nœud, et son dernier livrable
    est le rapport. On ne devine pas le chemin, on le lit dans le graphe.

    Rend (chemin, texte) ou (None, None). Aucune exception ne remonte : un
    rapport illisible ne doit pas empecher un nœud de repartir.
    """
    try:
        node = node_of(wf, nid)
        if node.get('verification'):
            return (None, None)         # un contrôle n'est pas juge par un contrôle
        rang = node.get('n')
        ctrl = next((x for x in (wf.get('noeuds') or wf.get('nodes') or [])
                     if x.get('verification') and x.get('n') == rang), None)
        if not ctrl:
            return (None, None)
        livr = [d for d in (ctrl.get('livrables') or []) if d.endswith('.md')]
        if not livr:
            return (None, None)
        p = pathlib.Path(ws) / livr[-1]
        if not p.exists():
            return (None, None)
        return (livr[-1], p.read_text(encoding='utf-8', errors='replace'))
    except Exception:
        return (None, None)


def acquis_du_controle(wf, ws, nid):
    """Les elements que le controle de ce nœud a deja juges conformes.

    Rend {source: [id, ...]} depuis `verifications/etape-<rang>/registre.json`,
    le registre tenu par le controle apparie a ce nœud par le rang `n`.

    A quoi ca sert : la consigne « ne touche pas a ce qui est acquis » ne vaut que
    si l'agent sait CE QUI l'est. Un element conforme qu'il retouche voit son
    empreinte changer, donc repasse au programme du controle — et la passe suivante
    rejuge ce que personne n'avait besoin de rouvrir.

    Aucune exception ne remonte : un registre absent ou illisible ne doit pas
    empecher un nœud de repartir.
    """
    try:
        node = node_of(wf, nid)
        if node.get('verification'):
            return {}
        rang = node.get('n')
        p = pathlib.Path(ws) / 'verifications' / f'etape-{rang}' / 'registre.json'
        if not p.exists():
            return {}
        d = json.loads(p.read_text(encoding='utf-8', errors='replace'))
        unites = d.get('unites') if isinstance(d, dict) else d
        out = {}
        for u in unites or []:
            if not isinstance(u, dict):
                continue
            elems = u.get('elements') or u.get('fiches') or {}
            ids = [i for i, e in elems.items()
                   if isinstance(e, dict) and e.get('verdict') == 'conforme']
            if ids:
                out[u.get('source') or u.get('lot') or u.get('id') or '?'] = sorted(ids)
            elif u.get('verdict') == 'conforme':
                out.setdefault('unites entieres', []).append(u.get('id') or '?')
        return out
    except Exception:
        return {}


def extrait_du_rapport(txt, plafond=3500):
    """Ce qu'on recopie dans le brief : le verdict, et ce qui prescrit la suite.

    Le verdict tient en une ligne quelque part dans l'en-tete ; les gestes
    demandes sont dans la DERNIERE section du rapport. On ne suppose aucun
    numero de section — seulement qu'un rapport se termine par ce qu'il attend.
    """
    lignes = txt.splitlines()
    # Le verdict est un TITRE ou une ligne qui commence par le mot : sans ce
    # resserrement, on ramasse toutes les phrases qui parlent de verdict au fil
    # du texte, et le brief ouvre sur trois lignes hors sujet.
    verdict = [l.strip().lstrip('#').strip() for l in lignes
               if 'erdict' in l and (l.startswith('#') or l.lstrip().startswith('Verdict')
                                     or l.lstrip().startswith('**Verdict'))][:2]
    debuts = [i for i, l in enumerate(lignes) if l.startswith('## ')]
    fin = []
    if debuts:
        bloc = lignes[debuts[-1]:]
        # une derniere section trop courte (un renvoi, une signature) ne dit
        # rien : on remonte d'une section pour attraper celle qui prescrit
        if len('\n'.join(bloc)) < 300 and len(debuts) > 1:
            bloc = lignes[debuts[-2]:]
        fin = bloc
    txt_fin = '\n'.join(fin)
    if len(txt_fin) > plafond:
        txt_fin = txt_fin[:plafond].rsplit('\n', 1)[0] + '\n\n[…] — la suite est dans le rapport.'
    return verdict, txt_fin


def build_brief(node, ws, manques, donef=None, refus=None, rapport=None, acquis=None,
                rapport_passe=None, equipes=None):
    """Le prompt de l'agent = objectif + check-list obligatoire + livrables + reprise."""
    L = []
    L.append(f"# {node['name']}\n")
    L.append(f"## Objectif\n{node['objectif']}\n")
    # Carte(s) agent de la fiche : role, outils, consignes — l'agent doit savoir QUI il est
    for ag in node.get('agents', []):
        L.append(f"## Ton rôle — {ag.get('role','')}")
        if ag.get('outils'):
            L.append("Outils et gestes attendus :")
            for o in ag['outils']:
                L.append(f"- {o}")
        if ag.get('consignes'):
            # A la reprise, l'agent recoit le protocole de REPARATION, pas celui de
            # production : sa carte doit dire ce qu'il a vraiment sous les yeux, sinon
            # il cherchera un mode d'emploi qui n'est plus la.
            if rapport and rapport[0] and node.get('consignes_reprise'):
                L.append("Consignes permanentes — **passe de réparation** :")
                for k in node['consignes_reprise']:
                    L.append(f"- **{k}**")
                L.append("Le protocole de production n'est PAS injecté : tu répares, tu ne "
                         "produis pas. Si une réparation touche un champ dont tu dois vérifier "
                         "la définition, ouvre le skill de l'étape sur disque avec `Read`.")
            else:
                L.append("Consignes permanentes (déjà sous tes yeux, à chaque tour) :")
                for k in ag['consignes']:
                    L.append(f"- **{k.get('nom')}** — {k.get('role','')}")
        # Le rôle est-il EQUIPE par le runtime ? Un sous-agent l'est desormais :
        # il part avec son protocole en prompt permanent, comme le meneur. Sans
        # cette ligne, le meneur croit devoir le lui recopier a la main — et il
        # en perd un morceau a chaque run.
        eq = next((x for x in (equipes or []) if x[1] == (ag.get('role') or '').strip()), None)
        if eq:
            L.append(f"**Ce rôle est ÉQUIPÉ par le flux — tu ne recopies aucun protocole.** "
                     f"Dispatche-le avec `subagent_type: \"{eq[0]}\"` : il démarre avec les "
                     f"protocoles ci-dessus déjà en prompt permanent. Ton prompt ne porte donc "
                     f"que ce qui lui est PROPRE — son périmètre, ses fichiers, sa part du "
                     f"travail. Recopier la méthode par-dessus la double et la fait diverger.")
        L.append("")

    # Une decision du proprietaire prime sur tout ce que le brief dit par ailleurs,
    # y compris sur un rapport de verification qui prescrirait autre chose. Elle
    # passe donc AVANT le cadrage : c'est la premiere chose que l'agent lit.
    for nom, txt in decisions_du_proprietaire(ws):
        L.append("## ⚖ DÉCISION DU PROPRIÉTAIRE DE LA RUN — elle prime sur tout le reste")
        L.append(f"Posée dans `state/{nom}`. Elle a été tranchée par l'opérateur et **ne se "
                 "rediscute pas** : tu l'appliques telle quelle. Si elle contredit ce qu'un "
                 "rapport de vérification te demande, **c'est elle qui l'emporte** — dis-le dans "
                 "ton cadrage et applique la décision.")
        L.append("")
        L.append(txt)
        L.append("")

    # Le cadrage precede tout — y compris la todo-list. Un agent parti de travers
    # ne se voyait qu'a la verification, une passe plus tard, apres avoir ecrit
    # dans les livrables. Ecrit en premier, son cadrage se lit en quelques
    # secondes dans la console et se coupe avant que le travail commence.
    L.append("## ① CADRAGE — ta toute première action, avant tout le reste")
    L.append("**Avant la check-list, avant d'ouvrir un fichier, avant d'écrire une ligne**, "
             "dis en clair, dans ta réponse (pas dans un fichier) :")
    L.append("")
    L.append("1. **Ce que tu as compris** — l'objectif de ce nœud, avec TES mots, pas ceux du "
             "brief recopiés. Si tu reprends une reprise : ce que le contrôle refuse, et ce "
             "qu'il te demande.")
    L.append("2. **Ce que tu vas faire** — tes gestes dans l'ordre, en une ligne chacun, avec "
             "les fichiers que chacun va produire ou modifier.")
    L.append("3. **Ce que tu ne toucheras PAS** — ce qui est déjà acquis, hors de ton "
             "périmètre, ou explicitement exclu.")
    L.append("4. **Ce qui te manque ou te semble contradictoire**, s'il y a lieu. Un doute écrit "
             "ici est une information ; un doute résolu en silence par une supposition est une "
             "faute qui ne se verra qu'à la vérification.")
    L.append("")
    L.append("Sois bref — une quinzaine de lignes suffisent. Ce cadrage n'est pas un livrable : "
             "il est **lu en direct** dans la console pour qu'on voie tout de suite si tu pars "
             "dans la bonne direction, et qu'on te coupe avant que tu écrives si ce n'est pas le "
             "cas.")
    # Dit deux fois, et en gras : un agent qui prend son cadrage pour sa reponse finale
    # rend la main, l'ecran se fige, et le runtime coupe sans qu'un geste ait ete fait.
    # Un message qui ne contient QUE du texte termine le tour : la session se fige
    # et le runtime coupe. La consigne ne peut donc pas etre  n enchaine pas  — il
    # faut que le cadrage et le premier appel d outil tiennent dans le MEME message.
    L.append("**Il n'attend AUCUNE validation, et personne ne te répondra.** Écris-le et "
             "**appelle `TodoWrite` dans le MÊME message, juste après** — un message qui ne "
             "contient que du texte termine ton tour, ta session se fige et le runtime la coupe "
             "sans que rien n'ait été fait. Le cadrage et ton premier appel d'outil partent "
             "ensemble.")
    L.append("")
    reprise = bool(rapport and rapport[0])
    if reprise:
        L.append("## ② CHECK-LIST DE RÉPARATION")
        L.append("**Tu ne rejoues pas cette étape, tu la répares.** Le mode d'emploi de production "
                 "n'est pas rappelé ici : il est dans ton protocole (system prompt), et ce que tu "
                 "dois faire est dans le rapport qui te refuse. Ci-dessous, les items de ta "
                 "todo-list et le livrable que chacun doit laisser derrière lui.")
        L.append("Ton cadrage écrit, ton geste suivant est un appel `TodoWrite` contenant EXACTEMENT ces items, dans cet ordre :\n")
    else:
        L.append("## ② Check-list OBLIGATOIRE")
        L.append("Ton cadrage écrit, ton geste suivant est un appel `TodoWrite` contenant EXACTEMENT ces items, dans cet ordre :\n")
    detail = {t.get('titre'): t for t in node.get('taches_detail', [])}

    def _rendre(d, num, prof):
        """Une tache et ses sous-taches. Chaque niveau porte son objectif et son
        livrable : c'est ce qui rend l'etape verifiable sans la jouer."""
        pad = '   ' * prof
        if d.get('par'):
            L.append(f"{pad}   _par : {d['par']}_")
        if d.get('objectif') and not reprise:
            L.append(f"{pad}   **Objectif** — {d['objectif']}")
        if d.get('livrable'):
            L.append(f"{pad}   **Livrable** — `{d['livrable']}`")
        if d.get('detail') and not reprise:
            for ligne in str(d['detail']).splitlines():
                L.append(f"{pad}   {ligne}" if ligne.strip() else "")
        if d.get('si_succes') and not reprise:
            L.append(f"{pad}   ✔ → {d['si_succes']}")
        if d.get('si_echec') and not reprise:
            L.append(f"{pad}   ✘ → {d['si_echec']}")
        for j, sd in enumerate(d.get('sous_taches') or [], 1):
            sous = f"{num}.{j}"
            L.append(f"{pad}   {sous} {sd.get('titre','')}")
            _rendre(sd, sous, prof + 1)

    # Les taches d'un nœud SONT les sous-etapes de l'etape : le nœud 1 porte 1.1, 1.2…
    # Numeroter a partir de 1 ferait croire que la tache 2 est l'etape 2 (la chasse).
    # Un nœud de verification porte le numero de l'etape qu'il juge, suffixe -verification :
    # un nœud de verification porte le rang de l'etape qu'il juge : ses taches
    # s'ecrivent 1-verification.1, .2, .3 — la verification n'est pas une etape de plus.
    rang = str(node.get('n', '') or 1).split('.')[0]
    if node.get('verification'):
        rang = rang + '-verification'
    for i, t in enumerate(node.get('taches', []), 1):
        num = f"{rang}.{i}"
        L.append(f"{num} {t}")
        _rendre(detail.get(t) or {}, num, 0)
    L.append("\nTu marques chaque item `completed` seulement quand son livrable est RÉELLEMENT écrit.")
    L.append("Un item non fait = tu le fais ; tu ne le sautes pas, tu ne le réinterprètes pas.\n")
    L.append(f"## Workspace\n`{ws}` — tous les chemins de livrables sont relatifs à ce dossier.\n")
    L.append("## Livrables attendus")
    for d in node.get('livrables', []):
        L.append(f"- `{d}`")
    if rapport_passe:
        L.append(f"- `{rapport_passe}` — **ton rapport de passe** (§ ci-dessous)")
    _g = (node.get('gate') or {}).get('validateur')
    if not _g and node.get('verification'):
        L.append("\nChacun doit exister à la fin : le nœud suivant ne démarre pas tant qu'il "
                 "en manque un. Aucun programme ne relit ton verdict — tu es le dernier juge "
                 "de cette étape, et c'est le post-mortem qui rouvrira ton rapport.")
    elif not _g:
        L.append("\nChacun doit exister à la fin : le nœud suivant ne démarre pas tant qu'il "
                 "en manque un. Leur CONTENU est jugé après toi, par des vérificateurs frais "
                 "qui liront ton transcript — ce que tu déclares avoir fait doit s'y retrouver.")
    if _g:
        L.append("\nÀ la fin, un validateur automatique vérifie tes livrables. S'il est rouge, le nœud suivant NE DÉMARRERA PAS.")
        L.append(
            "\n**Controle-toi avant de conclure.** Le meme validateur est a ta disposition :\n"
            + "```\npython3 %s %s %s\n```\n" % (VALIDATE, _g, ws)
            + "Il dit, finding par finding, ce qui ne va pas. Relance-le apres tes corrections et "
            + "ne pose DONE qu'une fois au vert : poser DONE avec des contrats rouges fait echouer "
            + "le nœud, et une nouvelle instance devra refaire le meme travail.")
    # LIRE UNE PAGE. Regle de socle : elle vaut pour tout nœud qui touche au web,
    # quelle que soit la pipeline. `WebFetch` ne rend pas la page — il rend la
    # reponse d'un petit modele qui l'a lue, selon la question posee. Un agent
    # qui l'ignore ecrit ses faits depuis un resume, et un controle les declare
    # inventes alors qu'ils sont sur la page.
    if set(node.get('tools') or []) & {'WebFetch', 'WebSearch'}:
        L.append("""
## LIRE UNE PAGE — télécharge-la, ne la fais pas résumer

**`WebFetch` ne te rend PAS la page.** Il rend la réponse d'un **petit modèle** qui l'a lue à ta
place, en fonction de la question que tu lui as posée. Ce qu'il n'a pas jugé utile de recopier
n'existera jamais pour toi — et un contrôle, plus tard, tiendra ce silence pour la preuve que le
fait n'était pas sur la page.

**Ta voie par défaut est donc le téléchargement**, et tu lis toi-même :

```bash
curl -sL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" "<url>" -o /tmp/p.html
```
puis `Grep` la chaîne qui t'intéresse, ou `Read` le fichier.

**Un `403` de `WebFetch` ne veut pas dire que le site est fermé** : les mêmes URL répondent
`200` en téléchargement avec cet en-tête de navigateur. Ne renonce pas à une source sur ce seul
signal.

**Si le téléchargement échoue vraiment** : `https://r.jina.ai/<url>` (rend le texte complet), puis
`web.archive.org`. **`WebFetch` en dernier recours** — et ce que tu en tires, tu l'écris comme
venant d'un résumé, pas d'une page lue.

**Une page de résultats de recherche n'est jamais une source.** Son résumé non plus : il est
écrit par le moteur, il n'atteste rien.
""")

    if rapport_passe:
        # Le SQUELETTE est du socle : meme en-tete, memes six sections partout.
        # Ce qui change d'une etape a l'autre, c'est ce qu'elle SEULE peut dire —
        # le graphe le declare dans `rapport_de_passe`. Sans ce cadre, chaque
        # agent invente sa forme, et deux passes du meme nœud ne se comparent
        # plus : un rapport libre se lit une fois, jamais deux.
        rp_cfg = node.get('rapport_de_passe') or {}
        chiffres = rp_cfg.get('chiffres') or []
        a_dire = rp_cfg.get('a_dire') or []
        en_rep = rp_cfg.get('en_reparation') or []
        rp_simple = rapport_de_passe_simple(node.get('id', ''), '<n>')
        L.append("\n## TON RAPPORT DE PASSE — le dernier geste, juste avant DONE\n")
        L.append(f"`{rapport_passe}`\n")
        L.append(f"Et son double simple, `{rp_simple}` — voir plus bas.\n")
        L.append(
            "Ce n'est pas un résumé de politesse. C'est ce qu'un humain lira **à la place** de ton\n"
            "transcript, et ce qu'un agent rouvrira dans six semaines pour comprendre ce qui s'est passé\n"
            "ici. Ton transcript fait des milliers de lignes : personne ne le lit, et c'est bien pour ça\n"
            "qu'il faut ce texte-là.\n")
        L.append("**Une page, pas plus.** Un en-tête que lit une machine, puis un texte que lit un humain.\n")
        L.append("\n## APRÈS TON RAPPORT — écris son double SIMPLE\n")
        L.append(f"Un second fichier, `{rp_simple}` (remplace `<n>` par le vrai numéro de "
                 "passe). **C'est lui, et lui seul, que la console montre** — le rapport "
                 "technique ci-dessus reste ouvert au clic, pour qui veut l'analyse détaillée.\n")
        L.append(
            "**La forme : une conclusion, puis des PUCES — jamais un récit.** Ce n'est ni un "
            "tl;dr de deux phrases (le détail compte, garde-le), ni de la prose qui raconte ta "
            "séance dans l'ordre où tu l'as vécue (« j'ai vérifié que… j'ai ensuite… j'ai aussi "
            "trouvé… ») — cette forme-là noie le fait sous le récit de comment tu l'as trouvé, "
            "et double la longueur pour rien. Une puce = un fait, un chiffre, une décision. Le "
            "COMMENT tu l'as établi ne se garde que s'il change ce que le lecteur doit faire "
            "(une source contredit une autre → dis laquelle tranche et pourquoi) ; sinon "
            "seul le fait reste.\n")
        L.append(
            "**Nomme les choses par ce qu'elles font, pas par leur identifiant.** Aucun nom "
            "technique — pas de `verif-essentiels`, pas de `registre.json`, pas de code de "
            "faute — sauf s'il faut vraiment le taper, et alors entre parenthèses, en fin de "
            "puce, jamais en tête. Le jargon se traduit à sa première apparition puis se tient : "
            "pas d'aller-retour entre le mot juste et sa traduction.\n")
        L.append(
            "**La console ne fait AUCUN rendu markdown, sauf deux choses — respecte-les à la "
            "lettre, sinon le texte sort collé en un seul bloc illisible :**\n"
            "- Une LIGNE VIDE entre CHAQUE puce (elle-même entre chaque paragraphe). Le rendu "
            "coupe sur double saut de ligne, jamais sur un seul : deux puces séparées d'un seul "
            "`\\n` s'affichent recollées l'une à l'autre, sans espace ni retour visuel.\n"
            "- `**mot ou chiffre**` devient gras — utilise-le pour faire ressortir le fait "
            "central de chaque puce (un verdict, un chiffre, une décision), jamais la puce "
            "entière.\n")
        L.append(
            "*Ce que ça donne (le contenu change à chaque fois — la ligne vide entre CHAQUE "
            "puce est ce qui manque le plus souvent, vérifie-la avant d'écrire le fichier)* :\n"
            "```markdown\n"
            "**Terrain prêt, tout est vert.**\n\n"
            "- Recherche en ligne, mémoire, doublon de chasse en cours : **les trois testés, "
            "rien à signaler**\n\n"
            "- Équipe de chasse : **11** — un par thème (8 thèmes), plus 3 transversaux (signal "
            "faible, contre-preuve, découverte libre)\n\n"
            "- Écart trouvé entre deux versions du profil de lecture (6 thèmes vs 8) — **les 8 "
            "l'emportent**, les deux manquants restent validés par un retour précédent ; la "
            "version courte mériterait une mise à jour, hors de mon périmètre ici\n\n"
            "- **6 découvertes récentes** mises de côté pour ne pas ressortir en doublon\n\n"
            "- Connexion distante bloquée par une vérification de sécurité, **contournée avec "
            "les mêmes données en local** — à revérifier si une étape suivante en a vraiment "
            "besoin\n\n"
            "La chasse peut démarrer.\n"
            "```\n"
            "Chaque puce reste une ligne ou deux : ce qui déborde ce format n'est pas un détail "
            "de plus, c'est un signal qu'il faudrait peut-être l'écrire ailleurs (le rapport "
            "technique).\n")
        tete = ["---", f"noeud: {node.get('id','')}", "passe: <n>",
                "verdict: reussi | partiel | bloque      # ce que TU constates, pas ce que tu espères",
                "duree: <ce que ça t'a pris>", "sous_agents: <combien tu en as lancés>"]
        for c in chiffres:
            # « cle: ce qu'on veut lire » — la cle part telle quelle dans l'en-tete,
            # le libelle devient la consigne. Deriver la cle du libelle donnait des
            # noms tronques, differents d'une passe a l'autre : illisibles a la machine.
            cle, _, lib = c.partition(':')
            tete.append(f"{cle.strip()}: <{(lib or c).strip()}>")
        tete.append("---")
        L.append("```markdown\n" + "\n".join(tete) + """

# <l'étape, en clair> — passe <n>

**Ce que j'ai fait** — deux ou trois phrases. Le geste, pas la procédure.

**Ce que je rends** — un livrable par ligne, avec ce qu'il porte VRAIMENT. Les chiffres de
l'en-tête se retrouvent ici, en toutes lettres.

**Ce que j'ai trouvé** — le fait saillant, chiffré. Si tu as jugé : le verdict et son motif.

**Mes sous-agents** — une ligne chacun : qui, ce qu'il a rendu, et en un mot ce qu'il faut en
retenir. Un sous-agent mort se dit ici, pas ailleurs.

**Ce qui coince** — ce que tu n'as pas pu faire, et pourquoi. « rien » est une réponse valide
et fréquente ; l'inventer ne l'est pas.

**Pour la suite** — ce que le nœud suivant doit savoir et ne devinerait pas.
```
""")
        if chiffres:
            L.append("**Les chiffres que CE nœud doit porter — aucun ne se saute.** Un chiffre "
                     "que tu n'as pas mesuré s'écrit `non mesuré`, jamais une estimation :\n")
            for c in chiffres:
                L.append(f"- {c}")
            L.append("")
        if a_dire:
            L.append("**Ce qu'on veut savoir de CETTE étape, et qu'aucune autre ne peut dire.** "
                     "Ça vit dans « ce que j'ai trouvé » et « pour la suite » :\n")
            for x in a_dire:
                L.append(f"- {x}")
            L.append("")
        if en_rep and reprise:
            L.append("**Cette passe est une RÉPARATION — ton rapport porte en plus :**\n")
            for x in en_rep:
                L.append(f"- {x}")
            L.append("")
        L.append("Chiffre ce qui se chiffre. N'invente rien : ce rapport se relit contre ton "
                 "transcript, et un écart entre les deux est une faute plus grave qu'un travail "
                 "incomplet.\n")

    # Reprise après refus : sans ça l'agent relancé refait à l'identique la faute
    # que le contrôle vient de rejeter (les livrables sont là, c'est leur CONTENU
    # qui est en cause — le seul "manque" visible est donc vide).
    if rapport and rapport[0]:
        chemin, verdict, fin = rapport
        L.append("\n## ⚠ Ce nœud a DÉJÀ tourné, et le contrôle l'a REFUSÉ")
        L.append(f"**Le rapport qui te refuse : `{chemin}`. Ouvre-le AVANT tout autre geste** — "
                 "avant la check-list, avant d'écrire une ligne. Il dit ce qui ne va pas, "
                 "livrable par livrable, et souvent la correction exacte à appliquer.")
        L.append("Tu reprends ce travail pour le corriger. Tu ne repars pas de zéro, et tu ne "
                 "touches PAS à ce que le rapport ne refuse ni ne prescrit : ce qui est jugé "
                 "conforme et sans geste prescrit est acquis, le rerédiger perd du travail validé "
                 "et en casse une partie.")
        # La consigne ci-dessus ne vaut que si l'agent sait CE QUI est acquis :
        # un element conforme retouche change d'empreinte et repasse au controle.
        if acquis:
            L.append("\n**Ce qui est ACQUIS — n'y touche pas de ta propre initiative.** Le "
                     "contrôle a déjà jugé ces éléments conformes ; les rouvrir pour faire propre "
                     "change leur empreinte et les remet au programme de la passe suivante, pour "
                     "rien.")
            # Décision du 2026-09-03 : un acquis peut porter des points perdus (precision non
            # attestee, champs jumeaux desynchronises, ecart de style). Les laisser dormir
            # rend le seuil inatteignable par arithmetique. Le rapport les prescrit, donc
            # la prescription prime sur la consigne de non-retouche.
            L.append("**La seule exception : un geste que le rapport PRESCRIT nommément sur l'un "
                     "d'eux.** Un élément conforme peut porter des points perdus — une précision "
                     "que rien n'atteste, deux champs jumeaux qui divergent, un écart au profil de "
                     "style. Le rapport te le nomme avec le passage et le geste attendu : tu "
                     "l'appliques, et **rien d'autre** sur cet élément. Une prescription prime "
                     "toujours sur la consigne de non-retouche ; l'inverse fige des points perdus "
                     "que plus aucune passe ne pourra jamais récupérer.")
            for source, ids in acquis.items():
                apercu = ', '.join(ids[:40]) + (f' … (+{len(ids)-40})' if len(ids) > 40 else '')
                L.append(f"- `{source}` — {len(ids)} acquis : {apercu}")
        # R1 — sans elle, la boucle fabrique son propre carburant : chaque
        # correction cree un enonce neuf que le juge frais suivant refusera.
        L.append("\n**Une correction RECOPIE la source, elle ne rédige pas.** Ce que tu écris à "
                 "la place d'un passage refusé doit **exister dans la source** — une phrase "
                 "recopiée, un chiffre lu dans le texte, une réserve que la source pose. "
                 "**Tu ne reformules pas, tu ne calcules pas, tu ne résumes pas.**")
        L.append("Le motif n'est pas stylistique : un juge frais lira ta correction sans savoir "
                 "qu'elle en est une, la comparera à la source, et refusera tout énoncé qu'il n'y "
                 "trouve pas. Reformuler te fait donc *fabriquer* le prochain refus. Si aucune "
                 "correction ne peut être une recopie — parce que la source ne dit plus ce que le "
                 "livrable affirme — **ne rédige pas : dis-le, et laisse le contrôle escalader**.")
        if verdict:
            L.append("\nSon verdict :")
            for v in verdict:
                L.append(f"> {v}")
        if fin:
            L.append("\nCe qu'il demande — recopié de sa dernière section, "
                     "le rapport complet fait foi :\n")
            L.append(fin)
    elif refus:
        L.append("\n## ⚠ Ce nœud a DÉJÀ tourné et le contrôle l'a REFUSÉ")
        L.append("Les livrables existent mais leur contenu casse ces contrats. "
                 "Tu reprends le travail pour les corriger — tu ne repars pas de zéro, "
                 "et tu ne te contentes pas de relire : tu MODIFIES les fichiers en cause.")
        for e in refus:
            L.append(f"- {e}")
    # LE GROUPAGE DES APPELS. Regle de SOCLE, posee dans le brief de tout nœud :
    # ce qui coute n'est pas ce que l'agent ecrit, c'est le nombre de fois ou
    # tout l'historique repart au modele. Elle ne depend d'aucune pipeline.
    L.append("\n## ⚡ Groupe les gestes qui ne s'attendent pas")
    L.append("**Un tour de parole coûte tout l'historique, pas seulement ce que tu demandes.** "
             "Le modèle n'a aucune mémoire entre deux tours : à chaque appel, la consigne et "
             "TOUS les résultats déjà obtenus repartent avec. Ton dixième appel transporte donc "
             "les neuf précédents.")
    L.append("**Donc : tout ce qui ne dépend pas d'un résultat précédent part dans le MÊME tour.** "
             "Lire quatre fichiers, lancer trois commandes indépendantes, écrire cinq traces — "
             "un seul tour à chaque fois. Tu ne sépares que là où tu as besoin de la réponse "
             "d'avant pour décider de la suite.")
    # LE DECLENCHEUR. La regle seule ne mordait pas : mesuree sur une run entiere,
    # 1,33 appel par tour chez le chef le plus cher. Un agent ne VOIT pas qu'il
    # enchaine — il lui faut un signal qu'il peut se donner a lui-meme.
    L.append("**Le signal que tu te donnes : deux appels seuls d'affilée.** Quand tu viens d'en "
             "faire deux, tu ne lances pas le troisième — tu demandes d'abord *qu'est-ce que je "
             "sais déjà devoir faire ensuite, et qui n'attend pas ce résultat ?*, et tu les envoies "
             "ensemble. **Vise trois gestes par tour** dès qu'il s'agit de mesurer, lire, compter "
             "ou tracer ; un tour à un seul appel doit être un choix, pas une habitude.")
    L.append("**Le cas qui domine, et il est facile à rater : les petits scripts de mesure.** "
             "Écrire un script jetable par fichier à mesurer, c'est un tour par fichier. Écris-le "
             "UNE fois, fais-lui prendre la liste complète, et lis tout d'un coup — la même mesure, "
             "en un tour au lieu de vingt.")
    # L'OUTIL QUI REND CES SCRIPTS INUTILES. Nomme dans le brief plutot que dans
    # un skill : il ne depend d'aucune pipeline, et recopie dans dix skills il
    # divergerait. Ce que l'agent ne connait pas, il le reecrit.
    if (PIPELINE_RUNTIME / 'etat-run.py').exists():
      L.append("**Et le plus souvent, tu n'as même pas à l'écrire.** "
             f"`python3 {PIPELINE_RUNTIME / 'etat-run.py'} <workspace>` rend en UN appel "
             "l'inventaire de la run : le nombre d'éléments de chaque source, les champs que tous "
             "portent et ceux qui manquent (avec la liste des id qui manquent), les répartitions "
             "par tier, axe, rôle, verdict, les longueurs en mots (min/médiane/max), les id en "
             "double, et l'empreinte md5 de chaque livrable. "
             "`--element <id>` rend tout ce que la run sait d'un élément, toutes sources "
             "confondues ; `--champs a,b` dit qui porte ces champs et qui ne les porte pas ; "
             "`--seulement findings|retenus|essentiels|fichiers` restreint ; `--json` pour la "
             "machine. **Lis-le avant d'écrire le moindre script de comptage.**")
    L.append("*Mesuré sur une run complète de cette pipeline : **1,33 à 1,68 appel par tour** selon "
             "les nœuds, jusqu'à **19 appels seuls d'affilée** chez un chef, et **153 des 452 "
             "commandes étaient des scripts jetables réécrits à chaque fois**. Le coût récupérable "
             "par le seul groupage : **63 M de jetons sur 506**, dont 24 M chez les sous-agents. "
             "Le travail était bon ; c'est la file indienne qui a coûté.*")
    L.append("**Ça ne change rien à ce que tu vérifies.** Mêmes gestes, mêmes contrôles, même "
             "ordre logique — tu arrêtes seulement de refaire la queue entre deux gestes qui "
             "n'ont rien à se dire.")
    L.append("**ET TU RECOPIES CETTE SECTION ENTIÈRE DANS LE PROMPT DE CHAQUE SOUS-AGENT que tu "
             "dispatches** — le signal des deux appels et le cas des scripts de mesure compris. Ils "
             "ne lisent pas ce brief : ce que tu ne leur transmets pas n'existe pas pour eux. Et "
             "c'est chez eux que le volume se joue — **38 % du gain mesuré vient des sous-agents**, "
             "parce qu'ils sont cent : une règle qui leur fait grouper trois gestes se paie cent "
             "fois, là où toi tu ne la paies qu'une.")

    # L'attente des sous-agents. Regle de SOCLE, posee dans le brief de tout nœud
    # qui fait tourner un agent : elle ne depend d'aucune pipeline, et recopiee
    # dans les skills de chacune elle divergerait.
    if node.get('type') in ('agent', 'fanout'):
        L.append("\n## ⏳ Attendre un sous-agent — jamais en relisant son fichier")
        L.append("Si tu dispatches des agents, tu attends leurs livrables. **N'attends jamais en "
                 "relisant le fichier attendu** : une relecture ne fait pas avancer l'agent, elle "
                 "consomme un tour complet et recommence. Attends par le mécanisme d'attente des "
                 "sous-agents, qui te rend la main quand ils ont fini.")
        L.append("**Un sous-agent meurt en silence** — pas d'erreur, son fichier ne s'écrit jamais. "
                 "Tu le reconnais à ce que ses frères de la même vague ont rendu et pas lui : "
                 "compare les horodatages. Alors, dans cet ordre — **relance-le UNE fois** ; "
                 "toujours rien, **écris le constat dans ton journal et poursuis sans lui**. "
                 "Jamais de troisième dispatch : si deux n'aboutissent pas, ce n'est pas "
                 "l'exécution qui cloche. Un livrable manquant est une information que le contrôle "
                 "saura lire ; une session bloquée n'en est pas une.")
    if donef:
        L.append("\n## Fin de nœud (obligatoire)")
        L.append(f"Ton TOUT DERNIER geste, une fois la check-list terminée : `touch {donef}`.")
        L.append("C'est ce fichier qui dit au runtime que tu as fini ; sans lui il attend, puis coupe la session.")
    return '\n'.join(L)

def build_sys(node, reprise=False):
    """Les protocoles colles en permanence a l'agent.

    A la REPRISE, un nœud peut en demander d'autres : `consignes_reprise` dans le
    graphe. Un reparateur applique une liste de gestes prescrits — il n'a pas besoin
    du manuel de production, et ce manuel lui est renvoye a chacun de ses tours de
    parole. Sans la cle, la reprise recoit les memes protocoles que la production.
    """
    liste = (node.get('consignes_reprise') or node.get('consignes', [])) if reprise \
        else node.get('consignes', [])
    parts = []
    for sk in liste:
        f = socle.fichier_skill(sk)
        if f.exists():
            parts.append(f"# ===== SKILL: {sk} =====\n" + f.read_text(encoding='utf-8'))
    return '\n\n'.join(parts)

def slug_role(s):
    """Un identifiant d'agent stable a partir d'un libelle de role."""
    s = (s or '').lower()
    s = re.sub(r"^(le|la|l')\s*", '', s.strip())
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:40] or 'agent'


def equiper_sous_agents(node, cfg_dir):
    """Ecrit une definition d'agent par ROLE de sous-agent declare par le nœud.

    LE RUNTIME EQUIPE, L'AGENT NE RECOPIE PAS. Le graphe declare deja, dans
    `agents[]`, quel skill va a quel role — mais rien ne s'en servait : le
    meneur dispatchait en `general-purpose`, un sous-agent NU, et devait donc
    reecrire le protocole de ses ouvriers a la main dans chaque prompt. A
    chaque run il en reecrivait une version un peu differente, et perdait une
    regle au passage. *Mesure qui motive la regle : la consigne « une page se
    telecharge, WebFetch n'en rend qu'un resume » vivait dans le brief du chef
    de chasse et dans le graphe ; aucun chasseur ne l'a jamais eue. Ils ont
    ecrit depuis des resumes, et le controle — qui telecharge la page — a
    rendu des fautes graves d'attestation.*

    Le premier role de la liste est le MENEUR : il est deja equipe par le
    system prompt de sa propre session, on ne lui fabrique rien. Les suivants
    sont les sous-agents.

    Rend la liste [(subagent_type, role)] pour que le brief la nomme.
    """
    roles = (node.get('agents') or [])[1:]
    if not roles:
        return []
    d = pathlib.Path(cfg_dir) / 'agents'
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    outils = ' '.join(node.get('tools') or []) or None
    faits = []
    for ag in roles:
        nom = 'flux-' + node.get('id', 'noeud') + '-' + slug_role(ag.get('role'))
        corps = []
        for k in (ag.get('consignes') or []):
            f = socle.fichier_skill(k.get('nom') or '')
            if f.exists():
                corps.append('# ===== SKILL: ' + str(k.get('nom')) + ' =====\n'
                             + f.read_text(encoding='utf-8'))
        if not corps:
            continue                      # un role sans skill n'a rien a equiper
        desc = (ag.get('role') or nom).strip().replace('\n', ' ')
        tete = ['---', 'name: ' + nom,
                'description: ' + desc + ' — equipe par le flux, protocole injecte '
                'en prompt permanent.']
        if outils:
            tete.append('tools: ' + outils)
        tete.append('---')
        try:
            (d / (nom + '.md')).write_text('\n'.join(tete) + '\n\n'
                                           + '\n\n'.join(corps), encoding='utf-8')
            faits.append((nom, desc))
        except Exception:
            pass
    return faits


ACCENTS = PIPELINE_RUNTIME / 'corriger_accents_run.py'

def passe_accents(ws):
    """Rattrape les accents manquants dans les livrables FR de l'étape.

    Le modèle écrit par intermittence « modele » pour « modèle » : la consigne
    tient quelques messages puis régresse. La correction est donc mécanique,
    fondée sur le lexique aspell-fr, appliquée avant le contrôle de l'étape.

    Rend True si elle a modifié quelque chose. C'est ce qui permet au gate de
    rejouer ce que cette passe vient de périmer : elle réécrit un livrable APRÈS
    la fin du nœud, donc après un éventuel déploiement, et ce que l'agent a
    attesté comme identique cesse de l'être sans que rien dans le nœud puisse
    le rattraper.
    """
    if not ACCENTS.exists(): return
    import glob
    cibles = {str(pathlib.Path(f).parent)
              for f in glob.glob(str(pathlib.Path(ws) / '**' / 'constellation-data.json'),
                                 recursive=True)}
    touche = False
    for d in sorted(cibles):
        try:
            r = subprocess.run([sys.executable, str(ACCENTS), d],
                               capture_output=True, text=True, timeout=300)
            tete = (r.stdout or '').splitlines()[:1]
            if tete and not tete[0].startswith('0 '):
                touche = True
                print(f'    accents · {pathlib.Path(d).name} : {tete[0]}', file=sys.stderr)
        except Exception as e:
            print(f'    accents · échec sur {d} : {e}', file=sys.stderr)
    return touche

TRANSCRIPTS = PIPELINE_RUNTIME / 'rendre_transcripts.py'

def rendre_transcripts(ws, sid, prefixe):
    """Rend le transcript du leader ET de chacun de ses sous-agents.

    Le vérificateur d'étape juge les livrables CONTRE les transcripts qui les ont
    produits — « le leader ET chacun de ses sous-agents », dit son brief. Rien ne
    sert de le lui demander si personne ne rend ces fichiers : un sous-agent ignore
    son propre identifiant, et seul le runtime connaît la session du leader, donc le
    dossier `subagents/` qui pend dessous.

    C'est donc le runtime qui les rend, à chaque fin de nœud, et pas l'agent : la
    trace ne dépend pas de la bonne volonté de celui qu'elle sert à juger. Un nœud
    a rendu 15 transcripts pour 15 sous-agents parce que son skill le lui disait ;
    le nœud d'à côté en a rendu 1 pour 203 parce que le sien ne le disait pas.
    """
    if not TRANSCRIPTS.exists() or not sid: return
    try:
        r = subprocess.run([sys.executable, str(TRANSCRIPTS), ws, sid, prefixe],
                           capture_output=True, text=True, timeout=600)
        tete = (r.stdout or r.stderr or '').splitlines()[:1]
        if tete: print(f'    transcripts · {tete[0]}', file=sys.stderr)
    except Exception as e:
        print(f'    transcripts · {type(e).__name__}: {e}', file=sys.stderr)

RAPPORT = PIPELINE_RUNTIME / 'build_run_report.py'

def rapport_de_run(ws):
    """Régénère la page de suivi de la run. Idempotent, à chaque fin de nœud.

    C'est LA page à ouvrir pour voir où en est la run et pourquoi ça bloque :
    elle ne dépend d'aucun validateur, une pipeline qui n'en déclare aucun la
    garde.
    """
    if not RAPPORT.exists(): return
    try:
        r = subprocess.run([sys.executable, str(RAPPORT), ws],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f'    rapport · échec (rc={r.returncode})', file=sys.stderr); return
        for l in (r.stdout or '').splitlines():
            if l.startswith('URL:'):
                print(f'    rapport · {l[4:].strip()}', file=sys.stderr); break
    except Exception as e:
        print(f'    rapport · {type(e).__name__}: {e}', file=sys.stderr)

def rejouer_apres_accents(node, ws):
    """Rejoue ce que la passe d'accents vient de périmer, si le nœud le déclare.

    Le runtime ne connaît aucune pipeline : c'est le graphe qui nomme la commande
    (`gate.apres_accents`), et elle reçoit le workspace en argument. Elle n'est
    lancée que si les accents ont réellement modifié un livrable.
    """
    cmd = ((node.get('gate') or {}).get('apres_accents') or '').strip()
    if not cmd:
        return
    # UNE RUN PEUT PORTER SA VARIANTE, et c'est elle qui gagne. Le socle interdit
    # de corriger un script de skill depuis une run en cours : la variante vit
    # alors dans `<ws>/scripts/`, et rejouer la version generique par-dessus
    # ecraserait justement ce que la variante corrigeait.
    tete = cmd.split()[0]
    variante = pathlib.Path(ws) / 'scripts' / pathlib.Path(tete).name
    if variante.exists():
        cmd = ' '.join([str(variante)] + cmd.split()[1:])
    try:
        r = subprocess.run(cmd.split() + [ws], capture_output=True, text=True, timeout=900)
        etat = 'ok' if r.returncode == 0 else f'code {r.returncode}'
        print(f'    après-accents · {cmd.split()[0].rsplit("/", 1)[-1]} : {etat}', file=sys.stderr)
        if r.returncode != 0:
            for l in (r.stderr or '').splitlines()[-3:]:
                print(f'      {l}', file=sys.stderr)
    except Exception as e:
        print(f'    après-accents · {type(e).__name__}: {e}', file=sys.stderr)


def gate(nnum, ws, sid=None, prefixe=None, node=None):
    """Contrôle de fin d'étape. (True, []) si vert.

    La passe d'accents et le rapport de run tournent TOUJOURS : ils ne jugent
    rien, ils préparent et rendent compte. Le validateur, lui, n'existe que là
    où une pipeline en déclare un — sans lui, le travail est jugé par le nœud
    de vérification qui suit, et le seul contrôle mécanique restant est la
    présence des livrables.
    """
    if sid:
        rendre_transcripts(ws, sid, prefixe)   # la trace AVANT le jugement, jamais après
    if passe_accents(ws) and node:   # accents rattrapés avant le contrôle, pas après
        rejouer_apres_accents(node, ws)   # …et ce qu'ils périment est rejoué aussitôt
    rapport_de_run(ws)
    if not nnum or not VALIDATE.exists(): return True, []
    r = subprocess.run([sys.executable, str(VALIDATE), str(nnum), ws],
                       capture_output=True, text=True, timeout=180)
    errs = [l.strip() for l in r.stderr.splitlines() if l.strip().startswith(tuple('0123456789')) and '.' in l[:4]]
    return r.returncode == 0, errs

SEQUENCEUR = FLUX / 'runtime' / 'sequenceur.py'

def reveiller_sequenceur(wf, ws, nid):
    """Une etape vient de finir : le SEQUENCEUR decide de la suite (v2, D4).

    C'est ICI que se voit la fin d'un nœud, cote serveur — la page, elle, ne
    l'apprend qu'en sondant. Le sequenceur ne fait rien si le toggle
    `state/autopilote` est absent : il vit dans le workspace, donc par run.
    Plus aucun agent n'est reveille pour decider : la regle est dans le
    programme, et les registres portent ce qu'il faut pour l'appliquer.
    """
    if not SEQUENCEUR.exists(): return
    try:
        subprocess.Popen([sys.executable, str(SEQUENCEUR), wf['id'], ws, '--noeud-fini', nid],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        print(f'    sequenceur · non réveillé : {type(e).__name__}: {e}', file=sys.stderr)

reveiller_orchestrateur = reveiller_sequenceur   # l'ancien nom, pour qui l'importe encore

MOTEURS = socle.fichier_moteurs()


def derniere_parole(sid, moteur_id=None):
    """Le dernier horodatage du journal de cette session, ou None.

    Le journal vit sous le dossier de config du moteur qui l'a jouee ; on les
    essaie tous, parce qu'une run peut avoir bascule de compte en cours de
    route et que le moteur note dans la passe peut manquer sur les anciennes.
    """
    if not sid:
        return None
    cfgs = socle.config_dirs_connus()
    for cfg in cfgs:
        for f in glob.glob(os.path.join(cfg, 'projects', '*', sid + '.jsonl')):
            dernier = None
            try:
                with open(f, encoding='utf-8') as fh:
                    for l in fh:
                        i = l.find('"timestamp":"')
                        if i >= 0:
                            dernier = l[i + 13:l.find('"', i + 13)]
            except Exception:
                continue
            if dernier:
                return dernier
    return None


def moteur_de(ws):
    """Le dossier de config Claude a poser pour cette run, et le moteur choisi.

    Rend (config_dir, id). Toute anomalie retombe sur le moteur par defaut :
    un moteur inconnu ou un fichier illisible ne doit jamais empecher un nœud
    de partir — au pire il part sur le compte habituel, et ca se voit.
    """
    defaut = (socle.claude_config_defaut(), 'defaut')
    try:
        cfg = json.loads(MOTEURS.read_text(encoding='utf-8'))
    except Exception:
        return defaut
    table = {m['id']: m for m in cfg.get('moteurs') or []}
    choix = None
    try:
        choix = (pathlib.Path(ws) / 'state' / 'moteur').read_text(
            encoding='utf-8').strip()
    except Exception:
        pass
    m = table.get(choix) or table.get(cfg.get('defaut'))
    if not m:
        return defaut
    cd = m.get('config_dir')
    return (os.path.expanduser(cd) if cd else defaut[0], m.get('id') or defaut[1])


def modele_du_moteur(ws, moteur_id):
    """Le modele choisi pour CE moteur sur CETTE run, ou None (defaut du noeud).

    Persiste a part de `state/moteur` : le moteur ACTIF et le modele choisi
    pour chaque moteur sont deux reglages independants. Porte par la run, pas
    par le moteur lui-meme -- change de compte en cours de route et Perso
    garde le modele qu'on lui a donne ici, pas celui d'une autre run.
    """
    try:
        table = json.loads((pathlib.Path(ws) / 'state' / 'moteur-modeles.json')
                            .read_text(encoding='utf-8'))
    except Exception:
        return None
    return table.get(moteur_id) or None


def marque_run(ws):
    """Six caracteres qui identifient UNE run dans un nom de session tmux.

    Sans elle, deux runs de la meme pipeline arrivant sur le meme nœud a la
    meme passe portent le meme nom de session — et le `kill-session` qui
    precede tout lancement tue la session de l'autre run sans un mot. Un hash
    plutot que le nom du dossier : les noms de run font 50 caracteres et tmux
    refuse `.` et `:`.
    """
    base = os.path.basename(os.path.normpath(ws))
    return hashlib.md5(base.encode('utf-8')).hexdigest()[:6]


def nom_session(wf, ws, nid, npass):
    # FLUX_SESSION_PREFIXE : les tests nomment leurs sessions `test-flux-…` pour
    # pouvoir les tuer par leur nom, jamais en bloc.
    pref = os.environ.get('FLUX_SESSION_PREFIXE') or 'flux'
    return f'{pref}-{wf["id"]}-{marque_run(ws)}-{nid}-p{npass}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workflow'); ap.add_argument('node')
    ap.add_argument('--workspace', default=None)
    # clore : constater la fin d'un nœud que plus aucun runtime ne suit
    ap.add_argument('--clore', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--brief-only', action='store_true',
                    help='ecrit brief.md/sys.md et sort : la session LOCALE les consomme')
    a = ap.parse_args()

    wf = load_wf(a.workflow); node = node_of(wf, a.node)
    ws = resolve_ws(wf, a.workspace)
    print(f'═══ {wf["name"]} · nœud « {node["name"]} » [{node.get("type") or node.get("moteur") or "agent"}]', file=sys.stderr)
    print(f'    workspace : {ws}', file=sys.stderr)

    if a.clore:
        # la session de l'agent peut encore vivre : elle a rendu son travail,
        # elle ne rendra plus rien
        st = {}
        try:
            st = json.loads(state_path(wf, ws, a.node).read_text(encoding='utf-8'))
        except Exception:
            pass
        sess = st.get('tmux')
        if sess:
            subprocess.call(['tmux', 'kill-session', '-t', sess],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exiges, dispenses = livrables_exiges(node, ws)
        manques = [d for d in exiges if not present(ws, d)]
        for x in dispenses:
            print('  · livrable non exige — ' + x['chemin'] + ' : ' + x['motif'], file=sys.stderr)
        ok, errs = gate((node.get('gate') or {}).get('validateur'), ws, node=node)
        # « fait » quoi que dise le validateur : c'est un avis, pas un verrou
        write_state(wf, ws, a.node, statut='fait', ended=now(),
                    erreurs=errs[:20], livrables_manquants=manques,
                    livrables_dispenses=dispenses, clos_a_la_main=True,
                    avis='vert' if (ok and not manques) else 'rouge')
        print('clos : fait (avis', 'vert)' if (ok and not manques) else 'rouge)', file=sys.stderr)
        return 0


    # ── 1. GATE D'ENTRÉE : les livrables amont sont-ils là ?
    missing_up = []
    for up in upstream(wf, a.node):
        up_exiges, up_dispenses = livrables_exiges(up, ws)
        for x in up_dispenses:
            print('  · amont non exige — ' + up['id'] + ':' + x['chemin']
                  + ' : ' + x['motif'], file=sys.stderr)
        for d in up_exiges:
            if not present(ws, d): missing_up.append(f'{up["id"]}:{d}')
    if missing_up:
        write_state(wf, ws, a.node, statut='bloque-entree', manques_amont=missing_up, started=now())
        print('  ✗ GATE ENTRÉE — livrables amont manquants :', file=sys.stderr)
        for m in missing_up: print(f'      {m}', file=sys.stderr)
        return 2
    # UNE ÉTAPE NE DÉMARRE PAS SUR UN TRAVAIL QUE LE CONTRÔLE A REFUSÉ.
    #
    # Le gate ne regardait que la PRÉSENCE des livrables amont : une étape dont
    # la vérification avait relevé une faute grave — un livrable qui affirme ce
    # que sa source ne soutient pas — laissait quand même partir la suivante,
    # qui bâtissait alors sur du faux. C'est arrivé : le montage a démarré sur
    # un vivier que la vérification de la chasse demandait de corriger, et seule
    # une alerte de l'orchestrateur l'a signalé, après coup.
    #
    # On ne juge ici QUE les fautes graves : elles sont bloquantes par
    # définition, sans seuil à comparer. Le seuil de qualité, lui, vit dans
    # l'orchestrateur et nulle part ailleurs — le recopier ici en ferait une
    # seconde vérité qui divergerait.
    rang = node.get('n')
    if rang and not node.get('verification'):
        try:
            reg = json.loads((pathlib.Path(ws) / 'verifications' / ('etape-%d' % (int(rang) - 1))
                              / 'registre.json').read_text(encoding='utf-8'))
            graves = ((reg.get('score') or {}).get('fautes_graves') or 0)
            if graves:
                write_state(wf, ws, a.node, statut='bloque-entree',
                            manques_amont=['etape-%d : %d faute(s) grave(s) non corrigée(s)'
                                           % (int(rang) - 1, graves)], started=now())
                print('  ✗ GATE ENTRÉE — l\'étape %d porte %d faute(s) GRAVE(S) non corrigée(s).'
                      % (int(rang) - 1, graves), file=sys.stderr)
                print('      Relance l\'étape %d sur les gestes que son rapport prescrit ; '
                      'bâtir dessus propagerait le faux.' % (int(rang) - 1), file=sys.stderr)
                return 2
        except Exception:
            pass                      # pas de registre : rien a opposer, on laisse passer

    print('  ✓ gate d\'entrée', file=sys.stderr)

    # ── nœud LOCAL : l'étape est interactive, elle se joue dans une session Claude
    #    Code sur le laptop (dialogue avec l'opérateur, canvas, arbitrages). Le VPS ne
    #    spawn rien : il prépare le brief, expose la commande, et constate.
    if (node.get('type') or node.get('moteur') or 'agent') == 'local':
        manques = [d for d in livrables_exiges(node, ws)[0] if not present(ws, d)]
        ndir = socle.dossier_run(wf['id'], ws) / a.node
        ndir.mkdir(parents=True, exist_ok=True)
        (ndir / 'brief.md').write_text(
            build_brief(node, ws, manques, rapport_passe=rapport_de_passe(a.node, 1)),
            encoding='utf-8')
        (ndir / 'sys.md').write_text(build_sys(node, False), encoding='utf-8')
        cmd = node.get('commande_locale') or f'flux-local {wf["id"]} {a.node}'
        if not manques:
            ok, errs = gate((node.get('gate') or {}).get('validateur'), ws, node=node)
            write_state(wf, ws, a.node, statut='vert' if ok else 'rouge', erreurs=errs,
                        commande_locale=cmd, brief=str(ndir / 'brief.md'))
            print('  ✓ livrables produits par la session locale' if ok
                  else '  ✗ GATE ROUGE sur le travail de la session locale', file=sys.stderr)
            print(str(ndir / 'brief.md'))
            for e in errs: print(f'      {e}', file=sys.stderr)
            return 0 if ok else 1
        write_state(wf, ws, a.node, statut='attente-local', livrables_manquants=manques,
                    commande_locale=cmd, brief=str(ndir / 'brief.md'))
        print('  ⏸ étape interactive — le flux ne la lance pas, joue-la en session ouverte :', file=sys.stderr)
        print(f'      {cmd}', file=sys.stderr)
        # stdout = le chemin du brief : c'est ce que flux-local rapatrie
        print(str(ndir / 'brief.md'))
        return 0

    # ── nœud humain : on ne spawn rien, on constate
    if (node.get('type') or node.get('moteur') or 'agent') == 'human':
        ok = all(present(ws, d) for d in livrables_exiges(node, ws)[0])
        # le graphe porte un gabarit d'URL : c'est la run qui le resout
        url = (node.get('url') or '').replace('{slug}', os.path.basename(ws))
        write_state(wf, ws, a.node, statut='vert' if ok else 'attente-humain', url=url or None)
        print(f'  {"✓ livrable humain présent" if ok else "⏸ en attente de ta notation"}', file=sys.stderr)
        return 0 if ok else 1

    # ── 2. BRIEF (avec reprise : ce qui manque encore)
    manques = [d for d in livrables_exiges(node, ws)[0] if not present(ws, d)]
    ndir = socle.dossier_run(wf['id'], ws) / a.node
    ndir.mkdir(parents=True, exist_ok=True)

    # Un nœud peut être relancé (gate rouge, reprise) : chaque lancement est une
    # INSTANCE distincte de Claude Code, numérotée, avec son transcript à elle.
    # Rien n'écrase rien — la console web ouvre une fenêtre par instance.
    prev = state_path(wf, ws, a.node)
    passages, refus = [], []
    if prev.exists():
        try:
            st = json.loads(prev.read_text(encoding='utf-8'))
            passages = st.get('passages') or []
            if st.get('statut') == 'rouge':
                refus = st.get('erreurs') or []
        except Exception: pass
    # UNE PASSE SANS FIN SE CLOT AVANT D'EN OUVRIR UNE AUTRE.
    #
    # Le lanceur ecrit la fin d'une passe quand la session se termine — mais s'il
    # MEURT lui-meme (ssh coupe, machine redemarree, kill), personne ne l'ecrit.
    # La passe reste alors ouverte pour toujours : la frise la montre en cours
    # des mois plus tard, la duree grandit toute seule, et une seconde passe se
    # superpose a elle dans le recapitulatif comme si les deux tournaient.
    #
    # La fin se retrouve pourtant : le journal de la session horodate chaque
    # ligne. Sa DERNIERE ligne est le dernier moment ou l'agent a parle — c'est
    # la fin reelle, et elle vaut mieux qu'un champ vide.
    for x in passages:
        if x.get('ended') or not x.get('claude_session_id'):
            continue
        sess_v = x.get('session')
        if sess_v and subprocess.call(['tmux', 'has-session', '-t', sess_v],
                                      stderr=subprocess.DEVNULL) == 0:
            continue                      # elle tourne vraiment : on n'y touche pas
        fin_reelle = derniere_parole(x.get('claude_session_id'), x.get('moteur'))
        if fin_reelle:
            x['ended'] = fin_reelle
            x['fin'] = 'lanceur-disparu'
            # D5 : une passe dont le lanceur a disparu n'a rien rendu qu'on ait
            # juge — c'est une PANNE, jamais un vert.
            x['verdict'] = 'panne'
            x['panne'] = {'cause': 'lanceur-disparu', 'releve_a': now(), 'releve_par': 'runtime',
                          'constat': 'passe sans fin ecrite, session tmux absente ; '
                                     'fin lue dans le journal (' + fin_reelle + ')'}
            try:
                d0 = datetime.datetime.fromisoformat(x['started'].replace('Z', '+00:00'))
                d1 = datetime.datetime.fromisoformat(fin_reelle.replace('Z', '+00:00'))
                x['duree_s'] = max(0, int((d1 - d0).total_seconds()))
            except Exception:
                pass
            print('  passe %s close depuis son journal (%s) — son lanceur avait disparu'
                  % (x.get('n'), fin_reelle), file=sys.stderr)

    npass = len(passages) + 1
    pdir = ndir / 'passes' / str(npass)
    pdir.mkdir(parents=True, exist_ok=True)
    donef = pdir / 'DONE'

    mode = socle.mode_run(wf['id'], ws)
    socle.ecrire_run(wf['id'], ws, noeud_courant=a.node)

    # ── 3-api. MOTEUR API : un nœud « formulaire » ne passe pas par tmux (D1).
    #    Pas de brief non plus : ses consignes sont du texte, pas un skill.
    #    Un appel direct, une reponse JSON contrainte par le schema du nœud, la
    #    meme passe que pour tmux (mesures, entree, sortie, note, garde).
    if (node.get('moteur') or 'tmux') == 'api':
        sys.path.insert(0, str(FLUX / 'runtime'))
        from moteurs import api as moteur_api
        _, moteur_id = moteur_de(ws)
        modele = modele_du_moteur(ws, moteur_id) or socle.modele_du_noeud(wf['id'], ws, node, wf) or 'sonnet'
        print(f'    moteur : api · modele {modele} · mode {mode}', file=sys.stderr)
        passages.append({'n': npass, 'moteur': 'api', 'modele': modele, 'started': now(),
                         'motif': 'reprise après refus' if refus else 'premier lancement',
                         'refus_recus': refus})
        write_state(wf, ws, a.node, statut='en-cours', started=now(), manques=manques,
                    passages=passages, passage_courant=npass, tmux=None)
        socle.ecrire_etat_compact(wf['id'], ws)
        # LE MOTEUR API TOURNE DANS UNE FENETRE : la console y montre ce que le
        # modele recoit et ce qu'il repond, au fil de l'eau (tmux + ttyd, comme
        # pour un agent). La passe elle-meme s'ecrit dans un fichier que l'on
        # relit a la fin ; la fenetre reste ouverte deux minutes apres, le temps
        # de la lire, puis se ferme seule.
        pdir.mkdir(parents=True, exist_ok=True)
        fpasse = pdir / 'passe.json'
        if fpasse.exists():
            fpasse.unlink()
        sess = nom_session(wf, ws, a.node, npass)
        # La fenetre herite de l'environnement du lanceur (dossiers d'etat, URL
        # d'Ollama, compte Claude) : tmux ne le transmet pas de lui-meme.
        env = ' '.join('%s=%s' % (k, shlex.quote(v)) for k, v in os.environ.items()
                       if k.startswith(('FLUX_', 'FAUX_')) or k in ('CLAUDE_CONFIG_DIR', 'PATH', 'HOME'))
        # …et ce que le lanceur N'A PAS ne doit pas venir de l'environnement global du
        # serveur tmux, pose par une session plus ancienne (un test, une autre run).
        try:
            glob_tmux = subprocess.run(['tmux', 'show-environment', '-g'], capture_output=True,
                                       text=True, timeout=10).stdout.splitlines()
        except Exception:
            glob_tmux = []
        orphelins = sorted({l.split('=', 1)[0] for l in glob_tmux
                            if l.startswith(('FLUX_', 'FAUX_'))} - set(os.environ))
        env = ' '.join(['-u %s' % k for k in orphelins] + ([env] if env else []))
        cmd = ('cd %s && env %s python3 %s %s %s --workspace %s --passe %d --modele %s --affiche --passe-json %s ; '
               'echo ; echo "[cette fenetre se ferme dans 120 s]" ; sleep 120'
               % (shlex.quote(str(FLUX)), env,
                  shlex.quote(str(FLUX / 'runtime' / 'moteurs' / 'api.py')), shlex.quote(wf['id']), shlex.quote(a.node),
                  shlex.quote(str(ws)), npass, shlex.quote(str(modele)), shlex.quote(str(fpasse))))
        subprocess.call(['tmux', 'kill-session', '-t', sess], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(['tmux', 'new-session', '-d', '-s', sess, '-x', str(TERM_COLS), '-y', str(TERM_ROWS), cmd])
        passages[-1]['session'] = sess
        write_state(wf, ws, a.node, tmux=sess, cols=TERM_COLS, rows=TERM_ROWS, passages=passages)
        t0 = _tm.time()
        while not fpasse.exists() and _tm.time() - t0 < 1800:
            if subprocess.call(['tmux', 'has-session', '-t', sess], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) != 0:
                break
            _tm.sleep(1)
        if fpasse.exists():
            passe = json.loads(fpasse.read_text(encoding='utf-8'))
        else:
            passe = {'n': npass, 'moteur': 'api', 'modele': modele, 'started': passages[-1]['started'],
                     'ended': now(), 'fin': 'panne', 'verdict': 'panne', 'erreurs': ['la fenetre du moteur api est morte sans rendre de passe'],
                     'panne': {'cause': 'session-morte', 'constat': 'moteur api sans passe.json', 'releve_a': now(), 'releve_par': 'runtime'},
                     'entree': list(node.get('entree') or []), 'sortie': list(node.get('livrables') or []), 'note': None, 'garde': None, 'mesures': None}
        passe['session'] = sess
        passe['motif'] = passages[-1]['motif']; passe['refus_recus'] = refus
        passages[-1] = passe
        return terminer_passe(wf, node, ws, a.node, passages, npass, pdir, mode, rc_panne=2)


    # Le rapport n'est joint qu'a une REPRISE : au premier passage il n'y a rien
    # a corriger, et un rapport d'une run anterieure induirait en erreur.
    rapport = None
    if passages:
        chemin, txt = rapport_de_verification(wf, ws, a.node)
        if txt:
            verdict, fin = extrait_du_rapport(txt)
            rapport = (chemin, verdict, fin)
            print(f'    reprise · rapport joint au brief : {chemin}', file=sys.stderr)
        else:
            print('    reprise · aucun rapport de controle trouve', file=sys.stderr)
    acquis = acquis_du_controle(wf, ws, a.node) if rapport else {}
    if acquis:
        total = sum(len(v) for v in acquis.values())
        print(f'    reprise · {total} éléments acquis nommés dans le brief', file=sys.stderr)
    rp = rapport_de_passe(a.node, npass)
    # Le moteur se resout ICI parce que l'equipement des sous-agents ecrit dans
    # SON dossier de config : c'est ce dossier que la session lira au dispatch.
    equipes = equiper_sous_agents(node, moteur_de(ws)[0])
    if equipes:
        print('    sous-agents équipés : '
              + ', '.join(n for n, _ in equipes), file=sys.stderr)
    brief = build_brief(node, ws, manques, donef, refus, rapport, acquis, rp, equipes)
    sysmd = build_sys(node, bool(rapport and rapport[0]))
    (pdir / 'brief.md').write_text(brief, encoding='utf-8')
    (pdir / 'sys.md').write_text(sysmd, encoding='utf-8')

    if a.brief_only:
        print(f'  ✓ brief prêt : {pdir / "brief.md"}', file=sys.stderr)
        print(str(pdir / 'brief.md'))
        return 0

    if a.dry_run:
        print('  ── DRY-RUN : check-list qui serait imposée ──', file=sys.stderr)
        for i, t in enumerate(node.get('taches', []), 1): print(f'    {i}. {t}', file=sys.stderr)
        print(f'    manques : {manques or "aucun"}', file=sys.stderr)
        return 0

    # ── 3. SPAWN de l'agent — VRAIE session Claude Code (TUI) dans un tmux.
    #    Pas de `-p` ni de reformatage : le pane contient la session telle qu'on la
    #    verrait en s'attachant au tmux, et c'est exactement ce que la console web rend.
    tools = node.get('tools', ['Bash', 'Read', 'Write', 'TodoWrite'])
    cfg_dir, moteur_id = moteur_de(ws)
    jeton_moteur = next((m.get('fichier_jeton') for m in socle.charger_moteurs().get('moteurs') or []
                         if m.get('id') == moteur_id), None)
    # LA VERSION DE CLAUDE CODE EST EPINGLEE (moteurs.json:claude_version). Le
    # moteur tmux lit l'ecran d'une TUI : une autre version peut changer ce
    # qu'il y lit. On refuse, avec le message qui dit quoi faire.
    ok_v, msg_v = socle.verifier_version_claude()
    print(f'    {msg_v}', file=sys.stderr)
    if not ok_v:
        write_state(wf, ws, a.node, statut='panne', erreurs=[msg_v])
        return 3
    modele = modele_du_moteur(ws, moteur_id) or socle.modele_du_noeud(wf['id'], ws, node, wf) or 'opus'
    print(f'    moteur : tmux · compte {moteur_id} ({cfg_dir}) · modele {modele} · mode {mode}',
          file=sys.stderr)
    sess = nom_session(wf, ws, a.node, npass)
    sid = str(uuid.uuid4())          # session Claude Code : transcript retrouvable par ID
    subprocess.call(['tmux', 'kill-session', '-t', sess], stderr=subprocess.DEVNULL)
    # LA PASSE, forme v2 : `moteur` est le MOTEUR (tmux|api), `compte` le compte
    # qui paie. Avant la v2, `moteur` portait le compte — socle.normaliser_passe
    # relit les anciennes.
    passages.append({'n': npass, 'session': sess, 'claude_session_id': sid,
                     'moteur': 'tmux', 'compte': moteur_id, 'modele': modele,
                     'started': now(), 'motif': 'reprise après refus' if refus else 'premier lancement',
                     'refus_recus': refus,
                     'entree': list(node.get('entree') or []),
                     'sortie': list(node.get('livrables') or []),
                     'note': None, 'garde': None, 'mesures': None})
    write_state(wf, ws, a.node, statut='en-cours', started=now(), manques=manques,
                passages=passages, passage_courant=npass)
    socle.ecrire_etat_compact(wf['id'], ws)
    briefp = pdir / 'brief.md'; sysp = pdir / 'sys.md'
    donef.unlink(missing_ok=True)

    # Le prompt est POSITIONNEL et doit rester en premier : --allowedTools et
    # --mcp-config sont variadiques et avaleraient un argument placé après eux.
    launch = pdir / 'launch.sh'
    launch.write_text(
        '#!/bin/bash\n'
        'export TERM=xterm-256color COLORTERM=truecolor\n'
        'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:' + os.path.expanduser('~/.local/bin') + '\n'
        'export CLAUDE_CODE_ENABLE_TASKS=false\n'
        # Le dossier de config est POSÉ, jamais hérité. Le serveur tmux de la machine
        # peut en porter un autre dans son environnement global — l'agent y écrirait
        # alors son journal, et la console, qui lit cette ligne pour savoir où
        # chercher, ne le trouverait pas. Écrit ici, les deux regardent au même
        # endroit.
        f'export CLAUDE_CONFIG_DIR={cfg_dir}\n'
        # Un moteur dont le compte s'authentifie par jeton longue duree (et non par le
        # .credentials.json du dossier de config) declare `fichier_jeton` : on le source.
        + (f'set -a; . {jeton_moteur}; set +a\n' if jeton_moteur else '') +
        # Le rendu CLASSIQUE, pas le plein écran. En écran alterné la conversation
        # vit dans un tampon sans historique : ni la barre de défilement de la
        # console, ni la molette, ni le glissement tactile n'ont de quoi remonter,
        # et tmux non plus. En classique tout part dans le scrollback du terminal,
        # que ttyd et tmux savent parcourir.
        'export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1\n'
        'cd "$HOME"\n'
        'exec ' + socle.claude_bin() + ' "$(cat ' + str(briefp) + ')" \\\n'
        '  --append-system-prompt "$(cat ' + str(sysp) + ')" \\\n'
        '  --model ' + modele + ' \\\n'
        '  --permission-mode dontAsk \\\n'
        '  --allowedTools ' + ' '.join(tools) + ' \\\n'
        '  --add-dir ' + ws + ' \\\n'
        '  --session-id ' + sid + ' \\\n'
        '  --strict-mcp-config --mcp-config \'{"mcpServers":{}}\'\n',
        encoding='utf-8')
    launch.chmod(0o755)

    print(f'  ▶ agent lancé ({len(node.get("taches",[]))} tâches imposées)…', file=sys.stderr)
    subprocess.call(['tmux', '-f', TMUXCONF, 'new-session', '-d', '-s', sess,
                     '-x', str(TERM_COLS), '-y', str(TERM_ROWS),
                     '-c', os.path.expanduser('~'), str(launch)])
    # sans focus-events la TUI affiche un bandeau d'avertissement dans le pane
    # l'historique et la barre de statut se reglent par session : la conf globale
    # ne les rattraperait pas pour une session deja creee
    # `window-size` n'est PAS repose ici : le reglage global (`latest`) doit tenir.
    # Fige a `largest`, la fenetre changeait de largeur des qu'un second lecteur
    # s'attachait, l'agent repeignait tout son fil, et l'historique melangeait les
    # enroulements — mesure : 149 copies d'un meme brief dans un scrollback.
    for opt, val in (('history-limit', '50000'), ('status', 'off')):
        subprocess.call(['tmux', 'set-option', '-t', sess, opt, val],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(['tmux', 'set-option', '-t', sess, 'focus-events', 'on'],
                    stderr=subprocess.DEVNULL)
    write_state(wf, ws, a.node, tmux=sess, cols=TERM_COLS, rows=TERM_ROWS)

    # ── attente : la TUI ne se ferme pas seule. Trois sorties, dans cet ordre.
    #    1) sentinelle DONE écrite par l'agent · 2) écran figé assez longtemps
    #    (l'agent a rendu la main) · 3) plafond dur. Dans tous les cas on garde
    #    le scrollback complet dans run.out avant de couper.
    import time, hashlib
    t0 = time.time(); last_h = None; stable_since = time.time(); fin = 'session-morte'
    cause_panne = 'session-morte'; tours = 0
    while True:
        if subprocess.call(['tmux', 'has-session', '-t', sess], stderr=subprocess.DEVNULL) != 0:
            break
        snap = capture(sess, full=True)
        tours += 1
        if snap:
            (pdir / 'run.out').write_text(snap, encoding='utf-8')
            archiver_ecran(pdir, snap)      # le direct devient rejouable apres coup
            if tours % 20 == 0:
                socle.tourner_ecrans(pdir)  # 200 derniers ecrans par passe, pas plus
            # UN MUR DE QUOTA, UN JETON EXPIRE : ce n'est pas l'agent qui a fini,
            # c'est le moteur qui s'est arrete. On coupe et on qualifie (D5).
            # Sentinelle posee = l'agent a fini : un « login expired » encore a l'ecran
            # n'est alors qu'une citation (resume de passe), pas un mur.
            mur = None if donef.exists() else mur_a_l_ecran(snap)
            if mur:
                fin, cause_panne = 'panne', mur
                break
        if tours % 12 == 0:
            socle.ecrire_etat_compact(wf['id'], ws)
        if donef.exists():
            # DONE peut tomber AVANT que l'agent ait fini d'écrire (merge en cours,
            # dernier tour de boucle). On lui laisse finir son geste : on ne coupe
            # que quand l'écran ne bouge plus. Couper trop tôt a corrompu la
            # chasse du 2026-08-17 (livrables écrits à moitié au moment du gate).
            if time.time() - stable_since > GRACE_S:
                fin = 'sentinelle'; break
        h = hashlib.md5(snap.encode('utf-8', 'replace')).hexdigest()
        if h != last_h:
            last_h = h; stable_since = time.time()
        elif time.time() - stable_since > IDLE_S:
            fin = f'ecran-fige-{IDLE_S}s'; break
        if time.time() - t0 > MAX_S:
            fin = 'plafond-temps'; break
        time.sleep(5)

    snap = capture(sess, full=True)
    if snap:
        (ndir / 'run.out').write_text(snap, encoding='utf-8')
    subprocess.call(['tmux', 'kill-session', '-t', sess], stderr=subprocess.DEVNULL)
    socle.tourner_ecrans(pdir)
    print(f'  agent terminé ({fin}, {int(time.time()-t0)}s)', file=sys.stderr)
    passages[-1].update({'ended': now(), 'fin': fin, 'duree_s': int(time.time() - t0)})
    # SESSION MORTE = PANNE (D5). Une session tmux disparue avant sa sentinelle
    # n'a pas fini : elle est tombee. Ni vert, ni rouge — une panne, que le
    # sequenceur rejoue. Avant la v2, elle comptait comme un succes, et une
    # run entiere est restee figee dessus.
    if fin == 'session-morte' or fin == 'panne':
        if fin == 'session-morte':
            cause_panne = mur_dans_le_journal(passages[-1].get('claude_session_id')) or 'session-morte'
        passages[-1]['fin'] = 'panne'
        passages[-1]['panne'] = {'cause': cause_panne, 'releve_a': now(), 'releve_par': 'runtime',
                                 'constat': ('session tmux disparue avant la sentinelle'
                                             if cause_panne == 'session-morte' else
                                             'detecte a l\'ecran ou dans le journal : ' + cause_panne)}
    passages[-1]['mesures'] = mesures_tmux(passages[-1])
    rendre_transcripts(ws, passages[-1].get('claude_session_id'), f"{a.node}-p{npass}")
    return terminer_passe(wf, node, ws, a.node, passages, npass, pdir, mode)


MURS = (('usage limit reached', 'quota-atteint'), ("you've hit your limit", 'quota-atteint'),
        ('rate_limit_error', 'quota-atteint'), ('api error: rate limit', 'quota-atteint'),
        ('429', 'quota-atteint'),
        ('login expired', 'jeton-expire'), ('oauth token has expired', 'jeton-expire'),
        ('token has expired', 'jeton-expire'), ('please run /login', 'jeton-expire'),
        ('authentication_error', 'jeton-expire'), ('invalid api key', 'jeton-expire'))


def _sans_ansi(txt):
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', txt or '')


def mur_a_l_ecran(snap):
    """Un mur de quota ou un jeton expire, lu sur les dernieres lignes de l'ecran."""
    # les dernieres lignes ECRITES : le pane rend aussi ses lignes vides du bas
    fin = [l for l in _sans_ansi(snap).lower().splitlines() if l.strip()][-25:]
    txt = '\n'.join(fin)
    for motif, cause in MURS:
        if motif == '429':
            # « rate limit » nu se lit aussi dans le libelle d'une tache d'agent
            # (« resetting anonymous rate limit config ») : seule une erreur d'API compte.
            if re.search(r'api error[^\n]*\b429\b', txt):
                return cause
            continue
        if motif in txt:
            return cause
    return None


def mur_dans_le_journal(sid):
    """Le journal de la session porte-t-il l'erreur qui l'a tuee ?"""
    if not sid:
        return None
    cfgs = socle.config_dirs_connus()
    for cfg in cfgs:
        for f in glob.glob(os.path.join(cfg, 'projects', '*', sid + '.jsonl')):
            try:
                with open(f, encoding='utf-8', errors='replace') as fh:
                    queue = fh.readlines()[-30:]
            except Exception:
                continue
            txt = ''.join(queue).lower()
            for motif, cause in MURS:
                if motif != '429' and motif in txt:
                    return cause
            if '"isapierrormessage":true' in txt and ('429' in txt or 'rate' in txt):
                return 'quota-atteint'
    return None


def mesures_tmux(passe):
    """Les mesures d'une passe tmux, dans la meme forme que celles du moteur api."""
    sid = passe.get('claude_session_id')
    if not sid:
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('mesures_noeud', str(FLUX / 'runtime' / 'mesures-noeud.py'))
        mn = importlib.util.module_from_spec(spec); spec.loader.exec_module(mn)
        m = mn.mesure(sid)
    except Exception:
        return None
    modele = passe.get('modele') or 'opus'
    return {'jetons_entree': (m.get('lu', 0) + m.get('ecrit', 0) + m.get('neuf', 0)),
            'jetons_sortie': m.get('sortie', 0), 'jetons_neuf': m.get('neuf', 0),
            'jetons_cache_ecrit': m.get('ecrit', 0), 'jetons_cache_lu': m.get('lu', 0),
            'duree_s': passe.get('duree_s'), 'tours': m.get('tours', 0), 'agents': m.get('agents', 0),
            'cout_usd': socle.cout_usd(modele, m.get('neuf', 0), m.get('sortie', 0),
                                       m.get('ecrit', 0), m.get('lu', 0)),
            'fournisseur': 'anthropic', 'compte': passe.get('compte'), 'modele': modele}


def terminer_passe(wf, node, ws, nid, passages, npass, pdir, mode, rc_panne=2):
    """La fin commune des deux moteurs : gate de sortie, note, etat, sequenceur.

    Codes : 0 vert · 1 rouge · 2 panne.
    - Une passe en PANNE n'est pas jugee : le gate n'accepte pas son livrable,
      le nœud porte le statut `panne`, jamais vert.
    - En BANC, le juge du nœud note la passe ; un juge qui plante = panne.
    - En PRODUCTION, `note: null`, le juge n'est pas lance.
    """
    p = passages[-1]
    if p.get('fin') == 'panne':
        p['verdict'] = 'panne'
        p.setdefault('erreurs', [])
        rapport_de_run(ws)
        write_state(wf, ws, nid, statut='panne', ended=now(), rc=rc_panne, passages=passages,
                    erreurs=p['erreurs'], livrables_manquants=[])
        socle.ecrire_etat_compact(wf['id'], ws)
        print('  ⚡ PANNE — ' + str((p.get('panne') or {}).get('cause')) +
              ' : le livrable de cette passe n\'est pas accepte, le nœud n\'est pas vert',
              file=sys.stderr)
        reveiller_sequenceur(wf, ws, nid)
        return rc_panne

    # ── 4. GATE DE SORTIE (la trace des transcripts est deja rendue pour tmux)
    ok, errs = gate((node.get('gate') or {}).get('validateur'), ws, node=node)
    restants = [d for d in livrables_exiges(node, ws)[0] if not present(ws, d)]
    # Le rapport de passe n'est PAS exige ici. Un nœud dont le travail est bon
    # echouerait pour un texte qui ne juge rien.
    vert = bool(ok and not restants)
    p['verdict'] = 'vert' if vert else 'rouge'
    p['erreurs'] = [] if vert else errs[:20]

    # ── 5. NOTE (banc seulement). Le juge est un script sans modele : il
    #    compare le livrable au corrige du cas. En production il n'y a pas de
    #    corrige, donc pas de note — c'est la relecture humaine qui juge.
    if vert and mode == 'banc' and (node.get('juge') is not None or node.get('juges')):
        try:
            p['note'] = socle.noter(wf['id'], node, ws, pdir=pdir)
            print('  ✎ note : %s/%s' % (p['note'].get('score'), p['note'].get('bareme')),
                  file=sys.stderr)
        except Exception as e:
            p.update({'fin': 'panne', 'verdict': 'panne', 'note': None,
                      'panne': {'cause': 'juge-en-panne', 'constat': str(e)[:400],
                                'releve_a': now(), 'releve_par': 'runtime'}})
            p['erreurs'] = [str(e)[:400]]
            write_state(wf, ws, nid, statut='panne', ended=now(), rc=rc_panne, passages=passages,
                        erreurs=p['erreurs'], livrables_manquants=restants)
            socle.ecrire_etat_compact(wf['id'], ws)
            print('  ⚡ PANNE — le juge a plante : ' + str(e)[:200], file=sys.stderr)
            reveiller_sequenceur(wf, ws, nid)
            return rc_panne
    elif mode != 'banc':
        p['note'] = None

    # Le verdict est un AVIS : il est conserve dans le passage et affiche, mais
    # il n'ecrit plus le statut du nœud — c'est « clore » qui le fait.
    write_state(wf, ws, nid, ended=now(), rc=0 if vert else 1, passages=passages,
                erreurs=p['erreurs'], livrables_manquants=restants)
    socle.ecrire_etat_compact(wf['id'], ws)
    reveiller_sequenceur(wf, ws, nid)
    if vert:
        print(f'  ✓ GATE SORTIE — avis vert, suivant autorisé : {node.get("suivant") or "(fin)"}', file=sys.stderr)
        return 0
    motif = f'{len(restants)} livrable(s) manquant(s)'
    if errs: motif = f'{len(errs)} contrat(s), ' + motif
    print(f'  ✗ GATE SORTIE — ROUGE ({motif})', file=sys.stderr)
    for e in errs[:5]: print(f'      {e}', file=sys.stderr)
    for m in restants: print(f'      manquant: {m}', file=sys.stderr)
    return 1

if __name__ == '__main__':
    sys.exit(main())
