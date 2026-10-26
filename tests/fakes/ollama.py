#!/usr/bin/env python3
"""Un faux Ollama : `POST /api/chat` repond ce que le SCENARIO dit, dans l'ordre.

    serveur = FauxOllama(['conforme', 'non-conforme', 'conforme'])
    serveur.demarrer()  ->  serveur.url
    serveur.arreter()

`conforme` rend un JSON qui valide le schema recu dans `format` (on remplit
chaque propriete requise avec une valeur du bon type) ; `non-conforme` rend un
JSON qui ne le valide pas ; `texte` rend de la prose sans JSON. Le scenario
epuise, le dernier element se repete. Chaque requete recue est gardee dans
`serveur.recues` (corps decode).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def valeur_pour(schema):
    t = schema.get('type')
    if 'enum' in schema:
        return schema['enum'][0]
    if t == 'string':
        p = schema.get('pattern') or ''
        if 'd{4}' in p:
            return '2026-09-04'
        if '[01]' in p:
            return '08:45'
        return 'valeur'
    if t in ('number', 'integer'):
        return schema.get('minimum', 1) or 1
    if t == 'boolean':
        return True
    if t == 'array':
        return [valeur_pour(schema.get('items') or {'type': 'string'})]
    if t == 'object':
        return {k: valeur_pour(v) for k, v in (schema.get('properties') or {}).items()
                if k in (schema.get('required') or list((schema.get('properties') or {}).keys()))}
    return 'valeur'


class FauxOllama:
    def __init__(self, scenario=('conforme',), modele='faux'):
        self.scenario = list(scenario) or ['conforme']
        self.recues = []
        self.modele = modele
        self._srv = None
        self._th = None
        self.url = None

    def demarrer(self):
        faux = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                corps = json.dumps({'models': [{'name': faux.modele}]}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.end_headers(); self.wfile.write(corps)

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                d = json.loads(self.rfile.read(n) or b'{}')
                faux.recues.append(d)
                i = min(len(faux.recues) - 1, len(faux.scenario) - 1)
                geste = faux.scenario[i]
                schema = d.get('format') if isinstance(d.get('format'), dict) else None
                if geste == 'conforme':
                    contenu = json.dumps(valeur_pour(schema) if schema else {'ok': True})
                elif geste == 'non-conforme':
                    contenu = json.dumps({'pas_la_bonne_cle': 42})
                elif geste == 'bloc':
                    contenu = '<think>je reflechis</think>\n```json\n' + json.dumps(
                        valeur_pour(schema) if schema else {'ok': True}) + '\n```'
                else:
                    contenu = 'Je ne sais pas repondre en JSON, desole.'
                rep = {'model': d.get('model'), 'message': {'role': 'assistant', 'content': contenu},
                       'done': True, 'total_duration': 1_500_000_000, 'prompt_eval_count': 120,
                       'eval_count': 30}
                corps = json.dumps(rep).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.end_headers(); self.wfile.write(corps)

        self._srv = HTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self._srv.server_port
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()
        return self.url

    def arreter(self):
        if self._srv:
            self._srv.shutdown(); self._srv.server_close()
