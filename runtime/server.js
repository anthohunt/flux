#!/usr/bin/env node
/**
 * Flux — serveur du framework de workflows.
 *
 * Sert la vue du graphe et exécute UN nœud à la demande (bouton ▶).
 * Le workflow JSON est la vérité ; l'UI n'en est qu'un rendu.
 *
 * Routes :
 *   GET  /                      → UI
 *   GET  /api/workflows         → liste des workflows
 *   GET  /api/wf/<id>           → le workflow + l'état de chaque nœud (+ workspace actif)
 *   POST /api/wf/<id>/nouvelle-run → ouvre un workspace neuf (champs de `ouverture`)
 *   POST /api/wf/<id>/run/<nid> → lance le nœud (spawn run-node.py), non bloquant
 *   POST /api/wf/<id>/clore/<nid> → constate la fin d'un nœud sans surveillant
 *   DELETE /api/wf/<id>/passage/<nid>?pass=N → efface un passage et sa trace
 *   GET  /api/wf/<id>/log/<nid> → dernières lignes de sortie du nœud
 *   GET  /api/wf/<id>/mesures/<nid> → par passe : temps, tours de parole, jetons
 *   GET  /api/moteurs/ping?wf=<id> → les moteurs API du graphe, joignables ou non
 *   POST /api/wf/<id>/valider?noeud=<nid>&f=<livrable> → le corrige devient un cas de banc
 *   ANY  /term/<id>/<nid>       → terminal attache (ttyd sur tmux attach)
 *   GET  /rendu/<id>/<nid>      → la session terminee, rendue en page (cache)
 */
const http = require('http');
const fs   = require('fs');
const path = require('path');
const { spawn, execFile, execFileSync } = require('child_process');

const FLUX   = path.resolve(__dirname, '..');
// FLUX_WORKFLOWS_DIR : un ou plusieurs dossiers de graphes, separes par « : » ; le premier gagne.
const WFDIRS = (process.env.FLUX_WORKFLOWS_DIR || '').split(':').filter(Boolean)
  .map(d => d.replace(/^~(?=\/|$)/, process.env.HOME || require('os').homedir()));
if (!WFDIRS.length) WFDIRS.push(path.join(FLUX, 'workflows'));
const WFDIR  = WFDIRS[0];
/** Les fichiers de graphes visibles : { 'id.json': chemin complet }, le premier dossier gagne. */
function fichiersWf() {
  const out = {};
  for (const d of WFDIRS) {
    let noms = [];
    try { noms = fs.readdirSync(d).filter(x => x.endsWith('.json')); } catch { continue; }
    for (const f of noms) if (!(f in out)) out[f] = path.join(d, f);
  }
  return out;
}
function cheminWf(id) { return fichiersWf()[`${id}.json`] || path.join(WFDIR, `${id}.json`); }
const STATE  = process.env.FLUX_STATE_DIR || path.join(FLUX, 'state');
const PUB    = path.join(FLUX, 'public');
const RUNNER = path.join(FLUX, 'runtime', 'run-node.py');
const RENDU  = path.join(FLUX, 'runtime', 'rendu-session.py');
const os     = require('os');
// Reglages de la machine (voir .env.example) : rien de propre a un serveur n'est ecrit en dur.
const HOME_DIR  = process.env.HOME || os.homedir();
const RUNS_HOME = process.env.FLUX_RUNS_HOME || HOME_DIR;
const CFG_DEFAUT = process.env.CLAUDE_CONFIG_DIR || path.join(HOME_DIR, '.claude');
const CLAUDE_BIN = process.env.FLUX_CLAUDE_BIN || 'claude';
// Proprietaire des fichiers ecrits par la console quand elle tourne sous un autre
// utilisateur que les agents (ex. FLUX_PROPRIETAIRE=agents:agents) ; vide = rien.
const PROPRIETAIRE = process.env.FLUX_PROPRIETAIRE || '';
const developper = p => String(p).replace(/^~(?=\/|$)/, HOME_DIR).replace(/\$\{?(\w+)\}?/g, (m, k) => process.env[k] || m);
/** La racine des runs d'un graphe (meme regle que socle.workspace_root). */
function wsRoot(wf) { return wf.workspace_root ? developper(wf.workspace_root) : path.join(RUNS_HOME, `${wf.id}-runs`); }
/** moteurs.json (reglages locaux), sinon moteurs.example.json. */
function fichierMoteurs() { if (process.env.FLUX_MOTEURS) return process.env.FLUX_MOTEURS; const f = fichierMoteurs(); return fs.existsSync(f) ? f : path.join(FLUX, 'moteurs.example.json'); }
function chownAgents(f) { if (PROPRIETAIRE) { try { execFileSync('chown', [PROPRIETAIRE, f]); } catch {} } }
// Le noeud juge d'un graphe : le bout de la chaine `suivant` depuis le premier noeud.
// (agenda-v5 : evenements -> relecture ; c'est la relecture qui est notee.)
// Les familles de workflows : `agenda`, `agenda-v3`, `agenda-v4`… vont ensemble.
function baseDe(id) { return String(id).replace(/-v\d+$/, ''); }
function familles() {
  const out = {};
  for (const f of Object.keys(fichiersWf())) {
    const id = f.replace(/\.json$/, '');
    (out[baseDe(id)] = out[baseDe(id)] || []).push(id);
  }
  for (const k of Object.keys(out)) out[k].sort((a, b) => (a === k ? -1 : b === k ? 1 : a.localeCompare(b, undefined, { numeric: true })));
  return out;
}
// L'etiquette de la version des consignes d'un graphe : `version` dans le graphe,
// sinon le suffixe -vN de l'id, sinon v1.
function versionDe(wf, id) { if (wf && wf.version) return String(wf.version); const m = String(id).match(/-v(\d+)$/); return m ? 'v' + m[1] : 'v1'; }
// Les runs (campagnes) d'une famille, une entree par (campagne, modele), les plus recentes d'abord.
function runsDeFamille(base, ids) {
  let tailles = {};
  try { const carte = JSON.parse(fs.readFileSync(path.join(FLUX, 'cas', 'cas.json'), 'utf8')); for (const c of carte.cas || []) tailles[c.lot] = (tailles[c.lot] || 0) + 1; } catch {}
  const runs = [];
  for (const id of ids) {
    const wf = loadWf(id); if (!wf) continue;
    const cdir = path.join(STATE, id, 'campagnes');
    let fichiers = []; try { fichiers = fs.readdirSync(cdir).filter(x => x.endsWith('.json')); } catch {}
    for (const f of fichiers) {
      let c; try { c = JSON.parse(fs.readFileSync(path.join(cdir, f), 'utf8')); } catch { continue; }
      const bareme = ((c.runs || []).find(r => r.bareme) || {}).bareme || null;
      for (const [modele, b] of Object.entries(c.bilan || {})) {
        if (b.moyenne == null) continue;
        const mails = (c.runs || []).filter(r => r.modele === modele);
        runs.push({ wf: id, version: versionDe(wf, id), nom: c.nom, lot: c.lot, debut: c.debut || '', fin: c.fin || null,
                    modele, note: b.moyenne, bareme, n: b.n, pannes: b.pannes || 0, attendus: tailles[c.lot] || null,
                    s_par_mail: b.s_par_mail || 0, jetons_entree: b.jetons_entree || 0, md5: c.consignes_md5 || '', noeuds: c.noeuds || null,
                    mails: mails.map(r => ({ cas: r.cas, score: r.score ?? null, duree_s: r.duree_s ?? null, panne: r.panne || null, run: r.run || null })) });
      }
    }
  }
  runs.sort((a, b) => String(b.debut).localeCompare(String(a.debut)));
  return runs;
}

function dernierNoeud(wf) {
  const par = new Map((wf.nodes || []).map(n => [n.id, n]));
  let cur = (wf.nodes || [])[0], vus = new Set();
  while (cur && !vus.has(cur.id)) {
    vus.add(cur.id);
    const s = (cur.suivant || [])[0];
    if (!s || !par.has(s)) break;
    cur = par.get(s);
  }
  return cur ? cur.id : '';
}

const MESURER = path.join(FLUX, 'runtime', 'mesures-noeud.py');
// cache des mesures, cle = fichier d'etat + sa date : un passage de plus le vide
const MESURES = new Map();
// pastilles des moteurs API : un ping par moteur, 30 s de cache
const PING_CACHE = new Map();
const PORT   = process.env.FLUX_PORT || 8901;
// La console n'a pas d'authentification : elle n'ecoute que la machine locale par
// defaut. FLUX_HOTE=0.0.0.0 l'ouvre au reseau (a reserver a un reseau prive).
const HOTE   = process.env.FLUX_HOTE || '127.0.0.1';
// doit rester aligné avec TERM_COLS/TERM_ROWS de run-node.py
const TERM_COLS = 160, TERM_ROWS = 45;

const send = (res, code, type, body) => {
  res.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store' });
  res.end(body);
};
const json = (res, code, obj) => send(res, code, 'application/json; charset=utf-8', JSON.stringify(obj));

function loadWf(id) {
  const p = cheminWf(id);
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

// ── L'USAGE D'UN COMPTE ─────────────────────────────────────────────────────
// Endpoint OAuth non documente : il peut disparaitre sans preavis, donc toute
// erreur rend null et l'interface s'en passe. Le rate-limit est par TOKEN et
// agressif — cache de 180 s, et le User-Agent claude-code est obligatoire
// (sans lui, 429 immediat).
const USAGE_CACHE = new Map();          // config_dir -> { t, v }
const USAGE_TTL = 180_000;
const USAGE_TTL_ERR = 20_000;   // un echec se recontrole vite : le jeton peut revenir a tout moment
let CLAUDE_VER = null;

function versionClaude() {
  if (CLAUDE_VER) return CLAUDE_VER;
  try {
    CLAUDE_VER = (execFileSync(CLAUDE_BIN, ['--version'], { encoding: 'utf8' })
                  .match(/[0-9]+\.[0-9]+\.[0-9]+/) || ['2.0.0'])[0];
  } catch { CLAUDE_VER = '2.0.0'; }
  return CLAUDE_VER;
}

/** Le compte auquel un dossier de config est REELLEMENT connecte.
 *  `moteurs.json` declare un compte par moteur, mais rien ne verifiait que le
 *  dossier y soit connecte : un dossier re-authentifie ailleurs continuait
 *  d'afficher l'ancien nom, et deux moteurs presentes comme distincts tiraient sur
 *  le meme quota — leurs jauges bougeaient ensemble sans qu'on comprenne pourquoi.
 *  Rend null quand on ne peut pas lire : on n'affirme pas ce qu'on n'a pas vu. */
function adresseConnectee(configDir) {
  try {
    const c = JSON.parse(fs.readFileSync(path.join(configDir, '.claude.json'), 'utf8'));
    return (c.oauthAccount && c.oauthAccount.emailAddress) || null;
  } catch { return null; }
}

// La jauge de quota lit un point d'acces d'Anthropic non documente, avec les
// identifiants locaux du moteur : desactivee sauf FLUX_JAUGE_QUOTA=1.
async function usageMoteur(configDir) {
  if (process.env.FLUX_JAUGE_QUOTA !== '1') return null;
  const hit = USAGE_CACHE.get(configDir);
  const ttl = (hit && hit.v && hit.v.erreur) ? USAGE_TTL_ERR : USAGE_TTL;
  if (hit && Date.now() - hit.t < ttl) return hit.v;
  let v = null;
  try {
    const c = JSON.parse(fs.readFileSync(path.join(configDir, '.credentials.json'), 'utf8'))
                  .claudeAiOauth;
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('https://api.anthropic.com/api/oauth/usage', {
      headers: { 'Authorization': 'Bearer ' + c.accessToken,
                 'anthropic-beta': 'oauth-2025-04-20',
                 'User-Agent': 'claude-code/' + versionClaude(),
                 'Content-Type': 'application/json' },
      signal: ctrl.signal });
    clearTimeout(to);
    if (r.ok) {
      const d = await r.json();
      const pct = x => (x && typeof x.utilization === 'number') ? Math.round(x.utilization) : null;
      v = { session: pct(d.five_hour), session_reset: d.five_hour && d.five_hour.resets_at,
            semaine: pct(d.seven_day), semaine_reset: d.seven_day && d.seven_day.resets_at };
    } else if (r.status === 401) {
      // Le jeton d'acces dure quelques heures ; celui de rafraichissement, des semaines.
      // Une session Claude lancee sur ce compte le renouvelle — donc la prochaine run
      // suffit a faire revenir les chiffres. Ce qui compte est de ne pas confondre
      // « ce compte n'a pas de donnees » avec « ce compte a un jeton perime ».
      v = { erreur: 'jeton' };
    } else {
      v = { erreur: 'http', code: r.status };
    }
  } catch { v = { erreur: 'reseau' }; }
  USAGE_CACHE.set(configDir, { t: Date.now(), v });
  return v;
}

/** Toutes les runs d'un workflow : les dossiers portant WORKSPACE.txt sous sa
 *  racine, du plus recent au plus ancien. */
function tousWs(wf) {
  const root = wsRoot(wf);
  return fs.existsSync(root)
    ? fs.readdirSync(root).map(n => path.join(root, n))
        .filter(d => fs.existsSync(path.join(d, 'WORKSPACE.txt')))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)
    : [];
}
/** L'entete d'une run, telle que l'ouverture l'a ecrite : des `cle: valeur`. */
function champsDe(dir) {
  const out = {};
  try {
    const t = fs.readFileSync(path.join(dir, 'WORKSPACE.txt'), 'utf8');
    t.split(/\r?\n/).forEach(l => {
      const i = l.indexOf(':');
      if (i > 0) out[l.slice(0, i).trim()] = l.slice(i + 1).trim();
    });
  } catch (e) { /* run sans entete : on rendra ce qu'on peut */ }
  return out;
}
/** Ce que la run EST, au-dela de ses nœuds : son mode (banc | production) et
 *  son statut, ecrits dans `state/<wf>/<run>/run.json` a l'ouverture. Les runs
 *  d'avant n'ont pas ce fichier : on lit alors l'entete, puis rien — un champ
 *  absent est un champ vide, jamais une panne. */
function infoRun(wf, dir) {
  let r = {};
  try { r = JSON.parse(fs.readFileSync(path.join(STATE, wf.id, path.basename(dir), 'run.json'), 'utf8')) || {}; }
  catch (e) { r = {}; }
  const mode = r.mode || champsDe(dir).mode || null;
  const prm = r.parametres || r.champs || {};
  // LA NOTE, LE COUT ET LA DUREE de la run : la somme des dernieres passes de
  // ses nœuds. C'est ce qui distingue deux runs du meme cas dans la liste —
  // « 15:40 · banc · mail-01 » ne dit rien, « qwen3:4b · 19/30 · 0 $ · 4 s » dit tout.
  let score = 0, bareme = 0, cout = 0, duree = 0, notee = false;
  try {
    const dir2 = path.join(STATE, wf.id, path.basename(dir));
    for (const f of fs.readdirSync(dir2)) {
      if (!f.endsWith('.json') || f === 'run.json' || f === 'etat-compact.json') continue;
      let st = {}; try { st = JSON.parse(fs.readFileSync(path.join(dir2, f), 'utf8')); } catch { continue; }
      const ps = st.passages || []; if (!ps.length) continue;
      for (const x of ps) { const m = x.mesures || {}; cout += m.cout_usd || 0; duree += m.duree_s || 0; }
      const n = ps[ps.length - 1].note;
      if (n && n.score != null) { score += n.score; bareme += n.bareme || 0; notee = true; }
    }
  } catch {}
  // LE TITRE d'une run de production : l'objet du premier message de son
  // entree (mail.md), sinon rien — la file de production se lit par sujet,
  // pas par horodatage.
  let titre = null;
  try {
    const prem = (wf.nodes || [])[0] || {};
    for (const e of (prem.entree || [])) {
      const f = path.join(dir, e); if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) continue;
      const txt = fs.readFileSync(f, 'utf8').slice(0, 4000);
      const m = txt.match(/^(?:Objet|Subject)\s*:\s*(.+)$/mi) || txt.match(/^#\s+(.+)$/m);
      if (m) { titre = m[1].trim().slice(0, 90); break; }
    }
  } catch {}
  return { mode, statut: r.statut || null, cas: r.cas || prm.cas || null, titre, relu: r.relu || null, test: prm.test || null,
           modele: prm.modele || null,
           note: notee ? { score: Math.round(score * 10) / 10, bareme } : null,
           cout: Math.round(cout * 1000) / 1000, duree: Math.round(duree) };
}

