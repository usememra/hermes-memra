# Memra ↔ Hermes Agent

A [Memra](https://usememra.com) memory-provider plugin for
[Hermes Agent](https://hermes-agent.nousresearch.com) (Nous Research).

Hermes Agent supports pluggable, single-select external memory providers
(Honcho, Mem0, Hindsight, OpenViking, Holographic, RetainDB, ByteRover,
Supermemory). This makes **Memra** an option alongside them: hybrid
semantic + structured recall, typed memories, importance ranking, EU-native
self-hosting, and server-side compression of long-lived memories.

`memra/` is the drop-in plugin directory. In the Hermes **source tree** it
lives at `plugins/memory/memra/` (the bundled-provider layout, imported as the
package `plugins.memory.memra`). A **user install** instead goes one level deep
at `$HERMES_HOME/plugins/memra/` — see below.

## What it does

| Hermes lifecycle | Memra behavior |
|------------------|----------------|
| `prefetch` / `queue_prefetch` | Background hybrid recall before each turn |
| `sync_turn` | Persists each turn as an `episodic` memory |
| `on_pre_compress` | Ships about-to-be-discarded context to Memra so it survives window compression (Memra then compresses it server-side) |
| `on_memory_write` | Mirrors Hermes `MEMORY.md` / `USER.md` writes into Memra |
| Tools | `memra_search`, `memra_remember`, `memra_profile` |

All network calls are wrapped in a circuit breaker so a Memra outage never
blocks the agent loop. The plugin is self-contained (only `httpx`) — it calls
the Memra REST API directly, no Memra client package required.

## Install today (Hermes ≥ 0.11)

Hermes discovers user-installed providers one level deep under
`$HERMES_HOME/plugins/<name>/`. The easiest path is the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/usememra/hermes-memra/main/install.sh | bash
hermes memory setup        # select "memra", paste API key + project id
```

Or do it by hand — note the destination is `plugins/memra/`, not
`plugins/memory/memra/` (that's the in-tree bundled path):

```bash
cp -r memra ~/.hermes/plugins/memra
hermes memory setup        # select "memra", paste API key + project id
```

Or wire it manually:

```bash
hermes config set memory.provider memra
echo "MEMRA_API_KEY=memra_live_xxx" >> ~/.hermes/.env
echo '{"project_id": "proj_xxx"}' > ~/.hermes/memra.json
```

Get an API key and project id at [usememra.com](https://usememra.com), or point
`base_url` at your own self-hosted Memra to keep all data on your infra.

## Ship to all Hermes users (PR)

To appear in Hermes's official provider list, the `memra/` directory is
submitted to `NousResearch/hermes-agent` under `plugins/memory/memra/`:

1. Fork `NousResearch/hermes-agent`.
2. Copy `memra/` → `plugins/memory/memra/`.
3. Add an entry to `website/docs/user-guide/features/memory-providers.md`.
4. Open a PR. (See `docs/developer-guide/memory-provider-plugin.md` for their
   contribution requirements — this plugin already follows that contract.)

PR note for maintainers: unlike the other providers this one ships
self-contained (httpx only) rather than depending on a `memra-sdk` PyPI package,
because the import name collides with an unrelated existing `memra` package.
Happy to switch to the SDK once it's published under a non-colliding name.

## Test

```bash
cd /path/to/hermes-agent
cp -r integrations/hermes-agent/memra plugins/memory/memra
python3 integrations/hermes-agent/test_memra_smoke.py   # network-stubbed
```

The smoke test exercises ABC compliance, all three tools, the prefetch/sync/
compress/mirror hooks, the circuit breaker, and `MemoryManager` registration —
no live Memra account required.

## Troubleshooting

**Very long memories occasionally fail to store with some local models.**
This is a host-side (Hermes) limitation, not a Memra one. Some local models
(e.g. certain OpenRouter local models) emit slightly-malformed JSON when a
tool call carries a large argument; Hermes' tool-call sanitizer may drop the
argument before it reaches Memra. Memra itself accepts content up to 10,000
characters of any shape. Workarounds: use a well-behaved model (Claude, GPT,
most hosted models), or split very large memories into smaller writes.
