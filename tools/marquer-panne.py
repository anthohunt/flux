#!/usr/bin/env python3
"""marquer-panne.py — dire qu'une passe est tombée en panne, et pourquoi.

**Une panne n'est pas un refus.** Un refus est un jugement sur le travail : la
passe a produit quelque chose, un contrôle l'a rejeté, et la boucle repart. Une
panne est un accident d'exécution — la machine est tombée, la session est morte,
le moteur a basculé de compte, le lanceur a disparu. Rien n'a été jugé, et il n'y
a rien à corriger : il faut rejouer.

Les deux se ressemblent dans l'état d'un nœud (une passe qui finit mal) et la
console les peignait pareil, ou pas du tout. Ce marquage les sépare : c'est
l'ORCHESTRATEUR qui le pose, au moment où il constate la panne et décide de
relancer — lui seul voit la passe morte et la passe qui la remplace.

    marquer-panne.py <état-du-nœud.json> <n° de passe> --cause <code> --constat "…"
    marquer-panne.py … --relancee-en <n>     (le n° de la passe qui la rejoue)

Codes de cause, fermés — un code hors liste est refusé, sinon chaque réveil
invente le sien et rien ne se compte :
    session-morte · lanceur-disparu · machine-tombee · moteur-bascule
    quota-atteint · reseau-coupe · inconnue
"""
import argparse, datetime, json, pathlib, sys

CAUSES = ('session-morte', 'lanceur-disparu', 'machine-tombee', 'moteur-bascule',
          'quota-atteint', 'reseau-coupe', 'inconnue')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('etat', help="le fichier d'état du nœud (…/state/<wf>/<run>/<nœud>.json)")
    ap.add_argument('passe', type=int, help='le numéro de la passe tombée')
    ap.add_argument('--cause', required=True, choices=CAUSES)
    ap.add_argument('--constat', required=True,
                    help='ce que tu as CONSTATÉ, pas ce que tu supposes — une phrase')
    ap.add_argument('--relancee-en', type=int, default=None,
                    help='le numéro de la passe qui la rejoue, si elle est déjà lancée')
    a = ap.parse_args()

    p = pathlib.Path(a.etat)
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'état illisible : {type(e).__name__}: {e}', file=sys.stderr)
        return 2

    cible = next((x for x in (d.get('passages') or []) if x.get('n') == a.passe), None)
    if cible is None:
        print(f'aucune passe n°{a.passe} dans {p.name}', file=sys.stderr)
        return 1

    # La panne ne remplace RIEN de ce que la passe a laissé : ni son verdict, ni
    # sa fin, ni ses horodatages. Elle s'ajoute — sinon on perdrait la trace de
    # ce qui a été constaté au moment où ça s'est produit.
    cible['panne'] = {
        'cause': a.cause,
        'constat': a.constat,
        'releve_a': datetime.datetime.now(datetime.timezone.utc)
                    .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'releve_par': 'orchestrateur',
        **({'relancee_en': a.relancee_en} if a.relancee_en else {}),
    }
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'panne posée sur {p.stem} p{a.passe} : {a.cause}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
