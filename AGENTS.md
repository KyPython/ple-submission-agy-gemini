# PLE Frontier Submission: agy-gemini (Antigravity on Gemini 4 Argon)

This repository packages the Google Antigravity CLI (`agy`) headless harness for the
**Physical-Engineering's Last Exam (PLE)** competition on Kaggle / Google Research.

## Architecture

- **Agent:** Antigravity CLI 1.4.10 (`agy -p`)
- **Model:** `google-cloud/gemini-4-argon` (Reasoning Effort: `high`)
- **Task Interaction:** Stdio MCP via `task_mcp.py` + `taskbridge.Relay`
- **Guardrails:** Explicit deliverable identification, early skeleton materialization, and pre-completion integrity verification.

## Local Verification Commands

```bash
# Offline spec validation
uv run python -m kit validate .

# Offline bundle build verification
uv run python -m kit build .

# Pre-flight container check
/Users/ky/physical-engineering/scripts/check_submission.sh .
```
