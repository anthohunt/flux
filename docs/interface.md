# flux — public interface for pipelines

This page is the contract between flux (the engine) and a pipeline that runs on it (for example a
web-app pipeline or a research-watch pipeline living in its own repository). A pipeline never
copies flux: it ships its graph, its validators, its skills and its tools, and points flux at them.

## 1. Plugging a pipeline into flux

| What the pipeline ships | Where flux looks for it |
|---|---|
| Graph `workflows/<id>.json` | every folder of `FLUX_WORKFLOWS_DIR` (`:`-separated; default `<flux>/workflows`), first match wins, e.g. `FLUX_WORKFLOWS_DIR=$PWD/flux/workflows:$PWD/my-pipeline/workflows` |
| Validators (gates) `<name>.py` exposing `validate(workspace) -> list[str]` | every folder of `FLUX_VALIDATORS_PATH` (`:`-separated), then `<flux>/validators`, then `<FLUX_PIPELINE_RUNTIME>/validators` |
| Skills injected into agents (`consignes` of a node) `<skill>/SKILL.md` | every folder of `FLUX_SKILLS_DIR` (`:`-separated; default `<flux>/skills`), first match wins |
| Optional run tools: `build_run_report.py`, `rendre_transcripts.py`, `corriger_accents_run.py`, `etat-run.py` | `FLUX_PIPELINE_RUNTIME` (default `<first FLUX_SKILLS_DIR>/pipeline-runtime`); each is used only if present |
| Runs (working folders) | the graph's `workspace_root` (absolute, `~/…` or `$VAR/…`), default `<FLUX_RUNS_HOME>/<id>-runs` |

A pipeline may instead ship its own gate launcher: `FLUX_VALIDATE=/path/to/validate.py` replaces
`runtime/validate.py`; it is called as `<script> <validator-id> <workspace>` and must exit 0 (green),
2 (contracts broken, numbered lines `  1. …` on stderr) or 3 (internal error).

A validator identifier `abc-def` resolves to the module `abc_def.py`. A `validators.json` file in a
validators folder can map other identifiers to module names: `{"1": "etape1_prevol"}`.

## 2. Commands

All paths are relative to the flux repository.

```bash
# Open a run (creates <workspace_root>/<slug>-<date>/ with WORKSPACE.txt and state/<wf>/<run>/run.json)
python3 runtime/new-run.py <wf> <slug> [--mode banc|production] [--cas <id>] [--champ key=value]... [--depuis DIR|URL] [--note TEXT]

# Play one node on a run (blocking; tmux engine: opens a Claude Code session in tmux and waits for it)
python3 runtime/run-node.py <wf> <node> --workspace <workspace> [--brief-only]

# Run a gate by hand (exit 0 = green, 2 = contracts broken, 3 = internal error)
python3 runtime/validate.py <validator-id> <workspace>

# Chain the steps (normally woken up by run-node.py at the end of each node)
python3 runtime/sequenceur.py <wf> <workspace> [--reprendre | --clore | --dry-run]

# Check every graph against runtime/workflow.schema.json
python3 runtime/verifier-graphes.py            # -> GRAPHES CONFORMES

# Scaffold a new pipeline (graph + one validator per node + an entry-point skill)
python3 tools/nouvelle-pipeline.py <id> "<Title>" [--noeuds cadrer:local,construire:agent,livrer:agent]

# Play a whole bench lot on several models and write a campaign
python3 tools/jouer-lot.py <wf> --lot boucle|defi|sentinelle --modeles opus,ollama:qwen3:4b --nom <name>

# Play an interactive (`local`) step on another machine
runtime/flux-local <wf> <node> [--pull | --push | --session]
```

The chain only runs when the run's autopilot is on: the file `<workspace>/state/autopilote` exists
(second line `jusqua=<node>` stops it after that node).

## 3. HTTP API of the console (`node runtime/server.js`)

| Method and path | Effect |
|---|---|
| `GET /api/workflows` | list of graphs |
| `GET /api/wf/<wf>` | the graph and the state of each node of the active run |
| `POST /api/wf/<wf>/nouvelle-run?mode=banc\|production` (JSON body: the `parametres_run` values) | opens a run |
| `POST /api/wf/<wf>/run/<node>` | launches a node (non-blocking) |
| `GET/POST /api/wf/<wf>/autopilote?on=1&jusqua=<node>` | reads / sets the autopilot |
| `GET /api/wf/<wf>/mesures/<node>` | time, turns and tokens of each pass |
| `POST /api/wf/<wf>/valider?noeud=<node>&f=<deliverable>` | saves a corrected deliverable as a bench case |

Default address `127.0.0.1:8901` (`FLUX_HOTE`, `FLUX_PORT`). No authentication.

## 4. Graph format (summary of `runtime/workflow.schema.json`)

```text
workflow
  id, name, description
  workspace_root                     absolute, ~/…, or $VAR/…
  parametres_run: [ {id, label, type: fichier|texte|choix, requis, mode, destination, options, defaut} ]
  moteurs_api:    [ {id, nom, type: anthropic|ollama|llamacpp, url, modele} ]
  garde:          {toutes_les_min, modele, relances_max}      optional
  nodes: [ node ]

node
  id, n, name
  type:      agent | local | human | fanout                   agent = launched by flux; local = played in a conversation; human = waits for a person
  moteur:    tmux | api                                       default tmux
  modele:    opus | sonnet | haiku | ollama:<name> | llamacpp:<name> | {parameter}
  entree:    [ paths relative to the workspace ]
  livrables: [ paths relative to the workspace ]
  juge:      {script, corrige, bareme} | {type: "presence"}   or `juges: [...]`
  schema:    path to the JSON schema of the output             api engine
  gate:      {validateur: "<id>"}                              optional
  consignes: [ skill names ]   (tmux) | [ text ]   (api)
  tools:     [ Claude Code tools allowed ]                     tmux engine
  objectif, taches, taches_detail                              written into the agent's brief
  suivant:   [ node ids ]                                      empty = end of the run
  relances_max, passes_max
```

Paths in a graph (`workspace_root`, `ouverture.sources.*.dossier`) may start with `~/` or contain
`$VAR`: flux expands them.

## 5. Python helpers for pipeline tools

A pipeline's own tools may import `runtime/socle.py` (add `<flux>/runtime` to `sys.path`; the
repository root is `FLUX_RACINE`, default the parent of `runtime/`). Stable functions:
`racine()`, `dossier_state()`, `dossier_run(wf, ws)`, `charger_graphe(wf)`, `fichier_workflow(wf)`,
`workspace_root(graph)`, `skills_dirs()`, `fichier_skill(name)`, `charger_moteurs()`,
`lire_json(path, default)`, `ecrire_json(path, obj)`, `now()`, `notifier(...)`.

## 6. Environment variables

See `.env.example` for the full list with defaults. The ones a pipeline usually sets:
`FLUX_WORKFLOWS_DIR`, `FLUX_VALIDATORS_PATH`, `FLUX_SKILLS_DIR`, `FLUX_PIPELINE_RUNTIME`,
`FLUX_RUNS_HOME`, `FLUX_STATE_DIR`, `FLUX_CLAUDE_BIN`, `CLAUDE_CONFIG_DIR`, `FLUX_NOTIF_URL`.

Claude accounts ("engines") are declared in `moteurs.json` (copy of `moteurs.example.json`). A run
selects one by writing its id into `<workspace>/state/moteur`.
