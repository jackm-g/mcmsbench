# CLAUDE.md

## What this repo is

MCMSBench is the bench, not an agent. It owns tasks, worlds, graders, the runner, reports and the agent protocol
(PROTOCOL.md). Agents live in their own repos with their own manifests, are found through `MCMSBENCH_AGENT_PATH`, and
are only ever started as processes. No agent ships with the bench.

- **Never import agent code** under `src/`. `tests/test_bench.py::test_the_bench_imports_no_agent` enforces it.
- **Tasks are agent-neutral.** A prompt is what a player would say. Hints about one agent's API go in that agent's repo.
- **A protocol change** is a change for every agent. Additive keys in the goal file or trace are fine within v1.
  Anything an agent must change for is v2: bump `PROTOCOL_VERSION` and say so in PROTOCOL.md.

## Testing

Validate in this order:

1. `uv run pytest`: graders on synthetic snapshots, the protocol against `tests/fake_agent.py`, the goal file. No
   server needed.
2. `uv run mcmsbench run --agent <reference-agent> --task <id>`, when a reference-solution agent is on the agent path:
   graders and the whole protocol path with no model. This needs the arena (flat tasks: `docker compose -f
   infra/docker-compose.yml up -d arena`).
3. `uv run mcmsbench check-agent <agent>`: protocol conformance. Prefer a free backend.
4. A model-backed run only when the change needs an agent to verify it.

Don't run cross-agent or cross-model sweeps to validate a change. They are slow, cost money, and are run on purpose
by the user. CLAUDE.local.md (not committed) may name the agents on this machine.
