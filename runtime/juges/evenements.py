#!/usr/bin/env python3
"""Juge des evenements d'agenda : le livrable du workflow `agenda`.

Contrat commun des juges de flux (design v2, section 3.3) :

    evenements.py <corrige> <sortie>   ->  JSON {score, bareme, lignes} sur stdout

Tout ce qui empeche de noter sort en exit 2, avec "PANNE ..." sur stderr.

BAREME FIXE : 72 points = 4 emplacements x 9 cases x 2. Il ne depend JAMAIS du
contenu du corrige (regle du corpus, docs/2026-09-07-corpus-mail-design.md) :
le corrige dit ce qu'il faut repondre, jamais combien de points sont en jeu.
Aucun cas du corpus ne porte plus de quatre evenements.

UN EMPLACEMENT = un evenement attendu, apparie a un evenement rendu. Les neuf
cases d'un emplacement, 2 points chacune :

    action_type    enum     "creer:rendez_vous", "annuler:echeance"...   jamais vide
    date_debut     date     AAAA-MM-JJ                                   jamais vide
    heure_debut    heure    HH:MM                                        "" possible
    fin            paire    (date_fin, heure_fin)                        ("","") possible
    lieu           texte    inclusion : le corrige porte la phrase       "" possible
                            minimale, la reponse a le droit d'etre longue
    lien           url      egalite normalisee                           "" possible
    reference      texte    egalite normalisee                           "" possible
    montant_euros  montant  au centime                                   0 possible
    documents      liste    mots-cles par inclusion, 2 / 1 / 0           [] possible

Le titre et les notes ne sont pas notes : ce sont des textes libres, et le
corpus ne note que ce que deux lecteurs frais lisent pareil.

LA REGLE D'ABSENCE (la meme que runtime/juges/formulaire.py) :

    corrige plein  + reponse egale    ->  2
    corrige plein  + reponse vide     ->  0          (oubli)
    corrige vide   + reponse vide     ->  2          (s'abstenir a raison paie)
    corrige vide   + reponse pleine   ->  0, et -1 de malus d'hallucination

Elle vaut aussi pour un emplacement entier : un emplacement vide des deux cotes
rend 18 (le cas n'appelait pas d'evenement la, et le modele n'en a pas invente) ;
un evenement rendu sans evenement attendu rend 0 et -1 ; un evenement attendu
sans evenement rendu rend 0. Au-dela de quatre evenements rendus, chaque
evenement en trop coute encore -1. Le score n'est jamais negatif.

APPARIEMENT : chaque evenement rendu est note contre chaque evenement attendu
sur les neuf cases ; les paires sont prises par score decroissant (glouton),
chacun ne servant qu'une fois. C'est ce qui rend l'ordre de la liste rendue
sans importance, et fait qu'un evenement deplace d'un jour perd la case date,
pas l'emplacement entier.

`fin` : une reponse qui repete date_debut en date_fin avec heure_fin vide est
lue comme (\"\",\"\") — c'est la meme journee, dite autrement.

PLANCHER : la derniere ligne donne la note d'une reponse entierement vide
(zero evenement) sur CE corrige = 18 x (4 - nombre d'evenements attendus).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from formulaire import (Panne, norm_texte, norm_cle, norm_date, norm_heure,  # noqa: E402
                        norm_montant, norm_url, liste_items, _court)

EMPLACEMENTS = 4
POINTS = 2
CASES = ('action_type', 'date_debut', 'heure_debut', 'fin', 'lieu', 'lien',
         'reference', 'montant_euros', 'documents')
PAR_EVENEMENT = POINTS * len(CASES)                 # 18
BAREME = EMPLACEMENTS * PAR_EVENEMENT               # 72, fixe
MALUS = 1

ACTIONS = ('creer', 'annuler')
TYPES = ('rendez_vous', 'echeance', 'periode')


# --------------------------------------------------------------------------- lecture

def _lire(chemin, quoi):
    try:
        with open(chemin, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise Panne('%s introuvable : %s' % (quoi, chemin))
    except json.JSONDecodeError as e:
        raise Panne('%s illisible (%s) : %s' % (quoi, e, chemin))


def _evenements(doc, quoi):
    """La liste d'evenements d'un document, ou une panne si la forme est impossible."""
    if isinstance(doc, list):
        evs = doc
    elif isinstance(doc, dict):
        evs = doc.get('evenements', [])
        if evs is None:
            evs = []
    else:
        raise Panne('%s : ni objet ni liste' % quoi)
    if not isinstance(evs, list):
        raise Panne('%s : `evenements` n est pas une liste' % quoi)
    out = []
    for e in evs:
        if not isinstance(e, dict):
            raise Panne('%s : un evenement n est pas un objet' % quoi)
        out.append(e)
    return out


# --------------------------------------------------------------------------- normalisation

def _s(v):
    return '' if v is None else str(v)


def n_action_type(e):
    a, t = norm_cle(_s(e.get('action', ''))), norm_cle(_s(e.get('type', '')))
    return '%s:%s' % (a, t) if a or t else ''


def n_fin(e):
    df, hf = norm_date(_s(e.get('date_fin', ''))), norm_heure(_s(e.get('heure_fin', '')))
    if _s(e.get('date_fin', '')).strip() == '':
        df = ''
    if _s(e.get('heure_fin', '')).strip() == '':
        hf = ''
    if df and df == norm_date(_s(e.get('date_debut', ''))) and not hf:
        df = ''                                     # meme journee, dite autrement
    return (df, hf)


def _vide_texte(v):
    return _s(v).strip() == ''


# --------------------------------------------------------------------------- notation d'une case

def note_simple(attendu_vide, obtenu_vide, egal):
    """La regle d'absence, pour une case a 0 ou 2. Rend (points, malus, verdict)."""
    if attendu_vide and obtenu_vide:
        return POINTS, 0, 'OK'
    if attendu_vide and not obtenu_vide:
        return 0, MALUS, 'INVENTE'
    if not attendu_vide and obtenu_vide:
        return 0, 0, 'OUBLI'
    return (POINTS, 0, 'OK') if egal else (0, 0, 'FAUX')


