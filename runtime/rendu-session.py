# -*- coding: utf-8 -*-
"""Une session terminee est un fichier, pas un processus.

La relecture rouvrait la session dans Claude Code : fidele, mais absurde — 500 Mo
de processus pour afficher un texte qui ne bougera plus. On rend donc le journal
en HTML, une fois, avec la meme grammaire que l'interface : une pastille par
tour, le nom de l'outil et ses arguments, le resultat en retrait.

Ce que la vraie session ne montre pas, celui-ci le montre : les sous-agents ont
leur propre journal, et il est repliable a cote du tour qui les a lances.

Usage : rendu-session.py <fichier.jsonl> [dossier-des-sous-agents] > sortie.html
"""
import json, sys, html, pathlib, re

MAX_RES = 12          # lignes de resultat avant repli, comme a l'ecran
MAX_ARG = 220         # caracteres d'argument montres

# L'argument qui identifie l'appel, dans l'ordre ou on le cherche. Le reste du
# JSON n'apprend rien a la lecture : c'est du bruit entre deux tours.
CLES = ('command', 'file_path', 'path', 'pattern', 'query', 'url', 'prompt',
        'description', 'notebook_path', 'skill', 'subagent_type')


def argument(inp):
    if not isinstance(inp, dict):
        return str(inp or '')
    for k in CLES:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace('\n', ' ⏎ ')
    return json.dumps(inp, ensure_ascii=False)


def lire(f):
    out = []
    for ligne in pathlib.Path(f).read_text(encoding='utf-8', errors='replace').splitlines():
        if not ligne.strip():
            continue
        try:
            out.append(json.loads(ligne))
        except Exception:
            continue
    return out


def ech(t):
    return html.escape(str(t if t is not None else ''))


def gras(t):
    """Le journal porte du markdown leger ; on ne rend que ce qui structure."""
    t = ech(t)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t, flags=re.S)
    t = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', t)
    return t


def bloc_resultat(txt):
    """Le resultat pend a son appel, comme le ⎿ de l'interface."""
    lignes = txt.rstrip().split('\n')
    if len(lignes) <= MAX_RES:
        return '<div class="res"><span class="fil">⎿</span>' + ech(txt.rstrip()) + '</div>'
    tete = '\n'.join(lignes[:MAX_RES])
    return ('<details class="res"><summary><span class="fil">⎿</span>' + ech(tete)
            + '\n<span class="plus">… +' + str(len(lignes) - MAX_RES)
            + ' lignes</span></summary>'
            + ech('\n'.join(lignes[MAX_RES:])) + '</details>')


def tours(entrees, agents_par_id):
    h = []
    for o in entrees:
        msg = o.get('message') or {}
        role = msg.get('role') or o.get('type') or ''
        cont = msg.get('content')
        if isinstance(cont, str):
            cont = [{'type': 'text', 'text': cont}]
        if not isinstance(cont, list):
            continue
        for c in cont:
            t = c.get('type')
            if t == 'text' and (c.get('text') or '').strip():
                cls = 'moi' if role == 'user' else 'lui'
                h.append('<div class="tour ' + cls + '">' + gras(c['text'].strip()) + '</div>')
            elif t == 'tool_use':
                nom = c.get('name') or 'outil'
                inp = c.get('input') or {}
                arg = argument(inp)
                court = arg if len(arg) <= MAX_ARG else arg[:MAX_ARG] + '…'
                d = inp.get('description') if isinstance(inp, dict) else None
                h.append('<div class="outil"><span class="pt"></span><b>' + ech(nom)
                         + '</b>(<span class="arg">' + ech(court) + '</span>)'
                         + ('<span class="desc"> · ' + ech(d) + '</span>'
                            if d and d != arg else '') + '</div>')
                # un Task lance un sous-agent : son journal se replie ici meme
                sa = agents_par_id.get(c.get('id'))
                if sa:
                    sa['pose'] = True
                    h.append('<details class="sous" id="sa-' + ech(sa['ancre'])
                             + '"><summary>journal du sous-agent · '
                             + ech(sa['titre']) + '</summary>' + sa['corps'] + '</details>')
            elif t == 'tool_result':
                v = c.get('content')
                txt = v if isinstance(v, str) else (
                    ' '.join(x.get('text', '') for x in v) if isinstance(v, list) else '')
                if txt.strip():
                    h.append(bloc_resultat(txt))
    return '\n'.join(h)


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1526;color:#cfe0ff;
  font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  padding:18px 20px 60px}
