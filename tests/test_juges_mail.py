"""Les deux juges du corpus mail : bareme fixe, regle d'absence, normalisation.

    python3 -m unittest discover -s tests -v      (pytest absent sur la cible)

Ce que ces tests tiennent, et pourquoi :

  - le bareme est FIXE (22 et 14) sur les 31 corriges du depot. C'est la panne
    du 2026-09-07 : les juges derivaient leur bareme du corrige (13 cases pour
    l'un, 2 x le nombre d'actions pour l'autre), donc runtime/juge.py mettait
    30 cas sur 30 en panne des que le graphe declarait 20 et 10 ;
  - chaque corrige, juge contre lui-meme, vaut le bareme entier : sans cela le
    plafond n'est pas atteignable et la mesure ne veut rien dire ;
  - la regle d'absence : s'abstenir a raison paie autant que remplir a raison,
    et inventer coute. C'est la seule facon de noter mecaniquement un corpus ou
    la moitie des mails n'ont ni rendez-vous ni montant ;
  - les deux sols du corpus (reponse vide, reponse constante) sont figes en
    dur. Ils bougent des qu'un corrige bouge - c'est voulu : la note normalisee
    de resultats.md se calcule sur eux, et une campagne ne se compare pas a une
    autre si le sol a change. Pour les recalculer :

        cd <depot flux> && python3 - <<'PY'
        import json, sys, glob
        sys.path.insert(0, 'runtime/juges')
        import formulaire as F, actions as A
        cas = [c for c in sorted(glob.glob('cas/mail-*/')) if 'mail-01' not in c]
        for nom, rep in [('vide', F.VIDE), ('constante', dict(F.VIDE, nature='convocation',
                                                              reponse_attendue=True))]:
            t = [F.juger(rep, json.load(open(c + 'golden-formulaire.json')))['score']
                 + A.juger([], json.load(open(c + 'golden-actions.json')))['score'] for c in cas]
            print(nom, sum(t), sum(t) / len(t))
        PY
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / 'runtime'))
sys.path.insert(0, str(RACINE / 'runtime' / 'juges'))
import juge                     # noqa: E402  (runtime/juge.py, le lanceur)
import actions as jactions      # noqa: E402  (runtime/juges/actions.py)
import formulaire as jform      # noqa: E402  (runtime/juges/formulaire.py)

FORMULAIRE = 'runtime/juges/formulaire.py'
ACTIONS = 'runtime/juges/actions.py'
# Les cas vivent dans DEUX racines depuis la decision D-b : le lot de boucle dans le
# depot, le lot de mesure hors depot (/var/lib/flux-mesure/cas/). Le juge est le meme
# pour les deux ; seul git ignore le second. Meme convention que tools/sols.py.
RACINES_CAS = [RACINE / 'cas', pathlib.Path('/var/lib/flux-mesure/cas')]


def _cas_de(racine):
    """Les cas d une racine — et rien du tout si elle nous est FERMEE.

    Depuis le verrou du 2026-09-07 le lot de mesure appartient a l utilisateur
    flux-mesure, en 0700 : l utilisateur de la boucle (celui du moteur) ne peut
    ni le lister ni meme le stat. Une racine interdite n est pas une erreur a
    faire remonter, c est le mur qui fait son travail — le corpus se reduit et
    les totaux se declarent non verifiables (SANS_MESURE). Joue la suite en
    root ou sous flux-mesure pour retrouver les 31 cas.
    """
    try:
        return [p for p in racine.glob('mail-*') if p.is_dir()] if racine.is_dir() else []
    except PermissionError:
        return []


CAS = sorted((p for r in RACINES_CAS for p in _cas_de(r)), key=lambda p: p.name)
CAS30 = [p for p in CAS if p.name != 'mail-01']
CORPUS_ENTIER = len(CAS) == 31   # faux sur un clone qui n'a pas le lot de mesure
SANS_MESURE = 'lot de mesure absent (%d cas sur 31) : totaux de corpus non verifiables' % len(CAS)

# Les trois cas cites en exemple, une difficulte chacun (doc section 4).
FACILE, MOYEN, TRES_DIFFICILE = 'mail-02', 'mail-13', 'mail-27'

# Sols mesures sur les 30 cas le 2026-09-07 (total des scores, pas la moyenne).
SOL_VIDE = 233          # 7,767 / 36 en moyenne
SOL_CONSTANTE = 313     # 10,433 / 36 en moyenne
CONSTANTE = dict(jform.VIDE, nature='convocation', reponse_attendue=True)


def lire(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def ecrire(dossier, nom, contenu):
    p = pathlib.Path(dossier) / nom
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(contenu, f, ensure_ascii=False)
    return p


def cli(script, *args):
    r = subprocess.run([sys.executable, str(RACINE / script)] + list(args),
                       capture_output=True, text=True, cwd=str(RACINE))
    return r.returncode, r.stdout, r.stderr


def dossier_cas(nom):
    """Le cas <nom>, cherche dans les deux racines (depot puis lot de mesure)."""
    for r in RACINES_CAS:
        if (r / nom).is_dir():
            return r / nom
    raise unittest.SkipTest('cas %s introuvable dans %s' % (nom, RACINES_CAS))


def golden_f(nom):
    return lire(dossier_cas(nom) / 'golden-formulaire.json')


def golden_a(nom):
    return lire(dossier_cas(nom) / 'golden-actions.json')


class TestBaremeFixe(unittest.TestCase):
    """Le bareme ne depend pas du contenu du corrige. C'est P0-1."""

    def test_formulaire_rend_22_sur_les_31_corriges(self):
        rendus = {}
        for d in CAS:
            rep = jform.juger(jform.VIDE, lire(d / 'golden-formulaire.json'))
            rendus[d.name] = rep['bareme']
        self.assertEqual(set(rendus.values()), {22}, rendus)
        if not CORPUS_ENTIER:
            self.skipTest(SANS_MESURE)
        self.assertEqual(len(rendus), 31)

    def test_actions_rend_14_sur_les_31_corriges(self):
        rendus = {}
        for d in CAS:
            rendus[d.name] = jactions.juger([], lire(d / 'golden-actions.json'))['bareme']
        self.assertEqual(set(rendus.values()), {14}, rendus)
        if not CORPUS_ENTIER:
            self.skipTest(SANS_MESURE)
        self.assertEqual(len(rendus), 31)

    def test_les_31_corriges_valent_le_bareme_entier_contre_eux_memes(self):
        """22/22 et 14/14, par le lanceur, avec le bareme declare - le chemin reel."""
        faux = []
        for d in CAS:
            f = juge.juger(FORMULAIRE, 'golden-formulaire.json', ['golden-formulaire.json'],
                           bareme=22, cas=d, workspace=d)
            a = juge.juger(ACTIONS, 'golden-actions.json', ['golden-actions.json'],
                           bareme=14, cas=d, workspace=d)
            if (f['score'], a['score']) != (22, 14):
                faux.append((d.name, f['score'], a['score']))
        self.assertEqual(faux, [], 'corriges qui ne valent pas 22/22 + 14/14 : %s' % faux)

    def test_un_bareme_declare_faux_est_une_panne_pas_une_note(self):
        d = dossier_cas(FACILE)
        with self.assertRaises(juge.JugeEnPanne) as cm:
            juge.juger(FORMULAIRE, 'golden-formulaire.json', ['golden-formulaire.json'],
                       bareme=20, cas=d, workspace=d)
        self.assertIn('bareme de 22, le graphe declare 20', str(cm.exception))


