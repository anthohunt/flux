#!/usr/bin/env python3
"""Un faux juge au contrat v2 : `juge.py <corrige> <sortie>` -> {score, bareme, lignes}.

Compare les cles de premier niveau du corrige a celles de la sortie : un point
par cle egale. Sert aux tests du runtime, pas a juger quoi que ce soit de vrai.
"""
import json, os, sys

corrige, sortie = sys.argv[1], sys.argv[2]
bareme = int(os.environ.get('FLUX_BAREME') or 10)
if os.environ.get('FAUX_JUGE_PLANTE') == '1':
    sys.exit('le faux juge plante exprès')
try:
    g = json.load(open(corrige)) if corrige != '-' else {}
    r = json.load(open(sortie))
except Exception as e:
    print(json.dumps({'score': 0, 'bareme': bareme, 'lignes': ['illisible : %s' % e]}))
    sys.exit(0)
lignes, ok = [], 0
for k, v in g.items():
    if r.get(k) == v:
        ok += 1; lignes.append('OK  %s' % k)
    else:
        lignes.append('NON %s attendu=%r obtenu=%r' % (k, v, r.get(k)))
score = round(bareme * ok / len(g), 2) if g else bareme
print(json.dumps({'score': int(score) if score == int(score) else score, 'bareme': bareme, 'lignes': lignes}))
