# Remote evals with Harbor

Can an AI agent find the cheapest nonstop flight, stick to a budget, and show
where its answer came from? This tutorial runs those checks against a test
deployment. [Harbor](https://docs.harborframework.com/) gives each trial a fresh
workspace, records the agent’s actions, and grades its report separately.

[View the public Harbor job](https://hub.harborframework.com/jobs/b3b9d703-ea26-40e5-b510-19ed640d7f76) · 3 of 3 trials passed.

## Three examples

All three use the same SFO → JFK workflow. The [task builder](harbor/task/package.py)
switches the tool data or the rule for accepting an answer.

| Suite | How it works | Result from the public job |
| --- | --- | --- |
| `smoke` | Replay a saved inventory and check the exact cheapest flight, date, and fare. | AA166, Dec 3, $174 · **1/1** |
| `budget` | Replay the same inventory with a $150 cap. Check that the agent reports no match if nothing qualifies. | `{"status":"no_match"}` · **1/1** |
| `live-smoke` | Use live Fli and check the report’s format, route, and date. The trace shows whether the searches succeeded. | 3 dates, 21 offers on the selected date, valid report · **1/1** |

The public job ran [release `05cd413`](https://github.com/punitarani/remote-evals/commit/05cd413c140aa2e0727a93ac7d2d5c87fb1876db)
on OpenRouter with `deepseek/deepseek-v4.1-flash-1017`. The first two trials
shared one snapshot: 62 offers for December 2–4, 2026, with SHA-256
`65c04a06b021e822862ba94ee225fb21ce74dd5954a58962a212f2878ef36f92`.
The live trial does not grade against a fixed fare.

## Capture the data

From this repo, with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv run --locked python harbor/task/capture_fli.py \
  --origin SFO --destination JFK --output jobs/fli-snapshot.json
```

By default, this captures three days starting 21 days from today (UTC). Use
`--start-date YYYY-MM-DD` and `--days` (2–7) to change the window. The script
keeps every nonstop offer Fli returns in scope and checks each day’s lowest
fare against Fli’s date search. It prints the snapshot’s SHA-256 checksum after
saving. If coverage is incomplete or prices disagree, it leaves the output untouched.

The examples ship with a [verified snapshot](harbor/task/fixtures/sfo-jfk.json).
To use a new capture, review it and commit it in place of that file.
The emulated tool uses the same Fli tool interface as the live server, but reads
the snapshot instead of calling the network. It freezes the clock at capture
time and rejects requests outside the captured scope. Identical supported
requests return identical data, and identical reports earn identical scores.
The model may still take different steps.

## Run the examples

You’ll need a Harbor account, an OpenRouter API key, and a configured
[ngrok](https://ngrok.com/docs/share-localhost/tunnels) CLI. uv manages Python 3.14
for the service; the Harbor adapter has its own Python 3.12 lockfile.

1. Copy [.env.example](.env.example) to `.env` if you don’t already have one. Set
   `OPENROUTER_API_KEY` and a random `BRIDGE_TOKEN` (for example, generate one
   with `openssl rand -hex 32`). Run `uv run --locked harbor auth login`, or set
   `HARBOR_API_KEY` in `.env`.
2. Start the staging agent from the release commit:

   ```sh
   AGENT_RELEASE="$(git rev-parse HEAD)" \
     uv run --locked --env-file .env python -m agent.service
   ```

3. In another terminal, expose the local service:

   ```sh
   ngrok http http://127.0.0.1:8000 --inspect=false
   ```

   Put the forwarding HTTPS URL in `.env` as `STAGING_URL`. Keep both processes running.
4. Run all three examples in one job:

   ```sh
   uv run --locked --env-file .env python -m scripts.run_eval --suite showcase --target remote
   ```

To run just one example, replace `showcase` with its suite name from the table.
The launcher checks the staging commit, packages the tasks from Git, and prints
the Harbor job link. It saves reports, traces, and `eval-summary.json` under
`jobs/`. For a fork, update `REPOSITORY` in the [launcher](scripts/run_eval.py).

<details>
<summary>Codex control, CI, and local checks</summary>

Use `--target both` to compare with native Codex on either emulated suite. Set
`CODEX_OPENROUTER_API_KEY` for the control. Both agents default to
`deepseek/deepseek-v4.1-flash-1017` on OpenRouter. For GitHub Actions, configure the
`staging` environment with the `STAGING_URL` variable and the `HARBOR_API_KEY`,
`BRIDGE_TOKEN`, and `CODEX_OPENROUTER_API_KEY` secrets. A successful staging
deployment runs `smoke`; the other suites are available by manual dispatch.

```sh
uv run --locked --group fixture pytest -q
uv --directory harbor/adapter run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

</details>

## How the agent moved to Harbor

The [starting agent](https://github.com/punitarani/remote-evals/commit/ab72dd7)
called Bash and live Fli directly. Now the [agent loop](agent/harness.py) takes
a [tool executor](agent/tools.py) for each run. Its prompts and decision-making
stay the same when the tools move:

```diff
-async def run_agent(prompt, complete, workspace):
+async def run_agent(prompt, complete, executor):
     ...
-    result = await call_fli(name, arguments)
+    result = await executor.execute(name, arguments)
```

The staging service still decides what to do. The [Harbor adapter](harbor/adapter/)
runs those actions inside the trial: `search_dates` finds the cheapest day,
`search_flights` confirms the fare, and Bash writes `/workspace/report.json`.
Harbor transfers that file to an [offline verifier](harbor/task/verify.py), which
awards 1 for a correct report or 0 otherwise. The model API key stays on staging.