class TestCorrigeParfait(unittest.TestCase):
    """Le plafond : trois cas de difficulte differente, corrige contre corrige."""

    def test_36_sur_36_sur_trois_difficultes(self):
        for nom in (FACILE, MOYEN, TRES_DIFFICILE):
            with self.subTest(cas=nom):
                f = jform.juger(golden_f(nom), golden_f(nom))
                a = jactions.juger(golden_a(nom), golden_a(nom))
                self.assertEqual((f['score'], f['bareme']), (22, 22), f['lignes'])
                self.assertEqual((a['score'], a['bareme']), (14, 14), a['lignes'])
                self.assertEqual(f['score'] + a['score'], 36)

    def test_le_formulaire_complet_note_aussi_ses_actions(self):
        """actions.py accepte un resultat.json qui porte `actions` (workflow en 1 noeud)."""
        nom = MOYEN
        complet = dict(golden_f(nom), actions=golden_a(nom))
        self.assertEqual(jactions.juger(complet, golden_a(nom))['score'], 14)


class TestSolsDuCorpus(unittest.TestCase):
    """Les deux sols, et le plancher que le juge publie cas par cas."""

    def test_reponse_vide_vaut_le_plancher_publie(self):
        for d in CAS30:
            with self.subTest(cas=d.name):
                gf, ga = lire(d / 'golden-formulaire.json'), lire(d / 'golden-actions.json')
                f, a = jform.juger(jform.VIDE, gf), jactions.juger([], ga)
                self.assertEqual(f['score'], jform.plancher(gf))
                self.assertEqual(a['score'], jactions.plancher(jactions.corrige(ga)))
                self.assertIn('plancher', f['lignes'][-1])
                self.assertIn('%s/22' % f['score'], f['lignes'][-1])
                self.assertIn('%s/14' % a['score'], a['lignes'][-1])

    def test_sol_reponse_vide_sur_les_30_cas(self):
        t = [jform.juger(jform.VIDE, lire(d / 'golden-formulaire.json'))['score']
             + jactions.juger([], lire(d / 'golden-actions.json'))['score'] for d in CAS30]
        if not CORPUS_ENTIER:
            self.skipTest(SANS_MESURE)
        self.assertEqual(len(t), 30)
        self.assertEqual(sum(t), SOL_VIDE, 'sol vide = %.3f/36' % (sum(t) / 30.0))

    def test_sol_reponse_constante_sur_les_30_cas(self):
        """La baseline de D-e : nature=convocation, reponse_attendue=true, rien d'autre."""
        t = [jform.juger(CONSTANTE, lire(d / 'golden-formulaire.json'))['score']
             + jactions.juger([], lire(d / 'golden-actions.json'))['score'] for d in CAS30]
        if not CORPUS_ENTIER:
            self.skipTest(SANS_MESURE)
        self.assertEqual(sum(t), SOL_CONSTANTE, 'sol constante = %.3f/36' % (sum(t) / 30.0))

    def test_la_constante_bat_le_vide_de_trois_points(self):
        """Devine sans lire : c'est ce que la note normalisee doit cesser de crediter."""
        self.assertGreater(SOL_CONSTANTE, SOL_VIDE)
        self.assertAlmostEqual((SOL_CONSTANTE - SOL_VIDE) / 30.0, 2.667, places=3)

    def test_aucun_cas_au_plafond_ni_au_ras_du_sol(self):
        """Regle de lot : plafond utile >= 12/36 sur les 30 cas."""
        utiles = {}
        for d in CAS30:
            p = (jform.plancher(lire(d / 'golden-formulaire.json'))
                 + jactions.plancher(jactions.corrige(lire(d / 'golden-actions.json'))))
            utiles[d.name] = 36 - p
        self.assertGreaterEqual(min(utiles.values()), 12, utiles)
        self.assertEqual(min(utiles.values()), 18)     # mail-17
        if CORPUS_ENTIER:
            self.assertEqual(max(utiles.values()), 34)


