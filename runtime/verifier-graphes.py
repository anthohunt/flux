#!/usr/bin/env python3
"""Verifie les graphes de flux contre le contrat v2 (runtime/workflow.schema.json).

    verifier-graphes.py [--racine DIR] [graphe.json ...]

Sans argument : tous les workflows/*.json de la racine (la racine = le parent de
runtime/). Une section lisible par graphe, `exit 1` des qu'un graphe est refuse.

Deux couches, dans cet ordre, parce que la seconde parle mal :
  1. les regles metier, ecrites a la main, avec un message qui dit QUOI et OU :
     - id de workflow = nom du fichier ; ids de noeuds uniques ;
     - tout noeud porte `modele`, `entree` et un juge (`juge` OU `juges`, pas les deux) ;
     - un noeud `api` porte `schema` (et le fichier existe, est un JSON Schema
       valide, reste dans le sous-ensemble que les deux moteurs acceptent) ;
     - `moteur` figure dans `moteurs_autorises` ;
     - `suivant` pointe vers des ids existants ;
     - `juge.script` existe ; `juges[].sortie` est un des `livrables` ;
     - `modele: "{x}"` reprend un parametre de run `x` de type choix ;
     - `entree` d'un noeud non premier : avertissement si un chemin n'est produit
       par aucun predecesseur ni aucun parametre de run (les globs comptent).
  2. la validation jsonschema (Draft 2020-12) : types, required,
     additionalProperties, if/then du moteur — ce que la premiere couche ne dit pas.

Le corrige d'un juge (`juge.corrige`) se resout dans cas/<cas>/ au moment de la
run, pas ici : on avertit seulement si aucun cas/*/ ne le porte.
"""
import argparse
import fnmatch
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle  # noqa: E402

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
except ImportError:  # pragma: no cover
    sys.stderr.write('jsonschema manquant : pip install jsonschema\n')
    sys.exit(2)

ICI = pathlib.Path(__file__).resolve().parent
RACINE_DEFAUT = ICI.parent
MODELE_PARAM = re.compile(r'^\{([a-z][a-z0-9_]*)\}$')

# Mots-cles de JSON Schema que le tool_use strict d'Anthropic refuse (doc
# « structured outputs », lue le 2026-09-04) et qu'Ollama n'honore pas toujours.
# Un schema de noeud api qui les emploie serait rejete par un moteur et pas par
# l'autre : on le refuse ici, avant qu'une run le decouvre.
MOTS_CLES_HORS_SOUS_ENSEMBLE = {
    'pattern', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'multipleOf', 'minLength', 'maxLength', 'maxItems', 'uniqueItems',
    'minProperties', 'maxProperties', 'patternProperties', 'not', 'oneOf',
}


def charger_schema(racine):
    return json.load(open(racine / 'runtime' / 'workflow.schema.json', encoding='utf-8'))


def _sous_schemas(s, chemin=''):
    """Parcourt un JSON Schema et rend (chemin, mot-cle, valeur) pour chaque mot-cle."""
    if isinstance(s, dict):
        for k, v in s.items():
            yield chemin, k, v
            if k in ('properties', '$defs', 'definitions') and isinstance(v, dict):
                for nom, sub in v.items():
                    yield from _sous_schemas(sub, '%s/%s/%s' % (chemin, k, nom))
            elif k in ('items', 'then', 'else', 'if') and isinstance(v, dict):
                yield from _sous_schemas(v, '%s/%s' % (chemin, k))
            elif k in ('anyOf', 'allOf', 'oneOf') and isinstance(v, list):
                for i, sub in enumerate(v):
                    yield from _sous_schemas(sub, '%s/%s/%d' % (chemin, k, i))


