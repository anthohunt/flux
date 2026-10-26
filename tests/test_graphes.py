"""C3 — pas d'etape sans juge : le validateur refuse un graphe sans juge, sans
modele, sans entree, un noeud api sans schema, un `suivant` inconnu ; il
accepte les six graphes du depot.

    python3 -m unittest discover -s tests -v      (pytest absent sur la cible)

Chaque refus est teste sur un graphe minimal ecrit dans un dossier jetable qui
imite la racine du depot (runtime/workflow.schema.json + workflows/ + le juge),
pour que le message de refus soit celui d'une seule faute.
"""
import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parents[1]
VERIF = RACINE / 'runtime' / 'verifier-graphes.py'
GRAPHES_ATTENDUS = {'mail', 'mail-1noeud', 'agenda', 'exemple-agent'}

NOEUD_TMUX = {
    "id": "un", "n": 1, "type": "agent", "name": "Un", "modele": "opus", "entree": ["WORKSPACE.txt"],
    "objectif": "faire", "taches": ["faire"], "livrables": ["a.md"], "juge": {"type": "presence"}, "suivant": ["deux"],
}
NOEUD_API = {
    "id": "deux", "n": 2, "name": "Deux", "moteur": "api", "modele": "sonnet", "entree": ["a.md"],
    "consignes": ["remplis"], "schema": "workflows/s.schema.json", "livrables": ["r.json"],
    "juge": {"script": "runtime/juges/presence.py", "corrige": "golden.json", "bareme": 20}, "suivant": [],
}
GRAPHE = {
    "id": "g", "name": "G", "workspace_root": "/tmp/g", "moteurs_autorises": ["tmux", "api"],
    "nodes": [NOEUD_TMUX, NOEUD_API],
}
SCHEMA_OK = {"type": "object", "additionalProperties": False, "required": ["x"],
             "properties": {"x": {"type": "string", "description": "AAAA-MM-JJ"}}}


def ecrire_json(p, d):
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)


