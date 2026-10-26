#!/usr/bin/env python3
"""Juge du formulaire mail : les onze cases du formulaire commun.

Contrat commun des juges de flux (design v2, section 3.3) :

    formulaire.py <corrige> <sortie>   ->  JSON {score, bareme, lignes} sur stdout

Tout ce qui empeche de noter sort en exit 2, avec "PANNE ..." sur stderr.

BAREME FIXE : 22 points, 11 cases x 2. Il ne depend JAMAIS du contenu du
corrige. C'est la regle qui interdit la panne "rend un bareme de N, le graphe
declare 22" (runtime/juge.py) des qu'on change de cas : le corrige dit ce
qu'il faut repondre, jamais combien de points sont en jeu.

Les onze cases, dans l'ordre du corpus
(docs/2026-09-07-corpus-mail-design.md, section 2) :

    expediteur           texte    adresse du dernier expediteur     jamais vide
    date_du_message      date     date du message le plus recent    jamais vide
    nature               enum     6 valeurs, l'ordre = l'arbitrage  jamais vide
    reponse_attendue     booleen  false EST une reponse             jamais vide
    reference            texte    le numero que le mail donne       "" possible
    echeance             date     la date limite la plus proche     "" possible
    rdv_date             date     date du rendez-vous               "" possible
    rdv_heure            heure    HH:MM, 24 h                       "" possible
    montant_du_euros     montant  somme due par le destinataire     0 possible
    documents_a_fournir  liste    pieces a fournir                  [] possible
    lien                 url      adresse a ouvrir pour agir        "" possible

LA REGLE D'ABSENCE - le point dur. L'absence est une valeur notee, et c'est le
CORRIGE qui tranche, case par case :

    corrige plein  + reponse egale    ->  2 points
    corrige plein  + reponse vide     ->  0          (oubli)
    corrige vide   + reponse vide     ->  2 points   (s'abstenir a raison paie)
    corrige vide   + reponse pleine   ->  0, et -1 de malus d'hallucination

Une information absente se dit "" / 0 / [] / false, jamais par une cle
manquante et jamais par null - mais une cle manquante et un null sont lus
comme vides, des deux cotes, plutot que de mettre la passe en panne.
`reponse_attendue` n'est jamais vide : false y est une reponse, pas une
abstention, donc jamais de malus sur cette case.

`documents_a_fournir` est la seule case a note partielle : `manques` =
mots-cles du corrige qu'aucun item ne contient, `surplus` = items qui ne
contiennent aucun mot-cle ; 2 points si manques + surplus = 0, 1 si = 1, 0
au-dela. Le corrige porte le mot-cle minimal ("identite"), la reponse a le
droit d'etre bavarde ("carte d'identite a jour") : c'est l'inclusion qui compte,
pas l'egalite. Le malus d'hallucination s'y applique comme ailleurs (corrige
[] et reponse non vide).

NORMALISATION, par type, toujours appliquee des deux cotes :

    texte    accents, casse, espaces et ponctuation ecrases :
             "810 2604 471 9" = "810-2604-471-9"
    date     2026-10-31 = 31/10/2026 = 2026/10/31 = 31.10.2026
    heure    8:45 = 08:45 = 8h45 = 08:45:00 = "8 h 45"
    enum     comme texte, mais l'underscore compte (demande_de_paiement) ;
             une valeur hors des six est simplement fausse
    booleen  true/vrai/oui/1 et false/faux/non/0, en chaine comme en nombre
    montant  nombre ou chaine : "1 202,75 EUR" = "1.202,75" = 1202.75,
             compare au centime ; un separateur suivi de trois chiffres est
             un separateur de milliers (un montant en euros n'a pas trois
             decimales)
    url      schema, "www." et "/" final ignores, casse ecrasee
    liste    inclusion de mots-cles, voir plus haut

PLANCHER. La derniere ligne rendue donne la note qu'obtiendrait une reponse
entierement vide sur CE corrige : c'est le sol dont la note normalisee a
besoin, (score - plancher) / (22 - plancher). Il se calcule depuis le seul
corrige, sans rien savoir de la reponse.

Ce qui a disparu de la version "mail-01" : le bareme derive du corrige, la
constante DATE_OBSOLETE = '2026-09-22' et ses deux points de piege cables sur
un cas unique, la comparaison de chaines sur les nombres, le corrige vide qui
donnait le point d'office, le surplus de liste impuni, et les actions (elles
ont leur juge, runtime/juges/actions.py, sur leur propre bareme de 14). Les
pieges ne sont plus des cas particuliers du juge : ils sont dans les cas, et
se notent par les cases.
"""
import json
import re
import sys
import unicodedata

