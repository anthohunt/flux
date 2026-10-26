#!/usr/bin/env python3
"""Juge des actions : les sept verbes du vocabulaire ferme, verbe par verbe.

Contrat commun des juges de flux (design v2, section 3.3) :

    actions.py <corrige> <sortie>   ->  JSON {score, bareme, lignes} sur stdout

Tout ce qui empeche de noter sort en exit 2, avec "PANNE ..." sur stderr.

BAREME FIXE : 14 points, 7 verbes x 2. Il ne depend JAMAIS du nombre d'actions
du corrige. C'est la regle qui interdit la panne "rend un bareme de N, le
graphe declare 14" (runtime/juge.py) : l'ancien bareme valait 2 x le nombre
d'actions du corrige, donc 10 sur les onze cas a cinq actions et autre chose
sur les dix-neuf autres.

Le vocabulaire, ferme, dans l'ordre du corpus
(docs/2026-09-07-corpus-mail-design.md, section 2) :

    repondre    si reponse_attendue      date : echeance, sinon date_du_message
    remplir     si lien non vide         date : echeance, sinon date_du_message
    rassembler  si documents non vide    date : veille du rdv, sinon echeance,
                                                sinon date_du_message
    payer       si montant > 0           date : echeance, sinon date_du_message
    creer_rdv   si rdv_date non vide     date : rdv_date
    rappel      si rdv_date non vide     date : veille de rdv_date
    classer     si aucun des six autres  date : date_du_message

Ces regles sont celles qui ENGENDRENT le corrige (tools/genere-actions.py). Le
juge ne les rejoue pas : il compare a `golden-actions.json`, qui en sort. Ici
on ne note que l'ecart au corrige.

LA GRILLE - chaque verbe vaut 2 points, qu'il soit au corrige ou non :

    verbe au corrige, absent de la reponse                 0    (oubli)
    verbe au corrige, present, date fausse                 1
    verbe au corrige, present, date exacte                 2
    verbe hors corrige, absent de la reponse               2    (abstention juste)
    verbe hors corrige, invente par la reponse             0

"Present avec la date exacte" veut dire : toutes les occurrences de ce verbe
dans la reponse portent la date du corrige. Un verbe donne deux fois avec deux
dates differentes n'a pas repondu une fois - il vaut 1, pas 2.

VERBE HORS VOCABULAIRE : -1 par verbe distinct que la reponse invente en dehors
des sept ("confirmer", "appeler", "verifier"...). Un verbe inconnu n'est
pas une case du bareme : il ne peut pas rapporter, et il coute, sinon repondre
large serait gratuit. Le total est plancher a 0.

Le corrige, lui, doit tenir dans le vocabulaire : un `golden-actions.json` qui
porte un verbe hors des sept, ou deux fois le meme verbe, n'est pas notable sur
une grille fixe - c'est une PANNE, pas une note basse. C'est le garde-fou qui
signale un corrige reste sur l'ancien vocabulaire au lieu de le noter en
silence.

PLANCHER. La derniere ligne rendue donne la note d'une liste d'actions vide sur
CE corrige, soit 2 x (7 - nombre de verbes du corrige) : le sol dont la note
normalisee a besoin.

Le corrige et la sortie sont des listes [{verbe, date}] ; la sortie peut aussi
etre un objet qui porte cette liste sous `actions` (la forme du formulaire
complet), ce qui permet de noter les actions d'un resultat.json sans etape de
conversion.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from formulaire import norm_cle, norm_date              # noqa: E402  (meme normalisation)

VERBES = ('repondre', 'remplir', 'rassembler', 'payer', 'creer_rdv', 'rappel', 'classer')

POINTS_PAR_VERBE = 2
BAREME = POINTS_PAR_VERBE * len(VERBES)                 # 14, fixe, jamais calcule sur le corrige
MALUS_HORS_VOCABULAIRE = 1


class Panne(Exception):
    """Le juge ne peut pas noter. Le message dit pourquoi, en une ligne."""


def liste_actions(x):
    """La liste [{verbe, date}], que l'entree soit la liste ou le formulaire entier."""
    if isinstance(x, dict):
        x = x.get('actions', [])
    if not isinstance(x, list):
        return []
    return [a for a in x if isinstance(a, dict)]


