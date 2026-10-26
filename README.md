# flux — run and measure pipelines of AI agents

flux plays a **workflow** — a graph of steps described in JSON (`workflows/<id>.json`) — on a
**run** (a working folder), step after step, and shows everything in a web console: every pass
of every step with its input, its output, its duration, its tokens, its cost and **its score**.

It is the engine behind several agent pipelines (a web-app building pipeline, a research-watch
pipeline). Those pipelines live in their own repositories and use flux as a dependency.

> The code comments and most user-facing strings are in French.

## Ideas

- **One step = one deliverable + one judge.** A node declares what it receives (`entree`), what it
  returns (`livrables`), which model plays it (`modele`) and how it is scored (`juge`). The graph
  checker (`runtime/verifier-graphes.py`) refuses a graph without them.
- **Two engines.** `tmux`: a Claude Code session with tools, for "agent" steps (the session runs in
  tmux, the console attaches a live terminal to it). `api`: one direct model call (Anthropic through
  `claude -p`, Ollama or llama.cpp over HTTP) whose output is constrained by a JSON schema, for
  "form" steps.
- **Bench or production, per run.** On the bench, a run receives a *case* (`cas/<id>/`: input +
  expected answer) and each step is scored by a script (`runtime/juges/*.py`), never by a model.
  In production the input is new and there is no score; validating or correcting the final
  deliverable in the console saves it as a new bench case.
- **A program chains the steps, a guard watches.** `runtime/sequenceur.py`: green pass → next node;
  red pass or failure → retry up to `relances_max`; exhausted → run `bloque` + alert. A dead tmux
  session, an expired login or a quota wall are failures, never successes. `runtime/garde.py`: a
  small model reads a compact state every N minutes and can only retry, mark red or alert.
- **Gates.** A node can declare `gate: {validateur: "<id>"}`: `runtime/validate.py` runs that
  validator on the deliverables and the next node does not start until it is green.
- **Model descent.** For a production workflow, fix a target score with a large model, then try
  smaller and smaller models on the same bench (`tools/jouer-lot.py`) and keep the smallest one
  that reaches the target. See `docs/architecture.md`.

## Requirements

- Linux or macOS, Python ≥ 3.10, Node.js ≥ 18, `tmux`, `git`.
- Python package: `jsonschema` (graph checker). Everything else is the standard library.
- For agent steps (engine `tmux`) and Anthropic models (engine `api`):
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in. Claude Code
  is a separate product, subject to Anthropic's terms; flux only calls it. Run `claude` once by hand
  (theme, login) before the first agent step: an agent session left on the first-run screens never
  starts working and ends as a failure.
- Optional: [ttyd](https://github.com/tsl0922/ttyd) to attach a live terminal to an agent session in
  the console.
- Optional: [Ollama](https://ollama.com) or a [llama.cpp](https://github.com/ggml-org/llama.cpp)
  server for local models.

## Install

```bash
git clone <this repository> flux && cd flux
python3 -m pip install -r requirements.txt
cp moteurs.example.json moteurs.json     # one entry per Claude account you want to use
cp .env.example .env                     # optional settings, all have defaults
set -a; . ./.env; set +a
python3 runtime/verifier-graphes.py      # -> GRAPHES CONFORMES
FLUX_SANS_RESEAU=1 python3 -m unittest discover -s tests
```

## Run

```bash
node runtime/server.js                   # the console on http://127.0.0.1:8901
```

The console has **no authentication** and listens on localhost by default. To reach it from
another machine, put it behind a private network or an authenticated reverse proxy
(`FLUX_HOTE=0.0.0.0` opens it).

A bench run of the `agenda` workflow (one `api` node: an e-mail thread → calendar events, scored
on 72 fixed points) from the command line:

```bash
python3 runtime/new-run.py agenda essai --mode banc --cas mail-02 --champ modele=sonnet
python3 runtime/run-node.py agenda evenements --workspace ~/agenda-runs/<run>
```

With a local model instead: `--champ modele=ollama:qwen3:4b` (Ollama on `127.0.0.1:11434`, or set
`FLUX_OLLAMA_URL`).

A production run of the agent example (`exemple-agent`: a Claude Code session writes a summary
note, a validator checks its structure):

```bash
python3 runtime/new-run.py exemple-agent demo --mode production --champ sujet=/path/to/topic.md
python3 runtime/run-node.py exemple-agent rediger --workspace ~/exemple-agent-runs/<run>
```

Or open the console, pick a workflow, click « + Nouvelle run », and switch on the autopilot.

## Write a workflow

1. Write `workflows/<id>.json`: `id`, `name`, `workspace_root`, `parametres_run` (what a new run
   asks for: case, model, input file…), `nodes`. The contract is `runtime/workflow.schema.json`;
   `tools/nouvelle-pipeline.py <id> "<Title>"` scaffolds a conforming graph and its validators.
2. Each node: `id`, `n`, `name`, `moteur`, `modele`, `entree`, `livrables`, `suivant`, `juge`
   (`{script, corrige, bareme}` or `{type: "presence"}`), `schema` for the `api` engine, and
   optionally `gate`, `consignes` (skills injected into the agent), `tools`, `taches`.
3. `python3 runtime/verifier-graphes.py` must print `GRAPHES CONFORMES`.
4. A bench case is `cas/<id>/` with the input (e.g. `mail.md`) and the expected answer named as
   `juge.corrige`, declared in `cas/cas.json`.

The public interface used by other pipelines (launch commands, graph format, environment
variables) is described in `docs/interface.md`.

## Layout

- `runtime/` — `server.js` (console + HTTP API), `run-node.py` (runs one node; tmux engine),
  `moteurs/api.py` (api engine), `sequenceur.py`, `garde.py`, `juge.py` + `juges/`,
  `validate.py` (gates), `socle.py` (shared state helpers), `new-run.py`, `verifier-graphes.py`,
  `mesures-noeud.py` (time, turns and tokens of a pass, read from Claude Code session logs),
  `flux-local` (play an interactive step from another machine).
- `workflows/` — example graphs: `mail` (two `api` nodes), `mail-1noeud`, `agenda`,
  `exemple-agent` (one `tmux` agent node with a gate).
- `validators/` — validators of the example graphs.
- `cas/` — ten invented bench cases (French e-mail threads and their expected answers).
- `public/` — the console (single HTML page, xterm.js vendored).
- `tools/` — campaigns (`jouer-lot.py`, `jouer-test.py`, `jouer-cas.sh`), graph scaffolding,
  failure marking, a guard against starting a given node.
- `tests/` — unit and integration tests (fake `claude`, fake Ollama, fake notification server).
- `state/` (not versioned) — `state/<wf>/<run>/`: `run.json`, one `<node>.json` per node (its
  passes), `etat-compact.json`, `sequenceur.log`, `garde.jsonl`.

## Licenses

- Code: [Apache License 2.0](LICENSE) — see [NOTICE](NOTICE).
- Documentation, graph descriptions, schemas' prose and the example cases in `cas/`:
  [Creative Commons Attribution 4.0 International](LICENSE-docs).
- Third party: xterm.js 5.5.0 (MIT, `public/vendor/LICENSE-xterm`). Test tools: puppeteer
  (Apache 2.0). Runtime dependencies (not included): jsonschema (MIT), Claude Code
  (Anthropic commercial terms), Ollama and llama.cpp (MIT).