class TestRegleAbsence(unittest.TestCase):
    """Les quatre lignes du tableau : c'est le corrige qui tranche, jamais le juge."""

    GOLDEN = {'expediteur': 'a@b.fr', 'date_du_message': '2026-10-05', 'nature': 'convocation',
              'reponse_attendue': True, 'reference': '', 'echeance': '2026-10-31',
              'rdv_date': '', 'rdv_heure': '', 'montant_du_euros': 0,
              'documents_a_fournir': [], 'lien': ''}

    def note(self, **modifs):
        return jform.juger(dict(self.GOLDEN, **modifs), self.GOLDEN)['score']

    def test_corrige_plein_reponse_egale_vaut_2(self):
        self.assertEqual(self.note(), 22)

    def test_corrige_plein_reponse_vide_vaut_0(self):
        self.assertEqual(self.note(echeance=''), 20)

    def test_corrige_vide_reponse_vide_vaut_2(self):
        """Les cinq cases vides du corrige rapportent deja dans le 22 ci-dessus."""
        rep = dict(self.GOLDEN, reference='', rdv_date='', rdv_heure='',
                   montant_du_euros=0, documents_a_fournir=[], lien='')
        self.assertEqual(jform.juger(rep, self.GOLDEN)['score'], 22)

    def test_corrige_vide_reponse_pleine_vaut_0_et_moins_1(self):
        self.assertEqual(self.note(rdv_date='2026-10-20'), 22 - 2 - 1)
        lignes = jform.juger(dict(self.GOLDEN, rdv_date='2026-10-20'), self.GOLDEN)['lignes']
        self.assertTrue(any('hallucination' in l for l in lignes), lignes)

    def test_inventer_sur_toutes_les_cases_vides(self):
        rep = dict(self.GOLDEN, reference='X-1', rdv_date='2026-10-20', rdv_heure='10:00',
                   montant_du_euros=42, documents_a_fournir=['identite'], lien='https://x.fr')
        # 5 cases scalaires a 0 (-10) + documents a 1 (-1) + 6 malus = 22 - 17
        self.assertEqual(jform.juger(rep, self.GOLDEN)['score'], 5)

    def test_cle_absente_et_null_valent_vide_des_deux_cotes(self):
        golden = {k: v for k, v in self.GOLDEN.items() if k != 'rdv_date'}
        golden['rdv_heure'] = None
        self.assertEqual(jform.juger({k: v for k, v in self.GOLDEN.items()
                                      if k not in ('rdv_date', 'rdv_heure')}, golden)['score'], 22)

    def test_false_est_une_reponse_pas_une_absence(self):
        golden = dict(self.GOLDEN, reponse_attendue=False)
        self.assertEqual(jform.juger(dict(golden), golden)['score'], 22)
        # false attendu, true rendu : faux, mais pas une hallucination
        rep = dict(golden, reponse_attendue=True)
        note = jform.juger(rep, golden)
        self.assertEqual(note['score'], 20)
        self.assertFalse(any('hallucination' in l for l in note['lignes']))

    def test_un_cas_reel_ou_la_reponse_invente_les_cases_vides(self):
        """mail-17 : ni rendez-vous ni document au corrige."""
        g = golden_f('mail-17')
        rep = dict(g, rdv_date='2026-10-15', rdv_heure='10:00', documents_a_fournir=['identite'])
        # rdv_date et rdv_heure : -2 chacune ; documents : 2 -> 1 ; malus 3
        self.assertEqual(jform.juger(rep, g)['score'], 22 - 2 - 2 - 1 - 3)