def fautes_schema_api(chemin_schema, racine):
    """Ce qui rend un schema de sortie inutilisable tel quel par un des deux moteurs."""
    fautes = []
    p = racine / chemin_schema
    if not p.is_file():
        return ['schema introuvable : %s' % chemin_schema]
    try:
        s = json.load(open(p, encoding='utf-8'))
    except ValueError as e:
        return ['schema illisible (%s) : %s' % (e, chemin_schema)]
    try:
        Draft202012Validator.check_schema(s)
    except Exception as e:  # SchemaError
        return ['schema invalide : %s' % str(e).splitlines()[0]]
    for ch, k, v in _sous_schemas(s):
        if k in MOTS_CLES_HORS_SOUS_ENSEMBLE:
            fautes.append('schema %s : `%s` en %s n\'est pas accepte par le tool_use strict '
                          'd\'Anthropic — mets la contrainte dans `description`' % (chemin_schema, k, ch or '/'))
        elif k == 'minItems' and v not in (0, 1):
            fautes.append('schema %s : `minItems: %s` en %s (Anthropic strict : 0 ou 1 seulement)' % (chemin_schema, v, ch or '/'))
        elif k == 'additionalProperties' and v is not False:
            fautes.append('schema %s : `additionalProperties` doit valoir false en %s' % (chemin_schema, ch or '/'))
        elif k == '$ref' and isinstance(v, str) and not v.startswith('#'):
            fautes.append('schema %s : `$ref` externe en %s' % (chemin_schema, ch or '/'))
    # tout objet doit fermer ses proprietes (exigence Anthropic strict)
    for ch, k, v in _sous_schemas(s):
        if k == 'type' and v == 'object':
            parent = _objet_a(s, ch)
            if parent is not None and parent.get('additionalProperties') is not False:
                fautes.append('schema %s : l\'objet en %s ne porte pas `additionalProperties: false`' % (chemin_schema, ch or '/'))
    return fautes


def _objet_a(s, chemin):
    cur = s
    for seg in [x for x in chemin.split('/') if x]:
        if isinstance(cur, list):
            cur = cur[int(seg)]
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur if isinstance(cur, dict) else None


def _couvre(motifs, chemin):
    """Un chemin d'entree est couvert si un livrable/destination l'egale, le
    contient (dossier) ou le matche (glob)."""
    for m in motifs:
        if m == chemin or fnmatch.fnmatch(chemin, m) or fnmatch.fnmatch(m, chemin):
            return True
        if m.endswith('/') and chemin.startswith(m):
            return True
        if chemin.endswith('/') and m.startswith(chemin):
            return True
    return False


def regles_metier(d, fichier, racine):
    """Rend (refus[], avertissements[])."""
    refus, avert = [], []
    if not isinstance(d, dict) or not isinstance(d.get('nodes'), list):
        return ['pas un objet avec une liste `nodes`'], avert
    if d.get('id') != fichier.stem:
        refus.append('id `%s` different du nom de fichier `%s`' % (d.get('id'), fichier.stem))
    params = {p.get('id'): p for p in d.get('parametres_run', []) if isinstance(p, dict)}
    destinations = [p['destination'] for p in params.values() if p.get('destination')]
    moteurs_ok = d.get('moteurs_autorises') or ['tmux']
    ids = [n.get('id') for n in d['nodes'] if isinstance(n, dict)]
    for i in sorted({x for x in ids if ids.count(x) > 1}, key=str):
        refus.append('id de noeud en double : %s' % i)
    connus = set(ids)
    preds = {i: [] for i in connus}
    for n in d['nodes']:
        for s in (n.get('suivant') or []) if isinstance(n, dict) else []:
            if s in preds:
                preds[s].append(n)
    for n in d['nodes']:
        if not isinstance(n, dict):
            refus.append('un noeud n\'est pas un objet')
            continue
        nid = n.get('id', '?')
        moteur = n.get('moteur', 'tmux')
        if moteur not in moteurs_ok:
            refus.append('%s : moteur `%s` hors de moteurs_autorises %s' % (nid, moteur, moteurs_ok))
        if 'modele' not in n:
            refus.append('%s : `modele` manquant (obligatoire : opus | sonnet | haiku | ollama:<nom> | {parametre})' % nid)
        else:
            m = MODELE_PARAM.match(str(n['modele']))
            if m:
                p = params.get(m.group(1))
                if not p:
                    refus.append('%s : modele `%s` reprend un parametre de run inconnu' % (nid, n['modele']))
                elif p.get('type') != 'choix':
                    refus.append('%s : modele `%s` reprend un parametre qui n\'est pas de type choix' % (nid, n['modele']))
        if 'entree' not in n:
            refus.append('%s : `entree` manquante (obligatoire, liste de chemins relatifs au workspace ; [] si le noeud part de la conversation)' % nid)
        elif not isinstance(n['entree'], list):
            refus.append('%s : `entree` doit etre une liste' % nid)
        if 'juge' not in n and 'juges' not in n:
            refus.append('%s : pas de juge (obligatoire : `juge: {type: presence}` ou `juge: {script, corrige, bareme}`, ou `juges: [...]`)' % nid)
        if 'juge' in n and 'juges' in n:
            refus.append('%s : `juge` et `juges` a la fois — un seul des deux' % nid)
        juges = []
        if isinstance(n.get('juge'), dict):
            juges.append((n['juge'], None))
        if isinstance(n.get('juges'), list):
            juges.extend((j, j.get('sortie')) for j in n['juges'] if isinstance(j, dict))
        for j, sortie in juges:
            if j.get('type') == 'presence':
                continue
            script = j.get('script')
            if script and not (racine / script).is_file():
                refus.append('%s : juge.script introuvable : %s' % (nid, script))
            corrige = j.get('corrige')
            if corrige and (racine / 'cas').is_dir():
                if not any((c / corrige).is_file() for c in (racine / 'cas').iterdir() if c.is_dir()):
                    avert.append('%s : aucun cas/*/ ne porte le corrige `%s`' % (nid, corrige))
        for j, sortie in juges:
            if sortie is not None and sortie not in (n.get('livrables') or []):
                refus.append('%s : juges[].sortie `%s` n\'est pas un des livrables %s' % (nid, sortie, n.get('livrables')))
        if moteur == 'api':
            if 'schema' not in n:
                refus.append('%s : noeud api sans `schema` (obligatoire : le JSON Schema de sa sortie)' % nid)
            else:
                refus.extend('%s : %s' % (nid, f) for f in fautes_schema_api(n['schema'], racine))
            if not n.get('consignes'):
                refus.append('%s : noeud api sans `consignes` (texte libre de l\'appel)' % nid)
        for s in n.get('suivant') or []:
            if s not in connus:
                refus.append('%s : suivant `%s` ne correspond a aucun noeud' % (nid, s))
        # entree couverte par un predecesseur ou un parametre de run
        if isinstance(n.get('entree'), list) and preds.get(nid):
            produits = [l for p in preds[nid] for l in (p.get('livrables') or [])] + destinations
            for e in n['entree']:
                if e != 'WORKSPACE.txt' and not _couvre(produits, e):
                    avert.append('%s : entree `%s` n\'est produite par aucun predecesseur %s'
                                 % (nid, e, [p.get('id') for p in preds[nid]]))
    return refus, avert


