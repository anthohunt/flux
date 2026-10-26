#!/usr/bin/env python3
"""Le moteur `api` (runtime/moteurs/api.py) — C4.

Hors reseau : un faux serveur HTTP local imite Ollama (tests/fakes/ollama.py).
  - reponse conforme -> livrable ecrit, passe verte, mesures et jetons portes ;
  - non conforme puis conforme -> 2 essais, passe verte ;
  - jamais conforme -> 3 essais, passe en PANNE (sortie-invalide) ;
  - bloc ```json et <think> toleres ;
  - via run-node.py en banc : la note est ecrite par le juge ; en production, note: null.

Reseau (sautes si FLUX_SANS_RESEAU=1 ou injoignable) :
  - qwen3:4b sur un Ollama joignable (FLUX_OLLAMA_URL), nœud « remplir » de mail.json ;
  - `claude -p --model haiku` sur un mini-schema (pas de cle API : compte Max).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

RACINE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / 'runtime'))
sys.path.insert(0, str(RACINE / 'tests' / 'fakes'))
sys.path.insert(0, str(RACINE / 'runtime' / 'juges'))
import formulaire as jform  # noqa: E402  (son bareme fixe, pour le comparer au graphe)
import socle  # noqa: E402
from moteurs import api  # noqa: E402
from ollama import FauxOllama  # noqa: E402

SANS_RESEAU = os.environ.get('FLUX_SANS_RESEAU') == '1'

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['ville', 'annee'],
          'properties': {'ville': {'type': 'string', 'minLength': 1},
                         'annee': {'type': 'integer', 'minimum': 1000}}}


def racine_mail():
    """La racine qui porte workflows/mail.json et cas/mail-01 : ce depot, sinon None."""
    for r in (RACINE,):
        if (r / 'workflows' / 'mail.json').exists() and (r / 'cas' / 'mail-01' / 'mail.md').exists():
            return r
    return None


def ollama_joignable(url):
    try:
        with urllib.request.urlopen(url + '/api/tags', timeout=5) as r:
            noms = [m['name'] for m in json.load(r).get('models', [])]
        return noms
    except Exception:
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.ws = d / 'ws'
        (self.ws / 'state').mkdir(parents=True)
        (self.ws / 'entree.md').write_text('Paris a ete fondee vers -250 ; capitale de la France.\n')
        self.pdir = d / 'passes' / '1'
        self._env = dict(os.environ)
        os.environ['FLUX_STATE_DIR'] = str(d / 'state')
        os.environ['FLUX_ORCHESTRATEUR'] = 'regle'
        os.environ['FLUX_WORKFLOWS_DIR'] = str(d / 'workflows')
        (d / 'workflows').mkdir()
        self.wf = {'id': 'wf-api', 'name': 'test api', 'workspace_root': str(d),
                   'nodes': [self.noeud()]}
        (d / 'workflows' / 'wf-api.json').write_text(json.dumps(self.wf))
        self.faux = FauxOllama()

    def noeud(self):
        return {'id': 'formulaire', 'n': 1, 'name': 'Formulaire', 'moteur': 'api',
                'modele': 'ollama:faux', 'entree': ['entree.md'],
                'consignes': ['Extrais la ville et son annee de fondation.'],
                'schema': SCHEMA, 'livrables': ['resultat.json'],
                'juge': {'script': str(RACINE / 'tests' / 'fakes' / 'juge-json.py'),
                         'corrige': 'golden.json', 'bareme': 10},
                'suivant': []}

    def demarrer(self, scenario):
        self.faux = FauxOllama(scenario)
        os.environ['FLUX_OLLAMA_URL'] = self.faux.demarrer()

    def tearDown(self):
        self.faux.arreter()
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()


class TestFauxOllama(Base):
    def test_reponse_conforme(self):
        self.demarrer(['conforme'])
        p = api.jouer(self.wf, self.noeud(), str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'sentinelle')
        self.assertEqual(p['verdict'], 'vert')
        self.assertEqual(p['moteur'], 'api')
        self.assertEqual(p['modele'], 'ollama:faux')
        self.assertEqual(p['entree'], ['entree.md'])
        self.assertEqual(p['sortie'], ['resultat.json'])
        self.assertIsNone(p['note'])
        self.assertIsNone(p['garde'])
        self.assertEqual(p['mesures']['jetons_entree'], 120)
        self.assertEqual(p['mesures']['jetons_sortie'], 30)
        self.assertEqual(p['mesures']['cout_usd'], 0.0)
        self.assertEqual(len(p['essais']), 1)
        obj = json.loads((self.ws / 'resultat.json').read_text())
        self.assertIn('ville', obj)
        # le schema est bien parti dans `format`, le contexte dans `options`
        req = self.faux.recues[0]
        self.assertEqual(req['format'], SCHEMA)
        self.assertEqual(req['options']['num_ctx'], 32768)
        self.assertFalse(req['stream'])
        self.assertIn('Extrais la ville', req['messages'][0]['content'])
        self.assertIn('Paris a ete fondee', req['messages'][0]['content'])
        self.assertTrue((self.pdir / 'prompt.md').exists())
        self.assertTrue((self.pdir / 'run.out').exists())

    def test_reessai_apres_non_conforme(self):
        self.demarrer(['non-conforme', 'conforme'])
        p = api.jouer(self.wf, self.noeud(), str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'sentinelle')
        self.assertEqual(len(p['essais']), 2)
        self.assertFalse(p['essais'][0]['ok'])
        self.assertIn('ValidationError', p['essais'][0]['erreur'])
        self.assertTrue(p['essais'][1]['ok'])
        self.assertEqual(p['mesures']['jetons_entree'], 240)   # les deux essais sont payes

    def test_panne_apres_trois_essais(self):
        self.demarrer(['non-conforme'])
        p = api.jouer(self.wf, self.noeud(), str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'panne')
        self.assertEqual(p['verdict'], 'panne')
        self.assertEqual(p['panne']['cause'], 'sortie-invalide')
        self.assertEqual(len(p['essais']), 3)
        self.assertFalse((self.ws / 'resultat.json').exists())

    def test_prose_sans_json_est_une_panne(self):
        self.demarrer(['texte'])
        p = api.jouer(self.wf, self.noeud(), str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'panne')

    def test_bloc_json_et_think_toleres(self):
        self.demarrer(['bloc'])
        p = api.jouer(self.wf, self.noeud(), str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'sentinelle')

    def test_entree_absente_est_une_panne(self):
        self.demarrer(['conforme'])
        n = self.noeud()
        n['entree'] = ['inexistant.md']
        p = api.jouer(self.wf, n, str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'panne')
        self.assertEqual(p['panne']['cause'], 'entree-manquante')
        self.assertEqual(self.faux.recues, [])

    def test_plusieurs_livrables_une_cle_par_fichier(self):
        self.demarrer(['conforme'])
        n = self.noeud()
        n['livrables'] = ['resultat.json', 'actions.json']
        n['schema'] = {'type': 'object', 'required': ['resultat', 'actions'],
                       'properties': {'resultat': SCHEMA,
                                      'actions': {'type': 'array', 'items': {'type': 'string'}}}}
        p = api.jouer(self.wf, n, str(self.ws), 1, self.pdir)
        self.assertEqual(p['fin'], 'sentinelle')
        self.assertIn('ville', json.loads((self.ws / 'resultat.json').read_text()))
        self.assertIsInstance(json.loads((self.ws / 'actions.json').read_text()), list)

    def test_extraire_json(self):
        self.assertEqual(api.extraire_json('{"a": 1}'), {'a': 1})
        self.assertEqual(api.extraire_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(api.extraire_json('<think>hum</think>\nVoici : {"a": 1} voila'), {'a': 1})
        with self.assertRaises(ValueError):
            api.extraire_json('rien du tout')


class TestViaRunNode(Base):
    """run-node.py joue un nœud api sans tmux, note en banc, pas en production."""

    def lancer(self, mode, cas=None):
        d = pathlib.Path(self.tmp.name)
        (self.ws / 'state').mkdir(exist_ok=True)
        socle.ecrire_run(self.wf['id'], str(self.ws), mode=mode, cas=cas, statut='en-cours')
        env = dict(os.environ, FLUX_SANS_GARDE='1')
        r = subprocess.run([sys.executable, str(RACINE / 'runtime' / 'run-node.py'), 'wf-api',
                            'formulaire', '--workspace', str(self.ws)],
                           capture_output=True, text=True, timeout=120, env=env)
        st = json.loads((d / 'state' / 'wf-api' / 'ws' / 'formulaire.json').read_text())
        return r, st

    def test_banc_note_ecrite(self):
        d = pathlib.Path(self.tmp.name)
        cas = d / 'cas-1'
        cas.mkdir()
        (cas / 'golden.json').write_text(json.dumps({'ville': 'valeur', 'annee': 1000}))
        self.demarrer(['conforme'])
        r, st = self.lancer('banc', cas=str(cas))
        self.assertEqual(r.returncode, 0, r.stderr)
        p = st['passages'][0]
        self.assertEqual(p['moteur'], 'api')
        self.assertEqual(p['verdict'], 'vert')
        self.assertEqual(p['note']['score'], 10)
        self.assertEqual(p['note']['bareme'], 10)
        # v2 : le moteur api tourne dans une fenetre tmux que la console montre
        self.assertTrue(str(st.get('tmux') or '').endswith('-formulaire-p1'), st.get('tmux'))
        run = json.loads((d / 'state' / 'wf-api' / 'ws' / 'run.json').read_text())
        self.assertEqual(run['mode'], 'banc')
        self.assertTrue((d / 'state' / 'wf-api' / 'ws' / 'etat-compact.json').exists())

    def test_banc_juge_qui_plante_est_une_panne(self):
        d = pathlib.Path(self.tmp.name)
        cas = d / 'cas-1'
        cas.mkdir()
        (cas / 'golden.json').write_text('{}')
        self.demarrer(['conforme'])
        os.environ['FAUX_JUGE_PLANTE'] = '1'
        r, st = self.lancer('banc', cas=str(cas))
        self.assertEqual(r.returncode, 2)
        p = st['passages'][0]
        self.assertEqual(p['fin'], 'panne')
        self.assertEqual(p['panne']['cause'], 'juge-en-panne')
        self.assertEqual(st['statut'], 'panne')

    def test_production_sans_note(self):
        self.demarrer(['conforme'])
        r, st = self.lancer('production')
        self.assertEqual(r.returncode, 0, r.stderr)
        p = st['passages'][0]
        self.assertEqual(p['verdict'], 'vert')
        self.assertIsNone(p['note'])

    def test_sortie_invalide_noeud_en_panne(self):
        self.demarrer(['non-conforme'])
        r, st = self.lancer('production')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(st['statut'], 'panne')
        self.assertEqual(st['passages'][0]['fin'], 'panne')


@unittest.skipIf(SANS_RESEAU, 'FLUX_SANS_RESEAU=1')
class TestReseau(unittest.TestCase):
    """Marque reseau : le laptop (Ollama) et le compte Max (claude -p) doivent repondre."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        d = pathlib.Path(self.tmp.name)
        os.environ['FLUX_STATE_DIR'] = str(d / 'state')
        self.ws = d / 'ws'
        (self.ws / 'state').mkdir(parents=True)
        self.pdir = d / 'passes' / '1'

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_qwen3_4b_sur_mail_01(self):
        r = racine_mail()
        if r is None:
            self.skipTest('workflows/mail.json ou cas/mail-01 absents')
        url = socle.moteur_api_ollama_url()
        noms = ollama_joignable(url)
        if noms is None:
            self.skipTest('Ollama injoignable a ' + url + ' (laptop eteint ?) — test SAUTE')
        if 'qwen3:4b' not in noms:
            self.skipTest('qwen3:4b absent de ' + url)
        os.environ['FLUX_RACINE'] = str(r)
        wf = socle.lire_json(r / 'workflows' / 'mail.json')
        node = socle.noeud(wf, 'remplir')
        shutil.copy2(r / 'cas' / 'mail-01' / 'mail.md', self.ws / 'mail.md')
        socle.ecrire_run(wf['id'], str(self.ws), mode='banc', cas='mail-01', statut='en-cours',
                         parametres={'modele': 'ollama:qwen3:4b'})
        p = api.jouer(wf, node, str(self.ws), 1, self.pdir, modele='ollama:qwen3:4b')
        self.assertIn(p['fin'], ('sentinelle', 'panne'))
        self.assertEqual(p['mesures']['cout_usd'], 0.0)
        self.assertGreater(p['mesures']['jetons_entree'], 100)
        if p['fin'] == 'sentinelle':
            obj = json.loads((self.ws / 'resultat.json').read_text())
            self.assertIn('rdv_date', obj)
            # la note, comme le runtime la poserait en banc
            declare = node['juge']['bareme']
            if declare != jform.BAREME:
                # Ce test porte sur le moteur, pas sur l'accord graphe/juge : le
                # desaccord est signale une fois, par test_juges_mail.TestCoherenceGraphes.
                self.skipTest('mail.json declare un bareme de %s, formulaire.py rend %s '
                              '(voir test_juges_mail.TestCoherenceGraphes)' % (declare, jform.BAREME))
            note = socle.noter(wf['id'], node, str(self.ws), pdir=self.pdir)
            self.assertEqual(note['bareme'], declare)
            self.assertGreaterEqual(note['score'], 0)
            print('\n  qwen3:4b · mail-01 · note %s/%s · %.1f s · %d+%d jetons' % (
                note['score'], note['bareme'], p['mesures']['duree_s'],
                p['mesures']['jetons_entree'], p['mesures']['jetons_sortie']))
        else:
            print('\n  qwen3:4b · mail-01 · PANNE apres %d essais : %s' % (len(p['essais']), p['panne']['constat'][:120]))

    def test_claude_p_haiku_mini_schema(self):
        if not pathlib.Path(socle.claude_bin()).exists():
            self.skipTest('claude absent : ' + socle.claude_bin())
        (self.ws / 'entree.md').write_text('Lyon, fondee en -43 par les Romains sous le nom de Lugdunum.\n')
        node = {'id': 'mini', 'n': 1, 'name': 'Mini', 'moteur': 'api', 'modele': 'haiku',
                'entree': ['entree.md'], 'consignes': ['Extrais la ville et son annee de fondation (entier, negatif avant J.-C.).'],
                'schema': {'type': 'object', 'additionalProperties': False, 'required': ['ville', 'annee'],
                           'properties': {'ville': {'type': 'string'}, 'annee': {'type': 'integer'}}},
                'livrables': ['resultat.json']}
        wf = {'id': 'wf-haiku', 'nodes': [node]}
        try:
            p = api.jouer(wf, node, str(self.ws), 1, self.pdir, modele='haiku')
        except RuntimeError as e:
            self.skipTest('claude -p injoignable : %s' % e)
        self.assertEqual(p['fin'], 'sentinelle', p.get('essais'))
        obj = json.loads((self.ws / 'resultat.json').read_text())
        self.assertEqual(obj['ville'].lower(), 'lyon')
        self.assertGreater(p['mesures']['jetons_entree'], 0)
        self.assertGreater(p['mesures']['cout_usd'], 0)
        self.assertEqual(p['mesures']['modele'], 'haiku')
        print('\n  haiku · mini-schema · %.1f s · %d+%d jetons · %.4f $' % (
            p['mesures']['duree_s'], p['mesures']['jetons_entree'], p['mesures']['jetons_sortie'],
            p['mesures']['cout_usd']))


if __name__ == '__main__':
    unittest.main()