def noter_case(nom, g, r):
    """Rend (points, malus, verdict, attendu, obtenu) pour une case."""
    if nom == 'action_type':
        a, o = n_action_type(g), n_action_type(r)
        p, m, v = note_simple(False, o == '', a == o)
        return p, m, v, a, o
    if nom == 'date_debut':
        a, o = norm_date(_s(g.get('date_debut', ''))), norm_date(_s(r.get('date_debut', '')))
        p, m, v = note_simple(False, _vide_texte(r.get('date_debut', '')), a == o)
        return p, m, v, a, o
    if nom == 'heure_debut':
        av, ov = _vide_texte(g.get('heure_debut', '')), _vide_texte(r.get('heure_debut', ''))
        a, o = norm_heure(_s(g.get('heure_debut', ''))), norm_heure(_s(r.get('heure_debut', '')))
        p, m, v = note_simple(av, ov, a == o)
        return p, m, v, a, o
    if nom == 'fin':
        a, o = n_fin(g), n_fin(r)
        p, m, v = note_simple(a == ('', ''), o == ('', ''), a == o)
        return p, m, v, '%s %s' % a, '%s %s' % o
    if nom == 'lieu':
        av, ov = _vide_texte(g.get('lieu', '')), _vide_texte(r.get('lieu', ''))
        a, o = norm_texte(_s(g.get('lieu', ''))), norm_texte(_s(r.get('lieu', '')))
        p, m, v = note_simple(av, ov, a != '' and a in o)
        return p, m, v, _s(g.get('lieu', '')), _court(_s(r.get('lieu', '')), 40)
    if nom == 'lien':
        av, ov = _vide_texte(g.get('lien', '')), _vide_texte(r.get('lien', ''))
        a, o = norm_url(_s(g.get('lien', ''))), norm_url(_s(r.get('lien', '')))
        p, m, v = note_simple(av, ov, a == o)
        return p, m, v, a, o
    if nom == 'reference':
        av, ov = _vide_texte(g.get('reference', '')), _vide_texte(r.get('reference', ''))
        a, o = norm_texte(_s(g.get('reference', ''))), norm_texte(_s(r.get('reference', '')))
        p, m, v = note_simple(av, ov, a == o)
        return p, m, v, a, o
    if nom == 'montant_euros':
        a, o = norm_montant(g.get('montant_euros', 0)), norm_montant(r.get('montant_euros', 0))
        if a is None:
            raise Panne('corrige : montant_euros illisible (%r)' % (g.get('montant_euros'),))
        av = (a == 0)
        ov = (o == 0) or (o is None)
        p, m, v = note_simple(av, ov, o is not None and a == o)
        return p, m, v, a, o
    if nom == 'documents':
        ga, ro = liste_items(g.get('documents', [])), liste_items(r.get('documents', []))
        if not ga and not ro:
            return POINTS, 0, 'OK', [], []
        if not ga and ro:
            return 0, MALUS, 'INVENTE', [], ro
        if ga and not ro:
            return 0, 0, 'OUBLI', ga, []
        ka = [norm_texte(x) for x in ga]
        ko = [norm_texte(x) for x in ro]
        manques = [x for x in ka if not any(x in y for y in ko)]
        surplus = [y for y in ko if not any(x in y for x in ka)]
        ecarts = len(manques) + len(surplus)
        p = POINTS if ecarts == 0 else (1 if ecarts == 1 else 0)
        return p, 0, ('OK' if p == POINTS else 'PARTIEL' if p else 'FAUX'), ga, ro
    raise Panne('case inconnue %s' % nom)