class TestNormalisation(unittest.TestCase):
    """Meme fait, autre ecriture : le juge ne doit pas compter une faute."""

    G = TestRegleAbsence.GOLDEN

    def egal(self, cle, valeur, golden_valeur):
        g = dict(self.G, **{cle: golden_valeur})
        return jform.juger(dict(g, **{cle: valeur}), g)['score'] == 22

    def test_heures(self):
        for v in ('08:45', '8:45', '8h45', '8 h 45', '08:45:00'):
            self.assertTrue(self.egal('rdv_heure', v, '08:45'), v)
        self.assertFalse(self.egal('rdv_heure', '08:54', '08:45'))

    def test_dates(self):
        for v in ('2026-10-31', '31/10/2026', '2026/10/31', '31.10.2026'):
            self.assertTrue(self.egal('echeance', v, '2026-10-31'), v)
        self.assertFalse(self.egal('echeance', '2026-11-01', '2026-10-31'))

    def test_montants(self):
        for v in (1202.75, '1202.75', '1202,75', '1 202,75 EUR', '1.202,75', '1,202.75'):
            self.assertTrue(self.egal('montant_du_euros', v, 1202.75), v)
        self.assertTrue(self.egal('montant_du_euros', '1.202', 1202))
        self.assertTrue(self.egal('montant_du_euros', 12, 12.0))
        self.assertFalse(self.egal('montant_du_euros', 1202.76, 1202.75))
        self.assertFalse(self.egal('montant_du_euros', 'douze', 12))

    def test_booleens(self):
        for v in (True, 'true', 'vrai', 'oui', 1):
            self.assertTrue(self.egal('reponse_attendue', v, True), v)
        for v in (False, 'false', 'faux', 'non', 0):
            self.assertTrue(self.egal('reponse_attendue', v, False), v)
        self.assertFalse(self.egal('reponse_attendue', 'peut-etre', True))

    def test_urls(self):
        for v in ('https://espace.example.fr/pieces', 'HTTPS://Espace.Example.fr/pieces',
                  'https://www.espace.example.fr/pieces', 'https://espace.example.fr/pieces/'):
            self.assertTrue(self.egal('lien', v, 'https://espace.example.fr/pieces'), v)
        self.assertFalse(self.egal('lien', 'https://espace.example.fr/autre',
                                   'https://espace.example.fr/pieces'))

    def test_textes_accents_casse_ponctuation(self):
        self.assertTrue(self.egal('expediteur', 'Declarations@Cabinet-Vergnes.example.FR',
                                  'declarations@cabinet-vergnes.example.fr'))
        self.assertTrue(self.egal('reference', '810 2604 471 9', '810-2604-471-9'))

    def test_enum_stricte(self):
        g = dict(self.G, nature='demande_de_paiement')
        note = jform.juger(dict(g, nature='facture'), g)
        self.assertEqual(note['score'], 20)
        self.assertTrue(any('hors enumeration' in l for l in note['lignes']))