TEXTE, DATE, HEURE, ENUM, BOOLEEN, MONTANT, LISTE, URL = (
    'texte', 'date', 'heure', 'enum', 'booleen', 'montant', 'liste', 'url')

CHAMPS = (
    ('expediteur', TEXTE),
    ('date_du_message', DATE),
    ('nature', ENUM),
    ('reponse_attendue', BOOLEEN),
    ('reference', TEXTE),
    ('echeance', DATE),
    ('rdv_date', DATE),
    ('rdv_heure', HEURE),
    ('montant_du_euros', MONTANT),
    ('documents_a_fournir', LISTE),
    ('lien', URL),
)

POINTS_PAR_CASE = 2
BAREME = POINTS_PAR_CASE * len(CHAMPS)          # 22, fixe, jamais calcule sur le corrige
MALUS_HALLUCINATION = 1

NATURES = ('convocation', 'demande_de_paiement', 'demande_de_document',
           'proposition', 'decision', 'information')

# La reponse "vide" de reference : une valeur d'absence par type. Sert au
# plancher, et c'est exactement ce qu'un modele doit rendre quand il n'a rien.
VIDE = {'expediteur': '', 'date_du_message': '', 'nature': '', 'reponse_attendue': False,
        'reference': '', 'echeance': '', 'rdv_date': '', 'rdv_heure': '',
        'montant_du_euros': 0, 'documents_a_fournir': [], 'lien': ''}


class Panne(Exception):
    """Le juge ne peut pas noter. Le message dit pourquoi, en une ligne."""


# --------------------------------------------------------------------------- normalisation

def _plat(v):
    """Sans accents, en minuscules, sans espaces de bord."""
    return unicodedata.normalize('NFD', str(v)).encode('ascii', 'ignore').decode().lower().strip()


def norm_texte(v):
    """Identites, references, mots-cles : tout separateur est du bruit.

    "810 2604 471 9" = "810-2604-471-9" = "81026044719". Un numero de dossier
    ne se distingue pas d'un autre par ses espaces ou ses tirets."""
    return re.sub(r'[^a-z0-9]', '', _plat(v))


def norm_cle(v):
    """Enumerations et verbes : l'underscore fait partie du mot.

    Sans lui, "demande_de_paiement" et "creer_rdv" perdraient leur forme et
    ne se retrouveraient plus dans leur vocabulaire."""
    return re.sub(r'[^a-z0-9_]', '', _plat(v))


def norm_date(v):
    """AAAA-MM-JJ. Accepte aussi JJ/MM/AAAA et les separateurs . / - et espace."""
    s = _plat(v)
    m = re.fullmatch(r'(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})', s)
    if m:
        a, mo, j = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r'(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})', s)
        if not m:
            return norm_texte(v)
        j, mo, a = m.group(1), m.group(2), m.group(3)
    return '%04d-%02d-%02d' % (int(a), int(mo), int(j))


def norm_heure(v):
    """HH:MM sur 24 h. 8:45, 8h45, "8 h 45", 08:45:00 -> 08:45."""
    s = _plat(v)
    m = re.fullmatch(r'(\d{1,2})\s*[:h]\s*(\d{2})(?:\s*[:m]\s*\d{2})?', s)
    if m:
        return '%02d:%s' % (int(m.group(1)), m.group(2))
    m = re.fullmatch(r'(\d{1,2})\s*h', s)
    if m:
        return '%02d:00' % int(m.group(1))
    return norm_texte(v)


def norm_montant(v):
    """Nombre d'euros arrondi au centime, ou None si ce n'en est pas un."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r'[^0-9,.+-]', '', _plat(v))
    signe = -1 if s.startswith('-') else 1
    s = s.lstrip('+-')
    if not re.search(r'\d', s):
        return None
    seps = [i for i, c in enumerate(s) if c in ',.']
    if seps:
        dernier = seps[-1]
        frac = s[dernier + 1:]
        if re.fullmatch(r'\d{3}', frac):        # 1.202 / 155.325 : separateur de milliers
            s = re.sub(r'[,.]', '', s)
        else:
            s = re.sub(r'[,.]', '', s[:dernier]) + '.' + re.sub(r'[^0-9]', '', frac)
    try:
        return round(signe * float(s or '0'), 2)
    except ValueError:
        return None


def norm_booleen(v):
    """True, False, ou None si la valeur n'est pas un booleen lisible."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and float(v) in (0.0, 1.0):
        return bool(v)
    s = norm_texte(v)
    if s in ('true', 'vrai', 'oui', 'yes', 'y', 'o', '1'):
        return True
    if s in ('false', 'faux', 'non', 'no', 'n', '0'):
        return False
    return None


def norm_url(v):
    s = _plat(v)
    s = re.sub(r'^[a-z][a-z0-9+.-]*://', '', s)
    s = re.sub(r'^www\.', '', s)
    return s.rstrip('/')


