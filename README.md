# Building Remote Evals with Harbor

This is the starting flight agent. Its harness calls live Fli and runs Bash in the
workspace supplied at launch.

```sh
uv sync --locked
uv run --env-file .env python -m agent --workspace "$(mktemp -d)" "Find a nonstop flight from SFO to JFK next month."
```

Set `OPENROUTER_API_KEY` in your local `.env` for live inference.
