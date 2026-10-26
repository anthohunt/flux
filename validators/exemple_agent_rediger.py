"""Validateur d'exemple — nœud « rediger » du graphe `exemple-agent`.

Contrats :
  C1  `note.md` existe et n'est pas vide
  C2  la note commence par un titre de niveau 1
  C3  les trois sections attendues sont presentes, chacune non vide
"""
import pathlib
import re

SECTIONS = ("Ce qu'il faut retenir", 'Les points ouverts', 'La suite proposée')


def validate(workspace):
    f = pathlib.Path(workspace) / 'note.md'
    if not f.exists() or not f.read_text(encoding='utf-8').strip():
        return ['C1 note.md absent ou vide']
    texte = f.read_text(encoding='utf-8')
    erreurs = []
    if not re.match(r'\s*# \S', texte):
        erreurs.append('C2 la note ne commence pas par un titre « # … »')
    for titre in SECTIONS:
        m = re.search(r'^## ' + re.escape(titre) + r'\s*$(.*?)(?=^## |\Z)', texte, re.M | re.S)
        if not m:
            erreurs.append('C3 section « %s » absente' % titre)
        elif not m.group(1).strip():
            erreurs.append('C3 section « %s » vide' % titre)
    return erreurs
