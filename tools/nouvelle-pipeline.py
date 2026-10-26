#!/usr/bin/env python3
"""nouvelle-pipeline.py — fabrique une pipeline conforme au socle.

    nouvelle-pipeline.py <id> "<Titre lisible>" [--noeuds cadrer,construire,livrer]

Produit, en une passe et sans rien inventer :
  · workflows/<id>.json          le graphe, conforme à workflow.schema.json
  · validators/<id>_<noeud>.py   un contrat par nœud, qui vérifie ses livrables
  · <id>-start/SKILL.md          le point d'entrée qu'une session neuve invoque
  · <workspace_root>/            la racine des runs, à elle seule

Ce que la fabrique NE fait pas : décider du métier. Les objectifs, les tâches et les
livrables sont des amorces à remplir — c'est le travail de conception de la pipeline.
Ce qu'elle garantit, c'est que la structure est la bonne dès le départ : mêmes clés,
même moteur, même gate, même lanceur que les pipelines déjà en service.
"""
import json, os, pathlib, re, sys, argparse

FLUX = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FLUX / 'runtime'))
import socle  # noqa: E402

VALIDATORS = FLUX / 'validators'
SKILLS = socle.skills_dir()

TYPES = {'agent', 'local', 'human', 'fanout'}


def slug(s):
    return re.sub(r'[^a-z0-9-]+', '-', s.lower()).strip('-')


def graphe(pid, titre, noeuds):
    nodes = []
    for i, (nid, ntype) in enumerate(noeuds, 1):
        n = {
            'id': nid,
            'n': i,
            'type': ntype,
            'name': nid.replace('-', ' ').replace('_', ' ').capitalize(),
            'objectif': 'À ÉCRIRE — ce que ce nœud doit produire, en prose.',
            'skills': [f'{pid}-start'],
            'tools': ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'TodoWrite', 'WebSearch', 'WebFetch', 'Skill'],
            'taches': ['À ÉCRIRE — premier geste imposé'],
            'taches_detail': [{
                'titre': 'À ÉCRIRE — premier geste imposé',
                'par': "l'agent",
                'detail': 'À ÉCRIRE — le comment, injecté sous la tâche dans le brief.',
            }],
            'livrables': [f'docs/pipeline/{nid}.md'],
            'gate': {'validateur': f'{pid}-{nid}'},
            'suivant': [noeuds[i][0]] if i < len(noeuds) else [],
        }
        if ntype == 'local':
            n['note_locale'] = "Étape jouée en conversation : le flux donne le brief et constate le résultat."
            n['banderole'] = f'FIN DE L\'ÉTAPE {nid.upper()}'
        if ntype == 'human':
            n['note_humaine'] = 'À ÉCRIRE — ce qu\'on attend de la personne.'
            n['contexte_valide'] = 'À ÉCRIRE — le fait à constater pour considérer la barrière franchie.'
        nodes.append(n)
    return {
        '$schema': '../runtime/workflow.schema.json',
        'id': pid,
        'name': titre,
        'description': 'À ÉCRIRE — une phrase sur ce que la pipeline produit.',
        'workspace_root': f'~/{pid}-runs',
        'nodes': nodes,
    }


VALIDATEUR = '''"""
Validateur {pid} — {nid}.

Gate de sortie du nœud « {nid} ». Un contrat juge le FAIT, jamais un numéro d'étape :
le découpage des nœuds peut changer, ce que la run a réellement franchi ne change pas.

Il accepte les variantes équivalentes d'emballage (nom de clé, emplacement du fichier) :
l'endroit où l'agent a écrit n'est pas un critère de qualité. Ce qui se juge, c'est le
contenu.

Contrats :
1. Les livrables du nœud sont présents et non vides.
2. À COMPLÉTER — ce qui distingue un travail fait d'un travail bâclé.
"""
import pathlib

LIVRABLES = {livrables!r}


def _txt(p):
    try:
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def validate(workspace):
    errs = []
    ws = pathlib.Path(workspace)

    for f in LIVRABLES:
        p = ws / f
        if not p.exists() or p.stat().st_size == 0:
            errs.append(f"C1: `{{f}}` absent/vide — le nœud n'a pas produit ce qu'il annonce")

    # C2 : à compléter quand on saura, sur une vraie run, ce qui distingue un bon
    # livrable d'un livrable de façade. Une règle qui n'est pas ici n'existe pas.

    return errs
'''