def _verbe(a):
    return norm_cle(a.get('verbe', ''))


def _date(a):
    return norm_date(a.get('date', ''))


def corrige(golden):
    """{verbe: date} depuis le corrige. Leve Panne si le corrige n'est pas notable."""
    if not isinstance(golden, (list, dict)):
        raise Panne('le corrige n\'est ni une liste d\'actions ni un objet qui en porte une')
    att = {}
    for a in liste_actions(golden):
        v = _verbe(a)
        if v not in VERBES:
            raise Panne('corrige : verbe hors vocabulaire "%s" - les sept verbes sont %s'
                        % (v or '(vide)', ', '.join(VERBES)))
        if v in att:
            raise Panne('corrige : le verbe "%s" y figure deux fois - sa date est ambigue' % v)
        att[v] = _date(a)
    return att


def _noter(reponse, att):
    """(score, lignes) sur les sept verbes. Plancher 0, plafond 14."""
    obt, hors = {}, []
    for a in liste_actions(reponse):
        v = _verbe(a)
        if v in VERBES:
            obt.setdefault(v, []).append(_date(a))
        else:
            hors.append(v or '(vide)')
    score, lignes = 0, []
    for v in VERBES:
        dates = obt.get(v, [])
        if v in att:
            if not dates:
                pts, motif = 0, 'oubliee'
            elif all(d == att[v] for d in dates):
                pts, motif = POINTS_PAR_VERBE, ''
            else:
                pts, motif = 1, 'presente, date fausse'
        else:
            pts, motif = (POINTS_PAR_VERBE, 'absente des deux cotes') if not dates else (0, 'inventee')
        score += pts
        lignes.append('%s %-12s attendu=%-12s obtenu=%-24s -> %d/%d%s'
                      % ('OK ' if pts == POINTS_PAR_VERBE else 'NON', v,
                         att.get(v, '-'), ','.join(dates) or '-', pts, POINTS_PAR_VERBE,
                         '  (%s)' % motif if motif else ''))
    inconnus = sorted(set(hors))
    if inconnus:
        score -= MALUS_HORS_VOCABULAIRE * len(inconnus)
        lignes.append('NON %d verbe(s) hors vocabulaire (%s) : -%d'
                      % (len(inconnus), ', '.join(inconnus), MALUS_HORS_VOCABULAIRE * len(inconnus)))
    return max(score, 0), lignes


def juger(reponse, golden):
    att = corrige(golden)
    score, lignes = _noter(reponse, att)
    lignes.append('--- plancher (aucune action rendue sur ce corrige) : %s/%d'
                  % (plancher(att), BAREME))
    return {'score': score, 'bareme': BAREME, 'lignes': lignes}


def plancher(att):
    """La note d'une liste d'actions vide : 2 x les verbes que le corrige ne porte pas."""
    return POINTS_PAR_VERBE * (len(VERBES) - len(att))


# --------------------------------------------------------------------------- entree

def _lire(chemin, quoi):
    try:
        with open(chemin, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Panne('%s illisible (%s) : %s' % (quoi, chemin, e))


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    try:
        if len(args) != 2:
            raise Panne('usage : actions.py <corrige> <sortie>')
        golden = _lire(args[0], 'corrige')
        reponse = _lire(args[1], 'sortie')
        if not isinstance(reponse, (list, dict)):
            raise Panne('la sortie n\'est ni une liste d\'actions ni un objet qui en porte une')
        rep = juger(reponse, golden)
    except Panne as e:
        sys.stderr.write('PANNE %s\n' % e)
        return 2
    print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