NORMALISEURS = {TEXTE: norm_texte, ENUM: norm_cle, DATE: norm_date, HEURE: norm_heure,
                MONTANT: norm_montant, BOOLEEN: norm_booleen, URL: norm_url}


def liste_items(v):
    """La case documents, ramenee a une liste de chaines."""
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if str(x).strip() != '']
    return [str(v)] if str(v).strip() != '' else []


def est_vide(v, type_):
    """L'absence, telle que le formulaire commun la dit : "" / 0 / [] / null."""
    if v is None:
        return True
    if type_ == BOOLEEN:
        return False                                    # false est une reponse, pas une absence
    if type_ == LISTE:
        return len(liste_items(v)) == 0
    if type_ == MONTANT:
        if isinstance(v, str) and v.strip() == '':
            return True
        m = norm_montant(v)
        return m == 0
    return str(v).strip() == ''


def egal(type_, attendu, obtenu):
    f = NORMALISEURS[type_]
    a, o = f(attendu), f(obtenu)
    if a is None or o is None:                          # montant ou booleen illisible
        return False
    return a == o


def note_liste(attendu, obtenu):
    """(points, manques, surplus) sur documents_a_fournir."""
    mots = [m for m in (norm_texte(a) for a in liste_items(attendu)) if m]
    items = [norm_texte(o) for o in liste_items(obtenu)]
    manques = sum(1 for m in mots if not any(m in i for i in items))
    surplus = sum(1 for i in items if not any(m in i for m in mots))
    ecarts = manques + surplus
    return (POINTS_PAR_CASE if ecarts == 0 else 1 if ecarts == 1 else 0), manques, surplus


# --------------------------------------------------------------------------- notation

def _court(v, n=30):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace('\n', ' ')
    return s if len(s) <= n else s[:n - 1] + '...'


def _noter(reponse, golden):
    """(score, lignes) sur les onze cases. Plancher 0, plafond 22."""
    score, malus, lignes = 0, 0, []
    for cle, type_ in CHAMPS:
        attendu, obtenu = golden.get(cle), reponse.get(cle)
        vide_a, vide_o = est_vide(attendu, type_), est_vide(obtenu, type_)
        if type_ == LISTE:
            pts, manques, surplus = note_liste(attendu, obtenu)
            motif = 'manques=%d surplus=%d' % (manques, surplus)
        elif vide_a and vide_o:
            pts, motif = POINTS_PAR_CASE, 'vide des deux cotes : abstention juste'
        elif vide_a:
            pts, motif = 0, 'le corrige laisse la case vide : valeur inventee'
        elif vide_o:
            pts, motif = 0, 'case oubliee'
        elif egal(type_, attendu, obtenu):
            pts, motif = POINTS_PAR_CASE, ''
        else:
            pts = 0
            motif = ('hors enumeration' if type_ == ENUM and norm_cle(obtenu) not in NATURES
                     else 'valeur differente')
        if vide_a and not vide_o:
            malus += MALUS_HALLUCINATION
            motif = motif + (' ; ' if motif else '') + '-%d d\'hallucination' % MALUS_HALLUCINATION
        score += pts
        lignes.append('%s %-20s attendu=%-32s obtenu=%-32s -> %d/%d%s'
                      % ('OK ' if pts == POINTS_PAR_CASE else 'NON', cle,
                         _court(attendu, 32), _court(obtenu, 32), pts, POINTS_PAR_CASE,
                         '  (%s)' % motif if motif else ''))
    if malus:
        score -= malus
        lignes.append('NON malus d\'hallucination : -%d (case(s) vide(s) au corrige et remplie(s))' % malus)
    return max(score, 0), lignes


def juger(reponse, golden):
    score, lignes = _noter(reponse, golden)
    plancher, _ = _noter(VIDE, golden)
    lignes.append('--- plancher (reponse entierement vide sur ce corrige) : %s/%d'
                  % (plancher, BAREME))
    return {'score': score, 'bareme': BAREME, 'lignes': lignes}


def plancher(golden):
    """La note d'une reponse entierement vide sur ce corrige."""
    return _noter(VIDE, golden)[0]


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
            raise Panne('usage : formulaire.py <corrige> <sortie>')
        golden = _lire(args[0], 'corrige')
        reponse = _lire(args[1], 'sortie')
        if not isinstance(golden, dict):
            raise Panne('le corrige n\'est pas un objet JSON : %s' % args[0])
        if not isinstance(reponse, dict):
            raise Panne('la sortie n\'est pas un objet JSON : %s' % args[1])
    except Panne as e:
        sys.stderr.write('PANNE %s\n' % e)
        return 2
    print(json.dumps(juger(reponse, golden), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
