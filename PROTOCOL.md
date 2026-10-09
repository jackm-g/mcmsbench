# MCMSBench agent protocol, v1

This is the whole contract between the bench and an agent. An agent can be written in any language and framework: the
bench starts it as a process, gives it a Minecraft player and a goal, reads lines from its stdout, and grades the world
afterwards. The bench never imports agent code, and grading never asks the agent anything.

## 1. The manifest

An agent is described by a manifest, `<name>.toml`. It usually lives in the agent's own repo, for example
`myagent/mcmsbench/myagent.toml`. The bench looks for manifests in each entry of `MCMSBENCH_AGENT_PATH`, then in an
`agents/` folder in the bench checkout if there is one. Entries are separated by `:` (`;` on Windows) and can be set in
the environment or in the bench's `.env`. A relative entry is relative to the bench checkout. An entry is a directory of
manifests or one manifest file, and the first manifest found under a name wins. `--agent path/to/myagent.toml` takes a
manifest directly.

```toml
description = "one line for `mcmsbench agents`"
command = ["node", "bin/myagent.js", "eval"]      # the bench appends the flags in §2
cwd = ".."                                         # relative to this manifest; ${ENV} and ${ENV:-default} expand
prefix = "@myagent"                                # the event-line prefix (default: "@<name>")
provider = "default-backend"                       # the default for {provider}; --provider overrides it
model = ""                                         # the default for {model}; --model overrides it
args = [["--backend", "{provider}"], ["--model", "{model}"], "--always"]
label = "myagent ({provider})"                     # the model column when no --model is given
capabilities = ["fast_nights"]                     # see §6
requires_files = ["bin/myagent.js", "node_modules"]   # what `mcmsbench agents` checks a checkout for
env = { MYAGENT_QUIET = "1" }                      # added to the environment; values expand ${ENV}
goal_format = "json"                               # "text": the goal file holds only the prompt (older agents)

[vars]                                             # more placeholders for args
system2 = "${MYAGENT_SYSTEM2}"
```

`args` entries are strings, which are always passed, or lists, which are passed only when every placeholder in them
has a value. So `["--model", "{model}"]` is left out when there is no model. `mcmsbench run --agent-arg X` appends
arguments verbatim.

## 2. Launch

```
<command> --host H --port P --username U --version V --goal-file F --max-seconds S --max-cost-usd C \
          --out TRACE.json --log LOG.jsonl <args>
```

- **Auth** is offline mode. Log in as `--username` with protocol `--version` (e.g. `26.1`).
- **The environment** includes `MCMSBENCH_PROTOCOL=1`.
- **The working directory** is the manifest's `cwd`.
- **Process group:** the agent runs in its own process group. Signals (§5) go to the whole group, so a launcher such as
  `node bin/x.js` or `uv run` and whatever it starts get them too.

## 3. The player hand-off

1. Before the agent starts, the bench's stand-in client is logged in as `--username`. The player is placed at the
   start, given the task's inventory and gamemode, and the task's setup has run.
2. The stand-in logs out. The agent then logs in as the same player. Inventory, position, health and hunger live on the
   server, so the agent finds the player exactly as it was left.
3. When the agent's process exits, the stand-in logs back in for the final reads.

The agent must not expect to be op, and it never gets RCON.

## 4. The goal file

`--goal-file` is JSON (unless the manifest says `goal_format = "text"`):

```jsonc
{
  "protocol": 1,
  "task_id": "shelter_in_desert",
  "trial": 0,
  "prompt": "Build a shelter here that will keep mobs out tonight: ...",   // the task, as a player would say it
  "check": { "kind": "all", "parts": [...], "uncovered": [...] },          // the grader's terms; null for none (§4.1)
  "movements": { "can_dig": true, "towers": true, "scaffolding": ["sand", "dirt"] },   // what the task allows
  "profile": {
    "multiplayer": false, "longRun": false,
    "spawnProtection": { "x": 154, "z": 163, "radius": 16 },    // or null: the server refuses digs/places inside it;
                                                                //   round the world spawn, not always the plot's centre
    "border": { "x": 154, "z": 163, "radius": 48 },             // or null (flat plots)
    "fastNights": true,      // a night_skip_request (§5) will be answered
    "graderStops": true      // the bench grades live and stops the agent once the task passes
  },
  "world": { "type": "survival", "seed": 1, "difficulty": "normal", "daylight": true,
             "dimension": "minecraft:overworld", "keep_inventory": true },
  "plot": { "min": [106, 58, 115], "max": [202, 98, 211], "center": [154, 70, 163], "floor_y": null,
            "anchor": [106, 58, 115] },     // the graded volume; floor_y on a flat plot; the point {ax..} in prompts
  "start": [154, 70, 163],
  "inventory": { "torch": 12 },             // what the player was given
  "budget": { "max_seconds": 900, "max_cost_usd": 5.0 },
  "options": { "xray": true, "guard": false, "journal": false },   // §4.2
  "tags": ["tier-c", "survival", "night"]
}
```

