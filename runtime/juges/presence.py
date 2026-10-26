#!/usr/bin/env python3
"""Juge minimal : les livrables sont-ils la, et non vides ?

Contrat commun des juges de flux (design v2, section 3.3) :

    juge.py <corrige> <sortie> [<sortie>...]   ->  JSON {score, bareme, lignes} sur stdout

Ce juge-ci ne compare rien : il n'a pas de corrige. Il existe pour les workflows
dont les etapes rendent de la prose ou des dossiers, qu'aucun bareme ne sait
noter case par case (veille, webapp), et qui seraient sinon refuses par la regle
« pas d'etape sans juge » (D3). Le graphe le declare explicitement par
`juge: {"type": "presence"}` : le lecteur voit ainsi que l'etape n'est PAS notee
sur son contenu, seulement sur le fait qu'elle a rendu quelque chose.

Tout ou rien : un seul livrable manquant ou vide met le score a 0. Un demi-point
pour un demi-livrable donnerait une note qui ressemble a une mesure de qualite
alors qu'elle ne mesure que de la presence.

L'argument <corrige> est accepte et ignore (mettre `-`) : c'est le prix a payer
pour que le runtime lance tous les juges de la meme facon.
"""
import json
import os
import sys


def non_vide(chemin):
    """Un fichier non vide, ou un dossier qui porte au moins un fichier non vide."""
    if os.path.isdir(chemin):
        for racine, _dossiers, fichiers in os.walk(chemin):
            for f in fichiers:
                try:
                    if os.path.getsize(os.path.join(racine, f)) > 0:
                        return True
                except OSError:
                    continue
        return False
    try:
        return os.path.getsize(chemin) > 0
    except OSError:
        return False


def juger(sorties, bareme=20):
    lignes = []
    manquants = 0
    for s in sorties:
        if not os.path.exists(s):
            manquants += 1
            lignes.append('NON %s : absent' % s)
        elif not non_vide(s):
            manquants += 1
            lignes.append('NON %s : present mais vide' % s)
        else:
            lignes.append('OK  %s : present et non vide' % s)
    score = 0 if (manquants or not sorties) else bareme
    if not sorties:
        lignes.append('NON aucun livrable a verifier')
    return {'score': score, 'bareme': bareme, 'lignes': lignes}


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    bareme = 20
    for a in argv:
        if a.startswith('--bareme='):
            bareme = float(a.split('=', 1)[1])
    if 'FLUX_BAREME' in os.environ:
        try:
            bareme = float(os.environ['FLUX_BAREME'])
        except ValueError:
            pass
    if bareme == int(bareme):
        bareme = int(bareme)
    if len(args) < 2:
        sys.stderr.write(__doc__.split('\n\n')[1].strip() + '\n')
        return 2
    _corrige, sorties = args[0], args[1:]
    print(json.dumps(juger(sorties, bareme), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
