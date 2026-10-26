#!/usr/bin/env python3
"""Le sequenceur (runtime/sequenceur.py) : la regle unique, sur des etats fabriques.

  vert -> nœud suivant ; dernier nœud vert -> run terminee ;
  rouge/panne -> relance jusqu'a relances_max ; epuise -> bloque + alerte ;
  verification qui refuse -> relance du nœud juge (registre etape-N) ;
  la run figee du 03/09 (chasse p6, session-morte) est lue comme une panne.
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / 'runtime'))
import socle  # noqa: E402

spec = importlib.util.spec_from_file_location('sequenceur', str(RACINE / 'runtime' / 'sequenceur.py'))
seq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seq)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ['FLUX_STATE_DIR'] = str(d / 'state')
        os.environ['FLUX_WORKFLOWS_DIR'] = str(d / 'workflows')
        os.environ['FLUX_NOTIF_URL'] = 'http://127.0.0.1:9/notif'   # personne n'ecoute : echec rapide
        os.environ['FLUX_ORCHESTRATEUR'] = 'regle'                    # les tests jugent le programme, pas l'agent
        (d / 'workflows').mkdir()
        self.ws = d / 'run-1'
        (self.ws / 'state').mkdir(parents=True)
        (self.ws / 'state' / 'autopilote').write_text('on\n')
        self.wf = {'id': 'tw', 'name': 't', 'workspace_root': str(d),
                   'garde': {'toutes_les_min': 10, 'relances_max': 2},
                   'nodes': [
                       {'id': 'a', 'n': 1, 'name': 'A', 'moteur': 'api', 'modele': 'haiku', 'entree': [],
                        'livrables': ['a.json'], 'juge': {'bareme': 10}, 'suivant': ['b']},
                       {'id': 'b', 'n': 2, 'name': 'B', 'moteur': 'api', 'modele': 'haiku', 'entree': ['a.json'],
                        'livrables': ['b.json'], 'juge': {'bareme': 10}, 'seuil': 7, 'relances_max': 1,
                        'suivant': []}]}
        (d / 'workflows' / 'tw.json').write_text(json.dumps(self.wf))
        socle.ecrire_run('tw', str(self.ws), mode='banc', cas='x', statut='en-cours')

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def etat(self, nid, passages, statut='fait'):
        socle.ecrire_json(socle.dossier_run('tw', str(self.ws)) / (nid + '.json'),
                          {'node': nid, 'statut': statut, 'passages': passages})

    def passe(self, n, verdict='vert', fin='sentinelle', note=None, **k):
        return {'n': n, 'moteur': 'api', 'modele': 'haiku', 'started': 't', 'ended': 't',
                'fin': fin, 'verdict': verdict, 'note': note, **k}


class TestRegle(Base):
    def test_vert_lance_le_suivant(self):
        self.etat('a', [self.passe(1, note={'score': 10, 'bareme': 10})])
        r = seq.decider(self.wf, str(self.ws), 'a', dry=True)
        self.assertEqual({k: r[k] for k in ('geste', 'noeud')}, {'geste': 'lancer', 'noeud': 'b'})

    def test_banc_note_sous_le_seuil_relance(self):
        self.etat('a', [self.passe(1, note={'score': 9, 'bareme': 10})])
        r = seq.decider(self.wf, str(self.ws), 'a', dry=True)
        self.assertEqual(r['geste'], 'relancer')
        self.assertEqual(r['noeud'], 'a')
        self.assertEqual(r['passe'], 2)

    def test_seuil_du_noeud(self):
        self.etat('b', [self.passe(1, note={'score': 7, 'bareme': 10})])
        r = seq.decider(self.wf, str(self.ws), 'b', dry=False)
        self.assertEqual(r['geste'], 'terminer')
        self.assertEqual(socle.lire_run('tw', str(self.ws))['statut'], 'terminee')

    def test_panne_relance_puis_bloque(self):
        self.etat('a', [self.passe(1, verdict='panne', fin='panne', panne={'cause': 'session-morte'})])
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'a', dry=True)['geste'], 'relancer')
        self.etat('a', [self.passe(1, verdict='panne', fin='panne'), self.passe(2, verdict='panne', fin='panne')])
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'a', dry=True)['geste'], 'relancer')
        self.etat('a', [self.passe(n, verdict='panne', fin='panne') for n in (1, 2, 3)])
        r = seq.decider(self.wf, str(self.ws), 'a', dry=False)
        self.assertEqual(r['geste'], 'bloquer')
        run = socle.lire_run('tw', str(self.ws))
        self.assertEqual(run['statut'], 'bloque')
        self.assertEqual(run['bloque_sur'], 'a')

    def test_relances_max_du_noeud(self):
        self.etat('b', [self.passe(1, note={'score': 1, 'bareme': 10}), self.passe(2, note={'score': 1, 'bareme': 10})])
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'b', dry=True)['geste'], 'bloquer')

    def test_production_sans_note_verte(self):
        socle.ecrire_run('tw', str(self.ws), mode='production', cas=None)
        self.etat('a', [self.passe(1, note=None)])
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'a', dry=True)['geste'], 'lancer')

    def test_passe_en_cours_attend(self):
        self.etat('a', [{'n': 1, 'moteur': 'api', 'started': 't'}], statut='en-cours')
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'a', dry=True)['geste'], 'attendre')

    def test_run_close_ne_fait_rien(self):
        socle.ecrire_run('tw', str(self.ws), statut='close')
        self.assertEqual(seq.decider(self.wf, str(self.ws), 'a', dry=True)['geste'], 'rien')

    def test_sans_noeud_fini_lance_le_premier(self):
        r = seq.decider(self.wf, str(self.ws), None, dry=True)
        self.assertEqual(r, {'geste': 'lancer', 'noeud': 'a'})

    def test_ancienne_passe_session_morte_verdict_vert_est_une_panne(self):
        # le cas reel du 03/09 : fin session-morte, verdict vert, pas de champ panne
        self.etat('a', [{'n': 6, 'moteur': 'perso', 'session': 'x', 'started': 't', 'ended': 't',
                         'fin': 'session-morte', 'verdict': 'vert'}], statut='en-cours')
        r = seq.decider(self.wf, str(self.ws), 'a', dry=True)
        self.assertEqual(r['geste'], 'relancer')


class TestVerification(Base):
    def setUp(self):
        super().setUp()
        self.wf['nodes'] = [
            {'id': 'chasse', 'n': 2, 'type': 'agent', 'name': 'Chasse', 'modele': 'opus', 'entree': [],
             'livrables': ['x.md'], 'juge': {'type': 'presence'}, 'suivant': ['verif-chasse']},
            {'id': 'verif-chasse', 'n': 2, 'type': 'agent', 'name': 'Verif', 'verification': True,
             'modele': 'opus', 'entree': [], 'livrables': ['v.md'], 'juge': {'type': 'presence'},
             'passes_max': 3, 'suivant': ['monter']},
            {'id': 'monter', 'n': 3, 'type': 'agent', 'name': 'Monter', 'modele': 'opus', 'entree': [],
             'livrables': ['m.md'], 'juge': {'type': 'presence'}, 'suivant': []}]
        (pathlib.Path(self.tmp.name) / 'workflows' / 'tw.json').write_text(json.dumps(self.wf))
        socle.ecrire_run('tw', str(self.ws), mode='production', cas=None)

    def registre(self, **score):
        d = self.ws / 'verifications' / 'etape-2'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'registre.json').write_text(json.dumps({'passe': 1, 'score': score}))

    def test_registre_conforme_lance_le_suivant(self):
        self.registre(pourcentage=97, fautes_graves=0)
        self.etat('verif-chasse', [self.passe(1)])
        r = seq.decider(self.wf, str(self.ws), 'verif-chasse', dry=True)
        self.assertEqual({k: r[k] for k in ('geste', 'noeud')}, {'geste': 'lancer', 'noeud': 'monter'})

    def test_registre_qui_refuse_relance_le_noeud_juge(self):
        self.registre(pourcentage=80, fautes_graves=1)
        self.etat('chasse', [self.passe(1)])
        self.etat('verif-chasse', [self.passe(1)])
        r = seq.decider(self.wf, str(self.ws), 'verif-chasse', dry=True)
        self.assertEqual(r['geste'], 'relancer')
        self.assertEqual(r['noeud'], 'chasse')

    def test_borne(self):
        self.registre(pourcentage=97, fautes_graves=0)
        self.assertTrue(seq.borne_atteinte(self.wf, str(self.ws), 'verif-chasse', 'chasse'))
        self.assertTrue(seq.borne_atteinte(self.wf, str(self.ws), 'monter', 'chasse'))
        self.assertFalse(seq.borne_atteinte(self.wf, str(self.ws), 'chasse', 'monter'))


if __name__ == '__main__':
    unittest.main()
