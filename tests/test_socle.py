#!/usr/bin/env python3
"""Le socle v2 : lecture tolerante des passes, regle « passe verte », run.json, juge."""
import json
import os
import pathlib
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / 'runtime'))
import socle  # noqa: E402


class TestNormaliserPasse(unittest.TestCase):
    def test_ancienne_passe_compte_dans_moteur(self):
        p = socle.normaliser_passe({'n': 2, 'moteur': 'perso', 'session': 'x', 'fin': 'sentinelle',
                                    'verdict': 'vert'})
        self.assertEqual(p['moteur'], 'tmux')
        self.assertEqual(p['compte'], 'perso')
        self.assertIsNone(p['note'])
        self.assertIsNone(p['garde'])
        self.assertEqual(p['entree'], [])
        self.assertFalse(p['est_panne'])

    def test_session_morte_ancienne_est_une_panne_en_lecture(self):
        # le cas reel : chasse p6 de la run du 03/09, fin session-morte, verdict vert
        p = socle.normaliser_passe({'n': 6, 'moteur': 'perso', 'fin': 'session-morte', 'verdict': 'vert'})
        self.assertTrue(p['est_panne'])
        self.assertEqual(p['panne']['cause'], 'session-morte')

    def test_passe_api_v2_inchangee(self):
        p = socle.normaliser_passe({'n': 1, 'moteur': 'api', 'modele': 'ollama:qwen3:4b',
                                    'note': {'score': 18, 'bareme': 20}})
        self.assertEqual(p['moteur'], 'api')
        self.assertIsNone(p['compte'])
        self.assertEqual(p['note']['score'], 18)


class TestPasseVerte(unittest.TestCase):
    def test_panne_jamais_verte(self):
        ok, motif = socle.passe_verte({'juge': {'bareme': 20}},
                                      {'verdict': 'vert', 'fin': 'panne'}, 'banc')
        self.assertFalse(ok)

    def test_banc_sans_note_pas_verte(self):
        ok, _ = socle.passe_verte({'juge': {'bareme': 20}}, {'verdict': 'vert', 'fin': 'sentinelle'}, 'banc')
        self.assertFalse(ok)

    def test_banc_seuil_par_defaut_est_le_bareme(self):
        n = {'juge': {'bareme': 20}}
        self.assertFalse(socle.passe_verte(n, {'verdict': 'vert', 'fin': 'sentinelle',
                                               'note': {'score': 19, 'bareme': 20}}, 'banc')[0])
        self.assertTrue(socle.passe_verte(n, {'verdict': 'vert', 'fin': 'sentinelle',
                                              'note': {'score': 20, 'bareme': 20}}, 'banc')[0])

    def test_banc_seuil_du_noeud(self):
        n = {'juge': {'bareme': 20}, 'seuil': 15}
        self.assertTrue(socle.passe_verte(n, {'verdict': 'vert', 'fin': 'sentinelle',
                                              'note': {'score': 16, 'bareme': 20}}, 'banc')[0])

    def test_production_sans_note_verte(self):
        ok, _ = socle.passe_verte({'juge': {'bareme': 20}},
                                  {'verdict': 'vert', 'fin': 'sentinelle', 'note': None}, 'production')
        self.assertTrue(ok)