class TestDocuments(unittest.TestCase):
    """La seule case a note partielle : inclusion de mots-cles, 2 / 1 / 0."""

    G = dict(TestRegleAbsence.GOLDEN, documents_a_fournir=['identite', 'domicile', 'photo'])

    def note(self, items):
        return jform.juger(dict(self.G, documents_a_fournir=items), self.G)['score'] - 20

    def test_mot_cle_inclus_dans_un_item_bavard(self):
        self.assertEqual(self.note(["carte d'identite a jour", 'justificatif de domicile',
                                    'une photo recente']), 2)

    def test_accents_et_casse(self):
        g = dict(self.G, documents_a_fournir=['controle technique', 'resume'])
        rep = dict(g, documents_a_fournir=['Contrôle technique de moins de 6 mois', 'Résumé'])
        self.assertEqual(jform.juger(rep, g)['score'], 22)

    def test_un_manque_vaut_1(self):
        self.assertEqual(self.note(['identite', 'domicile']), 1)

    def test_un_surplus_vaut_1(self):
        self.assertEqual(self.note(['identite', 'domicile', 'photo', 'passeport']), 1)

    def test_un_manque_et_un_surplus_valent_0(self):
        self.assertEqual(self.note(['identite', 'domicile', 'passeport']), 0)

    def test_liste_vide_contre_trois_mots_cles_vaut_0(self):
        self.assertEqual(self.note([]), 0)

    def test_items_fusionnes_acceptes(self):
        self.assertEqual(self.note(["carte d'identite, justificatif de domicile et photo"]), 2)