// ── LES SOURCES D'OPTIONS D'UNE OUVERTURE ───────────────────────────────────
// Un champ du dialogue « nouvelle run » peut declarer `options_de: <nom>` : sa
// valeur se CHOISIT dans une liste, elle ne se tape pas. D'ou vient la liste,
// c'est le GRAPHE qui le dit, dans `ouverture.sources[<nom>]` — le serveur ne
// connait que trois formes generiques :
//   { type: "dossiers", dossier, exclure? }
//       les sous-dossiers d'un chemin ; valeur = libelle = le nom du dossier
//   { type: "fichiers-json", dossier, fichiers[], valeur, libelle, groupe?,
//     paire?, distinct?, prime_runs? }
//       un sous-dossier = une option ; ses champs sont ceux du premier fichier
//       de `fichiers` qui existe dedans, plus `@dossier` (le nom du dossier).
//       `valeur`, `libelle`, `groupe` et les valeurs de `paire` nomment un de
//       ces champs (ou une liste : le premier qui porte une valeur) ;
//       `distinct` ne garde qu'une option par valeur ;
//       `prime_runs: {cle, champ, remplace}` : la run la plus recente dont
//       l'entete porte cle == @dossier remplace le champ `remplace` par son
//       champ `champ` (ce que les runs ont reellement ecrit prime le profil).
//   { type: "cas" } (ou `options_de: "cas"` sans declaration)
//       les dossiers de cas/ hors `_*` : un cas de banc = une entree + un corrige
// Une option rendue : {valeur, libelle, groupe?, paire?}. La page filtre par
// `groupe` quand un champ porte `filtre`, et aligne les champs nommes par `paire`.
const DOSSIER_CAS = path.join(FLUX, 'cas');
function sousDossiers(dossier, exclure) {
  const rx = exclure ? new RegExp(exclure) : null;
  try {
    return fs.readdirSync(dossier)
      .filter(d => !d.startsWith('.') && !(rx && rx.test(d))
                   && fs.statSync(path.join(dossier, d)).isDirectory())
      .sort();
  } catch (e) { return []; }
}
function listeCas() {
  return sousDossiers(DOSSIER_CAS, '^_').sort((a, b) => b.localeCompare(a))
    .map(d => ({ valeur: d, libelle: d }));
}
function optionsFichiersJson(wf, src) {
  const runs = src.prime_runs ? tousWs(wf).map(champsDe) : [];
  const out = [], vus = new Set();
  const dossierSrc = developper(src.dossier || '');
  for (const d of sousDossiers(dossierSrc, src.exclure)) {
    let c = null;
    for (const f of (src.fichiers || [])) {
      try { c = JSON.parse(fs.readFileSync(path.join(dossierSrc, d, f), 'utf8')); break; }
      catch (e) { /* pas ce fichier : le suivant */ }
    }
    if (!c) continue;
    const champs = { ...c, '@dossier': d };
    if (src.prime_runs) {
      const p = src.prime_runs;
      const r = runs.find(x => x[p.cle] === d && x[p.champ]);
      if (r) champs[p.remplace] = r[p.champ];
    }
    // un nom de champ, ou une liste : le premier qui porte une valeur
    const lire = k => {
      for (const c of (Array.isArray(k) ? k : [k]))
        if (c && champs[c] != null && String(champs[c]).trim()) return String(champs[c]).trim();
      return '';
    };
    const valeur = lire(src.valeur);
    if (!valeur) continue;
    if (src.distinct) { if (vus.has(valeur)) continue; vus.add(valeur); }
    const libelle = lire(src.libelle) || valeur;
    const o = { valeur, libelle: src.distinct ? `${libelle}  (${valeur})` : libelle };
    if (src.groupe) o.groupe = lire(src.groupe);
    if (src.paire) {
      o.paire = {};
      for (const [k, v] of Object.entries(src.paire)) o.paire[k] = lire(v);
    }
    out.push(o);
  }
  return src.distinct ? out : out.sort((a, b) => a.libelle.localeCompare(b.libelle, 'fr'));
}
function optionsDe(wf, nom) {
  const src = ((ouvertureDe(wf) || {}).sources || {})[nom] || (nom === 'cas' ? { type: 'cas' } : null);
  if (!src) return null;
  if (src.type === 'cas') return listeCas();
  if (src.type === 'dossiers') return sousDossiers(developper(src.dossier || ''), src.exclure).map(d => ({ valeur: d, libelle: d }));
  if (src.type === 'fichiers-json') return optionsFichiersJson(wf, src);
  return null;
}
/** Le corps d'une requete, en texte. */
function lireCorps(req) {
  return new Promise((res, rej) => { let b = ''; req.on('data', c => b += c); req.on('end', () => res(b)); req.on('error', rej); });
}
/** L'ouverture d'un graphe v2 qui ne declare que `parametres_run` (design 3.1) :
 *  la console en derive le dialogue. Un parametre `fichier` se colle en texte
 *  (multiligne) ; le serveur l'ecrit dans un fichier a l'ouverture. Un parametre
 *  qui porte `mode` n'est demande que dans ce mode. */
function ouvertureDe(wf) {
  if (wf.ouverture) return wf.ouverture;
  const prms = wf.parametres_run || [];
  if (!prms.length) return null;
  const avecCas = prms.some(x => x.id === 'cas');
  return {
    titre: 'Ouvrir une run', derivee: true,
    slug: avecCas ? `${wf.id}-{cas}` : wf.id,
    champs: prms.map(x => ({
      cle: x.id, libelle: x.label || x.id, requis: x.requis !== false,
      defaut: x.defaut, options_de: x.options_de,
      options: Array.isArray(x.options) ? x.options.map(o => ({ valeur: o, libelle: o })) : undefined,
      multiligne: x.type === 'fichier', fichier: x.type === 'fichier',
      destination: x.destination, mode: x.mode })),
  };
}
/** L'ouverture du graphe, ses listes remplies. */
function ouvertureRemplie(wf) {
  const ouv = ouvertureDe(wf);
  if (!ouv) return ouv;
  return { ...ouv,
    champs: (ouv.champs || []).map(c => {
      if (!c.options_de) return c;
      const options = optionsDe(wf, c.options_de);
      return options ? { ...c, options } : c;
    }) };
}

/** Le workspace regarde : celui que l'onglet demande, sinon le plus recent.
 *  Sans parametre `ws`, le comportement est celui d'avant les onglets. */
function activeWs(wf, u) {
  const l = tousWs(wf);
  const voulu = u && u.searchParams.get('ws');
  if (voulu) {
    const hit = l.find(d => path.basename(d) === path.basename(voulu));
    if (hit) return hit;
  }
  return l[0] || null;
}

// ── LA PASSE, AU CONTRAT V2 ─────────────────────────────────────────────────
// Le runtime v2 garde `state/<wf>/<run>/<nid>.json` et AJOUTE a chaque entree
// de `passages[]` : moteur, modele, entree[], sortie[], note (null ou {score,
// bareme, type, detail | lignes}), garde (null ou {drapeau, motif}). C'est la
// source canonique. Un fichier `passes/<n>.json` portant les memes champs est
// lu en repli s'il existe. Ce qui manque est vide, jamais une erreur.
function passeV2(wid, ws, nid, n, passage) {
  const base = passage || {};
  let f = {};
  try {
    f = JSON.parse(fs.readFileSync(
      path.join(STATE, wid, path.basename(ws), nid, 'passes', String(n) + '.json'), 'utf8')) || {};
  } catch (e) { f = {}; }
  const prend = k => (base[k] !== undefined && base[k] !== null) ? base[k] : (f[k] !== undefined ? f[k] : null);
  const note = prend('note'), garde = prend('garde'), entree = prend('entree'), sortie = prend('sortie');
  return {
    note: (note && typeof note === 'object') ? note : null,
    garde: (garde && typeof garde === 'object') ? garde : null,
    entree: Array.isArray(entree) ? entree : null,
    sortie: Array.isArray(sortie) ? sortie : null,
    moteur_type: prend('moteur'),
    modele: prend('modele'),
  };
}
/** Les fichiers d'entree et de sortie d'une passe : ceux qu'elle a ecrits,
 *  sinon ceux que le nœud declare (`entree[]`, `livrables[]`), resolus. */
function entreeSortie(wf, ws, nid, v2) {
  const n = (wf.nodes || []).find(x => x.id === nid) || {};
  const entree = v2.entree || (n.entree || []).flatMap(d => fichiersDuLivrable(ws, d).length
                                                            ? fichiersDuLivrable(ws, d) : [d]);
  const sortie = v2.sortie || (n.livrables || []).flatMap(d => fichiersDuLivrable(ws, d));
  return { entree, sortie };
}

function nodeState(wf, ws, nid) {
  if (!ws) return { statut: 'inconnu' };
  const p = path.join(STATE, wf.id, path.basename(ws), `${nid}.json`);
  if (!fs.existsSync(p)) return { statut: 'a-faire' };
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return { statut: 'inconnu' }; }
}