`budget.ends`, when present, says the trial ends on the world's clock rather than on a pass: `{"at_dawn": 2}` ends it
when day 3 begins (day 1 is the one it starts in). The bench counts dawns itself and stops you there (§6), so keep
going until then: a `done` before it ends the run short of the task.

Unknown keys may be added within protocol 1, so ignore keys you don't use. `mcmsbench goal --task ID` prints the goal
file any task makes, with no server needed.

### 4.1 `check`

`check` is the task's grader rewritten as parts an agent can score on its own, so it knows when it is done. It is
optional reading: the bench grades from the server whatever the agent believes. All positions are absolute.

| kind | fields | passes when |
|---|---|---|
| `inventory` | `item` (glob or list of globs), `count` or `max` | the player carries that many (at most `max`) |
| `placed` | `block`, `min` | this agent placed that many this trial |
| `have_or_placed` | `item`, `count` | carried + placed reaches `count` |
| `blocks` | `block`, `min`, `radius`, `bounds` | that many such blocks stand within `bounds` |
| `position` | `x`, `y`, `z`, `tolerance`, `y_tolerance` | the player stands there |
| `rail_path` | `from`, `to`, `powered_every` | a connected rail line joins them |
| `spawn` | `center`, `min_from_spawn?`, `max_from_spawn?`, `inside?` | a bed respawn point is set (inside a closed room) |
| `time` | `between: [t0, t1]` | the day time is in range (wrapping past 24000) |
| `alive` | `max_deaths`, `min_health?` | the player died at most that often |
| `hydrated` | `crop`, `min` | that many crops stand on watered farmland |
| `food` / `food_stock` | `min` / `min_points` | the hunger bar / the food carried |
| `grader` | `spec`, `plot`, `floor_y`, `center` | the bench's own grader of that kind passes on the plot |
| `all` | `parts: [{name, role, check, proxy, terminal}]`, `uncovered: [{name, kind, says, check?}]` | every part |

An `uncovered` part is one an agent cannot read live, such as a server counter or a test the grader runs on the finished
world. `says` describes it. A `stat` part carries its bound in `check`, for an agent that keeps its own count of its
kills. A `container` part carries `check: {kind: "container", at: [x, y, z], items: {name: count}}`: the chest the
grader reads at the end and what it must hold then, for an agent that keeps a record of what it put where. A `herd`
part carries `check: {kind: "herd", types: [kind], min_each: n, within: [x1, y1, z1, x2, y2, z2]}`: the animals that must
stand in that box at the end, the young counted. A `block_state` part carries `check: {kind: "block_state", at: [x, y, z],
block, state: {name: value}}`, such as a gate that must be shut. An agent that can see animals and blocks can score
both as it goes. A `rooms`, `windows` or `exit_route` part carries `check: {kind: "grader", spec, plot, floor_y,
center}`, the shape of a live `grader` part: the bench's spec on this trial's plot, for an agent that keeps its own
copy of that grader. `exit_route` reads the trial's frames and position track, so such an agent keeps its own record
of where it walked.

### 4.2 `options`

Some agents run their own harness with features the bench cannot provide. `options` passes on what the task asks of
such a harness. An agent without the feature ignores the option.

- `xray: false`: only see blocks with an open face.
- `guard: true`: refuse to break others' things (§6).
- `journal: true`: start with a memory.
- `memories: [{text, at, slot?, ago?}]`: notes to start with.
- `own: [[x0,y0,z0,x1,y1,z1]]`: setup blocks that count as the agent's own build.
- `curfew`, `spawn_first`, `max_turns`, `exec_timeout`, `effort`: the task's settings for those.