def noter_paire(g, r):
    """Toutes les cases d'un evenement attendu contre un evenement rendu."""
    total, malus, lignes = 0, 0, []
    for nom in CASES:
        p, m, v, a, o = noter_case(nom, g, r)
        total += p
        malus += m
        lignes.append({'case': nom, 'points': p, 'sur': POINTS, 'malus': m, 'verdict': v,
                       'attendu': a, 'obtenu': o})
    return total, malus, lignes


# --------------------------------------------------------------------------- appariement

def apparier(attendus, rendus):
    """Glouton par score decroissant. Rend la liste des (i_attendu, j_rendu, total, malus, lignes)."""
    grille = []
    for i, g in enumerate(attendus):
        for j, r in enumerate(rendus):
            t, m, lignes = noter_paire(g, r)
            grille.append((t - m, i, j, t, m, lignes))
    grille.sort(key=lambda x: (-x[0], x[1], x[2]))
    pris_i, pris_j, paires = set(), set(), []
    for _, i, j, t, m, lignes in grille:
        if i in pris_i or j in pris_j:
            continue
        pris_i.add(i)
        pris_j.add(j)
        paires.append((i, j, t, m, lignes))
    paires.sort()
    return paires


def _etiquette(e):
    return '%s %s %s %s' % (_s(e.get('action', '?')), _s(e.get('type', '?')),
                           _s(e.get('date_debut', '?')), _s(e.get('titre', '')))


def _ligne_cases(detail):
    """Les cases fautives d'une paire, en une chaine : `fin FAUX (' 16:00' -> ' ')`."""
    fautes = []
    for c in detail:
        if c['verdict'] == 'OK':
            continue
        fautes.append('%s %s (%r -> %r)%s' % (c['case'], c['verdict'], c['attendu'], c['obtenu'],
                                              ' -1' if c['malus'] else ''))
    return ' ; '.join(fautes) if fautes else 'toutes cases OK'


def juger(reponse, golden):
    """Rend {score, bareme, lignes} — `lignes` est une liste de chaines (contrat de runtime/juge.py)."""
    attendus = _evenements(golden, 'corrige')
    rendus = _evenements(reponse, 'sortie')
    if len(attendus) > EMPLACEMENTS:
        raise Panne('corrige : %d evenements, le bareme n en tient que %d' % (len(attendus), EMPLACEMENTS))
    score, malus, lignes = 0, 0, []
    paires = apparier(attendus, rendus)
    for i, j, t, m, detail in paires:
        score += t
        malus += m
        lignes.append('[E%d] %d/%d%s | attendu : %s | obtenu : %s | %s' % (
            i + 1, t, PAR_EVENEMENT, ' -%d' % m if m else '', _etiquette(attendus[i]),
            _etiquette(rendus[j]), _ligne_cases(detail)))
    for i, g in enumerate(attendus):
        if i not in {p[0] for p in paires}:
            lignes.append('[E%d] 0/%d | attendu : %s | obtenu : rien | OUBLI' % (i + 1, PAR_EVENEMENT, _etiquette(g)))
    en_trop = [r for j, r in enumerate(rendus) if j not in {p[1] for p in paires}]
    vides = EMPLACEMENTS - len(attendus)
    for k, r in enumerate(en_trop):
        if k < vides:
            lignes.append('[E%d] 0/%d -1 | attendu : rien | obtenu : %s | INVENTE' % (
                len(attendus) + k + 1, PAR_EVENEMENT, _etiquette(r)))
        else:
            lignes.append('[E+] -1 | attendu : rien | obtenu : %s | EN-TROP' % _etiquette(r))
        malus += MALUS
    for k in range(max(0, vides - len(en_trop))):
        score += PAR_EVENEMENT
        lignes.append('[E%d] %d/%d | attendu : rien | obtenu : rien | OK' % (
            len(attendus) + len(en_trop) + k + 1, PAR_EVENEMENT, PAR_EVENEMENT))
    final = max(0, score - malus)
    lignes.append('plancher %d ; brut %d ; malus %d ; bareme %d' % (plancher(golden), score, malus, BAREME))
    return {'score': final, 'bareme': BAREME, 'lignes': lignes}


def plancher(golden):
    """Note d'une reponse sans aucun evenement sur ce corrige."""
    n = len(_evenements(golden, 'corrige'))
    return PAR_EVENEMENT * max(0, EMPLACEMENTS - n)


# --------------------------------------------------------------------------- cli

def main(argv):
    args = [a for a in argv[1:] if not a.startswith('--')]      # --bareme=N vient du runtime
    if len(args) != 2:
        sys.stderr.write('usage: evenements.py <corrige> <sortie>\n')
        return 2
    try:
        golden = _lire(args[0], 'corrige')
        reponse = _lire(args[1], 'sortie')
        res = juger(reponse, golden)
    except Panne as e:
        sys.stderr.write('PANNE %s\n' % e)
        print(json.dumps({'panne': str(e), 'score': None, 'bareme': BAREME, 'lignes': []}))
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
