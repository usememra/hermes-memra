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
| `tenant_id` | _unset_ → gateway `user_id` → `hermes-user` | Tenant scope. **Single-user installs must pin this** (see below). |
| `base_url` | `https://usememra.com/api/v1` | Point at your self-hosted Memra to keep data on your own infra. |

## Tenant scoping (READ THIS — silent fragmentation footgun)

Memra rows are partitioned by `tenant_id`. A session only sees rows tagged
with the tenant it was initialized under. The plugin resolves the tenant in
this priority order:

1. Explicit `tenant_id` in `memra.json` / `MEMRA_TENANT_ID` env var.
2. Gateway-provided `user_id` kwarg (Telegram chat_id, Discord user id, etc.).
3. Built-in default `"hermes-user"`.

**Single-user deployment** (most personal installs): pin `tenant_id` in
`memra.json`. The `memra-profile-setup` script does this automatically
(default `"hermes-user"`, override via `MEMRA_DEFAULT_TENANT` env). Without
the pin, Telegram sessions silently write to `tenant=<chat_id>` while CLI
and cron sessions use the default — same person, multiple invisible stores.

**Multi-user gateway**: leave `tenant_id` unset (or set
`MEMRA_DEFAULT_TENANT=""` when running the setup script). The plugin will
scope each session to its gateway `user_id`, giving every user a private
memory store.

### Detect and repair fragmentation

```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/memra/scripts/memra_doctor.py
```

Exit code 0 = healthy. Exit 1 = fragmentation (rows live in >1 tenant for
this project). The doctor reports every distinct tenant with rows.

If fragmentation is detected, merge the rows with:

```bash
~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/plugins/memra/scripts/migrate_tenant.py \
  --from-tenant <orphan_tenant_id> \
  --to-tenant hermes-user
```

Each migrated row carries a `migrated:from:<src>` tag and the original
`id` + `created_at` in metadata, so the timeline is preserved.

### Verifying which tenant a session is using

The plugin logs the resolved tenant on every session init:

```
agent.memory_manager: Memory provider 'memra' registered (3 tools)
plugins.memra: Memra initialized: tenant_id='hermes-user' (source=config)
              project_id='proj_xxxxxxxx...' session='20260601_104043_xyz'
```

Grep `tenant_id=` in `~/.hermes/logs/agent.log` to confirm every platform's
session is binding the same tenant.

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
