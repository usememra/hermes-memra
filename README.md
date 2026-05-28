# hermes-memra

[Memra](https://usememra.com) memory provider for
[Hermes Agent](https://hermes-agent.nousresearch.com) — give Hermes persistent,
cross-session memory backed by Memra's hybrid semantic + structured recall.

Memra is a self-hosted, EU-native memory API: typed memories
(semantic / episodic / procedural / working), importance ranking, async
embeddings, and server-side compression of long-lived memories.

## Install (Hermes ≥ 0.11)

```bash
curl -fsSL https://raw.githubusercontent.com/usememra/hermes-memra/main/install.sh | bash
hermes memory setup        # select "memra"
```

That's it. `hermes memory setup` will prompt for your Memra API key and project
id — get them at [usememra.com](https://usememra.com). Self-hosting Memra? Set
`base_url` to your instance to keep all data on your own infrastructure.

Check `hermes --version` first — user-installed providers require Hermes 0.11+.

## What it does

| Hermes lifecycle | Memra behavior |
|------------------|----------------|
| `prefetch` | Background hybrid recall before each turn |
| `sync_turn` | Persists each turn as an `episodic` memory |
| `on_pre_compress` | Ships about-to-be-discarded context to Memra so it survives window compression |
| `on_memory_write` | Mirrors Hermes `MEMORY.md` / `USER.md` writes into Memra |

**Tools:** `memra_search`, `memra_remember`, `memra_profile`.

Self-contained (only `httpx`) and wrapped in a circuit breaker, so a Memra
outage never blocks the agent loop.

## Config

| Key | Default | Description |
|-----|---------|-------------|
| `api_key` | — | Memra API key (`memra_live_...`), stored in `~/.hermes/.env` |
| `project_id` | — | Memra project id (`proj_...`) |
| `tenant_id` | `hermes-user` | User/tenant scope (gateway sessions use the platform `user_id`) |
| `base_url` | `https://usememra.com/api/v1` | Point at your self-hosted Memra |

## License

MIT.
