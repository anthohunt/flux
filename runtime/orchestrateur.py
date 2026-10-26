#!/usr/bin/env python3
"""L'orchestrateur agent n'existe plus (flux v2, D4) : le programme enchaine.

Ce fichier ne reste que pour qui l'appelle encore par son ancien nom (le
bouton « reprendre » de la console, un script) : il passe la main au
sequenceur avec les memes arguments. L'ancien code est dans
runtime/_archive/orchestrateur.py, et n'est plus appele par personne.

    orchestrateur.py <workflow-id> <workspace> [--noeud-fini <id>]
        ->  sequenceur.py <workflow-id> <workspace> [--noeud-fini <id>]
"""
import os
import pathlib
import sys

if __name__ == '__main__':
    print('  orchestrateur.py est retire : le sequenceur prend la main (flux v2, D4)',
          file=sys.stderr)
    seq = pathlib.Path(__file__).resolve().parent / 'sequenceur.py'
    args = [a for a in sys.argv[1:] if a != '--dry-run'] + \
        (['--dry-run'] if '--dry-run' in sys.argv else [])
    os.execv(sys.executable, [sys.executable, str(seq)] + args)
