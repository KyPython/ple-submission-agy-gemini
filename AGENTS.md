# agy-gemini — Agent Operating Doctrine

**Last updated:** 2026-11-16
**This repo:** https://github.com/KyPython/ple-submission-agy-gemini
**Workspace copy:** `/Users/ky/physical-engineering/AGENTS.md`
**Notion:** https://app.notion.com/p/4168621e0a7a8103b151fa402ab028c9
**Attempt log:** `/Users/ky/physical-engineering/docs/ATTEMPT_LOG.md`

Edit this file and the workspace `AGENTS.md` in the same change.

## Objective

The contest scores the mean task reward, from 0 to 1, of the engineering deliverable. A spec, unit test, bundle build, or harness commit is preparation. It becomes contest progress when `eval/run.py` or the official evaluator records a reward for this tree.

This repository is the canonical Physical-Engineering's Last Exam entry. Model `google-cloud/gemini-4-argon`, `AGY_EFFORT=high`, Antigravity CLI `agy` 1.4.10. Stage 1 code baseline is `4597775` on `feat/frontier-stage1-readiness` (2026-11-14). On 2026-11-16 `main` was still `222df50`.

## How to work

1. Read the last entry in the attempt log.
2. When this tree has no dev manifest, the job is one dev-set evaluation. Official evaluation uses Google-sponsored `google-cloud/*` models, on the competition server or the local proxy documented in the workspace `ple-bench/docs/models.md`. A commercial API outside that path waits for an explicit yes from Ky.
3. When a manifest exists, choose one recorded failure, change this harness to address it, and re-export with `eval/run.py` from this same tree.
4. The next edit to `serve.py`, `task_mcp.py`, `taskbridge.py`, or `deliverable_gate.py` names the task, the reward, and the failure it addresses. Stage 1 through `4597775` is already the code baseline.
5. In that same session, append the attempt log and update the Notion page. Submit to Kaggle only after Ky explicitly says to submit.

## Reference baseline

The official Opus sample mean is **0.4912** (manifest 2026-10-20, harness `my-harness`, commit `6ab05b9f5f25`). That is the sample's score. Ours starts at the first reward recorded here. Sample zeros, as hypotheses to measure: `foundation-strategy`, `solar-witness-test-package`, `data-hall-mep-routing`, `data-hall-mep-routing-offset`.

Those artifacts belong to the sample tree. They are not this entry's `artifacts/`.

## Fair play

The harness solves the task it is given and stays task-agnostic. Submission artifacts are the `eval/run.py` export for the committed harness and the `model_api` in `submission.yaml`. The serve container reaches the model endpoint. The task container stays offline. Reading graders from the harness, branching on task ids, and hand-editing `artifacts/` are disqualifying.

## Current evidence (2026-11-16)

No Kaggle submission. No `artifacts/` in this repo. Offline Stage 1 tests are recorded in `specs/frontier-stage1-readiness/tasks.md` as 14 + 6 + 4 passing, plus `kit validate`. Cached base images had no PDF rasterizer, and task images had not been pulled. Treat drawing reads as unproven on a real task container until a run shows otherwise.

## Architecture

- **Agent:** Antigravity CLI 1.4.10 (`agy -p`)
- **Model:** `google-cloud/gemini-4-argon` (reasoning effort `high`)
- **Task interaction:** stdio MCP via `task_mcp.py` and `taskbridge.Relay`
- **After the model returns:** `deliverable_gate.py` checks the deliverable before `solve()` exits

Offline packaging, from the workspace `ple-bench` directory:

```bash
uv run python -m kit validate ../agy-gemini
uv run python -m kit build ../agy-gemini
```

A passing check is packaging evidence. The contest number is the reward in the manifest.