class TestActions(unittest.TestCase):
    """Sept verbes, 2 points chacun, presence ET date."""

    ATT = [{'verbe': 'repondre', 'date': '2026-10-02'},
           {'verbe': 'creer_rdv', 'date': '2026-10-06'},
           {'verbe': 'rappel', 'date': '2026-10-05'}]

    def note(self, rep):
        return jactions.juger(rep, self.ATT)['score']

    def test_corrige_contre_lui_meme(self):
        self.assertEqual(self.note(self.ATT), 14)

    def test_liste_vide_vaut_le_plancher(self):
        self.assertEqual(self.note([]), 2 * (7 - 3))

    def test_verbe_present_date_fausse_vaut_1(self):
        rep = [dict(self.ATT[0], date='2026-01-01')] + self.ATT[1:]
        self.assertEqual(self.note(rep), 13)

    def test_verbe_oublie_vaut_0(self):
        self.assertEqual(self.note(self.ATT[1:]), 12)

    def test_verbe_du_vocabulaire_invente_vaut_0(self):
        self.assertEqual(self.note(self.ATT + [{'verbe': 'payer', 'date': '2026-10-02'}]), 12)

    def test_verbe_hors_vocabulaire_coute_1(self):
        self.assertEqual(self.note(self.ATT + [{'verbe': 'confirmer', 'date': '2026-10-02'}]), 13)
        lignes = jactions.juger(self.ATT + [{'verbe': 'confirmer', 'date': '2026-10-02'}],
                                self.ATT)['lignes']
        self.assertTrue(any('hors vocabulaire' in l and 'confirmer' in l for l in lignes), lignes)

    def test_le_meme_verbe_deux_fois_avec_deux_dates_ne_vaut_pas_2(self):
        rep = [self.ATT[0], dict(self.ATT[0], date='2026-01-01')] + self.ATT[1:]
        self.assertEqual(self.note(rep), 13)

    def test_plancher_0_quand_tout_est_faux(self):
        rep = [{'verbe': 'classer', 'date': '2026-01-01'}, {'verbe': 'payer', 'date': '2026-01-01'},
               {'verbe': 'remplir', 'date': '2026-01-01'}, {'verbe': 'dormir', 'date': '2026-01-01'},
               {'verbe': 'appeler', 'date': '2026-01-01'}]
        self.assertEqual(self.note(rep), 0)

    def test_dates_normalisees_comme_ailleurs(self):
        self.assertEqual(self.note([{'verbe': 'repondre', 'date': '02/10/2026'},
                                    self.ATT[1], self.ATT[2]]), 14)

    def test_corrige_hors_vocabulaire_est_une_panne(self):
        with self.assertRaises(jactions.Panne) as cm:
            jactions.juger([], [{'verbe': 'confirmer', 'date': '2026-10-02'}])
        self.assertIn('hors vocabulaire', str(cm.exception))

    def test_corrige_avec_un_verbe_deux_fois_est_une_panne(self):
        with self.assertRaises(jactions.Panne) as cm:
            jactions.juger([], [{'verbe': 'payer', 'date': '2026-10-02'},
                                {'verbe': 'payer', 'date': '2026-10-03'}])
        self.assertIn('deux fois', str(cm.exception))


