"""Le juge des evenements d'agenda : bareme fixe, regle d'absence, appariement.

    python3 -m unittest tests.test_juge_evenements
"""
import copy
import glob
import json
import os
import pathlib
import sys
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / 'runtime' / 'juges'))
import evenements as J  # noqa: E402


def _cas_boucle():
    return sorted(glob.glob(str(RACINE / 'cas' / 'mail-*' / 'golden-evenements.json')))


def _lire(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


class Bareme(unittest.TestCase):
    def test_bareme_fixe(self):
        self.assertEqual(J.BAREME, 72)
        self.assertEqual(J.PAR_EVENEMENT, 18)

    def test_corrige_contre_lui_meme(self):
        for p in _cas_boucle():
            g = _lire(p)
            self.assertEqual(J.juger(g, g)['score'], 72, p)

    def test_reponse_vide_rend_le_plancher(self):
        for p in _cas_boucle():
            g = _lire(p)
            vide = {'date_du_message': '', 'evenements': []}
            self.assertEqual(J.juger(vide, g)['score'], J.plancher(g), p)
            self.assertEqual(J.plancher(g), 18 * (4 - len(g['evenements'])), p)

    def test_aucun_cas_au_dessus_de_quatre(self):
        for p in _cas_boucle():
            self.assertLessEqual(len(_lire(p)['evenements']), 4, p)


class Absence(unittest.TestCase):
    def setUp(self):
        self.g = _lire(str(RACINE / 'cas' / 'mail-02' / 'golden-evenements.json'))

    def test_date_deplacee_perd_deux_points(self):
        r = copy.deepcopy(self.g)
        r['evenements'][1]['date_debut'] = '2026-10-07'
        self.assertEqual(J.juger(r, self.g)['score'], 70)

    def test_evenement_invente_coute_dix_neuf(self):
        r = copy.deepcopy(self.g)
        r['evenements'].append(dict(self.g['evenements'][0], date_debut='2026-10-20', titre='Invente'))
        # l'emplacement 3 passe de 18 (vide-vide) a 0, et -1 de malus
        self.assertEqual(J.juger(r, self.g)['score'], 72 - 18 - 1)

    def test_evenement_oublie_perd_son_emplacement(self):
        r = copy.deepcopy(self.g)
        r['evenements'] = r['evenements'][1:]
        self.assertEqual(J.juger(r, self.g)['score'], 72 - 18)

    def test_lieu_invente_sur_une_echeance(self):
        r = copy.deepcopy(self.g)
        r['evenements'][0]['lieu'] = 'salle Jaures'
        self.assertEqual(J.juger(r, self.g)['score'], 72 - 2 - 1)

    def test_lieu_bavard_est_accepte(self):
        r = copy.deepcopy(self.g)
        r['evenements'][1]['lieu'] = 'Salle JAURES, batiment E, 1er etage'
        self.assertEqual(J.juger(r, self.g)['score'], 72)

    def test_heure_de_fin_absente_perd_la_case_fin(self):
        r = copy.deepcopy(self.g)
        r['evenements'][1]['heure_fin'] = ''
        self.assertEqual(J.juger(r, self.g)['score'], 70)

    def test_date_fin_egale_a_date_debut_est_vide(self):
        r = copy.deepcopy(self.g)
        r['evenements'][0]['date_fin'] = r['evenements'][0]['date_debut']
        self.assertEqual(J.juger(r, self.g)['score'], 72)

    def test_ordre_sans_importance(self):
        r = copy.deepcopy(self.g)
        r['evenements'].reverse()
        self.assertEqual(J.juger(r, self.g)['score'], 72)

    def test_cinquieme_evenement_en_trop(self):
        g = _lire(str(RACINE / 'cas' / 'mail-24' / 'golden-evenements.json'))
        r = copy.deepcopy(g)
        r['evenements'].append(dict(g['evenements'][0], date_debut='2026-11-02', titre='Rentree'))
        self.assertEqual(J.juger(r, g)['score'], 71)

    def test_documents_partiels(self):
        g = _lire(str(RACINE / 'cas' / 'mail-06' / 'golden-evenements.json'))
        r = copy.deepcopy(g)
        r['evenements'][2]['documents'] = ['piece d identite', 'bulletins de salaire', 'avis d imposition']
        self.assertEqual(J.juger(r, g)['score'], 71)


class Panne(unittest.TestCase):
    def test_corrige_trop_long(self):
        g = _lire(str(RACINE / 'cas' / 'mail-24' / 'golden-evenements.json'))
        g['evenements'].append(dict(g['evenements'][0]))
        with self.assertRaises(J.Panne):
            J.juger(g, g)

    def test_sortie_difforme(self):
        g = _lire(str(RACINE / 'cas' / 'mail-02' / 'golden-evenements.json'))
        with self.assertRaises(J.Panne):
            J.juger({'evenements': 'non'}, g)


if __name__ == '__main__':
    unittest.main()
