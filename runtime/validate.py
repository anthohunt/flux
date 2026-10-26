#!/usr/bin/env python3
"""
Le controle de fin d'etape (« gate ») : lance le validateur qu'un nœud declare.

Usage :
    python3 validate.py <id-du-validateur> <workspace>

Un nœud declare `gate: {validateur: "<id>"}` dans son graphe. Le validateur est
un module Python qui expose `validate(workspace) -> list[str]` (une liste vide =
tous les contrats respectes). Il est cherche, dans l'ordre, dans :
  1. chaque dossier de FLUX_VALIDATORS_PATH (separes par « : ») ;
  2. <racine du depot>/validators ;
  3. <FLUX_PIPELINE_RUNTIME>/validators (defaut : <FLUX_SKILLS_DIR>/pipeline-runtime).

Dans chaque dossier, un fichier `validators.json` facultatif associe un
identifiant a un nom de module ({"1": "etape1_prevol"}) ; sans entree, le module
porte le nom de l'identifiant, tirets remplaces par des soulignes
(`webapp-construire` -> `webapp_construire.py`).

Codes de sortie :
    0  tous les contrats sont respectes
    2  au moins un contrat casse (detail numerote sur stderr)
    3  erreur interne (validateur introuvable, workspace invalide)
"""
import importlib.util
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle  # noqa: E402


def dossiers():
    out = [pathlib.Path(d) for d in os.environ.get('FLUX_VALIDATORS_PATH', '').split(':') if d]
    out.append(socle.racine() / 'validators')
    out.append(socle.pipeline_runtime_dir() / 'validators')
    return out


def trouver(ident):
    for d in dossiers():
        nom = None
        try:
            nom = json.loads((d / 'validators.json').read_text(encoding='utf-8')).get(ident)
        except Exception:
            pass
        f = d / ('%s.py' % (nom or ident.replace('-', '_')))
        if f.exists():
            return f
    return None


def charger(f):
    spec = importlib.util.spec_from_file_location(f.stem, f)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(f.parent))
    spec.loader.exec_module(mod)
    return mod


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 3
    ident = sys.argv[1].strip()
    workspace = os.path.abspath(sys.argv[2])
    if not os.path.isdir(workspace):
        print('workspace introuvable: %s' % workspace, file=sys.stderr)
        return 3
    f = trouver(ident)
    if not f:
        print("[validate] validateur '%s' introuvable (cherche dans : %s)"
              % (ident, ', '.join(str(d) for d in dossiers())), file=sys.stderr)
        return 3
    print('[validate] %s — %s — workspace %s' % (ident, f, workspace), file=sys.stderr)
    errors = charger(f).validate(workspace) or []
    # Les codes de contrat (C1, C2…) sont locaux a leur validateur : on les prefixe
    # par son identifiant pour qu'un « C2 » se cherche sans ambiguite.
    prefixe = ident if not ident[:1].isdigit() else ident.split('-')[0]
    errors = [re.sub(r'^\s*(C\d+)', prefixe + '·' + r'\1', e) if isinstance(e, str) else e
              for e in errors]
    if not errors:
        print('[validate] ✓ %s — tous les contrats respectés' % ident, file=sys.stderr)
        return 0
    print('\n[validate] ✗ %s — %d contrat(s) cassé(s) :\n' % (ident, len(errors)), file=sys.stderr)
    for i, e in enumerate(errors, 1):
        print('  %d. %s' % (i, e), file=sys.stderr)
    print("\n[validate] l'étape suivante ne démarrera pas. Corrige et relance.", file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
