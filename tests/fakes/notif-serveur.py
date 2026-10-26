#!/usr/bin/env python3
"""Un faux serveur de notification (FLUX_NOTIF_URL) : chaque requete recue est
ecrite, une par ligne, dans le fichier donne en argument.

    notif-serveur.py <fichier-journal> [port]   -> affiche « PORT <n> » puis sert
"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

journal = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 0


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        with open(journal, 'a', encoding='utf-8') as f:
            f.write(self.path + '\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')

    do_POST = do_GET


srv = HTTPServer(('127.0.0.1', port), H)
print('PORT %d' % srv.server_port, flush=True)
srv.serve_forever()