class TestContrat(unittest.TestCase):
    """`script <corrige> <sortie>` -> {score, bareme, lignes} ; sinon exit 2 et PANNE."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_sortie_json_du_juge_formulaire(self):
        g = ecrire(self.d, 'g.json', golden_f('mail-17'))
        rc, out, err = cli(FORMULAIRE, str(g), str(g), '--bareme=22')
        self.assertEqual(rc, 0, err)
        d = json.loads(out)
        self.assertEqual(set(d), {'score', 'bareme', 'lignes'})
        self.assertEqual((d['score'], d['bareme']), (22, 22))
        self.assertTrue(all(isinstance(l, str) for l in d['lignes']))
        self.assertEqual(len(d['lignes']), 12)          # 11 cases + le plancher

    def test_sortie_json_du_juge_actions(self):
        g = ecrire(self.d, 'a.json', golden_a('mail-17'))
        rc, out, err = cli(ACTIONS, str(g), str(g), '--bareme=14')
        self.assertEqual(rc, 0, err)
        d = json.loads(out)
        self.assertEqual((d['score'], d['bareme']), (14, 14))
        self.assertEqual(len(d['lignes']), 8)           # 7 verbes + le plancher

    def test_mauvais_usage_est_une_panne(self):
        for script in (FORMULAIRE, ACTIONS):
            rc, out, err = cli(script, 'un-seul-argument.json')
            self.assertEqual(rc, 2, out)
            self.assertTrue(err.startswith('PANNE'), err)

    def test_json_illisible_est_une_panne(self):
        p = pathlib.Path(self.d) / 'casse.json'
        p.write_text('{ pas du json', encoding='utf-8')
        rc, out, err = cli(FORMULAIRE, str(p), str(p))
        self.assertEqual(rc, 2)
        self.assertIn('PANNE', err)
        self.assertIn('illisible', err)

    def test_sortie_qui_n_est_pas_un_objet_est_une_panne(self):
        g = ecrire(self.d, 'g.json', golden_f('mail-17'))
        liste = ecrire(self.d, 'liste.json', [1, 2, 3])
        rc, out, err = cli(FORMULAIRE, str(g), str(liste))
        self.assertEqual(rc, 2)
        self.assertIn('PANNE', err)

    def test_le_lanceur_relaie_la_panne_du_corrige(self):
        d = dossier_cas('mail-01')
        with tempfile.TemporaryDirectory() as ws:
            ecrire(ws, 'actions.json', [])
            ecrire(ws, 'faux-corrige.json', [{'verbe': 'confirmer', 'date': '2026-09-12'}])
            with self.assertRaises(juge.JugeEnPanne) as cm:
                juge.juger(ACTIONS, os.path.join(ws, 'faux-corrige.json'), ['actions.json'],
                           bareme=14, cas=d, workspace=ws)
        self.assertIn('PANNE', str(cm.exception))
        self.assertIn('hors vocabulaire', str(cm.exception))


class TestCoherenceGraphes(unittest.TestCase):
    """Les graphes doivent declarer le bareme que les juges rendent - sinon panne
    a chaque passe (P0-1). Les graphes ne sont pas dans le perimetre des juges,
    ce test est la pour que la desynchronisation se voie ici et pas en campagne."""

    def declarations(self, graphe):
        wf = lire(RACINE / 'workflows' / graphe)
        out = []
        for n in wf['nodes']:
            for j in ([n['juge']] if 'juge' in n else []) + list(n.get('juges', [])):
                if j.get('script', '').endswith('formulaire.py'):
                    out.append((n['id'], 'formulaire', j['bareme']))
                elif j.get('script', '').endswith('actions.py'):
                    out.append((n['id'], 'actions', j['bareme']))
        return out

    def test_mail_et_mail_1noeud_declarent_22_et_14(self):
        attendu = {'formulaire': jform.BAREME, 'actions': jactions.BAREME}
        for graphe in ('mail.json', 'mail-1noeud.json'):
            decl = self.declarations(graphe)
            self.assertTrue(decl, graphe)
            for noeud, quel, bareme in decl:
                with self.subTest(graphe=graphe, noeud=noeud):
                    self.assertEqual(bareme, attendu[quel],
                                     '%s / %s declare %s, le juge rend %s'
                                     % (graphe, noeud, bareme, attendu[quel]))


if __name__ == '__main__':
    unittest.main()