class TestRunJson(unittest.TestCase):
    def test_run_heritee_puis_ecrite(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['FLUX_STATE_DIR'] = d
            try:
                ws = pathlib.Path(d) / 'ws-1'
                ws.mkdir()
                r = socle.lire_run('wf', str(ws))
                self.assertEqual(r['mode'], 'production')
                self.assertTrue(r.get('herite'))
                socle.ecrire_run('wf', str(ws), mode='banc', cas='mail-01', statut='en-cours')
                r = socle.lire_run('wf', str(ws))
                self.assertEqual(r['mode'], 'banc')
                self.assertEqual(r['cas'], 'mail-01')
                self.assertNotIn('herite', r)
                self.assertTrue((pathlib.Path(d) / 'wf' / 'ws-1' / 'run.json').exists())
            finally:
                del os.environ['FLUX_STATE_DIR']


class TestJuge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        os.environ['FLUX_STATE_DIR'] = str(d / 'state')
        os.environ['FLUX_ORCHESTRATEUR'] = 'regle'
        self.ws = d / 'ws'
        self.ws.mkdir()
        self.cas = d / 'cas-x'
        self.cas.mkdir()
        (self.cas / 'golden.json').write_text('{"a": 1, "b": 2}')
        (self.ws / 'sortie.json').write_text('{"a": 1, "b": 3}')
        socle.ecrire_run('wf', str(self.ws), mode='banc', cas=str(self.cas))

    def tearDown(self):
        del os.environ['FLUX_STATE_DIR']
        self.tmp.cleanup()

    def test_juge_script_repli(self):
        node = {'id': 'n', 'livrables': ['sortie.json'],
                'juge': {'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                         'corrige': 'golden.json', 'bareme': 10}}
        note = socle.noter('wf', node, str(self.ws))
        self.assertEqual(note['score'], 5)
        self.assertEqual(note['bareme'], 10)
        self.assertEqual(len(note['detail'] if 'detail' in note else note['lignes']), 2)

    def test_juges_multiples(self):
        (self.ws / 'autre.json').write_text('{"a": 1, "b": 2}')
        node = {'id': 'n', 'livrables': ['sortie.json', 'autre.json'],
                'juges': [{'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                           'corrige': 'golden.json', 'bareme': 10, 'sortie': 'sortie.json'},
                          {'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                           'corrige': 'golden.json', 'bareme': 10, 'sortie': 'autre.json'}]}
        note = socle.noter('wf', node, str(self.ws))
        self.assertEqual(note['score'], 15)
        self.assertEqual(note['bareme'], 20)

    def test_juge_presence(self):
        node = {'id': 'n', 'livrables': ['sortie.json'], 'juge': {'type': 'presence'}}
        note = socle.noter('wf', node, str(self.ws))
        self.assertEqual(note['score'], note['bareme'])
        node = {'id': 'n', 'livrables': ['absent.json'], 'juge': {'type': 'presence'}}
        note = socle.noter('wf', node, str(self.ws))
        self.assertEqual(note['score'], 0)

    def test_juge_qui_plante_leve(self):
        os.environ['FAUX_JUGE_PLANTE'] = '1'
        try:
            node = {'id': 'n', 'livrables': ['sortie.json'],
                    'juge': {'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                             'corrige': 'golden.json', 'bareme': 10}}
            with self.assertRaises(RuntimeError):
                socle.noter('wf', node, str(self.ws))
        finally:
            del os.environ['FAUX_JUGE_PLANTE']

    def test_lanceur_du_contrat_si_present(self):
        """Avec un runtime/juge.py pose ailleurs (FLUX_JUGE_DIR), meme resultat."""
        autre = pathlib.Path(os.environ.get('FLUX_JUGE_DIR_TEST') or (RACINE / 'runtime'))
        if not (autre / 'juge.py').exists() and not (RACINE / 'runtime' / 'juge.py').exists():
            self.skipTest('runtime/juge.py absent (chantier contrat non fusionne)')
        if (autre / 'juge.py').exists():
            os.environ['FLUX_JUGE_DIR'] = str(autre)
        try:
            node = {'id': 'n', 'livrables': ['sortie.json'],
                    'juge': {'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                             'corrige': 'golden.json', 'bareme': 10}}
            note = socle.noter('wf', node, str(self.ws))
            self.assertEqual(note['score'], 5)
            self.assertEqual(note['lanceur'], 'runtime/juge.py')
        finally:
            os.environ.pop('FLUX_JUGE_DIR', None)


class TestModeleDuNoeud(unittest.TestCase):
    def test_substitution_parametre_de_run(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['FLUX_STATE_DIR'] = d
            try:
                ws = pathlib.Path(d) / 'ws'
                ws.mkdir()
                wf = {'id': 'wf', 'parametres_run': [{'id': 'modele', 'defaut': 'opus'}]}
                self.assertEqual(socle.modele_du_noeud('wf', str(ws), {'modele': '{modele}'}, wf), 'opus')
                socle.ecrire_run('wf', str(ws), parametres={'modele': 'ollama:qwen3:4b'})
                self.assertEqual(socle.modele_du_noeud('wf', str(ws), {'modele': '{modele}'}, wf),
                                 'ollama:qwen3:4b')
                self.assertEqual(socle.modele_du_noeud('wf', str(ws), {'modele': 'haiku'}, wf), 'haiku')
            finally:
                del os.environ['FLUX_STATE_DIR']


if __name__ == '__main__':
    unittest.main()