def erreurs_schema(d, validateur):
    lignes = []
    for e in sorted(validateur.iter_errors(d), key=lambda e: list(e.absolute_path)):
        chemin = '/'.join(str(x) for x in e.absolute_path) or '/'
        msg = e.message
        if e.context:
            msg += ' — ' + best_match(e.context).message
        lignes.append('%s : %s' % (chemin, msg.replace('\n', ' ')[:300]))
    return lignes


def verifier(fichier, racine, validateur):
    """Rend (ok, lignes) pour un graphe."""
    lignes = []
    try:
        d = json.load(open(fichier, encoding='utf-8'))
    except ValueError as e:
        return False, ['REFUS JSON illisible : %s' % e]
    refus, avert = regles_metier(d, fichier, racine)
    lignes.extend('REFUS %s' % r for r in refus)
    lignes.extend('avert %s' % a for a in avert)
    if not refus:
        sch = erreurs_schema(d, validateur)
        lignes.extend('REFUS schema %s' % s for s in sch)
        refus.extend(sch)
    if not refus:
        nb = len(d['nodes'])
        api = sum(1 for n in d['nodes'] if n.get('moteur') == 'api')
        lignes.append('ok    %d noeud(s), %d api, %d avertissement(s)' % (nb, api, len(avert)))
    return not refus, lignes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--racine', default=str(RACINE_DEFAUT), help='racine du depot flux (defaut : parent de runtime/)')
    ap.add_argument('graphes', nargs='*', help='fichiers a verifier (defaut : workflows/*.json)')
    a = ap.parse_args(argv)
    racine = pathlib.Path(a.racine).resolve()
    validateur = Draft202012Validator(charger_schema(racine))
    fichiers = [pathlib.Path(g) for g in a.graphes] or (socle.tous_les_graphes() if os.environ.get('FLUX_WORKFLOWS_DIR') else sorted((racine / 'workflows').glob('*.json')))
    tout_ok = True
    for f in fichiers:
        ok, lignes = verifier(f, racine, validateur)
        print('%s %s' % ('ACCEPTE' if ok else 'REFUSE ', f.relative_to(racine) if f.is_relative_to(racine) else f))
        for l in lignes:
            print('   ' + l)
        if not ok:
            tout_ok = False
            break
    print('GRAPHES CONFORMES' if tout_ok else 'GRAPHE REFUSE')
    return 0 if tout_ok else 1


if __name__ == '__main__':
    sys.exit(main())