/** un livrable est-il présent ? (fichier non vide / dossier non vide / glob) */
function present(ws, pat) {
  const p = path.join(ws, pat);
  try {
    if (pat.endsWith('/')) return fs.existsSync(p) && fs.readdirSync(p).length > 0;
    if (pat.includes('*')) {
      const dir = path.dirname(p), base = path.basename(pat);
      if (!fs.existsSync(dir)) return false;
      const rx = new RegExp('^' + base.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
      return fs.readdirSync(dir).some(f => rx.test(f));
    }
    return fs.existsSync(p) && fs.statSync(p).size > 0;
  } catch { return false; }
}

/** Les fichiers REELS que couvre un motif de livrable, chemins relatifs au ws.
 *  Le graphe declare des motifs (`pistes-*.md`, `verifications/etape-2/`) ; la
 *  console a besoin de la liste, pas du motif -- sinon un lien pointe sur une
 *  etoile. Un dossier rend ce qu'il contient, a plat, sans descendre : un
 *  livrable est une chose qu'on ouvre, pas une arborescence a explorer. */
function fichiersDuLivrable(ws, pat) {
  const p = path.join(ws, pat);
  try {
    if (pat.endsWith('/')) {
      if (!fs.existsSync(p)) return [];
      return fs.readdirSync(p)
        .filter(f => { try { return fs.statSync(path.join(p, f)).isFile(); } catch { return false; } })
        .sort().map(f => pat + f);
    }
    if (pat.includes('*')) {
      const dir = path.dirname(p), base = path.basename(pat), rel = path.dirname(pat);
      if (!fs.existsSync(dir)) return [];
      const rx = new RegExp('^' + base.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
      return fs.readdirSync(dir).filter(f => rx.test(f)).sort()
        .map(f => (rel === '.' ? f : rel + '/' + f));
    }
    return (fs.existsSync(p) && fs.statSync(p).isFile()) ? [pat] : [];
  } catch { return []; }
}

const running = new Set();

// ---- Le terminal attache : un ttyd par session tmux vivante.
// On ne recopie pas l'ecran, on s'attache — c'est la seule facon d'avoir un
// defilement, l'ecran alterne ne laissant aucun historique a capturer.
const net = require('net');
// `tmux -f` n'est lu qu'au tout premier demarrage du serveur tmux ; celui de
// l'agent tourne deja. On lui applique donc la conf a chaud, sans quoi la
// molette resterait inerte dans la console.
// FLUX_SANS_TMUX : un serveur de test (autre port, autre copie du depot) ne
// touche ni au serveur tmux de l'utilisateur ni aux ttyd du serveur en service.
if (!process.env.FLUX_SANS_TMUX) {
  try { execFileSync('tmux', ['source-file', path.join(FLUX, 'tmux.conf')], { stdio: 'ignore' }); }
  catch (e) { /* pas de serveur tmux encore : la session le fera naitre avec -f */ }
  // Au demarrage, la table des terminaux est vide : ceux qui tournent encore sont
  // orphelins, et leurs ports feraient echouer les suivants.
  try {
    execFileSync('pkill', ['-f', 'ttyd .*-b /flux/(term|relire)/'], { stdio: 'ignore' });
  } catch (e) { /* aucun a balayer */ }
}
const ttyds = new Map();
// ttyd demande son jeton sur la racine de sa base, donc sans le numero de
// passage : on retient le terminal ouvert pour ce nœud afin de le lui servir.
const dernierTerm = new Map();          // session tmux -> { port, proc }
let portTtyd = 7150;
// Un compteur seul ne suffit pas : il repart a 7150 a chaque redemarrage. On
// n'accepte qu'un port que personne n'ecoute.
function portLibre() {
  for (let essais = 0; essais < 200; essais++) {
    const port = portLibre();
    if (portTtyd > 7400) portTtyd = 7150;
    try {
      execFileSync('sh', ['-c', `ss -ltn 'sport = :${port}' | grep -q LISTEN`], { stdio: 'ignore' });
    } catch { return port; }          // grep sans resultat : le port est libre
  }
  return portTtyd++;
}
function ensureTtyd(sess, base) {
  const t = ttyds.get(sess);
  if (t && !t.proc.killed) return t;
  const port = portTtyd++;
  // pas de -W : lecture seule. -i 127.0.0.1 : jamais expose hors de la machine.
  // -W : les touches remontent. Sans elles, pas de molette, donc pas de
  // defilement — et c'est tout l'objet de la manoeuvre.
  const proc = spawn('ttyd', ['-p', String(port), '-i', '127.0.0.1', '-W', '-b', base,
                              // L'agent tourne en rendu CLASSIQUE (pas d'ecran
                              // alterne) : tout son fil arrive dans le scrollback,
                              // donc xterm.js a de quoi remonter — barre, molette
                              // et glissement tactile compris.
                              '-t', 'scrollback=20000',
                              'tmux', '-u', '-f', path.join(FLUX, 'tmux.conf'),
                              'attach', '-t', sess],
                     { stdio: 'ignore' });
  proc.on('exit', () => ttyds.delete(sess));
  const n = { port, proc };
  ttyds.set(sess, n);
  return n;
}
function attendrePort(port, restant) {
  return new Promise(ok => {
    const essai = () => {
      const c = net.connect(port, '127.0.0.1');
      c.on('connect', () => { c.destroy(); ok(true); });
      c.on('error', () => { c.destroy(); (restant-- > 0) ? setTimeout(essai, 120) : ok(false); });
    };
    essai();
  });
}
// ---- Relire une session terminee : Claude Code la redessine lui-meme.
const relectures = new Map();     // cle -> nom de session tmux
// ── LE RAPPORT D'UNE PASSE, EN PAGE ────────────────────────────────────────
// Un rapport de passe est un markdown court, ecrit par l'agent : un en-tete que
// lit une machine, un texte que lit un humain. On le rend avec la grammaire de
// la console — pas de bibliotheque, il n'y a que six formes a reconnaitre.
function mdEnHtml(md) {
  const E = t => String(t).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  // le gras, le code et les liens survivent a l'echappement, le reste non
  const inline = t => E(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<i>$1</i>');

  const lignes = md.split('\n');
  let out = '', i = 0, entete = null;

  // L'en-tete YAML : ce que la machine lit. On le rend en etiquettes, parce
  // qu'un bloc de `cle: valeur` brut se saute a la lecture.
  if (lignes[0] && lignes[0].trim() === '---') {
    const fin = lignes.indexOf('---', 1);
    if (fin > 0) {
      const MUETS = new Set(['noeud', 'passe', 'redige_par']);
      entete = lignes.slice(1, fin).map(l => {
        const m = l.match(/^([\w-]+)\s*:\s*(.*)$/); return m ? [m[1], m[2]] : null;
      }).filter(x => x && !MUETS.has(x[0]));
      i = fin + 1;
    }
  }
  let meta = '';
  if (entete && entete.length)
    meta = '<div class="meta">' + entete.map(([k, v]) =>
      `<span><i>${E(k.replace(/_/g, ' '))}</i><b>${E(v.replace(/^["']|["']$/g, '')) || '—'}</b></span>`)
      .join('') + '</div>';

  let liste = false, tab = false;   // liste vaut 'ul' ou 'ol' quand une liste est ouverte
  const clore = () => { if (liste) { out += `</${liste}>`; liste = false; }
                        if (tab) { out += '</tbody></table></div>'; tab = false; } };
  for (; i < lignes.length; i++) {
    const l = lignes[i], t = l.trim();
    if (!t) { clore(); continue; }
    if (/^```/.test(t)) {                       // un bloc de code, tel quel
      const bloc = []; i++;
      while (i < lignes.length && !/^```/.test(lignes[i].trim())) bloc.push(lignes[i++]);
      clore(); out += `<pre>${E(bloc.join('\n'))}</pre>`; continue;
    }
    if (/^---+$/.test(t)) { clore(); out += '<hr>'; continue; }
    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) { clore(); const n = h[1].length; out += `<h${n}>${inline(h[2])}</h${n}>`; continue; }
    if (/^\|.*\|$/.test(t)) {                   // un tableau
      const cel = t.slice(1, -1).split('|').map(c => c.trim());
      if (cel.every(c => /^:?-{2,}:?$/.test(c))) continue;   // la ligne de tirets
      if (!tab) { clore(); out += '<div class="tbl"><table><tbody>'; tab = true;
                  out += '<tr>' + cel.map(c => `<th>${inline(c)}</th>`).join('') + '</tr>'; continue; }
      out += '<tr>' + cel.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>'; continue;
    }
    const puce = /^[-*]\s+/.test(t), num = /^\d+[.)]\s+/.test(t);
    if (puce || num) {
      const bal = puce ? 'ul' : 'ol';
      if (tab) clore();
      if (liste && liste !== bal) { out += `</${liste}>`; liste = false; }
      if (!liste) { out += `<${bal}>`; liste = bal; }
      out += `<li>${inline(t.replace(/^([-*]|\d+[.)])\s+/, ''))}</li>`; continue;
    }
    clore();
    const par = [t];
    while (i + 1 < lignes.length) {
      const s2 = lignes[i + 1].trim();
      if (!s2 || /^(#{1,4}\s|[-*]\s|\||```|---+$)/.test(s2)) break;
      par.push(s2); i++;
    }
    out += `<p>${inline(par.join(' '))}</p>`;
  }
  clore();
  // Une page Notion porte son titre EN PREMIER et ses propriétés dessous. Le
  // markdown, lui, commence par l'en-tête : on le glisse donc après le titre.
  if (meta) {
    const i = out.indexOf('</h1>');
    out = i >= 0 ? out.slice(0, i + 5) + meta + out.slice(i + 5) : meta + out;
  }
  return out;
}

function pageRapport(md, nid, n, lienTranscript) {
  const corps = md.trim() ? mdEnHtml(md)
    : '<p class="vide">Ce rapport est vide — l\'agent a posé le fichier sans l\'écrire.</p>';
  return `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${nid} · passe ${n}</title>
<style>
 /* La grammaire d'une page Notion : une colonne étroite, beaucoup d'air, une
    seule famille de caractères, et des gris qui font toute la hiérarchie.
    Rien n'y est encadré — ce qui sépare, c'est l'espace. */
 :root{
   --bg:#191919; --ink:rgba(255,255,255,.81); --fort:#fff; --gris:rgba(255,255,255,.44);
   --trait:rgba(255,255,255,.13); --pose:rgba(255,255,255,.055); --code:#ff7369;
   --ui:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif;
   --mono:"SFMono-Regular",Menlo,Consolas,"PT Mono","Liberation Mono",monospace;
 }
 *{box-sizing:border-box}
 html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--ui)}

 .page{max-width:708px;margin:0 auto;padding:0 24px 40vh}
 .haut{display:flex;align-items:center;gap:10px;height:45px;font-size:14px;color:var(--gris)}
 .haut .fil{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .haut a{margin-left:auto;color:var(--gris);text-decoration:none;padding:4px 8px;border-radius:4px;
         font-size:14px;white-space:nowrap}
 .haut a:hover{background:var(--pose);color:var(--ink)}

 h1{margin:36px 0 6px;font-size:40px;line-height:1.2;font-weight:700;color:var(--fort);
    letter-spacing:-.02em}

 /* Les proprietes d'une page : libelle gris a gauche, valeur a droite.
    DEUX COLONNES, pas une rangee qui se replie : les valeurs n'ont pas toutes
    la meme longueur — « 0 » cotoie une phrase entiere — et une rangee repliable
    les decalait chacune a un endroit different, ce qui donnait une liste en
    escalier illisible. En grille, tous les libelles sont alignes, toutes les
    valeurs commencent au meme endroit, et une valeur longue se replie DANS sa
    colonne au lieu de pousser la suivante. */
 .meta{display:grid;grid-template-columns:max-content minmax(0,1fr);
        gap:7px 20px;margin:12px 0 8px;padding-bottom:16px;
        border-bottom:1px solid var(--trait)}
 .meta span{display:contents}
 .meta i{color:var(--gris);font-style:normal;font-size:13px;line-height:1.55;
        white-space:nowrap}
 .meta b{color:var(--ink);font-weight:500;font-size:13.5px;line-height:1.55;
        min-width:0;overflow-wrap:anywhere}

 h2{margin:2em 0 4px;font-size:24px;line-height:1.3;font-weight:600;color:var(--fort)}
 h3{margin:1.6em 0 2px;font-size:20px;line-height:1.3;font-weight:600;color:var(--fort)}
 h4{margin:1.4em 0 2px;font-size:17px;line-height:1.3;font-weight:600;color:var(--fort)}
 p{margin:2px 0;padding:3px 2px}
 strong,b{color:var(--fort);font-weight:600}
 em,i{font-style:italic}
 a{color:inherit;text-decoration:underline;text-decoration-color:var(--gris);
   text-underline-offset:3px}

 code{font:.85em var(--mono);color:var(--code);background:rgba(135,131,120,.15);
      border-radius:4px;padding:.2em .4em;line-height:normal}
 pre{background:var(--pose);border-radius:4px;padding:16px 18px;margin:6px 0;overflow:auto;
     font:.85em/1.45 var(--mono);color:var(--ink);tab-size:2}
 pre code{background:none;color:inherit;padding:0;font-size:1em}

 ul{margin:2px 0;padding-left:1.7em}
 li{padding:3px 2px;margin:0}
 li::marker{color:var(--gris)}

 hr{border:0;border-top:1px solid var(--trait);margin:1.4em 0}

 /* Un tableau Notion : des filets fins partout, pas de zébrage, pas de coin rond. */
 .tbl{overflow-x:auto;margin:10px 0}
 table{border-collapse:collapse;font-size:14px;min-width:100%}
 th,td{border:1px solid var(--trait);padding:7px 10px;text-align:left;vertical-align:top}
 th{color:var(--gris);font-weight:500;background:var(--pose);white-space:nowrap}
 td{color:var(--ink)}

 .vide{color:var(--gris)}
 @media(max-width:640px){
   h1{font-size:32px} .page{padding:0 18px 30vh}
   .meta{grid-template-columns:1fr;gap:2px 0}
   .meta span + span{margin-top:6px}
 }
</style>
<div class="page">
 <div class="haut"><span class="fil">${nid} · passe ${n}</span>
   <a href="${lienTranscript}">Transcript complet ↗</a></div>
 ${corps}
</div>`;
}

function idSession(wf, ws, nid, n) {
  // le lancement d'origine porte l'identifiant et le dossier de configuration
  const dir = path.join(STATE, wf.id, path.basename(ws), nid, 'passes', String(n));
  for (const f of [path.join(dir, 'launch.sh'),
                   path.join(STATE, wf.id, path.basename(ws), nid, 'launch.sh')]) {
    if (!fs.existsSync(f)) continue;
    const txt = fs.readFileSync(f, 'utf8');
    const sid = (txt.match(/--session-id\s+([0-9a-f-]{36})/) || [])[1];
    if (!sid) continue;
    const cfg = (txt.match(/CLAUDE_CONFIG_DIR=(\S+)/) || [])[1]
                || CFG_DEFAUT;
    const cwd = ((txt.match(/\ncd (\S+)/) || [])[1] || HOME_DIR).replace(/^"\$HOME"$/, HOME_DIR);
    return { sid, cfg, cwd };
  }
  return null;
}
function ensureRelecture(wf, ws, nid, n) {
  const cle = `${wf.id}:${nid}:${n}`;
  const sess = relectures.get(cle);
  if (sess && vivante(sess)) return { sess, neuve: false };
  // une seule a la fois : chacune tient un processus de la taille d'un agent
  for (const autre of [...relectures.keys()]) if (autre !== cle) finRelecture(autre);
  const o = idSession(wf, ws, nid, n);
  if (!o) return null;
  const nom = `relire-${wf.id}-${nid}-p${n}`;
  try { execFileSync('tmux', ['kill-session', '-t', nom], { stdio: 'ignore' }); } catch {}
  const script = path.join(FLUX, 'state', wf.id, path.basename(ws), nid, `relire-${n}.sh`);
  fs.writeFileSync(script,
    '#!/bin/bash\n' +
    'export TERM=xterm-256color COLORTERM=truecolor\n' +
    `export CLAUDE_CONFIG_DIR=${o.cfg}\n` +
    `cd ${o.cwd}\n` +
    // --fork-session : la relecture ecrit dans une session neuve, l'archive
    // d'origine reste telle qu'elle etait
    `exec ${CLAUDE_BIN} --resume ${o.sid} --fork-session ` +
    `--strict-mcp-config --mcp-config '{"mcpServers":{}}'\n`, { mode: 0o755 });
  chownAgents(script);
  execFileSync('tmux', ['new-session', '-d', '-s', nom, '-x', String(TERM_COLS),
                        '-y', String(TERM_ROWS), script]);
  for (const [o2, v] of [['history-limit', '50000'], ['status', 'off'],
                         ['window-size', 'largest']]) {
    try { execFileSync('tmux', ['set-option', '-t', nom, o2, v], { stdio: 'ignore' }); } catch {}
  }
  // au demarrage, Claude Code demande s'il faut reprendre depuis un resume :
  // on ecarte la question, l'historique est deja dessine derriere
  setTimeout(() => {
    try { execFileSync('tmux', ['send-keys', '-t', nom, 'Escape'], { stdio: 'ignore' }); } catch {}
  }, 14000);
  relectures.set(cle, nom);
  return { sess: nom, neuve: true };
}
function finRelecture(cle) {
  const sess = relectures.get(cle);
  if (!sess) return;
  relectures.delete(cle);
  try { execFileSync('tmux', ['kill-session', '-t', sess], { stdio: 'ignore' }); } catch {}
}
// Une relecture sans personne devant elle ne sert plus a rien : on la ferme.
// L'age ne dit rien — une heure de lecture est legitime, dix secondes
// d'abandon ne le sont pas. C'est l'absence de client qui compte.
const seules = new Map();
setInterval(() => {
  for (const [cle, sess] of [...relectures]) {
    try {
      const n = Number(execFileSync('tmux', ['display', '-p', '-t', sess, '#{session_attached}'],
                                    { encoding: 'utf8' }).trim());
      if (n > 0) { seules.delete(cle); continue; }
      const depuis = seules.get(cle) || Date.now();
      seules.set(cle, depuis);
      if (Date.now() - depuis > 120000) { seules.delete(cle); finRelecture(cle); }
    } catch { relectures.delete(cle); seules.delete(cle); }
  }
}, 30000);

// Le runtime d'un nœud est un processus : sa presence dit que l'etape tourne,
// independamment de ce que le serveur a garde en memoire.
// La table des processus, lue UNE fois pour tous les nœuds. Avant, `suivi()`
// forkait un `pgrep` par nœud : quatorze forks par appel de /api/wf/<id>, que la
// page rappelle toutes les 4 s — 0,69 s de fork mesures sur 1,4 s de reponse.
// Le cache de 1,5 s est bien en dessous du rythme de la page (l'etat reste
// juste) mais absorbe les rafales d'un meme repaint.
let LIGNES_PS = { t: 0, v: [] };
function lignesPs() {
  if (Date.now() - LIGNES_PS.t < 1500) return LIGNES_PS.v;
  let v = [];
  try {
    v = execFileSync('ps', ['-eo', 'args'], { encoding: 'utf8', maxBuffer: 4 << 20 })
          .split('\n');
  } catch { v = []; }
  LIGNES_PS = { t: Date.now(), v };
  return v;
}

/** Le NUMERO de la derniere passe, jamais leur NOMBRE.
 *  Les deux coincident tant qu'aucune passe n'est effacee — et divergent des
 *  qu'on en supprime une : deux passes dont on retire la premiere laissent la
 *  #2 seule, donc un compte de 1, et la console servait alors la passe 1, qui
 *  n'existe plus. L'onglet disait #2, le contenu montrait la 1. */
function dernierePasse(passages) {
  const l = passages || [];
  return l.length ? (l[l.length - 1].n || l.length) : 1;
}

function suivi(wid, nid, ws) {
  const motif = `run-node.py ${wid} ${nid} --workspace ${ws}`;
  return lignesPs().some(l => l.includes(motif));
}
// Un agent au travail : sa session vit et il n'a pas pose son marqueur de fin.
// Le runtime, lui, peut avoir disparu sans que l'agent s'arrete.
function travailleEncore(wf, ws, nid, u) {
  const sess = sessionDe(wf, ws, nid, u);
  if (!vivante(sess)) return false;
  let passages = [];
  try {
    const st = JSON.parse(fs.readFileSync(
      path.join(STATE, wf.id, path.basename(ws), `${nid}.json`), 'utf8'));
    passages = st.passages || [];
  } catch {}
  const n = passages.length || 1;
  const done = path.join(STATE, wf.id, path.basename(ws), nid, 'passes', String(n), 'DONE');
  return !fs.existsSync(done);
}
function vivante(sess) {
  try { execFileSync('tmux', ['has-session', '-t', sess], { stdio: 'ignore' }); return true; }
  catch { return false; }
}
// La session d'un nœud : celle du passage demande, sinon le nom canonique.
function sessionDe(wf, ws, nid, u) {
  let passages = [];
  try {
    const st = JSON.parse(fs.readFileSync(
      path.join(STATE, wf.id, path.basename(ws), `${nid}.json`), 'utf8'));
    passages = st.passages || [];
  } catch {}
  const n = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || dernierePasse(passages));
  const p = passages.find(x => x.n === n);
  return (p && p.session) || `flux-${wf.id}-${nid}`;
}

// CE QUE LE SCRIPT DE MESURE NE SAIT PAS. Il lit l'etat du nœud et les
// journaux ; il ne sait ni si une session vit encore, ni ce que le contrat v2
// a ecrit a cote. Deux completions, a la source, avant tout cache :
//  - UNE PASSE SANS `ended` QUI NE TOURNE PLUS est figee : sa duree est celle
//    de son journal (dernier tour), pas le temps ecoule depuis son debut, qui
//    grandissait a chaque repeint pendant des heures (C8).
//  - la note, le drapeau de la garde, l'entree et la sortie de la passe (v2).
function completerMesures(wf, ws, nid, fEtat, d) {
  let st = {}; try { st = JSON.parse(fs.readFileSync(fEtat, 'utf8')); } catch {}
  const passages = st.passages || [];
  const lanceur = suivi(wf.id, nid, ws);
  let duree = 0, encore = false;
  for (const q of (d.passes || [])) {
    const x = passages.find(y => y.n === q.n) || {};
    if (q.en_cours) {
      const tourne = (x.session && vivante(x.session))
                  || (lanceur && (!st.passage_courant || st.passage_courant === q.n));
      if (!tourne) {
        const dernier = (q.tours_detail || []).map(t => t.fin).filter(Boolean).pop() || null;
        const t0 = Date.parse(q.debut || ''), t1 = Date.parse(dernier || '');
        q.duree = (isFinite(t0) && isFinite(t1) && t1 >= t0) ? Math.round((t1 - t0) / 1000) : null;
        q.attente = q.duree == null ? null : Math.max(0, q.duree - (q.parole || 0));
        q.en_cours = false; q.figee = true;
      }
    }
    const v2 = passeV2(wf.id, ws, nid, q.n, x);
    q.note = v2.note; q.garde = v2.garde;
    const es = entreeSortie(wf, ws, nid, v2);
    q.fichiers_entree = es.entree; q.fichiers_sortie = es.sortie;
    if (v2.modele) q.modele = v2.modele;
    if (v2.moteur_type && !q.moteur) q.moteur = v2.moteur_type;
    duree += q.duree || 0; encore = encore || !!q.en_cours;
  }
  if (d.total) { d.total.duree = duree; if (!encore) delete d.total.en_cours; }
  return d;
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname;

  // L'UI est servie sur `/`, mais aussi sur /<workflow> : l'adresse d'une
  // pipeline se lit alors comme une phrase — flux.example.org/mail.
  // sert aussi /<workflow>
  const wfSlug = /^\/([a-z0-9][a-z0-9-]*)\/?$/.exec(p);
  const wfConnu = wfSlug && fs.existsSync(cheminWf(wfSlug[1]));
  // Un workflow qui n'existe plus dans l'adresse (favori, onglet ancien) :
  // on renvoie au premier workflow visible plutot que de laisser une page vide.
  // La console v2 (workflows -> campagne -> cas) est la page par defaut depuis le 2026-09-09 ;
  // l'ancienne (une run, ses etapes, son terminal) reste servie sur /ancienne.
  if (p === '/' || p === '/index.html' || p === '/v3' || p === '/v3.html') {
    return send(res, 200, 'text/html; charset=utf-8', fs.readFileSync(path.join(PUB, 'v3.html')));
  }
  if (p === '/v2' || p === '/v2.html') {
    return send(res, 200, 'text/html; charset=utf-8', fs.readFileSync(path.join(PUB, 'v2.html')));
  }
  if (p === '/ancienne' || p === '/ancienne.html') {
    return send(res, 200, 'text/html; charset=utf-8', fs.readFileSync(path.join(PUB, 'index.html')));
  }
  if (wfSlug && !wfConnu && !p.startsWith('/api/') && !p.startsWith('/vendor/')) {
    const premier = Object.values(fichiersWf()).map(f => {
      try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; }
    }).filter(w => w && Array.isArray(w.nodes) && !w.cache)[0];
    if (premier) { res.writeHead(302, { Location: '/' + premier.id }); return res.end(); }
  }
  if (p === '/' || p === '/index.html' || wfConnu) {
    const f = path.join(PUB, 'index.html');
    if (!fs.existsSync(f)) return send(res, 404, 'text/plain', 'UI absente');
    return send(res, 200, 'text/html; charset=utf-8', fs.readFileSync(f));
  }

  // assets locaux (xterm.js vendorisé — aucun CDN)
  if (p.startsWith('/vendor/')) {
    const f = path.join(PUB, 'vendor', path.basename(p));
    if (!fs.existsSync(f)) return send(res, 404, 'text/plain', 'absent');
    const t = f.endsWith('.css') ? 'text/css' : 'application/javascript';
    // Les assets vendorises ne changent pas : un jour de cache, et gzip quand le
    // navigateur l'accepte (plotly : 4,5 Mo -> 1,3 Mo ; par un VPN ca compte).
    const gz = /\bgzip\b/.test(String(req.headers['accept-encoding'] || ''));
    const corps = fs.readFileSync(f);
    res.writeHead(200, { 'Content-Type': `${t}; charset=utf-8`, 'Cache-Control': 'public, max-age=86400',
                         ...(gz ? { 'Content-Encoding': 'gzip' } : {}) });
    return res.end(gz ? require('zlib').gzipSync(corps) : corps);
  }

  // ── CONSOLE V3 : familles, runs, detail ─────────────────────────────────
  // Une FAMILLE = un workflow et ses versions de consignes (agenda, agenda-v3,
  // agenda-v4, agenda-v5…). Une RUN = une campagne de tools/jouer-lot.py (un
  // modele x une version de consignes x un jeu). La console v3 ne montre que ca.
  if (p === '/api/familles') {
    const out = [];
    for (const [base, ids] of Object.entries(familles())) {
      const wf = loadWf(base) || loadWf(ids[0]);
      const runs = runsDeFamille(base, ids);
      const finies = runs.filter(r => r.fin);
      const der = finies[0] || null;
      out.push({ base, name: (wf.name || base).replace(/\s*\((v\d+|version)[^)]*\)\s*$/, ''), description: wf.description || '',
                 cible: wf.cible || null, nodes: (wf.nodes || []).length, versions: ids, runs: runs.length,
                 derniere: der ? { note: der.note, bareme: der.bareme, lot: der.lot, modele: der.modele, version: der.version, debut: der.debut, n: der.n, attendus: der.attendus } : null });
    }
    out.sort((a, b) => (b.runs > 0) - (a.runs > 0) || String((b.derniere || {}).debut || '').localeCompare(String((a.derniere || {}).debut || '')));
    return json(res, 200, out);
  }
  const mf = p.match(/^\/api\/famille\/([\w-]+)$/);
  if (mf) {
    const base = mf[1]; const ids = familles()[base];
    if (!ids) return json(res, 404, { error: 'famille inconnue' });
    const wf = loadWf(base) || loadWf(ids[0]);
    let cas = [];
    try {
      const carte = JSON.parse(fs.readFileSync(path.join(FLUX, 'cas', 'cas.json'), 'utf8'));
      cas = (carte.cas || []).filter(c => c.lot !== 'mesure').map(c => ({ id: c.id, lot: c.lot, theme: c.theme || '', difficulte: c.difficulte || '' }));
    } catch {}
    return json(res, 200, { base, name: (wf.name || base).replace(/\s*\((v\d+|version)[^)]*\)\s*$/, ''), description: wf.description || '',
                            cible: wf.cible || null, versions: ids, cas, runs: runsDeFamille(base, ids) });
  }
  // GET /api/wf/<id>/run-detail?nom=<campagne> : la campagne, et pour chacun de ses mails
  // la note du juge (score + lignes) et le workspace — ce qu'il faut pour le resume
  // d'une run deroulee et ses cases, sans ouvrir 15 fois passe-fichiers.
  const mr = p.match(/^\/api\/wf\/([\w-]+)\/run-detail$/);
  if (mr) {
    const wf = loadWf(mr[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const nom = String(new URL(req.url, 'http://x').searchParams.get('nom') || '');
    const f = path.join(STATE, wf.id, 'campagnes', path.basename(nom) + '.json');
    if (!nom || !fs.existsSync(f)) return json(res, 404, { error: 'campagne inconnue' });
    let camp; try { camp = JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return json(res, 500, { error: 'campagne illisible' }); }
    const nid = dernierNoeud(wf);
    const mails = (camp.runs || []).map(r => {
      let note = null, mes = {}, panne = r.panne || null;
      try {
        const e = JSON.parse(fs.readFileSync(path.join(STATE, wf.id, r.run, `${nid}.json`), 'utf8'));
        const der = (e.passages || [])[(e.passages || []).length - 1] || {};
        note = der.note || null; mes = der.mesures || {}; panne = panne || (der.panne && der.panne.cause) || null;
      } catch {}
      return { cas: r.cas, modele: r.modele, run: r.run, score: note ? note.score : (r.score ?? null), bareme: note ? note.bareme : (r.bareme ?? null),
               lignes: note ? (note.lignes || []) : [], duree_s: r.duree_s ?? mes.duree_s ?? null, panne, essai: r.essai || 1 };
    });
    return json(res, 200, { nom: camp.nom, workflow: wf.id, noeud: nid, noeuds: camp.noeuds || [nid], lot: camp.lot, modeles: camp.modeles, debut: camp.debut, fin: camp.fin,
                            consignes_md5: camp.consignes_md5 || '', bilan: camp.bilan || {}, mails });
  }

  if (p === '/api/workflows') {
    // Le dossier workflows/ porte aussi le CONTRAT du graphe (workflow.schema.json) :
    // un fichier sans tableau `nodes` n'est pas un graphe et ne se liste pas. Sans ce
    // filtre, un seul fichier annexe fait tomber toute la liste (w.nodes.length sur
    // undefined) et la console n'affiche plus aucun workflow.
    const list = Object.values(fichiersWf())
      .map(f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); }
                  catch { return null; } })
      .filter(w => w && Array.isArray(w.nodes))
      .map(w => ({ id: w.id, name: w.name, nodes: w.nodes.length, cache: !!w.cache, pourquoi: w._cache_pourquoi || null, variante_de: w.variante_de || null }));
    return json(res, 200, list);
  }



  // ── CONSOLE V2 : la page, et le lancement d'une campagne ─────────────
  // POST /api/wf/<id>/campagne {lot, modeles, nom, essais} : joue un lot entier sur des
  // modeles, en tache de fond (tools/jouer-lot.py). Le journal va dans
  // state/<wf>/campagnes/<nom>.log ; la campagne elle-meme dans <nom>.json.
  let m = p.match(/^\/api\/wf\/([\w-]+)\/campagne$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    let corps = ''; req.on('data', c => corps += c);
    req.on('end', () => {
      let b = {}; try { b = JSON.parse(corps || '{}'); } catch { return json(res, 400, { error: 'JSON attendu' }); }
      const lot = String(b.lot || 'defi'); const modeles = String(b.modeles || '').split(',').map(x => x.trim()).filter(Boolean);
      const essais = Math.max(1, Math.min(5, parseInt(b.essais || '1', 10) || 1));
      if (!/^[\w-]+$/.test(lot) || !modeles.length || !modeles.every(x => /^[\w.:-]+$/.test(x))) return json(res, 400, { error: 'lot ou modeles invalides' });
      const nom = String(b.nom || '').replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || `${lot}-${new Date().toISOString().slice(5, 16).replace(/[-:T]/g, '')}`;
      const cdir = path.join(STATE, wf.id, 'campagnes'); fs.mkdirSync(cdir, { recursive: true });
      const log = fs.openSync(path.join(cdir, nom + '.log'), 'a');
      const child = spawn('python3', [path.join(FLUX, 'tools', 'jouer-lot.py'), wf.id, '--lot', lot, '--modeles', modeles.join(','), '--nom', nom, '--essais', String(essais)],
                          { cwd: FLUX, detached: true, stdio: ['ignore', log, log] });
      child.unref();
      return json(res, 200, { ok: true, nom, pid: child.pid });
    });
    return;
  }

  // ── CONSOLE V2 ────────────────────────────────────────────────────────
  // GET /api/wf/<id>/campagnes : tout ce que l'ecran « campagne » affiche —
  // les cas declares (cas/cas.json, avec leur lot), toutes les runs du workflow
  // avec cas, modele, note, duree, jetons, et les campagnes jouees par
  // tools/jouer-lot.py (state/<wf>/campagnes/*.json). Rien n'est calcule ici
  // qui ne soit deja sur disque : la console lit, elle n'invente pas.
  m = p.match(/^\/api\/wf\/([\w-]+)\/campagnes$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const nid = dernierNoeud(wf);
    let cas = [];
    try {
      const carte = JSON.parse(fs.readFileSync(path.join(FLUX, 'cas', 'cas.json'), 'utf8'));
      cas = (carte.cas || []).filter(c => c.lot !== 'mesure')
        .map(c => ({ id: c.id, lot: c.lot, theme: c.theme || '', difficulte: c.difficulte || '', registre: c.registre || '', messages: c.messages || null }));
    } catch {}
    const sdir = path.join(STATE, wf.id);
    const runs = [];
    let dirs = []; try { dirs = fs.readdirSync(sdir); } catch {}
    for (const d of dirs) {
      const rj = path.join(sdir, d, 'run.json');
      if (!fs.existsSync(rj)) continue;
      let run = {}, etat = null;
      try { run = JSON.parse(fs.readFileSync(rj, 'utf8')); } catch { continue; }
      try { etat = JSON.parse(fs.readFileSync(path.join(sdir, d, `${nid}.json`), 'utf8')); } catch {}
      const passes = (etat && etat.passages) || [];
      const der = passes[passes.length - 1] || null;
      const note = (der && der.note) || null;
      const mes = (der && der.mesures) || {};
      runs.push({
        run: d, mode: run.mode || null, cas: run.cas || null,
        modele: (run.parametres || {}).modele || (der && der.modele) || null,
        statut: run.statut || null, ouvert: run.ouvert || null,
        fin: der ? der.fin : null, passes: passes.length,
        score: note ? note.score : null, bareme: note ? note.bareme : null,
        duree_s: mes.duree_s ?? null, jetons_entree: mes.jetons_entree ?? null,
        jetons_sortie: mes.jetons_sortie ?? null, cout_usd: mes.cout_usd ?? null,
        panne: (der && der.panne && der.panne.cause) || null,
      });
    }
    runs.sort((a, b) => String(b.ouvert || '').localeCompare(String(a.ouvert || '')));
    const campagnes = [];
    const cdir = path.join(sdir, 'campagnes');
    try {
      for (const f of fs.readdirSync(cdir).filter(x => x.endsWith('.json')).sort()) {
        try { campagnes.push(JSON.parse(fs.readFileSync(path.join(cdir, f), 'utf8'))); } catch {}
      }
    } catch {}
    campagnes.sort((a, b) => String(b.debut || '').localeCompare(String(a.debut || '')));
    return json(res, 200, { workflow: { id: wf.id, name: wf.name, description: wf.description || '', noeud: nid, nodes: wf.nodes.length, cible: wf.cible || null }, cas, runs, campagnes });
  }

  // GET /api/wf/<id>/passe-fichiers?run=<run>&noeud=<nid>&pass=<n> : la passe et ses
  // fichiers de preuve (prompt envoye, reponses brutes, journal du moteur, verdict
  // du juge) + l'entree du workspace. Noms de fichiers en liste fermee, jamais un
  // chemin venu de la requete.
  m = p.match(/^\/api\/wf\/([\w-]+)\/passe-fichiers$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const run = String(u.searchParams.get('run') || '');
    const nid = String(u.searchParams.get('noeud') || dernierNoeud(wf) || '');
    const n = parseInt(u.searchParams.get('pass') || '0', 10) || 0;
    if (!/^[\w.-]+$/.test(run) || !/^[\w-]+$/.test(nid)) return json(res, 400, { error: 'parametres' });
    const sdir = path.join(STATE, wf.id, run);
    if (!fs.existsSync(sdir)) return json(res, 404, { error: 'run inconnue' });
    let etat = null; try { etat = JSON.parse(fs.readFileSync(path.join(sdir, `${nid}.json`), 'utf8')); } catch {}
    const passes = (etat && etat.passages) || [];
    const passe = n ? passes.find(x => x.n === n) : passes[passes.length - 1];
    if (!passe) return json(res, 404, { error: 'passe inconnue' });
    const pdir = path.join(sdir, nid, 'passes', String(passe.n));
    const lire = f => { try { return fs.readFileSync(path.join(pdir, f), 'utf8').slice(0, 400000); } catch { return null; } };
    const fichiers = { 'prompt.md': lire('prompt.md'), 'run.out': lire('run.out'), 'juge.out': lire('juge.out') };
    let reponses = [];
    try { reponses = fs.readdirSync(pdir).filter(f => /^reponse-\d+\.txt$/.test(f)).sort(); } catch {}
    for (const f of reponses) fichiers[f] = lire(f);
    let entree = null, ws = null;
    try { ws = JSON.parse(fs.readFileSync(path.join(sdir, 'run.json'), 'utf8')).workspace; } catch {}
    const node = wf.nodes.find(x => x.id === nid) || {};
    if (ws && Array.isArray(node.entree) && node.entree[0]) {
      try { entree = fs.readFileSync(path.join(ws, node.entree[0]), 'utf8').slice(0, 200000); } catch {}
    }
    let sortie = null;
    if (ws && Array.isArray(node.livrables) && node.livrables[0]) {
      try { sortie = fs.readFileSync(path.join(ws, node.livrables[0]), 'utf8').slice(0, 200000); } catch {}
    }
    return json(res, 200, { run, noeud: nid, passe, fichiers, entree: { nom: (node.entree || [])[0] || null, contenu: entree }, sortie: { nom: (node.livrables || [])[0] || null, contenu: sortie } });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u);
    const resolus = pat => ws ? fichiersDuLivrable(ws, pat).map(f => {
      let taille = 0; try { taille = fs.statSync(path.join(ws, f)).size; } catch {}
      return { path: f, taille };
    }) : [];
    const nodes = wf.nodes.map(n => {
      const etat = nodeState(wf, ws, n.id);
      const passages = etat.passages || [];
      const dernier = passages[passages.length - 1] || null;
      // LA DERNIERE PASSE, au contrat v2 : sa note et le drapeau de la garde.
      // C'est ce que la ligne du nœud montre ; absent = rien, pas une erreur.
      const v2 = (ws && dernier) ? passeV2(wf.id, ws, n.id, dernier.n || passages.length, dernier)
                                 : { note: null, garde: null };
      return {
        ...n,
        etat,
        livrables_etat: (n.livrables || []).map(d => ({
          path: d, present: ws ? present(ws, d) : false, fichiers: resolus(d) })),
        // Ce que le nœud RECOIT, declare par `entree[]` — la console en fait le
        // lien « entree ». Un graphe qui ne le declare pas n'en montre pas.
        entree_etat: (n.entree || []).map(d => ({
          path: d, present: ws ? present(ws, d) : false, fichiers: resolus(d) })),
        note: v2.note, garde: v2.garde,
        en_cours: running.has(`${wf.id}:${n.id}`) || (ws ? suivi(wf.id, n.id, ws) : false),
      };
    });
    // Les runs, telles quelles, la plus recente d'abord : la console les liste
    // pour ce workflow, sans les trier par une notion qu'il ne declare pas.
    const runs = tousWs(wf).map(d => ({ nom: path.basename(d), ...infoRun(wf, d),
                                        maj: fs.statSync(d).mtimeMs }));
    // Les moteurs API que le graphe declare — pastille joignable / injoignable
    // servie a part (`/api/moteurs/ping`), ici la declaration seule.
    const moteurs_api = Array.isArray(wf.moteurs_api) ? wf.moteurs_api : [];
    // L'orchestrateur (agent en charge de la run, v2 04/09 soir) : ses passes
    // vivent dans orchestrateur.json ; la console montre sa carte des qu'il y en a.
    let orchestrateur_passes = 0;
    try { orchestrateur_passes = ((JSON.parse(fs.readFileSync(path.join(STATE, wf.id, path.basename(ws || ''), 'orchestrateur.json'), 'utf8')) || {}).passages || []).length; } catch {}
    return json(res, 200, { ...wf, ouverture: ouvertureRemplie(wf), nodes, runs, moteurs_api, orchestrateur_passes,
                            workspace: ws,
                            workspace_nom: ws ? path.basename(ws) : null,
                            run: ws ? infoRun(wf, ws) : null });
  }

  // LES MOTEURS API DECLARES PAR UN GRAPHE, sont-ils joignables ? Ollama repond
  // sur `GET <url>/api/tags` (3 s, pas plus : la page attend derriere) ;
  // Anthropic passe par `claude -p`, il n'y a rien a pinger — on le dit.
  // Cache de 30 s : la page repeint toutes les 4 s, un moteur distant ne se
  // sonde pas a ce rythme.
  if (p === '/api/moteurs/ping') {
    const wf = loadWf(u.searchParams.get('wf') || ''); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const decl = Array.isArray(wf.moteurs_api) ? wf.moteurs_api : [];
    const out = await Promise.all(decl.map(async x => {
      const cle = `${x.type}|${x.url || ''}`;
      const hit = PING_CACHE.get(cle);
      if (hit && Date.now() - hit.t < 30_000) return { ...x, ...hit.v };
      let v;
      if (x.type === 'ollama' && x.url) {
        const ctrl = new AbortController();
        const to = setTimeout(() => ctrl.abort(), 3000);
        try {
          const r = await fetch(String(x.url).replace(/\/+$/, '') + '/api/tags', { signal: ctrl.signal });
          let modeles = [];
          try { modeles = ((await r.json()).models || []).map(mm => mm.name); } catch {}
          v = { joignable: r.ok, detail: r.ok ? (modeles.length + ' modele(s)') : ('HTTP ' + r.status), modeles };
        } catch (e) { v = { joignable: false, detail: 'injoignable' }; }
        clearTimeout(to);
      } else if (x.type === 'anthropic') {
        v = { joignable: null, detail: 'via claude -p' };
      } else {
        v = { joignable: null, detail: 'type inconnu' };
      }
      PING_CACHE.set(cle, { t: Date.now(), v });
      return { ...x, ...v };
    }));
    return json(res, 200, { moteurs: out });
  }

  // VALIDER / CORRIGER — le geste de la production. Le livrable final d'une run
  // en production n'a pas de note : c'est la relecture humaine qui juge. Sa
  // version corrigee devient un CAS DE BANC : `cas/<date>-<id>/` avec l'entree
  // de la run et le corrige, nomme comme le corrige que le nœud declare
  // (`juge.corrige`), sinon `golden-<livrable>.json`. Le cas apparait aussitot
  // dans la liste `options_de: cas` du dialogue nouvelle run.
  // UN TEST : la suite declaree par le graphe (test.suite), jouee a la suite
  // sur un cas, dans flux. Le lanceur (tools/jouer-test.py) tourne detache et
  // ecrit state/<wf>/tests/<id>.json au fil des runs.
  m = p.match(/^\/api\/wf\/([\w-]+)\/test$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const cas = (u.searchParams.get('cas') || '').trim();
    if (!cas) return json(res, 400, { error: 'un test demande un cas' });
    if (!fs.existsSync(path.join(DOSSIER_CAS, cas))) return json(res, 404, { error: 'cas inconnu : ' + cas });
    const id = 'test-' + new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15);
    // Le lanceur rappelle CETTE console : a l'adresse ou elle ecoute (sur le
    // serveur, l'adresse du reseau prive seule — 127.0.0.1 n'y repond pas).
    const hoteLocal = (!HOTE || HOTE === '0.0.0.0') ? '127.0.0.1' : HOTE;
    const child = spawn('python3', [path.join(FLUX, 'tools', 'jouer-test.py'), wf.id, cas, id, `--hote=http://${hoteLocal}:${PORT}`],
                        { detached: true, stdio: 'ignore' });
    child.unref();
    return json(res, 202, { test: id, cas });
  }
  // LES MESURES D'UN TEST : toutes les passes de ses runs (chaque configuration,
  // chaque nœud), pour que le recapitulatif soit celui du test, pas d'une run.
  m = p.match(/^\/api\/wf\/([\w-]+)\/test\/([\w-]+)\/mesures$/);
  if (m && req.method === 'GET') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    let t; try { t = JSON.parse(fs.readFileSync(path.join(STATE, wf.id, 'tests', m[2] + '.json'), 'utf8')); }
    catch { return json(res, 404, { error: 'test inconnu' }); }
    const configs = (t.suite || []).map(s => {
      if (!s.run) return { ...s, noeuds: [] };
      const g = loadWf(s.workflow) || wf;
      const ws = path.join(wsRoot(g), s.run);
      const dir = path.join(STATE, g.id, s.run);
      const noeuds = (g.nodes || []).map(n => {
        const f = path.join(dir, n.id + '.json');
        if (!fs.existsSync(f)) return null;
        let d; try { d = JSON.parse(execFileSync('python3', [MESURER, f], { maxBuffer: 8 << 20, encoding: 'utf8' })); }
        catch { d = { passes: [] }; }
        d = completerMesures(g, ws, n.id, f, d);
        return { nid: n.id, name: n.name || n.id, passes: d.passes || [] };
      }).filter(Boolean);
      // l'orchestrateur de cette run, s'il a decide quelque chose
      const fo = path.join(dir, 'orchestrateur.json');
      if (fs.existsSync(fo)) {
        let d; try { d = JSON.parse(execFileSync('python3', [MESURER, fo], { maxBuffer: 8 << 20, encoding: 'utf8' })); } catch { d = { passes: [] }; }
        d = completerMesures(g, ws, 'orchestrateur', fo, d);
        if ((d.passes || []).length) noeuds.push({ nid: 'orchestrateur', name: 'Orchestrateur', passes: d.passes });
      }
      const info = fs.existsSync(ws) ? infoRun(g, ws) : {};
      return { ...s, workflow: g.id, note: info.note || null, statut: info.statut || s.statut, noeuds };
    });
    return json(res, 200, { id: t.id, cas: t.cas, fin: t.fin || null, configs });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/tests$/);
  if (m && req.method === 'GET') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const dir = path.join(STATE, wf.id, 'tests');
    let liste = [];
    try { liste = fs.readdirSync(dir).filter(f => f.endsWith('.json')).sort().reverse(); } catch {}
    const tests = liste.map(f => {
      let t = {}; try { t = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); } catch { return null; }
      t.suite = (t.suite || []).map(s => {
        if (!s.run) return s;
        const g = loadWf(s.workflow) || wf;
        const ws = path.join(wsRoot(g), s.run);
        const info = fs.existsSync(ws) ? infoRun(g, ws) : {};
        return { ...info, ...s, titre: s.titre, modele: s.modele || info.modele || null, statut: info.statut || s.statut };
      });
      return t;
    }).filter(Boolean);
    return json(res, 200, { tests, suite: (wf.test || {}).suite || null, cas: listeCas().map(c => c.valeur) });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/valider$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 400, { error: 'aucune run' });
    const nid = u.searchParams.get('noeud') || '';
    const n = (wf.nodes || []).find(x => x.id === nid);
    if (!n) return json(res, 400, { error: 'nœud inconnu : ' + nid });
    const rel = u.searchParams.get('f') || (n.livrables || [])[0] || '';
    if (!rel) return json(res, 400, { error: 'aucun livrable' });
    const abs = path.resolve(ws, rel);
    if (abs !== ws && !abs.startsWith(ws + path.sep)) return json(res, 403, { error: 'hors du workspace' });
    let corps = '';
    await new Promise(ok => { req.on('data', c => { corps += c; }); req.on('end', ok); });
    let corrige;
    try { corrige = JSON.parse(corps); } catch (e) { return json(res, 400, { error: 'le corrige n\'est pas du JSON : ' + e.message }); }
    const jour = new Date().toISOString().slice(0, 10);
    // Le nom du cas dit de quoi il parle (l'objet du mail), jamais d'ou il
    // vient : un cas ne de la production est un cas de test comme un autre.
    const slug = t => String(t || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').split('-').slice(0, 5).join('-');
    const id = ((u.searchParams.get('id') || '').trim() || slug(infoRun(wf, ws).titre) || path.basename(ws).replace(/-\d{4}-\d{2}-\d{2}-\d{6}$/, ''))
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'cas';
    let dossier = path.join(DOSSIER_CAS, `${jour}-${id}`);
    for (let k = 2; fs.existsSync(dossier); k++) dossier = path.join(DOSSIER_CAS, `${jour}-${id}-${k}`);
    fs.mkdirSync(dossier, { recursive: true });
    // L'entree du cas : ce que le PREMIER nœud recoit (le mail, le brief…), et
    // ce que le nœud valide recoit lui-meme — tel que la run les a eus.
    const premier = (wf.nodes || []).find(x => !x.verification) || {};
    const copies = [];
    for (const d of [...(premier.entree || []), ...(n.entree || [])]) {
      for (const f of fichiersDuLivrable(ws, d)) {
        const cible = path.join(dossier, path.basename(f));
        if (fs.existsSync(cible)) continue;
        try { fs.copyFileSync(path.join(ws, f), cible); copies.push(path.basename(f)); } catch {}
      }
    }
    const nomGolden = (n.juge && n.juge.corrige) ? path.basename(n.juge.corrige)
                    : 'golden-' + path.basename(rel).replace(/\.json$/i, '') + '.json';
    fs.writeFileSync(path.join(dossier, nomGolden), JSON.stringify(corrige, null, 2) + '\n');
    let brut = null; try { brut = fs.readFileSync(abs, 'utf8'); } catch {}
    if (brut != null) fs.writeFileSync(path.join(dossier, 'rendu-' + path.basename(rel)), brut);
    fs.writeFileSync(path.join(dossier, 'cas.json'), JSON.stringify({
      cree: new Date().toISOString(), workflow: wf.id, run: path.basename(ws), noeud: nid,
      livrable: rel, corrige: nomGolden, entree: copies, origine: 'valider-corriger' }, null, 2) + '\n');
    // La run est RELUE : la production la range dans « validé », et sait vers
    // quel cas de test sa relecture est partie.
    try {
      const fr = path.join(STATE, wf.id, path.basename(ws), 'run.json');
      let r = {}; try { r = JSON.parse(fs.readFileSync(fr, 'utf8')); } catch {}
      r.relu = { cas: path.basename(dossier), a: new Date().toISOString(), corrige: u.searchParams.get('tel_quel') === '1' ? false : true };
      fs.writeFileSync(fr, JSON.stringify(r, null, 2) + '\n');
    } catch {}
    return json(res, 201, { cas: path.basename(dossier), corrige: nomGolden, entree: copies });
  }

  // Ouvrir une NOUVELLE run. Sans cela l'interface reste sur la derniere et
  // affiche « fait » partout : l'etat d'une run finie, pas celui d'un depart.
  // Ce qu'il faut demander est propre a la pipeline — le workflow le declare
  // dans `ouverture`, le socle remplit le gabarit.
  // Le terminal attache. La page est servie derriere /flux, que le proxy retire
  // avant nous : ttyd doit donc annoncer le chemin PUBLIC, et nous le lui
  // remettons a l'entree.
  // CE QUI A ETE DONNE A L'AGENT, servi tel quel :
  //   /consigne/<wid>/<nid>?pass=N&quoi=brief|sys
  // Le brief et la consigne permanente vivent sous STATE, pas dans le workspace :
  // la route des livrables ne peut pas les servir, et sans elles on ne peut pas
  // relire ce qu'un agent avait REELLEMENT sous les yeux — la premiere question
  // qu'on se pose quand une passe part de travers.
  m = p.match(/^\/consigne\/([\w-]+)\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    const quoi = u.searchParams.get('quoi') || 'brief';
    const FICHIERS = { brief: 'brief.md', sys: 'sys.md' };
    if (!FICHIERS[quoi]) return json(res, 400, { error: 'quoi: brief ou sys' });
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(
        path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const n = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10)
                          || dernierePasse(passages));
    const fp = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(n), FICHIERS[quoi]);
    let d; try { d = fs.readFileSync(fp); } catch { return json(res, 404, { error: 'introuvable' }); }
    res.writeHead(200, { 'content-type': 'text/markdown; charset=utf-8',
                         'cache-control': 'no-store' });
    return res.end(d);
  }

  // UN LIVRABLE, servi tel quel : /livrable/<wid>?f=<chemin relatif au workspace>.
  // Le chemin est resolu PUIS verifie contre la racine du workspace -- un `..`
  // dans la requete ne peut pas sortir de la run, meme encode.
  m = p.match(/^\/livrable\/([\w-]+)$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    const rel = u.searchParams.get('f') || '';
    if (!rel) return json(res, 400, { error: 'aucun fichier demande' });
    const abs = path.resolve(ws, rel);
    if (abs !== ws && !abs.startsWith(ws + path.sep))
      return json(res, 403, { error: 'hors du workspace' });
    let st; try { st = fs.statSync(abs); } catch { return json(res, 404, { error: 'introuvable' }); }
    if (!st.isFile()) return json(res, 400, { error: 'pas un fichier' });
    const ext = path.extname(abs).toLowerCase();
    const type = ext === '.md' ? 'text/markdown; charset=utf-8'
               : ext === '.json' ? 'application/json; charset=utf-8'
               : ext === '.html' ? 'text/html; charset=utf-8'
               : ext === '.csv' ? 'text/csv; charset=utf-8'
               : 'text/plain; charset=utf-8';
    let d; try { d = fs.readFileSync(abs); } catch { return json(res, 500, { error: 'illisible' }); }
    res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store' });
    return res.end(d);
  }

  // La session terminee, en page : /rendu/<wid>/<nid>[?pass=N].
  // Le journal ne bougera plus — on le rend une fois et on garde le resultat.
  m = p.match(/^\/rendu\/([\w-]+)\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(
        path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const n = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || dernierePasse(passages));
    // LE RAPPORT DE PASSE D'ABORD. Le transcript dit tout, donc il ne dit rien :
    // personne ne lit 3 000 lignes pour savoir ce qu'une passe a fait. On sert
    // donc ce que l'agent en a ecrit, et le transcript reste a un clic — pour
    // quand on veut la preuve plutot que le recit.
    const rap = path.join(ws, 'rapports-de-passe', `${nid}-p${n}.md`);
    if (u.searchParams.get('transcript') !== '1' && fs.existsSync(rap)) {
      let md = '';
      try { md = fs.readFileSync(rap, 'utf8'); } catch {}
      if (u.searchParams.get('fragment') === '1')
        return send(res, 200, 'text/html; charset=utf-8', mdEnHtml(md));
      const lien = `/rendu/${wid}/${nid}?pass=${n}&transcript=1`
                 + (u.searchParams.get('ws') ? '&ws=' + encodeURIComponent(u.searchParams.get('ws')) : '');
      return send(res, 200, 'text/html; charset=utf-8', pageRapport(md, nid, n, lien));
    }
    const o = idSession(wf, ws, nid, n);
    if (!o) return send(res, 404, 'text/html; charset=utf-8',
                        '<body style="background:#0d1526;color:#8ea5c8;font:13px ui-monospace;'
                        + 'padding:20px">Pas de journal archivé pour cette étape.</body>');
    // le journal vit sous le projet correspondant au repertoire de travail
    let jsonl = null, sousdir = null;
    try {
      const base = path.join(o.cfg, 'projects');
      for (const d of fs.readdirSync(base)) {
        const f = path.join(base, d, o.sid + '.jsonl');
        if (fs.existsSync(f)) { jsonl = f; sousdir = path.join(base, d, o.sid, 'subagents'); break; }
      }
    } catch {}
    if (!jsonl) return send(res, 404, 'text/html; charset=utf-8',
                            '<body style="background:#0d1526;color:#8ea5c8;font:13px ui-monospace;'
                            + 'padding:20px">Journal introuvable pour cette session.</body>');
    const cache = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(n), 'session.html');
    let frais = false;
    try {
      frais = fs.existsSync(cache) &&
              fs.statSync(cache).mtimeMs >= fs.statSync(jsonl).mtimeMs;
    } catch {}
    if (!frais) {
      try {
        fs.mkdirSync(path.dirname(cache), { recursive: true });
        const args = [RENDU, jsonl];
        if (sousdir && fs.existsSync(sousdir)) args.push(sousdir);
        const out = execFileSync('python3', args, { maxBuffer: 256 * 1024 * 1024 });
        fs.writeFileSync(cache, out);
        chownAgents(cache);
      } catch (e) {
        return send(res, 500, 'text/html; charset=utf-8',
                    '<body style="background:#0d1526;color:#f0a8a8;font:13px ui-monospace;'
                    + 'padding:20px">Rendu impossible : ' + String(e.message || e) + '</body>');
      }
    }
    return send(res, 200, 'text/html; charset=utf-8', fs.readFileSync(cache));
  }

  // Relecture : /relire/<wid>/<nid>[?pass=N] — la session rouverte, en lecture.
  m = p.match(/^\/relire\/([\w-]+)\/([\w-]+)(\/.*)?$/);
  if (m) {
    const [, wid, nid, reste] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(
        path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const n = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || dernierePasse(passages));
    const r = ensureRelecture(wf, ws, nid, n);
    if (!r) return send(res, 404, 'text/plain; charset=utf-8',
                        'pas de session archivée pour cette étape');
    const base = `/flux/relire/${wid}/${nid}`;
    const t = ensureTtyd(r.sess, base);
    await attendrePort(t.port, 25);
    const chemin = base + (reste || '/') + (u.search || '');
    const dem = http.request({ hostname: '127.0.0.1', port: t.port, path: chemin,
                               method: req.method, headers: req.headers }, amont => {
      res.writeHead(amont.statusCode, amont.headers);
      amont.pipe(res);
    });
    dem.on('error', e => { try { res.writeHead(502); res.end('ttyd: ' + e.message); } catch {} });
    return req.pipe(dem);
  }

  // Fermer une relecture : elle tient un processus tant qu'elle vit.
  m = p.match(/^\/api\/wf\/([\w-]+)\/relire-fin\/([\w-]+)$/);
  if (m && req.method === 'POST') {
    const [, wid, nid] = m;
    const brut = u.searchParams.get('pass');
    if (brut) finRelecture(`${wid}:${nid}:${Math.max(1, parseInt(brut, 10))}`);
    else for (const cle of [...relectures.keys()])
      if (cle.startsWith(`${wid}:${nid}:`)) finRelecture(cle);
    return json(res, 200, { ferme: true });
  }

  m = p.match(/^\/term\/([\w-]+)\/([\w-]+)(\/.*)?$/);
  if (m) {
    const [, wid, nid, reste] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    let sess = sessionDe(wf, ws, nid, u);
    if (!vivante(sess)) sess = dernierTerm.get(`${wid}:${nid}`) || sess;
    if (!vivante(sess)) return send(res, 409, 'text/plain; charset=utf-8',
                                    'session terminée — la page de relecture prend le relais');
    dernierTerm.set(`${wid}:${nid}`, sess);
    const base = `/flux/term/${wid}/${nid}`;
    const t = ensureTtyd(sess, base);
    await attendrePort(t.port, 25);
    const chemin = base + (reste || '/') + (u.search || '');
    const dem = http.request({ hostname: '127.0.0.1', port: t.port, path: chemin,
                               method: req.method, headers: req.headers }, amont => {
      res.writeHead(amont.statusCode, amont.headers);
      amont.pipe(res);
    });
    dem.on('error', e => { try { res.writeHead(502); res.end('ttyd: ' + e.message); } catch {} });
    return req.pipe(dem);
  }

  // CE QUI TOURNE, DANS LA RUN AFFICHEE. Tout est sequentiel — une etape, sa
  // verification, puis l'orchestrateur : il n'y a jamais qu'une session vivante
  // par run. Mais le nom d'une session ne porte PAS la run : c'est l'etat des
  // nœuds du workspace affiche qui dit laquelle lui appartient. Sans ce
  // croisement, changer de veille laissait le bouton sur la session d'une autre.
  m = p.match(/^\/api\/wf\/([\w-]+)\/actif$/);
  if (m) {
    const wid = m[1];
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, { actif: null });
    let noms = [];
    try {
      noms = execFileSync('tmux', ['ls', '-F', '#{session_name}'],
                          { encoding: 'utf8' }).split(/\s+/).filter(Boolean);
    } catch { noms = []; }                       // aucun serveur tmux = rien ne tourne
    const vivante = new Set(noms);

    const dir = path.join(STATE, wid, path.basename(ws));
    let cands = [];
    try {
      for (const f of fs.readdirSync(dir)) {
        if (!f.endsWith('.json')) continue;
        const nid = f.slice(0, -5);
        let st; try { st = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); } catch { continue; }
        if (st.tmux && vivante.has(st.tmux))
          cands.push({ nid, sess: st.tmux,
                       passe: st.passage_courant || (st.passages || []).length || 1 });
      }
    } catch {}
    if (!cands.length) return json(res, 200, { actif: null });

    // L'orchestrateur passe en dernier : quand il tourne en meme temps qu'un
    // nœud, c'est le nœud qui interesse.
    const c = cands.find(x => x.nid !== 'orchestrateur') || cands[0];
    const n = (wf.nodes || []).find(x => x.id === c.nid);
    // Le CODE COURT : ce qui tient dans un onglet de navigateur etroit.
    // E3 = etape 3, V3 = sa verification, ORC = l'orchestrateur. Il se calcule
    // ici et pas dans la page pour qu'il n'existe qu'a un seul endroit.
    const code = c.nid === 'orchestrateur' ? 'ORC'
               : n && n.n != null ? (n.verification ? 'V' : 'E') + n.n
               : c.nid.slice(0, 3).toUpperCase();
    return json(res, 200, {
      actif: c.nid,
      nom: n ? n.name : (c.nid === 'orchestrateur' ? 'Orchestrateur' : c.nid),
      rang: n ? n.n : null,
      verification: n ? !!n.verification : false,
      passe: c.passe || 1,
      code,
    });
  }

  // LE MOTEUR d'une run : quel compte Claude fait tourner ses agents. Une limite
  // de 5 h se compte par COMPTE — quand celui qui tourne l'atteint, la run
  // s'arrete au milieu d'une etape. Le choix vit dans le workspace, comme
  // l'autopilote : propre a UNE run. Il est lu au LANCEMENT d'un nœud, donc il
  // prend effet au prochain nœud et ne coupe jamais la session en cours.
  // LE MODELE de CHAQUE moteur, pour CETTE run : opus est le defaut du graphe,
  // mais un moteur peut en porter un autre pour cette run precise. Persiste a
  // cote de `state/moteur`, pas dedans -- le moteur ACTIF et le modele de
  // CHAQUE moteur sont deux questions distinctes, l'une ne doit pas ecraser
  // l'autre au fichier.
  const MODELES_CONNUS = ['opus', 'sonnet', 'haiku'];
  m = p.match(/^\/api\/wf\/([\w-]+)\/moteur\/modele$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run active' });
    let cfg = { moteurs: [] };
    try { cfg = JSON.parse(fs.readFileSync(fichierMoteurs(), 'utf8')); } catch {}
    const id = (u.searchParams.get('id') || '').trim();
    if (!(cfg.moteurs || []).some(x => x.id === id)) return json(res, 400, { error: 'moteur inconnu : ' + id });
    const modele = (u.searchParams.get('modele') || '').trim();
    if (modele && !MODELES_CONNUS.includes(modele)) return json(res, 400, { error: 'modele inconnu : ' + modele });
    const f = path.join(ws, 'state', 'moteur-modeles.json');
    let table = {};
    try { table = JSON.parse(fs.readFileSync(f, 'utf8')); } catch {}
    if (modele) table[id] = modele; else delete table[id];
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, JSON.stringify(table));
    chownAgents(f);
    return json(res, 200, { id, modele: modele || null });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/moteur$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, { moteurs: [], actif: null });
    let cfg = { moteurs: [], defaut: null };
    try { cfg = JSON.parse(fs.readFileSync(fichierMoteurs(), 'utf8')); } catch {}
    const f = path.join(ws, 'state', 'moteur');
    if (req.method === 'POST') {
      const id = (u.searchParams.get('id') || '').trim();
      const connu = (cfg.moteurs || []).some(x => x.id === id);
      if (!connu) return json(res, 400, { error: 'moteur inconnu : ' + id });
      fs.mkdirSync(path.dirname(f), { recursive: true });
      fs.writeFileSync(f, id + '\n');
      chownAgents(f);
      return json(res, 200, { actif: id });
    }
    let actif = null;
    try { actif = fs.readFileSync(f, 'utf8').trim() || null; } catch {}
    if (!(cfg.moteurs || []).some(x => x.id === actif)) actif = cfg.defaut || null;
    let modeles = {};
    try { modeles = JSON.parse(fs.readFileSync(path.join(ws, 'state', 'moteur-modeles.json'), 'utf8')); } catch {}
    // L'etat des identifiants se lit au moment de la demande : un moteur dont
    // le jeton est mort ne doit pas se decouvrir au lancement d'un nœud.
    const moteurs = await Promise.all((cfg.moteurs || []).map(x => ({ ...x, config_dir: developper(x.config_dir || CFG_DEFAUT) })).map(async x => {
      let exp = null;
      try {
        const c = JSON.parse(fs.readFileSync(path.join(x.config_dir, '.credentials.json'),
                                             'utf8')).claudeAiOauth || {};
        exp = c.refreshTokenExpiresAt || c.expiresAt || null;
      } catch {}
      const utilisable = exp ? exp > Date.now() : false;
      // Pas d'appel reseau pour un compte qu'il faut de toute façon reconnecter.
      const reel = adresseConnectee(x.config_dir);
      // moteurs.json:compte -- le compte Anthropic que ce moteur fait tourner.
      // Notion generique (un moteur EST un compte), pas un mot de la veille :
      // le verificateur de C5 exclut cette ligne sur ce marqueur.
      const adresse = x.compte; // moteurs.json:compte
      return { id: x.id, nom: x.nom, adresse, adresse_reelle: reel,
               adresse_conforme: !reel || !adresse || reel === adresse, utilisable,
               modele: modeles[x.id] || null,
               usage: utilisable ? await usageMoteur(x.config_dir) : null };
    }));
    return json(res, 200, { moteurs, actif, modeles_connus: MODELES_CONNUS });
  }

  // CE QUI ATTEND UNE REPONSE. Une escalade posee par un controle arrete la run
  // et part par email ; sans ce signal a l'ecran, une run en attente ressemble a
  // une run en panne, et on rallume l'autopilote en croyant que ca suffit.
  // Meme source que l'orchestrateur : les registres de verification.
  // CE QU'UNE PASSE A COUTE — temps, tours de parole, jetons.
  //
  // Le nombre de TOURS commande le cout : a chaque tour on renvoie au modele tout
  // le contexte accumule, donc une session longue paie sa consigne autant de fois
  // qu'elle prend la parole. La page le montre par passe pour qu'une derive se
  // voie pendant la run, pas au post-mortem.
  //
  // Le calcul lit les journaux de session (des centaines de Mo) : il est delegue a
  // un script Python et garde en cache tant que l'etat du nœud ne bouge pas.
  m = p.match(/^\/api\/wf\/([\w-]+)\/mesures\/([\w-]+)$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, { passes: [] });
    const f = path.join(STATE, wf.id, path.basename(ws), `${m[2]}.json`);
    if (!fs.existsSync(f)) return json(res, 200, { passes: [] });
    const cle = `${f}:${fs.statSync(f).mtimeMs}`;
    // Un nœud qui tourne ecrit dans ses journaux de session sans toucher a son
    // fichier d'etat : le cache le figerait pendant toute la passe, et un compteur
    // fige pendant qu'une passe brule des jetons donne le sentiment que rien ne
    // bouge. La page demande alors `frais=1`, et on recalcule sans mettre en cache.
    const recalcul = u.searchParams.get('frais') === '1';
    const vu = recalcul ? null : MESURES.get(cle);
    if (vu) return json(res, 200, vu);
    return execFile('python3', [MESURER, f], { maxBuffer: 8 << 20 }, (e, out) => {
      if (e) return json(res, 200, { passes: [], erreur: String(e.message || e) });
      let d; try { d = JSON.parse(out); } catch { d = { passes: [] }; }
      d = completerMesures(wf, ws, m[2], f, d);
      if (MESURES.size > 200) MESURES.clear();
      if (!recalcul) MESURES.set(cle, d);
      json(res, 200, d);
    });
  }

  // LES BILANS DE PASSE, EN BREF — de quoi les mettre dans la bulle de survol
  // de la chronologie. Le rapport entier s'ouvre au clic ; ici on ne rend que
  // le verdict et la premiere phrase, parce qu'une bulle qui deborde ne se lit
  // pas plus qu'un transcript.
  m = p.match(/^\/api\/wf\/([\w-]+)\/rapports$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, {});
    const d = path.join(ws, 'rapports-de-passe');
    const out = {};
    try {
      for (const f of fs.readdirSync(d)) {
        const k = f.replace(/\.md$/, '');
        let t = ''; try { t = fs.readFileSync(path.join(d, f), 'utf8'); } catch { continue; }
        let verdict = null, par = null;
        const fm = t.match(/^---\n([\s\S]*?)\n---\n/);
        let corps = t;
        if (fm) {
          verdict = (fm[1].match(/^verdict:\s*(.+)$/m) || [])[1] || null;
          par = (fm[1].match(/^redige_par:\s*(.+)$/m) || [])[1] || null;
          corps = t.slice(fm[0].length);
        }
        if (/-simple$/.test(k)) {
          // LE BILAN SIMPLE EST INTEGRAL : il n'a plus de limite de longueur,
          // et le tronquer a 260 caracteres ou n'en garder que le premier
          // paragraphe couperait exactement ce qu'on vient d'autoriser.
          // Les sauts de paragraphe sont gardes (\n\n) pour que la bulle
          // les rende en plusieurs <p>, pas un seul bloc compact.
          // Les ** survivent : la bulle les interprete en gras cote client.
          // Seuls les backticks sautent -- la console ne rend pas le code inline.
          out[k] = { verdict: null, redige_par: null,
                     resume: corps.trim().replace(/`/g, '') || null };
          continue;
        }
        // un rapport extrait pose un préambule puis une règle horizontale : ce
        // qui compte est APRÈS, ce sont les mots de l'agent
        const ph = corps.split(/\n\s*\n/).map(x => x.trim())
          .find(x => x && !x.startsWith('#') && !x.startsWith('|') && !x.startsWith('```'));
        out[k] = { verdict, redige_par: par,
                   resume: ph ? ph.replace(/[*`]/g, '').replace(/\s+/g, ' ').slice(0, 260) : null };
      }
    } catch {}
    return json(res, 200, out);
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/attente$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, { escalades: [] });
    const esc = [];
    try {
      for (const d of fs.readdirSync(path.join(ws, 'verifications'), { withFileTypes: true })) {
        if (!d.isDirectory()) continue;
        const f = path.join(ws, 'verifications', d.name, 'registre.json');
        if (!fs.existsSync(f)) continue;
        let r; try { r = JSON.parse(fs.readFileSync(f, 'utf8')); } catch { continue; }
        if (!r || !r.escalade) continue;
        const e = r.escalade;
        esc.push({ etape: d.name, unite: e.unite || null,
                   question: e.question || String(e),
                   options: e.options || null,
                   pose_a: fs.statSync(f).mtimeMs });
      }
    } catch {}
    // Une decision ecrite APRES l'escalade la tranche. Avant, elle repond a autre
    // chose : c'est le cas quand une run en a deja connu une.
    let repondu = null;
    try {
      const dec = fs.readdirSync(path.join(ws, 'state'))
        .filter(f => /^DECISION-.*\.md$/.test(f))   // proprietaire OU orchestrateur
        .map(f => fs.statSync(path.join(ws, 'state', f)).mtimeMs);
      if (dec.length) repondu = Math.max(...dec);
    } catch {}
    const ouvertes = esc.filter(e => !repondu || repondu < e.pose_a);
    return json(res, 200, { escalades: ouvertes, toutes: esc.length,
                            derniere_decision: repondu });
  }

  // L'AUTOPILOTE. Un fichier dans le workspace : present = allume. Il vit la et
  // pas ailleurs parce qu'il est propre a UNE run — une nouvelle run demarre
  // toujours eteinte. `run-node.py` le lit a la fin de chaque nœud, et
  // l'orchestrateur le supprime pour s'eteindre lui-meme.
  m = p.match(/^\/api\/wf\/([\w-]+)\/autopilote$/);
  if (m) {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    const f = path.join(ws, 'state', 'autopilote');
    // La BORNE : jusqu'ou l'autopilote enchaine. Elle vit dans le meme fichier
    // que l'interrupteur, en deuxieme ligne — un seul etat, un seul endroit.
    // Sans borne, il va jusqu'a la premiere barriere humaine, comme avant.
    const lireBorne = () => {
      try {
        const l = fs.readFileSync(f, 'utf-8').split('\n').find(x => x.startsWith('jusqua='));
        return l ? l.slice(7).trim() || null : null;
      } catch (e) { return null; }
    };
    if (req.method === 'GET') return json(res, 200, { actif: fs.existsSync(f), jusqua: lireBorne() });
    if (req.method === 'POST') {
      const veut = u.searchParams.get('on') === '1';
      const borne = (u.searchParams.get('jusqua') || '').trim();
      try {
        if (veut) {
          fs.mkdirSync(path.dirname(f), { recursive: true });
          fs.writeFileSync(f, new Date().toISOString() + '\n'
            + (borne ? 'jusqua=' + borne + '\n' : ''));
        } else if (fs.existsSync(f)) fs.unlinkSync(f);
      } catch (e) { return json(res, 500, { error: String(e.message || e) }); }
      return json(res, 200, { actif: veut, jusqua: veut ? (borne || null) : null });
    }
    return json(res, 405, { error: 'méthode' });
  }

  // Defiler la console sans molette. Un ecran tactile n'emet pas d'evenement de
  // molette : la traduction molette -> PageUp du tmux.conf ne l'atteint jamais, et
  // xterm.js n'a pas de barre puisque son scrollback est a zero. On envoie donc la
  // touche cote tmux, ce qui marche au doigt comme a la souris.
  m = p.match(/^\/api\/wf\/([\w-]+)\/touche\/([\w-]+)$/);
  if (m && req.method === 'POST') {
    const [, wid, nid] = m;
    const k = (u.searchParams.get('k') || '').trim();
    if (!['PageUp', 'PageDown', 'Home', 'End'].includes(k))
      return json(res, 400, { error: 'touche non permise' });
    // L'historique est celui de TMUX, pas celui de l'application : l'agent tourne
    // en rendu classique, tout son fil est dans le scrollback du pane. On ouvre
    // donc le copy-mode et on y navigue — envoyer PageUp a l'application ne
    // ferait que deplacer son curseur.
    const gestes = {
      PageUp:   [['copy-mode', '-e'], ['send-keys', '-X', 'page-up']],
      PageDown: [['send-keys', '-X', 'page-down']],
      Home:     [['copy-mode', '-e'], ['send-keys', '-X', 'history-top']],
      End:      [['send-keys', '-X', 'cancel']],   // revenir au direct
    };
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 404, { error: 'aucune run' });
    let sess = sessionDe(wf, ws, nid, u);
    if (!vivante(sess)) sess = dernierTerm.get(`${wid}:${nid}`) || sess;
    if (!vivante(sess)) return json(res, 409, { error: 'session terminée' });
    try {
      for (const g of gestes[k])
        execFileSync('tmux', [g[0], ...(g[0] === 'copy-mode' ? [] : []), '-t', sess, ...g.slice(1)],
                     { stdio: 'ignore' });
    }
    catch (e) { return json(res, 500, { error: String(e.message || e) }); }
    return json(res, 200, { ok: true, touche: k });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/nouvelle-run$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ouv = ouvertureDe(wf) || {};
    // LE MODE de la run, banc ou production, decide a l'ouverture et jamais
    // apres (D6) : il s'ecrit dans run.json et dans l'entete du workspace.
    const mode = (u.searchParams.get('mode') || '').trim() || 'banc';
    if (!['banc', 'production'].includes(mode)) return json(res, 400, { error: 'mode inconnu : ' + mode });
    // Les champs de l'AUTRE mode ne sont ni demandes ni exiges.
    const champsDuMode = (ouv.champs || []).filter(c => !c.mode || c.mode === mode);
    const champs = {};
    let corps = null;
    try { corps = await lireCorps(req); } catch {}
    const donnees = corps && corps.length ? (() => { try { return JSON.parse(corps); } catch { return null; } })() : null;
    champsDuMode.forEach(c => {
      const brut = (donnees && donnees[c.cle] != null) ? String(donnees[c.cle]) : (u.searchParams.get(c.cle) || '');
      champs[c.cle] = brut.trim() || (c.defaut || '');
    });
    const vide = champsDuMode.filter(c => c.requis !== false && !champs[c.cle]);
    if (vide.length) return json(res, 400, { error: 'à remplir : ' + vide.map(c => c.libelle).join(', ') });
    // Un champ `fichier` colle en texte devient un fichier que new-run.py copie
    // a sa destination dans le workspace.
    champsDuMode.filter(c => c.fichier && champs[c.cle]).forEach(c => {
      const dir = path.join(STATE, wf.id, '_entrees'); fs.mkdirSync(dir, { recursive: true });
      const f = path.join(dir, `${Date.now()}-${c.cle}.txt`);
      fs.writeFileSync(f, champs[c.cle].endsWith('\n') ? champs[c.cle] : champs[c.cle] + '\n');
      champs[c.cle] = f;
    });
    const gabarit = ouv.slug || '{projet}';
    const slug = gabarit.replace(/\{(\w+)\}/g, (_, k) => champs[k] || '').replace(/^-+|-+$/g, '') || wf.id;
    if (!slug) return json(res, 400, { error: 'nom de run vide' });
    let ws;
    try {
      // new-run.py connait le mode, le cas et les parametres de run (v2) : il
      // copie les entrees du cas dans le workspace et ecrit run.json. On lui
      // passe tout ; l'entete (WORKSPACE.txt) reste ecrite ici, depuis le gabarit.
      const args = [path.join(FLUX, 'runtime', 'new-run.py'), wf.id, slug, '--mode', mode];
      Object.entries(champs).forEach(([k, v]) => { if (v !== '' && v != null) args.push('--champ', `${k}=${v}`); });
      ws = execFileSync('python3', args, { encoding: 'utf8' }).trim().split('\n').pop().trim();
    } catch (e) {
      return json(res, 500, { error: String((e.stderr || e.message || e)).slice(0, 400) });
    }
    if (ouv.entete) {
      const nom = path.basename(ws);
      const rempl = Object.assign({}, champs, {
        stamp: nom.startsWith(slug + '-') ? nom.slice(slug.length + 1) : nom,
        base: ws, iso: new Date().toISOString(),
      });
      const txt = ouv.entete.replace(/\{(\w+)\}/g, (_, k) => rempl[k] !== undefined ? rempl[k] : '');
      const cible = path.join(ws, 'WORKSPACE.txt');
      fs.writeFileSync(cible, txt);
      chownAgents(cible);
    }
    try {
      const cible = path.join(ws, 'WORKSPACE.txt');
      let txt = ''; try { txt = fs.readFileSync(cible, 'utf8'); } catch {}
      if (!/^mode:/m.test(txt)) fs.writeFileSync(cible, txt + (txt && !txt.endsWith('\n') ? '\n' : '') + 'mode: ' + mode + '\n');
    } catch {}
    try {
      const dir = path.join(STATE, wf.id, path.basename(ws));
      fs.mkdirSync(dir, { recursive: true });
      const f = path.join(dir, 'run.json');
      // new-run.py a deja ecrit run.json (mode, cas, parametres) : on complete
      // sans ecraser, sinon le cas et les parametres de la run disparaissent.
      let deja = {}; try { deja = JSON.parse(fs.readFileSync(f, 'utf8')); } catch {}
      fs.writeFileSync(f, JSON.stringify(Object.assign({ statut: 'en-cours', ouvert: new Date().toISOString() },
                                                       deja, { mode, champs }), null, 2) + '\n');
      chownAgents(f);
    } catch {}
    return json(res, 201, { workspace: path.basename(ws), mode });
  }

  // Supprimer un passage : celui qui n'aurait pas du partir. Ce qui precede
  // reprend la main, y compris pour le statut du nœud.
  m = p.match(/^\/api\/wf\/([\w-]+)\/passage\/([\w-]+)$/);
  if (m && req.method === 'DELETE') {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 400, { error: 'aucun workspace actif' });
    const n = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10));
    if (!n) return json(res, 400, { error: 'passage non precisé' });
    const f = path.join(STATE, wid, path.basename(ws), `${nid}.json`);
    let st; try { st = JSON.parse(fs.readFileSync(f, 'utf8')); }
    catch { return json(res, 404, { error: 'aucun état pour ce nœud' }); }
    const cible = (st.passages || []).find(x => x.n === n);
    if (!cible) return json(res, 404, { error: 'passage inconnu' });
    // ON NE REFUSE QUE LA PASSE QUI TOURNE, PAS TOUTES CELLES DU NŒUD. Le
    // garde-fou regardait s'il existait un lanceur vivant pour ce nœud — donc
    // une passe morte depuis longtemps devenait indestructible des qu'une
    // suivante repartait, ce qui est exactement le moment ou l'on veut effacer
    // la precedente. On demande maintenant : est-ce CELLE-CI qui tourne ?
    const enCours = suivi(wid, nid, ws) &&
                    (st.passage_courant === n || !st.passage_courant) &&
                    !cible.ended;
    const sessionVive = cible.session && vivante(cible.session);
    if (enCours || sessionVive)
      return json(res, 409, { error: 'ce passage tourne encore — arrête-le avant de l\'effacer' });
    if (cible.session) {
      try { execFileSync('tmux', ['kill-session', '-t', cible.session], { stdio: 'ignore' }); } catch {}
    }
    st.passages = (st.passages || []).filter(x => x.n !== n);
    const dernier = st.passages[st.passages.length - 1] || null;
    st.passage_courant = dernier ? dernier.n : 0;
    st.tmux = dernier ? dernier.session : undefined;
    // le statut du nœud etait celui du passage supprime : il redevient celui
    // d'avant, ou « a faire » s'il ne reste rien
    st.statut = dernier ? (dernier.verdict || 'en-cours') : 'a-faire';
    st.ended = dernier ? dernier.ended : undefined;
    fs.writeFileSync(f, JSON.stringify(st, null, 2));
    chownAgents(f);
    const dossier = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(n));
    try { fs.rmSync(dossier, { recursive: true, force: true }); } catch {}
    return json(res, 200, { supprime: n, reste: st.passages.length });
  }

  // Clore un nœud a la main : rejoue le validateur, ecrit le verdict, libere la
  // session. Pour les etapes que plus aucun runtime ne suit.
  m = p.match(/^\/api\/wf\/([\w-]+)\/clore\/([\w-]+)$/);
  if (m && req.method === 'POST') {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 400, { error: 'aucun workspace actif' });
    if (running.has(`${wid}:${nid}`) || suivi(wid, nid, ws)
        || travailleEncore(wf, ws, nid, u))
      return json(res, 409, { error: "l'agent de ce nœud travaille encore — sa session est "
                                     + "ouverte et il n'a pas posé son marqueur de fin" });
    return execFile('python3', [RUNNER, wid, nid, '--workspace', ws, '--clore'],
                    { encoding: 'utf8', timeout: 180000 }, (err, out, errOut) => {
      if (err) {
        const d = String((errOut || '') + (out || '') || err.message).slice(-400);
        return json(res, 500, { error: 'clôture échouée : ' + d });
      }
      return json(res, 200, { clos: true, node: nid, sortie: String(errOut || out).slice(-300) });
    });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/run\/([\w-]+)$/);
  if (m && req.method === 'POST') {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const key = `${wid}:${nid}`;
    const ws = activeWs(wf, u);
    if (!ws) return json(res, 400, { error: 'aucun workspace actif' });
    if (running.has(key) || suivi(wid, nid, ws))
      return json(res, 409, { error: 'déjà en cours' });
    running.add(key);
    const child = spawn('python3', [RUNNER, wid, nid, '--workspace', ws], { detached: true, stdio: 'ignore' });
    child.on('exit', () => running.delete(key));
    child.on('error', () => running.delete(key));
    child.unref();
    return json(res, 202, { started: true, node: nid, workspace: path.basename(ws) });
  }

  // REPRENDRE LA RUN. L'autopilote ne se reveille qu'a la FIN d'un nœud : une
  // run laissee en plan — session coupee, machine redemarree, borne atteinte —
  // reste donc immobile meme interrupteur allume, et rien ne le dit. Ce bouton
  // reveille l'orchestrateur tout de suite ; il lit l'etat et repart de la ou
  // c'est reste. Sans `--noeud-fini`, aucune borne ne se declenche : reprendre
  // n'est pas « finir un nœud », c'est demander une decision.
  m = p.match(/^\/api\/wf\/([\w-]+)\/reprendre$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 400, { error: 'aucune run' });
    // L'orchestrateur ne fait rien sans le toggle : reprendre l'allume donc,
    // avec la borne demandee. C'est le meme geste vu de l'interface — « repars,
    // et va jusque-la ».
    const f = path.join(ws, 'state', 'autopilote');
    const borne = (u.searchParams.get('jusqua') || '').trim();
    try {
      fs.mkdirSync(path.dirname(f), { recursive: true });
      fs.writeFileSync(f, new Date().toISOString() + '\n' + (borne ? 'jusqua=' + borne + '\n' : ''));
    } catch (e) { return json(res, 500, { error: String(e.message || e) }); }
    // v2 : c'est le sequenceur (programme) qui reprend, plus un agent.
    const child = spawn('python3', [path.join(FLUX, 'runtime', 'sequenceur.py'), wf.id, ws, '--reprendre'],
                        { detached: true, stdio: 'ignore' });
    child.unref();
    return json(res, 202, { repris: true, workspace: path.basename(ws), jusqua: borne || null });
  }

  // ARRETER LA RUN. Le pendant dur de « reprendre » : coupe la session vivante
  // de CETTE run tout de suite et eteint l'autopilote, pour que rien ne la
  // relance derriere. Le nœud coupe n'est pas juge ici -- son propre lanceur
  // constate la session morte et clot son etat lui-meme, comme pour toute
  // autre fin de session : aucune ecriture directe sur son fichier d'etat.
  m = p.match(/^\/api\/wf\/([\w-]+)\/arreter$/);
  if (m && req.method === 'POST') {
    const wf = loadWf(m[1]); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 400, { error: 'aucune run' });
    const fa = path.join(ws, 'state', 'autopilote');
    try { if (fs.existsSync(fa)) fs.unlinkSync(fa); } catch {}
    let noms = [];
    try {
      noms = execFileSync('tmux', ['ls', '-F', '#{session_name}'],
                          { encoding: 'utf8' }).split(/\s+/).filter(Boolean);
    } catch { noms = []; }
    const vivante = new Set(noms);
    const dir = path.join(STATE, m[1], path.basename(ws));
    let cible = null;
    try {
      for (const f of fs.readdirSync(dir)) {
        if (!f.endsWith('.json')) continue;
        const nid = f.slice(0, -5);
        let st; try { st = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); } catch { continue; }
        if (st.tmux && vivante.has(st.tmux)) { cible = { nid, sess: st.tmux }; break; }
      }
    } catch {}
    if (!cible) return json(res, 200, { arrete: null, autopilote: false });
    try { execFileSync('tmux', ['kill-session', '-t', cible.sess], { stdio: 'ignore' }); } catch {}
    return json(res, 200, { arrete: cible.nid, autopilote: false });
  }

  // rejeu du direct : la chronologie des ecrans archives, puis un ecran precis.
  // GET .../ecrans/<node>[?pass=N]        -> { total, ts:[...], sousAgents:[...] }
  // GET .../ecran/<node>?n=K[&pass=N]     -> { out } (contenu ANSI, tel qu'affiche)
  m = p.match(/^\/api\/wf\/([\w-]+)\/ecrans?\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); const ws = wf ? activeWs(wf, u) : null;
    const unitaire = p.includes('/ecran/');
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const wanted = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || (passages.length || 1));
    const pdir = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(wanted));
    const edir = path.join(pdir, 'ecrans');
    if (unitaire) {
      const n = Math.max(1, parseInt(u.searchParams.get('n') || '1', 10));
      let out = '';
      try { out = fs.readFileSync(path.join(edir, String(n).padStart(5, '0') + '.ans'), 'utf8'); } catch {}
      return json(res, 200, { n, out, cols: TERM_COLS, rows: TERM_ROWS });
    }
    let idx = [];
    try { idx = JSON.parse(fs.readFileSync(path.join(edir, 'index.json'), 'utf8')); } catch {}
    // sous-agents de la passe : un transcript par tache dispatchee
    let sousAgents = [];
    try {
      const launch = fs.readFileSync(path.join(pdir, 'launch.sh'), 'utf8');
      const sid = (launch.match(/--session-id\s+([0-9a-f-]{36})/) || [])[1];
      const cfg = process.env.CLAUDE_CONFIG_DIR
                  || CFG_DEFAUT;
      const base = path.join(cfg, 'projects');
      for (const d of fs.readdirSync(base)) {
        const sub = path.join(base, d, sid, 'subagents');
        if (!fs.existsSync(sub)) continue;
        sousAgents = fs.readdirSync(sub).filter(f => f.endsWith('.jsonl')).map(f => {
          const st = fs.statSync(path.join(sub, f));
          return { id: f.replace(/\.jsonl$/, ''), taille: st.size, maj: st.mtime };
        }).sort((a, b) => a.maj - b.maj);
        break;
      }
    } catch {}
    return json(res, 200, { pass: wanted, total: idx.length, ts: idx.map(x => x.ts),
                            instances: passages.map(x => ({ n: x.n, motif: x.motif })), sousAgents });
  }

  // un sous-agent : son transcript rendu en texte, meme cadre que le reste
  m = p.match(/^\/api\/wf\/([\w-]+)\/sousagent\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); const ws = wf ? activeWs(wf, u) : null;
    const aid = u.searchParams.get('id') || '';
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const wanted = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || (passages.length || 1));
    const pdir = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(wanted));
    let lignes = [];
    try {
      const launch = fs.readFileSync(path.join(pdir, 'launch.sh'), 'utf8');
      const sid = (launch.match(/--session-id\s+([0-9a-f-]{36})/) || [])[1];
      const cfg = process.env.CLAUDE_CONFIG_DIR
                  || CFG_DEFAUT;
      const base = path.join(cfg, 'projects');
      for (const d of fs.readdirSync(base)) {
        const f = path.join(base, d, sid, 'subagents', aid + '.jsonl');
        if (!fs.existsSync(f)) continue;
        for (const l of fs.readFileSync(f, 'utf8').split('\n').filter(Boolean)) {
          let o; try { o = JSON.parse(l); } catch { continue; }
          const msg = o.message || {};
          const role = msg.role || o.type || '';
          const cont = Array.isArray(msg.content) ? msg.content
                     : (msg.content ? [{ type: 'text', text: msg.content }] : []);
          for (const c of cont) {
            if (c.type === 'text' && (c.text || '').trim()) lignes.push({ role, texte: c.text.trim() });
            else if (c.type === 'tool_use')
              lignes.push({ role: 'outil', texte: c.name + ' ' + JSON.stringify(c.input || {}).slice(0, 300) });
            else if (c.type === 'tool_result') {
              const t = typeof c.content === 'string' ? c.content
                      : (Array.isArray(c.content) ? c.content.map(x => x.text || '').join(' ') : '');
              if (t.trim()) lignes.push({ role: 'resultat', texte: t.trim().slice(0, 900) });
            }
          }
        }
        break;
      }
    } catch (e) { lignes = [{ role: 'erreur', texte: String(e && e.message || e) }]; }
    return json(res, 200, { id: aid, lignes: lignes.slice(-800) });
  }

  // transcript (lecture seule) : le deroule COMPLET de la session, la ou la console
  // ne peut montrer que l'ecran courant (Claude Code occupe l'ecran alterne, que
  // tmux n'archive pas). Source : le JSONL ecrit en continu par la session.
  m = p.match(/^\/api\/wf\/([\w-]+)\/transcript\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); const ws = wf ? activeWs(wf, u) : null;
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const wanted = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || (passages.length || 1));
    const pass = passages.find(x => x.n === wanted) || passages[passages.length - 1] || null;
    let lignes = [];
    try {
      const dir = path.join(STATE, wid, path.basename(ws), nid, 'passes', String(wanted));
      const launch = fs.readFileSync(path.join(dir, 'launch.sh'), 'utf8');
      const sid = (launch.match(/--session-id\s+([0-9a-f-]{36})/) || [])[1];
      const cfg = (launch.match(/CLAUDE_CONFIG_DIR=(\S+)/) || [])[1] || process.env.CLAUDE_CONFIG_DIR
                  || CFG_DEFAUT;
      // le projet porte le repertoire de travail, encode avec des tirets
      const base = path.join(cfg, 'projects');
      let fichier = null;
      for (const d of fs.readdirSync(base)) {
        const f = path.join(base, d, sid + '.jsonl');
        if (fs.existsSync(f)) { fichier = f; break; }
      }
      if (fichier) {
        const brut = fs.readFileSync(fichier, 'utf8').split('\n').filter(Boolean);
        for (const l of brut) {
          let o; try { o = JSON.parse(l); } catch { continue; }
          const msg = o.message || {};
          const role = msg.role || o.type || '';
          const cont = Array.isArray(msg.content) ? msg.content : (msg.content ? [{type:'text', text:msg.content}] : []);
          for (const c of cont) {
            if (c.type === 'text' && (c.text || '').trim())
              lignes.push({ role, texte: c.text.trim() });
            else if (c.type === 'tool_use')
              lignes.push({ role: 'outil', texte: c.name + ' ' +
                JSON.stringify(c.input || {}).slice(0, 400) });
            else if (c.type === 'tool_result') {
              const t = typeof c.content === 'string' ? c.content
                      : (Array.isArray(c.content) ? c.content.map(x => x.text || '').join(' ') : '');
              if (t.trim()) lignes.push({ role: 'resultat', texte: t.trim().slice(0, 1200) });
            }
          }
        }
      }
    } catch (e) { lignes = [{ role: 'erreur', texte: String(e && e.message || e) }]; }
    return json(res, 200, { pass: wanted, instances: passages.map(x => ({ n: x.n, motif: x.motif })),
                            lignes: lignes.slice(-1200) });
  }

  // console tmux (lecture seule) : capture-pane du nœud, séquences ANSI COMPRISES (-e).
  // Le client est un xterm.js de la même géométrie -> rendu identique à `tmux attach`.
  m = p.match(/^\/api\/wf\/([\w-]+)\/console\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); const ws = wf ? activeWs(wf, u) : null;
    const ndir = ws ? path.join(STATE, wid, path.basename(ws), nid) : null;

    // Un nœud relancé = plusieurs instances Claude Code. On les liste toutes
    // (chacune son transcript) et on sert celle demandée ; par défaut la dernière.
    let passages = [];
    try {
      const st = JSON.parse(fs.readFileSync(path.join(STATE, wid, path.basename(ws), `${nid}.json`), 'utf8'));
      passages = st.passages || [];
    } catch {}
    const wanted = Math.max(1, parseInt(u.searchParams.get('pass') || '0', 10) || (passages.length || 1));
    const pass = passages.find(p => p.n === wanted) || null;
    const sess = pass ? pass.session : `flux-${wid}-${nid}`;

    // le nœud pose ce marqueur quand il a fini ; la session, elle, peut trainer
    const fini = fs.existsSync(path.join(ndir || '', 'passes', String(wanted), 'DONE'));
    let out = '', live = false, cols = TERM_COLS, rows = TERM_ROWS;
    try {
      execFileSync('tmux', ['has-session', '-t', sess], { stdio: 'ignore' });
      live = !fini;
      out = execFileSync('tmux', ['capture-pane', '-p', '-e', '-t', sess, '-S', '-3000'],
                         { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
      const dim = execFileSync('tmux', ['display', '-p', '-t', sess, '#{pane_width} #{pane_height}'],
                               { encoding: 'utf8' }).trim().split(/\s+/).map(Number);
      if (dim.length === 2 && dim[0] > 0) { cols = dim[0]; rows = dim[1]; }
    } catch {
      // instance terminée : son scrollback est figé sur disque
      if (ndir) {
        for (const fp of [path.join(ndir, 'passes', String(wanted), 'run.out'),
                          path.join(ndir, 'run.out')]) {          // ancien schéma
          if (fs.existsSync(fp)) { out = fs.readFileSync(fp, 'utf8').slice(-400000); break; }
        }
      }
    }
    const instances = passages.map(p => ({
      n: p.n, verdict: p.verdict || (p.ended ? '?' : 'en cours'), motif: p.motif,
      started: p.started, duree_s: p.duree_s, fin: p.fin,
      claude_session_id: p.claude_session_id,
      nb_refus: (p.erreurs || []).length,
      // Le compte qui a fait tourner CETTE instance — pas celui choisi pour la
      // prochaine. Les passages d'avant le moteur par run n'en portent pas.
      moteur: p.moteur || null,
    }));
    return json(res, 200, { live, session: sess, out, cols, rows, pass: wanted, instances });
  }

  m = p.match(/^\/api\/wf\/([\w-]+)\/log\/([\w-]+)$/);
  if (m) {
    const [, wid, nid] = m;
    const wf = loadWf(wid); if (!wf) return json(res, 404, { error: 'workflow inconnu' });
    const ws = activeWs(wf, u); if (!ws) return json(res, 200, { log: '' });
    const dir = path.join(STATE, wid, path.basename(ws), nid);
    let out = '';
    for (const f of ['run.err', 'run.out']) {
      const fp = path.join(dir, f);
      if (fs.existsSync(fp)) out += `--- ${f} ---\n` + fs.readFileSync(fp, 'utf8').slice(-4000) + '\n';
    }
    return json(res, 200, { log: out });
  }

  send(res, 404, 'text/plain', '404');
});

// Le flux du terminal passe par un websocket : on le relaie octet pour octet.
server.on('upgrade', (req, socket, tete) => {
  const u = new URL(req.url, 'http://x');
  const m = u.pathname.match(/^\/(term|relire)\/([\w-]+)\/([\w-]+)(\/.*)?$/);
  if (!m) return socket.destroy();
  const [, genre, wid, nid, reste] = m;
  const wf = loadWf(wid); const ws = wf ? activeWs(wf, u) : null;
  if (!ws) return socket.destroy();
  let sess;
  if (genre === 'relire') {
    const n = Math.max(1, parseInt(u.searchParams.get('pass') || '1', 10));
    const r = ensureRelecture(wf, ws, nid, n);
    if (!r) return socket.destroy();
    sess = r.sess;
  } else {
    sess = sessionDe(wf, ws, nid, u);
    if (!vivante(sess)) sess = dernierTerm.get(`${wid}:${nid}`) || sess;
  }
  if (!vivante(sess)) return socket.destroy();
  const base = `/flux/${genre}/${wid}/${nid}`;
  const t = ensureTtyd(sess, base);
  const chemin = base + (reste || '/') + (u.search || '');
  attendrePort(t.port, 25).then(() => {
  const amont = net.connect(t.port, '127.0.0.1', () => {
    let tete_ = `GET ${chemin} HTTP/1.1\r\n`;
    for (let i = 0; i < req.rawHeaders.length; i += 2)
      tete_ += `${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}\r\n`;
    amont.write(tete_ + '\r\n');
    if (tete && tete.length) amont.write(tete);
    amont.pipe(socket); socket.pipe(amont);
  });
  amont.on('error', () => socket.destroy());
  socket.on('error', () => amont.destroy());
  });
});

// L'ADRESSE D'ECOUTE EST UNE DECISION DE SECURITE, PAS UN DEFAUT.
// Sur 0.0.0.0 le serveur repond a qui trouve le port ; borne a l'adresse
// d'un reseau prive (VPN), il ne repond qu'aux appareils du reseau prive — proxy local
// compris, qui ne peut donc plus le republier sans qu'on l'ait voulu.
server.listen(PORT, HOTE, () => console.log(`flux server on ${HOTE}:${PORT}`));