.tour{white-space:pre-wrap;word-break:break-word;margin:0 0 14px}
.tour.moi{border-left:3px solid #35507f;padding:8px 0 8px 12px;color:#a9bcda;background:#101a2e}
.tour.lui{position:relative;padding-left:16px}
.tour.lui::before{content:'●';position:absolute;left:0;top:0;color:#4ade80}
.outil{margin:0 0 6px;padding-left:16px;position:relative;color:#9ec5ff;
  word-break:break-word}
.outil .pt{position:absolute;left:0;top:0;color:#7fd6a8}
.outil .pt::before{content:'●';color:#7fd6a8}
.outil b{color:#cfe0ff}
.arg{color:#6f86ad}
.res, details.res{white-space:pre-wrap;word-break:break-word;margin:0 0 14px;
  padding-left:30px;position:relative;color:#8ea5c8}
.fil{position:absolute;left:12px;color:#35507f}
details.res summary{cursor:pointer;white-space:pre-wrap;list-style:none}
details.res summary::-webkit-details-marker{display:none}
.desc{color:#6f86ad}
details.res summary::marker{color:#35507f}
.plus{color:#7fb0ff}
details.sous{margin:0 0 14px 16px;border:1px solid #22314e;border-radius:9px;
  padding:8px 10px;background:#0b1322}
details.sous summary{cursor:pointer;color:#9ec5ff;font-weight:600}
details.sous[open]{padding-bottom:12px}
code{background:#16233b;border-radius:4px;padding:1px 4px}
b{color:#eaf1ff}
.somm{position:sticky;top:0;z-index:2;margin:-18px -20px 16px;padding:10px 20px;
  background:#0b1322;border-bottom:1px solid #1e2b45;
  display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.somm b{color:#8ea5c8;font-weight:600;margin-right:4px}
.somm a{color:#9ec5ff;text-decoration:none;border:1px solid #22314e;border-radius:7px;
  padding:2px 8px;font-size:12px}
.somm a:hover{border-color:#7fb0ff;color:#fff}
.vide{color:#6f86ad;padding:20px 0}
"""


def main():
    src = pathlib.Path(sys.argv[1])
    dossier = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None

    # les sous-agents d'abord : on les indexe par l'identifiant de l'appel d'outil
    agents = {}
    if dossier and dossier.is_dir():
        for f in sorted(dossier.glob('*.jsonl')):
            meta = f.with_suffix('.meta.json')
            titre, cle = f.stem, None
            try:
                m = json.loads(meta.read_text(encoding='utf-8'))
                titre = m.get('description') or m.get('name') or titre
                cle = m.get('toolUseId') or m.get('tool_use_id') or m.get('id')
            except Exception:
                pass
            corps = tours(lire(f), {})
            agents[cle or f.stem] = {'titre': titre, 'ancre': f.stem,
                                     'corps': corps or
                                     '<div class="vide">journal vide</div>'}

    corps = tours(lire(src), agents)
    # un sous-agent dont l'appel n'apparait pas dans le journal principal (tour
    # tronque, lancement indirect) n'a pas de place naturelle : il la trouve ici
    orphelins = [a for a in agents.values() if not a.get('pose')]
    reste = ''
    if orphelins:
        reste = ('<div class="tour lui">Sous-agents sans tour rattaché</div>'
                 + ''.join('<details class="sous" id="sa-' + ech(a['ancre'])
                           + '"><summary>journal du sous-agent · '
                           + ech(a['titre']) + '</summary>' + a['corps'] + '</details>'
                           for a in orphelins))

    # le sommaire : tous les journaux, atteignables sans derouler la page
    somm = ''
    if len(agents) > 1:
        liens = ''.join('<a href="#sa-' + ech(a['ancre']) + '">' + ech(a['titre']) + '</a>'
                        for a in agents.values())
        somm = ('<div class="somm"><b>' + str(len(agents)) + ' sous-agents</b>'
                + liens + '</div>')
    if not corps:
        corps = '<div class="vide">Ce journal ne contient aucun tour lisible.</div>'
    # un clic ouvre le journal vise : sans cela l'ancre pointe dans un pli ferme
    JS = ('<script>document.addEventListener("click",function(e){'
          'var a=e.target.closest&&e.target.closest(".somm a");if(!a)return;'
          'var d=document.querySelector(a.getAttribute("href"));if(!d)return;'
          'e.preventDefault();d.open=true;d.scrollIntoView({block:"start"});});</script>')
    print('<!doctype html><meta charset="utf-8"><style>' + CSS + '</style>'
          + '<body>' + somm + corps + reste + JS)


if __name__ == '__main__':
    main()