## 5. Stdout events

Print one JSON object per line, after the prefix and a space, keyed by `event`:

```
@myagent {"event": "subgoal_started", "subgoal": "s1", "text": "gather logs"}
```

| event | fields | the bench |
|---|---|---|
| `subgoal_started` | `subgoal`, `text` | shows progress |
| `subgoal_completed` / `subgoal_failed` | `subgoal`, `text` | **takes a frame** (a snapshot of the world, for milestone timing and the report) |
| `night_skip_request` | `held_s` | when `profile.fastNights`: jumps the clock to morning (send it once your shelter has held a while) |
| `guard_refusal` | anything (`why`, `at`...) | counts it for the `guard` grader (§6) |
| `death`, `block_broken`, others | anything | keeps it in the trial's event log |
| `goal_end` | `stop`, `summary`, `seconds`, `cost_usd` | notes that your run is over; send it before you log out |
| `trace` | `trace: {...}` | records the trace (§7) |

Lines without the prefix are shown as progress and kept, the last 20 KB of them, in the record. The bench also takes
a frame every 30 seconds itself, so an agent that sends no subgoal events is not graded on less.

## 6. Signals and stopping

- **The clock:** stop at `--max-seconds` yourself. The bench sends SIGTERM 90 s past it.
- **SIGTERM:** stop, write your trace, exit. SIGKILL follows 15 s later.
- **A live pass:** when `profile.graderStops` is set, the bench grades the live world about every 10 s. On a pass it holds
  the agent still (SIGSTOP), waits 1.5 s for what the agent had already sent to land, and grades again. If the task
  still passes, it sends SIGTERM and then SIGCONT, and the record says `stop: grader_passed`. If not, SIGCONT and the
  agent plays on. A pause of a few seconds is normal, so don't treat it as a lost connection.
- **The task's last dawn:** when `budget.ends.at_dawn` is set, the bench holds the agent still at that dawn, takes its
  frame, then sends SIGTERM and SIGCONT. The record says `stop: dawn_reached`. `profile.graderStops` is false for such
  a task: passing early does not end it.
- **A death, where the task ends on one:** a task with `fail_on_death` is failed at the player's first death. The bench
  reads the server's death counter about every 3 s; at a death it holds the agent still, takes a `lost` frame, then
  sends SIGTERM and SIGCONT. The record says `stop: task_lost`.

**Capabilities** are what a task may need beyond this protocol (`requires:` in its YAML):

- `guard`: the agent refuses to break blocks that are not its own, and reports each refusal as `guard_refusal`. The
  `guard` grader counts those reports.
- `fast_nights`: the agent sends `night_skip_request`.

An agent missing a capability a task requires still runs. Its trial carries a `caveats` entry, and the summary says
which tasks have one: those scores are not comparable across agents.

## 7. The trace

Send it as a `trace` event, write it to `--out`, or both. The bench reads the event first and `--out` if no event
came. Write it on every exit, SIGTERM included.

| key | type | |
|---|---|---|
| `turns` | int | your unit of work (model calls, decisions, steps) |
| `cost_usd` | float | model spend for the trial |
| `done` | `{success: bool, summary: str}` | what you believe; a bare bool is accepted |
| `stop` | str | why you stopped: `done`, `deadline`, `budget`, `terminated`, `error`... |
| `steps` | list | optional; each `{turn, seconds, text, code?, stdout?, error?}`, shown in the report |
| `model` | str | optional; the model column (defaults to the manifest's label) |
| `error` | str | only with `stop: error`/`killed`; the trial counts as crashed |
| `check` | `{parts: [{name, role, outcome}]}` | optional; your last word on each part of the goal's `check` (`pass`/`fail`/`unknown`), set beside the grader's verdict |

Anything else you add is kept (`input_tokens`, `output_tokens`, `nudges`, `malformed_tool_calls` and
`cache_write_tokens` show up on the compare page).

## 8. Conformance

```
mcmsbench check-agent <name> [--provider P] [--model M]
```

This runs the agent twice against the flat arena, with no reset and no grading:

1. a 20 s goal it must finish by itself, with `goal_end`, the required trace keys and exit code 0;
2. a 120 s goal stopped at 8 s, where it must exit within the SIGTERM grace and still write its trace.

Use a backend that costs nothing if the agent has one.
