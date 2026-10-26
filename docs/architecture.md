# flux — architecture

## The pieces

```text
            ┌──────────────── console (public/index.html) ────────────────┐
            │  workflows → campaign (cases × models) → case → passes      │
            └───────────────▲─────────────────────────────┬───────────────┘
                            │ HTTP (runtime/server.js)    │ launch a node
                            │                             ▼
 workflows/<id>.json ──► runtime/run-node.py ──► engine tmux : Claude Code session in tmux
 (the graph = the truth)        │                engine api  : runtime/moteurs/api.py
                                │                              (claude -p | Ollama | llama.cpp)
                                ▼
                 end of node: transcripts → gate (runtime/validate.py) → judge (bench only)
                                │
                                ▼
                 runtime/sequenceur.py ── green → next node · red → retry · exhausted → blocked + alert
                                ▲
                 runtime/garde.py (optional): every N min, a small model reads etat-compact.json
                                and may only retry, mark red, or alert
```

## A run

- **Opening.** `runtime/new-run.py` creates a working folder under the graph's `workspace_root`,
  copies the run's input (a bench case's input files, or the production file given by
  `--champ`), and writes `state/<wf>/<run>/run.json` (`mode`, `cas`, `statut`, parameters).
- **A node.** `runtime/run-node.py` writes the agent's brief from the graph (objective, imposed
  tasks, deliverables, refusals of the previous pass, socle rules), launches the engine, and
  watches it. With the tmux engine, the end of the step is a sentinel the agent writes; a dead
  session, a frozen screen, an expired login or a usage wall end the pass as a **failure**.
- **A pass.** Each attempt of a node is recorded in `state/<wf>/<run>/<node>.json`: start, end,
  outcome, engine, model, measures (time, turns, tokens, cost), input, output, score, guard flag.
- **Scoring.** On the bench, `runtime/juge.py` runs the node's judge script against the case's
  expected answer and writes `{score, bareme, lignes}` into the pass. A judge is a program, never a
  model. In production there is no score; the human review in the console turns a corrected
  deliverable into a new bench case.
- **Gate.** If the node declares `gate.validateur`, `runtime/validate.py` must be green before the
  next node starts. The agent is given the same command so it can check itself before finishing.
- **Chaining.** With the autopilot on (`<workspace>/state/autopilote`), `runtime/sequenceur.py`
  applies one rule: green → next node, red or failure → retry up to `relances_max`, then
  `bloque` and an alert (`FLUX_NOTIF_URL`, then `FLUX_ALERTE_CMD`).

## Bench cases

`cas/<id>/` holds the input (`mail.md`) and one expected answer per judge
(`golden-formulaire.json`, `golden-actions.json`, `golden-evenements.json`). `cas/cas.json` declares
each case and its lot: `sentinelle` (a permanent guard), `boucle` (the cases on which instructions
are tuned), `defi` (kept apart, played to measure). A **measurement lot** that must never be seen
while tuning can live outside the repository (`FLUX_MESURE`); runs opened on it keep their state
there.

The ten cases shipped here are invented French e-mail threads (a fictional recipient in a fictional
town) with traps: a moved appointment, a cancellation, a relative deadline, noisy footers, an
instruction injected in the body.

## Model descent

For a workflow meant for production, flux does not pick a model, it fixes a quality and descends:

1. **The target is a score, not a model.** A large model sets the target: its score on the
   evaluation set, minus a written tolerance.
2. **Descend one step at a time**, from the largest to the smallest model that can be deployed:
   tune the node's instructions on the training lot, check on the evaluation lot, stop when the
   target is reached or when iterations stop improving.
3. **Keep the smallest model that reaches the target**, write it into the graph (`modele`), with
   the table that justified it (score, seconds per item, memory).
4. **Measure a local model with the engine that will run it in production** (`llamacpp:<name>`
   when the product embeds llama.cpp), so that the bench and the product share the same binary and
   the same output grammar.

`tools/jouer-lot.py <wf> --lot <lot> --modeles a,b --nom <campaign>` plays a whole lot on several
models and writes `state/<wf>/campagnes/<campaign>.json` (one line per run, a summary per model:
mean score, failures, seconds per item, tokens). The console lists campaigns side by side.