def lire_json(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def lancer(racine, *graphes):
    r = subprocess.run([sys.executable, str(VERIF), '--racine', str(racine)] + [str(g) for g in graphes],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


class Bac(unittest.TestCase):
    """Un faux depot par test : schema du contrat, juge de presence, un schema api."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='flux-graphes-'))
        (self.tmp / 'runtime' / 'juges').mkdir(parents=True)
        (self.tmp / 'workflows').mkdir()
        shutil.copy(RACINE / 'runtime' / 'workflow.schema.json', self.tmp / 'runtime')
        shutil.copy(RACINE / 'runtime' / 'juges' / 'presence.py', self.tmp / 'runtime' / 'juges')
        ecrire_json(self.tmp / 'workflows' / 's.schema.json', SCHEMA_OK)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def graphe(self, modif, nom='g'):
        d = copy.deepcopy(GRAPHE)
        d['id'] = nom
        modif(d)
        p = self.tmp / 'workflows' / ('%s.json' % nom)
        ecrire_json(p, d)
        return p

    def refuse(self, modif, attendu):
        p = self.graphe(modif)
        rc, out = lancer(self.tmp, p)
        self.assertEqual(rc, 1, out)
        self.assertIn('REFUS', out)
        self.assertIn(attendu, out, out)

    def accepte(self, modif=lambda d: None):
        p = self.graphe(modif)
        rc, out = lancer(self.tmp, p)
        self.assertEqual(rc, 0, out)
        self.assertIn('GRAPHES CONFORMES', out)


class TestRefus(Bac):
    def test_graphe_minimal_accepte(self):
        self.accepte()

    def test_sans_juge(self):
        self.refuse(lambda d: d['nodes'][0].pop('juge'), 'pas de juge')

    def test_sans_modele(self):
        self.refuse(lambda d: d['nodes'][1].pop('modele'), '`modele` manquant')

    def test_sans_entree(self):
        self.refuse(lambda d: d['nodes'][0].pop('entree'), '`entree` manquante')

    def test_api_sans_schema(self):
        self.refuse(lambda d: d['nodes'][1].pop('schema'), 'sans `schema`')

    def test_suivant_inconnu(self):
        self.refuse(lambda d: d['nodes'][0].__setitem__('suivant', ['trois']), 'suivant `trois`')

    def test_ids_en_double(self):
        self.refuse(lambda d: d['nodes'][1].__setitem__('id', 'un'), 'id de noeud en double')

    def test_juge_et_juges_a_la_fois(self):
        self.refuse(lambda d: d['nodes'][1].__setitem__('juges', [dict(d['nodes'][1]['juge'], sortie='r.json')]),
                    'un seul des deux')

    def test_juges_sortie_hors_livrables(self):
        def m(d):
            n = d['nodes'][1]
            n['juges'] = [dict(n.pop('juge'), sortie='autre.json')]
        self.refuse(m, 'n\'est pas un des livrables')

    def test_juge_script_introuvable(self):
        self.refuse(lambda d: d['nodes'][1]['juge'].__setitem__('script', 'runtime/juges/fantome.py'),
                    'juge.script introuvable')

    def test_juge_cle_inconnue_refusee_par_le_schema(self):
        self.refuse(lambda d: d['nodes'][0]['juge'].__setitem__('seuil', 10), 'REFUS schema')

    def test_moteur_hors_autorises(self):
        self.refuse(lambda d: d.__setitem__('moteurs_autorises', ['tmux']), 'hors de moteurs_autorises')

    def test_modele_parametre_inconnu(self):
        self.refuse(lambda d: d['nodes'][1].__setitem__('modele', '{modele}'), 'parametre de run inconnu')

    def test_modele_parametre_declare(self):
        def m(d):
            d['parametres_run'] = [{"id": "modele", "label": "M", "type": "choix", "options": ["opus", "sonnet"]}]
            d['nodes'][1]['modele'] = '{modele}'
        self.accepte(m)

    def test_schema_api_hors_sous_ensemble(self):
        s = dict(SCHEMA_OK)
        s['properties'] = {"x": {"type": "string", "pattern": "^\\d+$"}}
        ecrire_json(self.tmp / 'workflows' / 's.schema.json', s)
        self.refuse(lambda d: None, '`pattern`')

    def test_schema_api_objet_ouvert(self):
        s = {"type": "object", "properties": {"x": {"type": "string"}}}
        ecrire_json(self.tmp / 'workflows' / 's.schema.json', s)
        self.refuse(lambda d: None, 'additionalProperties')

    def test_id_different_du_fichier(self):
        self.refuse(lambda d: d.__setitem__('id', 'autre'), 'different du nom de fichier')

    def test_premier_refus_arrete(self):
        """exit 1 au premier graphe refuse, les suivants ne sont pas lus."""
        p1 = self.graphe(lambda d: d['nodes'][0].pop('juge'), 'a')
        p2 = self.graphe(lambda d: None, 'b')
        rc, out = lancer(self.tmp, p1, p2)
        self.assertEqual(rc, 1)
        self.assertIn('REFUSE  workflows/a.json', out)
        self.assertNotIn('workflows/b.json', out)


class TestDepot(unittest.TestCase):
    def test_les_graphes_du_depot_sont_acceptes(self):
        presents = {p.stem for p in (RACINE / 'workflows').glob('*.json')}
        self.assertTrue(GRAPHES_ATTENDUS <= presents, 'graphes manquants : %s' % (GRAPHES_ATTENDUS - presents))
        rc, out = lancer(RACINE)
        self.assertEqual(rc, 0, out)
        for g in GRAPHES_ATTENDUS:
            self.assertIn('ACCEPTE workflows/%s.json' % g, out)

    def test_tout_noeud_du_depot_porte_modele_entree_juge(self):
        for p in (RACINE / 'workflows').glob('*.json'):
            d = lire_json(p)
            for n in d['nodes']:
                self.assertIn('modele', n, '%s:%s' % (p.name, n['id']))
                self.assertIn('entree', n, '%s:%s' % (p.name, n['id']))
                self.assertTrue('juge' in n or 'juges' in n, '%s:%s' % (p.name, n['id']))

    def test_les_noeuds_presence_sont_explicites(self):
        """graphe d'agents : le juge minimal est marque type presence (C3)."""
        for nom in ('exemple-agent',):
            d = lire_json(RACINE / 'workflows' / ('%s.json' % nom))
            for n in d['nodes']:
                self.assertEqual(n['juge'], {'type': 'presence'}, '%s:%s' % (nom, n['id']))

    def test_mail_est_un_workflow_api_parametre(self):
        d = lire_json(RACINE / 'workflows' / 'mail.json')
        self.assertEqual([n['moteur'] for n in d['nodes']], ['api', 'api'])
        self.assertEqual({p['id'] for p in d['parametres_run']}, {'cas', 'modele', 'mail'})
        self.assertEqual(d['nodes'][0]['juge']['bareme'] + d['nodes'][1]['juge']['bareme'], 36)
        un = lire_json(RACINE / 'workflows' / 'mail-1noeud.json')
        self.assertEqual(sum(j['bareme'] for j in un['nodes'][0]['juges']), 36)


if __name__ == '__main__':
    unittest.main()
