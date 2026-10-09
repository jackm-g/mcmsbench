# MCMSBench: Minecraft Multiplayer Survival Bench

MCMSBench is a set of Minecraft tasks with worlds and graders. Any agent, in any language or framework, can run
against it through one protocol. Each task resets a world, puts a player in it, and hands the player to the agent. The
bench grades what is left on the server afterwards, and it never trusts what the agent says.

```bash
mcmsbench run --agent myagent --task gather_wood
mcmsbench run --agent myagent --model some-model --task shelter_in_desert --trials 3
mcmsbench compare runs/<a> runs/<b> --open
```

No agent ships with the bench. You bring your own (see [Adding an agent](#adding-an-agent)).

## What's here

| | |
|---|---|
| `tasks/*.yaml` | 66 tasks: building, survival nights, combat, mining, redstone, rails, the Nether (a room, and a highway through the real one), farming, escapes, a friend in chat |
| `profiles/*.yaml` | defaults a task inherits (`prod`: a hard, snowy server with spawn protection; `farmstead`) |
| `infra/docker-compose.yml` | the arenas: a superflat server, and survival servers restored from `infra/worlds/<seed>.tar` |
| `src/mcmsbench/` | the runner, the protocol, the observer, the graders, reports and the compare page |
| `mcmsbench.toml` | arena addresses, the trial budgets a task falls back on, and slots for parallel runs |
| `PROTOCOL.md` | the agent contract: what an agent gets and what it must send back |

## Set up

You need Python 3.11+, Node (for the bench's own mineflayer clients) and Docker.

```bash
uv venv && uv pip install -e ".[dev]"
npm install                                # the observer and stand-in clients
cp .env.example .env                       # RCON_PASSWORD (matching infra/docker-compose.yml), MCMSBENCH_AGENT_PATH
docker compose -f infra/docker-compose.yml up -d arena          # the flat arena
mcmsbench world prepare --seed 1           # once per seed: the survival world snapshot (needs arena-survival)
mcmsbench world prepare --seed 2           # helping_pat's and nether_highway's world
```

| service | world | game / RCON port | who starts it |
|---|---|---|---|
| `arena` | superflat, 26.1.2 | 25565 / 25575 | you, once |
| `arena-survival` | seeded terrain | 25567 / 25577 | the runner: it restores the world and restarts the server before every trial |
| `arena-survival-2` | seeded terrain | 25568 / 25578 | the runner, with `--slot 2`, for a run alongside another |
| `arena-survival-3` | seeded terrain | 25569 / 25579 | the runner, with `--slot 3`, for a run beside two others |

The arenas are disposable. Never point `mcmsbench.toml` at a server you care about: resets run `/fill` over the plots
and delete the survival world.

## Commands

```
mcmsbench run --agent NAME --task ID [--task ID ...] | --all [--tag T]   run trials
mcmsbench agents                                                        manifests on the agent path, and whether each is installed
mcmsbench tasks [--tag T]                                               the tasks
mcmsbench goal --task ID [--trial N]                                    the goal file a task makes (no server needed)
mcmsbench check-agent NAME                                              protocol conformance (runs the agent twice)
mcmsbench compare DIRS [--out FILE] [--summarize] [--open]              one HTML page across runs
mcmsbench render DIRS                                                   re-render frames and reports from saved records
mcmsbench world prepare --seed N | mcmsbench world list                 survival worlds
```

`run` takes:

| flag | |
|---|---|
| `--agent NAME` | a manifest name on the agent path, or a path to a manifest file |
| `--provider P`, `--model M` | the agent's `{provider}` and `{model}` (its manifest says what they mean) |
| `--agent-arg X` | an extra argument passed to the agent verbatim (repeatable) |
| `--trials N` | override each task's `trials` |
| `--run-dir D`, `--resume` | write into D; with `--resume`, skip trials that already have a record there |
| `--slot NAME` | run on the arenas of `[slots.NAME]` in `mcmsbench.toml` |
| `--no-render`, `--open` | skip frame images and HTML reports; open the run's index when done |

`compare --summarize` adds a one-paragraph strategy summary per trial. It needs `pip install -e ".[summarize]"` and
`ANTHROPIC_API_KEY`.

## How a trial runs

1. **Reset.** A flat plot is cleared with `/fill`. A survival world is restored from its snapshot and the server is
   restarted.
2. **Observe.** A spectator observer client snapshots the plot.
3. **Set up.** A stand-in client logs in as the trial's player. RCON places it at the start, gives it the task's
   inventory and runs the task's setup. A task's scripted players (`players:`, the other people on the server) log in
   before the setup, each its own client, in adventure mode. A second snapshot is taken, and that is the baseline.
4. **Hand off.** The stand-in logs out and the agent's command starts (PROTOCOL.md). While the agent works:
   - frames are taken on its subgoal events, and every 30 s;
   - the player's position is sampled over RCON (with its dimension, for a grader that reads a Nether leg);
   - the task's timed events fire, a scripted player's chat lines (`say:`) among them (with players, the events'
     clock starts when the agent is on the server);
   - every block broken in the plot is logged;
   - the grader runs on the live world, and the agent is stopped once the task passes. Set
     `MCMSBENCH_LIVE_GRADE=0` to let agents play on to their own stop or the clock.
5. **Take back.** The stand-in logs back in. Position, inventory, health, food, time, dimension and respawn point
   are read from the server.
6. **Grade.** The final snapshot is graded against the baseline, and the trial's record and report are written.

## What a run writes

```
runs/<stamp>-<agent>/
  summary.json, index.html        per task: pass rate, mean score, seconds, turns, cost, errors
  <task>/
    trial_N.json                  the record: verdict and grader detail, the agent's trace, final server reads,
                                  snapshots, the position track, what broke, `bench`, `caveats`
    trial_N.jsonl                 the trial's event log, line by line as it happened
    trial_N_goal.json             the goal file the agent got
    trial_N_agent.json(l)         the agent's own --out and --log
    trial_N.html, trial_N_frames/ the report: frames, final views, a timelapse
```

Each record's `bench` block carries the bench's git sha, the protocol version, a hash of the task as it ran (profile
included), and a hash of the agent's manifest. `compare` warns when two columns ran different versions of a task.

Some tasks grade something only an agent can report, such as `make_safe`, which counts the agent's guard refusals.
These tasks list the capability they need under `requires:`. An agent whose manifest doesn't declare that capability
still runs. Its trial carries a `caveats` entry, and the summary flags the task, because that score is not comparable
across agents.

## Adding an agent

An agent is any program, in any language, that can play Minecraft through a client such as
[mineflayer](https://github.com/PrismarineJS/mineflayer). The bench starts it as a process and never imports it.

1. Make your agent accept the flags in PROTOCOL.md §2, read the goal file (§4), print the events (§5) and write a
   trace (§7). `tests/fake_agent.py` is a minimal speaker of the protocol (no server, no model) to start from.
2. Write a manifest (PROTOCOL.md §1), usually in your agent's repo: `myagent/mcmsbench/myagent.toml`.
3. Point the bench at it with `MCMSBENCH_AGENT_PATH=../myagent/mcmsbench` in `.env`.
   - Entries are separated by `:` (`;` on Windows). Each is a directory of manifests or a single `.toml` file, relative
     to this checkout.
   - An `agents/` folder in this checkout is searched last.
   - `mcmsbench agents` lists what it finds and whether each checkout is installed.
4. Run `mcmsbench check-agent myagent` (with a free backend if your agent has one), then
   `mcmsbench run --agent myagent --task place_one`.

## Adding a task

A task is a YAML file in `tasks/`. It has a prompt, a world, a start, an inventory, setup commands, and a grader built
from server truth. `mcmsbench goal --task <id>` shows what an agent will get. Graders are in `src/mcmsbench/graders/`;
the milestone grader composes the rest. Keep prompts in a player's words. Hints about one agent's API belong in that
agent, not in the task. If a grader depends on something only an agent can report, add the capability to `requires:`.

## Checking the graders

Grader changes are checked in two ways:

- **Unit tests:** `uv run pytest` covers synthetic snapshots, the protocol against a fake agent, and the goal file. No
  server is needed.
- **Reference solutions:** an agent that plays each task from a hand-written script should pass every graded task, and
  a deliberately wrong twin of it should fail every one. Such an agent is an ordinary agent with its own manifest; none
  ships with the bench.