SKILL = '''---
name: {pid}-start
description: Ouvre ou reprend une run de la pipeline « {titre} » (flux `{pid}`) dans la session courante. Déclencheurs — « /{pid}-start », « reprends la run {pid} ».
---

# {pid}-start — entrer dans la pipeline « {titre} »

Point d'entrée unique du flux `{pid}`. Une session neuve invoque ce skill et se retrouve
avec tout ce qu'il faut : la run résolue, le workspace en local, le brief du nœud, les
protocoles injectés.

## Les nœuds

{tableau}

## Déroulé

### 1. Résoudre la run

```bash
flux-local {pid}          # liste les nœuds et leur état, nomme la run active
```

Aucune run active, ou nouveau sujet → en créer une :

```bash
ssh $FLUX_VPS "python3 $FLUX_DISTANT/runtime/new-run.py {pid} '<nom du sujet>'"
```

(`FLUX_VPS` = `user@hote` du serveur flux — voir `moteurs.json`, clé `serveur.ssh` ;
`FLUX_DISTANT` = le chemin du dépôt flux sur ce serveur.)

La run la plus récente devient la run active. Les runs de ce flux vivent sous
`~/{pid}-runs`, séparées de celles des autres pipelines : une run d'ici ne
devient jamais la run active d'un autre flux.

### 2. Rapatrier le workspace et sortir le brief

```bash
flux-local {pid} <nœud>
```

Le workspace canonique vit sur le serveur ; cette commande le copie sous `~/flux-runs/<run>/`,
écrit `.flux/<nœud>/brief.md` (objectif, check-list imposée, livrables attendus) et
`.flux/<nœud>/sys.md` (les protocoles injectés).

**Lire les deux fichiers en entier avant de parler.** Le brief EST la check-list.

### 3. Jouer le nœud

Suivre la check-list du brief, dans l'ordre, sans en sauter ni en réinterpréter.

### 4. Renvoyer et passer le gate

```bash
flux-local {pid} <nœud> --push
```

**Rouge = on corrige et on repousse**, dans la même session. Le nœud suivant ne démarre pas
tant que le gate n'est pas vert.

## Ce qu'il ne faut pas faire

- **Lancer un nœud interactif depuis le flux.** Il n'y aurait personne pour répondre.
- **Travailler directement dans le workspace du VPS.** On rapatrie, on travaille, on renvoie :
  les gates jugent des livrables datés, et éditer des deux côtés les fait diverger.
- **Repartir de zéro sur un gate rouge.** Le brief injecté à la reprise porte les refus reçus.

## Références

- Contrat du graphe : `<flux>/runtime/workflow.schema.json`
- Graphe : `<flux>/workflows/{pid}.json` (la vérité exécutable)
- Validateurs : `<flux>/validators/{pid}_*.py`
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('id')
    ap.add_argument('titre')
    ap.add_argument('--noeuds', default='cadrer:local,construire:agent,livrer:agent',
                    help='liste ordonnée nom:type, séparés par des virgules')
    a = ap.parse_args()

    pid = slug(a.id)
    cible = FLUX / 'workflows' / f'{pid}.json'
    if cible.exists():
        sys.exit(f'le flux « {pid} » existe déjà : {cible}')

    noeuds = []
    for part in a.noeuds.split(','):
        nid, _, ntype = part.partition(':')
        ntype = ntype or 'agent'
        if ntype not in TYPES:
            sys.exit(f'type de nœud inconnu : {ntype} (attendus : {", ".join(sorted(TYPES))})')
        noeuds.append((slug(nid), ntype))

    g = graphe(pid, a.titre, noeuds)
    cible.write_text(json.dumps(g, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('graphe        ', cible)

    for n in g['nodes']:
        v = VALIDATORS / f"{pid}_{n['id']}.py"
        v.write_text(VALIDATEUR.format(pid=pid, nid=n['id'], livrables=n['livrables']), encoding='utf-8')
        print('validateur    ', v)

    lignes = '\n'.join('| %d | **%s** | %s |' % (n['n'], n['name'], n['type']) for n in g['nodes'])
    tableau = '| # | Nœud | Mode |\n|---|---|---|\n' + lignes
    sk = SKILLS / f'{pid}-start'
    sk.mkdir(parents=True, exist_ok=True)
    (sk / 'SKILL.md').write_text(SKILL.format(pid=pid, titre=a.titre, tableau=tableau), encoding='utf-8')
    print('point d\'entrée', sk / 'SKILL.md')

    # Si la fabrique tourne sous un autre compte que le runtime (FLUX_PROPRIETAIRE =
    # utilisateur:groupe des agents), on transfère la propriété : sinon la première run
    # échoue sur un refus d'écriture.
    root = socle.workspace_root(g)
    root.mkdir(parents=True, exist_ok=True)
    proprio = os.environ.get('FLUX_PROPRIETAIRE', '')
    if proprio:
        try:
            import shutil
            u, _, grp = proprio.partition(':')
            shutil.chown(root, user=u, group=grp or None)
        except (LookupError, PermissionError, OSError) as e:
            print('  ! propriété de %s à régler à la main (%s)' % (root, e))
    print('racine des runs', root)

    print()
    print('Reste à écrire, dans cet ordre : les objectifs et les tâches de chaque nœud du')
    print('graphe, puis le contrat C2 de chaque validateur. La structure, elle, est déjà')
    print('celle du socle — vérifie-la avec verifier-graphes.py.')


if __name__ == '__main__':
    main()
