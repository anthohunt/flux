"""Le lanceur runtime/juge.py, et le contrat que les juges doivent tenir.

    python3 -m unittest discover -s tests -v      (pytest absent sur la cible)

Ce fichier tient le LANCEUR : resolution des chemins, panne claire plutot que
note quand quelque chose manque, forme de la reponse, et application de la
declaration `juge` / `juges` d'un noeud. Les deux juges du corpus mail ont leur
propre fichier, tests/test_juges_mail.py - c'est la que vivent le bareme fixe
(22 et 14), la regle d'absence et la normalisation.

Ce qui a disparu ici le 2026-09-07 : les valeurs gardees « reponse-A = 20/20 et
reponse-B = 8,5/20 sur cas/mail-01 ». Le bareme, la grille et le corrige de
mail-01 ont tous change avec le corpus de 30 cas ; ces chiffres ne se
comparaient plus a rien. Les tests de noeud ne lisent plus workflows/mail.json
non plus : ils construisent leur noeud, pour qu'une retouche de graphe ne casse
pas un test de lanceur (et reciproquement - la coherence graphe/juge est
verifiee, une fois, par test_juges_mail.TestCoherenceGraphes).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / 'runtime'))
import juge  # noqa: E402  (runtime/juge.py)

CAS = RACINE / 'cas' / 'mail-01'
FORMULAIRE = 'runtime/juges/formulaire.py'
ACTIONS = 'runtime/juges/actions.py'
PRESENCE = 'runtime/juges/presence.py'
BAREME_F, BAREME_A = 22, 14


def lire(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def ecrire(p, d):
    pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)


def cli(*args):
    r = subprocess.run([sys.executable, str(RACINE / 'runtime' / 'juge.py')] + list(args),
                       capture_output=True, text=True, cwd=str(RACINE))
    return r.returncode, r.stdout, r.stderr


class TestFormulaire(unittest.TestCase):
    """Le juge du formulaire, vu du lanceur. Le detail est dans test_juges_mail.py."""

    def test_golden_contre_lui_meme_vaut_le_bareme(self):
        rep = juge.juger(FORMULAIRE, 'golden-formulaire.json', ['golden-formulaire.json'],
                         bareme=BAREME_F, cas=CAS, workspace=CAS)
        self.assertEqual((rep['score'], rep['bareme']), (BAREME_F, BAREME_F), rep['lignes'])

    def test_une_case_oubliee_coute_deux_points(self):
        with tempfile.TemporaryDirectory() as ws:
            r = dict(lire(CAS / 'golden-formulaire.json'), rdv_heure='')
            ecrire(os.path.join(ws, 'resultat.json'), r)
            rep = juge.juger(FORMULAIRE, 'golden-formulaire.json', ['resultat.json'],
                             bareme=BAREME_F, cas=CAS, workspace=ws)
        self.assertEqual(rep['score'], BAREME_F - 2)
        self.assertTrue(any(l.startswith('NON') and 'rdv_heure' in l for l in rep['lignes']))

    def test_le_plancher_est_publie_dans_les_lignes(self):
        rep = juge.juger(FORMULAIRE, 'golden-formulaire.json', ['golden-formulaire.json'],
                         bareme=BAREME_F, cas=CAS, workspace=CAS)
        self.assertIn('plancher', rep['lignes'][-1])


class TestActions(unittest.TestCase):
    def test_golden_contre_golden_vaut_le_bareme(self):
        rep = juge.juger(ACTIONS, 'golden-actions.json', ['golden-actions.json'],
                         bareme=BAREME_A, cas=CAS, workspace=CAS)
        self.assertEqual((rep['score'], rep['bareme']), (BAREME_A, BAREME_A))
        self.assertEqual(len(rep['lignes']), 8)        # 7 verbes + le plancher

    def test_une_action_inventee_coute_deux_points(self):
        """`payer` est du vocabulaire mais pas du corrige de mail-01 : 2 -> 0."""
        with tempfile.TemporaryDirectory() as ws:
            ecrire(os.path.join(ws, 'actions.json'),
                   lire(CAS / 'golden-actions.json') + [{"verbe": "payer", "date": "2026-09-24"}])
            rep = juge.juger(ACTIONS, 'golden-actions.json', ['actions.json'],
                             bareme=BAREME_A, cas=CAS, workspace=ws)
        self.assertEqual(rep['score'], BAREME_A - 2)
        self.assertTrue(any('payer' in l and 'inventee' in l for l in rep['lignes']))

    def test_objet_avec_cle_actions_accepte(self):
        """actions.py lit aussi un resultat.json complet (forme du workflow en un noeud)."""
        with tempfile.TemporaryDirectory() as ws:
            ecrire(os.path.join(ws, 'resultat.json'),
                   dict(lire(CAS / 'golden-formulaire.json'),
                        actions=lire(CAS / 'golden-actions.json')))
            rep = juge.juger(ACTIONS, 'golden-actions.json', ['resultat.json'],
                             bareme=BAREME_A, cas=CAS, workspace=ws)
        self.assertEqual(rep['score'], BAREME_A)


class TestPresence(unittest.TestCase):
    def test_fichier_vide_vaut_0(self):
        with tempfile.TemporaryDirectory() as ws:
            open(os.path.join(ws, 'vide.md'), 'w').close()
            rep = juge.juger(PRESENCE, '-', ['vide.md'], bareme=20, cas=None, workspace=ws)
        self.assertEqual((rep['score'], rep['bareme']), (0, 20))
        self.assertIn('vide', rep['lignes'][0])

    def test_fichier_absent_vaut_0_sans_panne(self):
        with tempfile.TemporaryDirectory() as ws:
            rep = juge.juger(PRESENCE, '-', ['absent.md'], bareme=20, workspace=ws)
        self.assertEqual(rep['score'], 0)

    def test_fichiers_presents_valent_le_bareme(self):
        with tempfile.TemporaryDirectory() as ws:
            ecrire(os.path.join(ws, 'a.json'), {"x": 1})
            os.makedirs(os.path.join(ws, 'd'))
            ecrire(os.path.join(ws, 'd', 'b.json'), {"y": 2})
            rep = juge.juger(PRESENCE, '-', ['a.json', 'd/'], bareme=20, workspace=ws)
        self.assertEqual(rep['score'], 20)

    def test_presence_respecte_le_contrat_en_ligne_de_commande(self):
        r = subprocess.run([sys.executable, str(RACINE / PRESENCE), '-', str(CAS / 'mail.md'), '--bareme=7'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(set(d), {'score', 'bareme', 'lignes'})
        self.assertEqual((d['score'], d['bareme']), (7, 7))


class TestLanceur(unittest.TestCase):
    def test_sortie_absente_est_une_panne_claire(self):
        with self.assertRaises(juge.JugeEnPanne) as cm:
            juge.juger(FORMULAIRE, 'golden-formulaire.json', ['resultat.json'],
                       bareme=BAREME_F, cas=CAS, workspace=CAS)
        self.assertIn('sortie absente : resultat.json', str(cm.exception))
        rc, out, err = cli(FORMULAIRE, 'golden-formulaire.json', 'resultat.json', '--bareme', str(BAREME_F),
                           '--cas', str(CAS), '--workspace', str(CAS))
        self.assertEqual(rc, 2)
        self.assertIn('PANNE', err)
        self.assertIn('sortie absente', err)
        d = json.loads(out)
        self.assertIsNone(d['score'])
        self.assertIn('panne', d)

    def test_corrige_absent_est_une_panne(self):
        with self.assertRaises(juge.JugeEnPanne) as cm:
            juge.juger(FORMULAIRE, 'golden-fantome.json', ['golden-formulaire.json'], cas=CAS, workspace=CAS)
        self.assertIn('corrige introuvable', str(cm.exception))

    def test_script_absent_est_une_panne(self):
        with self.assertRaises(juge.JugeEnPanne) as cm:
            juge.juger('runtime/juges/fantome.py', 'golden-formulaire.json', ['golden-formulaire.json'],
                       cas=CAS, workspace=CAS)
        self.assertIn('juge introuvable', str(cm.exception))

    def test_bareme_declare_different_est_une_panne(self):
        with self.assertRaises(juge.JugeEnPanne) as cm:
            juge.juger(ACTIONS, 'golden-actions.json', ['golden-actions.json'], bareme=20,
                       cas=CAS, workspace=CAS)
        self.assertIn('bareme de 14, le graphe declare 20', str(cm.exception))

    def test_reponse_mal_formee_est_une_panne(self):
        for rep, motif in [({'score': 5, 'bareme': 10}, 'lignes'),
                           ({'score': 11, 'bareme': 10, 'lignes': []}, 'hors de'),
                           ({'score': 'x', 'bareme': 10, 'lignes': []}, 'non numerique'),
                           ({'score': 1, 'bareme': 0, 'lignes': []}, 'bareme invalide'),
                           ([1, 2], 'objet')]:
            with self.assertRaises(juge.JugeEnPanne) as cm:
                juge.valider_reponse(rep)
            self.assertIn(motif, str(cm.exception))

    def test_cli_rend_le_json_du_juge(self):
        rc, out, err = cli(FORMULAIRE, 'golden-formulaire.json', 'golden-formulaire.json',
                           '--bareme', str(BAREME_F), '--cas', str(CAS), '--workspace', str(CAS))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['score'], BAREME_F)

    def test_chemins_absolus_pris_tels_quels(self):
        rep = juge.juger(str(RACINE / FORMULAIRE), str(CAS / 'golden-formulaire.json'),
                         [str(CAS / 'golden-formulaire.json')])
        self.assertEqual(rep['score'], BAREME_F)


class TestNoeud(unittest.TestCase):
    """juger_noeud() applique la declaration juge / juges d'un noeud.

    Les noeuds sont construits ici plutot que lus dans workflows/ : ce test
    porte sur le lanceur, pas sur le contenu des graphes."""

    UN_JUGE = {"id": "remplir", "livrables": ["resultat.json"],
               "juge": {"script": FORMULAIRE, "corrige": "golden-formulaire.json", "bareme": BAREME_F}}
    DEUX_JUGES = {"id": "tout", "livrables": ["resultat.json", "actions.json"],
                  "juges": [{"script": FORMULAIRE, "corrige": "golden-formulaire.json",
                             "bareme": BAREME_F, "sortie": "resultat.json"},
                            {"script": ACTIONS, "corrige": "golden-actions.json",
                             "bareme": BAREME_A, "sortie": "actions.json"}]}

    def workspace(self, ws, *quoi):
        for nom, source in quoi:
            shutil.copy(CAS / source, os.path.join(ws, nom))

    def test_noeud_a_un_juge(self):
        with tempfile.TemporaryDirectory() as ws:
            self.workspace(ws, ('resultat.json', 'golden-formulaire.json'))
            rep = juge.juger_noeud(self.UN_JUGE, cas=CAS, workspace=ws)
        self.assertEqual((rep['score'], rep['bareme']), (BAREME_F, BAREME_F))

    def test_noeud_a_deux_juges_somme_les_baremes(self):
        with tempfile.TemporaryDirectory() as ws:
            self.workspace(ws, ('resultat.json', 'golden-formulaire.json'),
                           ('actions.json', 'golden-actions.json'))
            rep = juge.juger_noeud(self.DEUX_JUGES, cas=CAS, workspace=ws)
        self.assertEqual((rep['score'], rep['bareme']), (36, 36))
        self.assertEqual([j['sortie'] for j in rep['juges']], ['resultat.json', 'actions.json'])
        self.assertTrue(all(l.startswith('[resultat.json]') or l.startswith('[actions.json]')
                            for l in rep['lignes']))

    def test_noeud_deux_juges_une_sortie_absente_est_une_panne(self):
        with tempfile.TemporaryDirectory() as ws:
            self.workspace(ws, ('resultat.json', 'golden-formulaire.json'))
            with self.assertRaises(juge.JugeEnPanne) as cm:
                juge.juger_noeud(self.DEUX_JUGES, cas=CAS, workspace=ws)
        self.assertIn('actions.json', str(cm.exception))

    def test_noeud_presence(self):
        n = {"id": "x", "livrables": ["a.md", "b/"], "juge": {"type": "presence"}}
        with tempfile.TemporaryDirectory() as ws:
            rep = juge.juger_noeud(n, workspace=ws)
            self.assertEqual((rep['score'], rep['bareme']), (0, 20))
            ecrire(os.path.join(ws, 'a.md'), {"x": 1})
            ecrire(os.path.join(ws, 'b', 'c.md'), {"x": 1})
            rep = juge.juger_noeud(n, workspace=ws)
            self.assertEqual(rep['score'], 20)

    def test_cli_noeud(self):
        with tempfile.TemporaryDirectory() as ws:
            self.workspace(ws, ('resultat.json', 'golden-formulaire.json'))
            graphe = os.path.join(ws, 'graphe.json')
            ecrire(graphe, {"id": "g", "nodes": [self.UN_JUGE]})
            rc, out, err = cli('--noeud', graphe, 'remplir', '--cas', str(CAS), '--workspace', ws)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['score'], BAREME_F)


if __name__ == '__main__':
    unittest.main()
