# Memra Memory Provider

[Memra](https://usememra.com) is a self-hosted, EU-native memory API for AI
agents: hybrid semantic + structured recall, async embeddings, typed memories
(fact / event / preference / context / decision / pattern / entity / working),
importance ranking, and server-side compression of long-lived memories.

This provider is **self-contained** — it talks to the Memra REST API directly
over HTTPS. No Memra client package required.

## Requirements

- A Memra API key (`memra_live_...`) and project id (`proj_...`) from
  [usememra.com](https://usememra.com), or your own self-hosted Memra instance.
- `httpx` (installed automatically via `pip_dependencies`).

## Setup

```bash
hermes memory setup    # select "memra"
```

Or manually:
```bash
hermes config set memory.provider memra
echo "MEMRA_API_KEY=memra_live_xxx" >> ~/.hermes/.env
echo '{"project_id": "proj_xxx"}' > ~/.hermes/memra.json
```

## Config

Config file: `$HERMES_HOME/memra.json` (secrets go to `.env`).

| Key | Default | Description |
|-----|---------|-------------|
| `api_key` | — | Memra API key (`memra_live_...`). Stored in `.env` as `MEMRA_API_KEY`. |
| `project_id` | — | Memra project id (`proj_...`). Required. |
| `tenant_id` | `hermes-user` | Tenant/user scope. Gateway sessions auto-use the platform `user_id`. |
| `base_url` | `https://usememra.com/api/v1` | Point at your self-hosted Memra to keep data on your own infra. |

## Tools

| Tool | Description |
|------|-------------|
| `memra_profile` | Overview of stored memories (importance-ranked). |
| `memra_search` | Hybrid semantic + structured recall by meaning. |
| `memra_remember` | Store a durable fact (type `fact`, importance, tags). |

## Behavior

- **Prefetch** — recalls relevant memories before each turn (background, non-blocking).
- **Turn sync** — persists each turn as an `event` memory.
- **Compression survival** — `on_pre_compress` ships the about-to-be-discarded
  context to Memra so it stays recallable after Hermes compresses the window;
  Memra applies its own server-side compression to the stored span.
- **Built-in mirror** — `on_memory_write` mirrors Hermes `MEMORY.md` / `USER.md`
  writes into Memra (`USER.md` → `preference`, `MEMORY.md` → `context`).

All network calls are guarded by a circuit breaker (pauses after 5 consecutive
failures for 120s) so a Memra outage never blocks the agent loop.
