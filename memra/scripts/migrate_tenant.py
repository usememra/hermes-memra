#!/usr/bin/env python3
"""Migrate every Memra row from one tenant_id to another (single project).

Use case: a single-user Hermes install that didn't pin ``tenant_id`` ended
up with separate memory stores keyed by platform identity — e.g. CLI/cron
writes landed in tenant=``hermes-user`` while Telegram writes landed in
tenant=``<chat_id>``. After fixing the config (see ``memra_doctor.py``), run
this to merge the orphan tenant into the canonical one.

Per row::

  1. GET full content for the source row (the list endpoint returns metadata
     only — content lives in storage and is hydrated separately).
  2. POST a copy to the destination tenant, preserving type/importance/tags
     /source. The original id and ``created_at`` are stamped into metadata
     so the timeline isn't lost.
  3. Verify the new row reads back with matching content.
  4. DELETE the original.

Safe to re-run: rows already migrated carry a ``migrated:from:<src>`` tag
and are skipped on subsequent passes. On 429 the script backs off using the
server's ``Retry-After`` hint.

Server-side cascade: Memra cascades migration of extracted-fact children
when their parent event is moved. After the script migrates a parent and
DELETEs the original, the child rows from the source list will return 404
on the per-row GET — they were already moved server-side. Those rows are
reported as ``cascaded`` (counted as success), not ``failed``.

Usage::

  ~/.hermes/hermes-agent/venv/bin/python migrate_tenant.py \\
      --from-tenant <orphan_tenant_id> \\
      --to-tenant hermes-user
  # add --dry-run for a no-op walk (no POSTs, no DELETEs).
  # add --keep-source to skip the DELETE step.

Exit codes::

  0 = every row migrated (directly or via server-side cascade)
  1 = at least one row failed to migrate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _load_dotenv_into(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("MEMRA_") and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def _load_config(hermes_home: Path) -> dict:
    cfg = {
        "api_key": os.environ.get("MEMRA_API_KEY", ""),
        "project_id": os.environ.get("MEMRA_PROJECT_ID", ""),
        "base_url": os.environ.get("MEMRA_BASE_URL", "https://usememra.com/api/v1"),
    }
    cfg_path = hermes_home / "memra.json"
    if cfg_path.exists():
        try:
            file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for k, v in file_cfg.items():
                if v and not cfg.get(k):
                    cfg[k] = v
        except Exception:
            pass
    return cfg


class Client:
    def __init__(self, base_url: str, api_key: str):
        import httpx
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def request(self, method: str, url: str, **kw):
        for _ in range(6):
            r = self._http.request(method, url, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", "10")) + 1
                try:
                    wait = int(r.json().get("retry_after", wait))
                except Exception:
                    pass
                wait = min(max(wait, 1), 60)
                print(f"    429 — sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            return r
        return r


def list_source(client: Client, tenant: str, project_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        r = client.request("GET", "/memories", params={
            "tenant_id": tenant, "project_id": project_id,
            "limit": 100, "offset": offset,
            "sort": "created_at", "order": "asc",
        })
        r.raise_for_status()
        page = r.json().get("memories", []) or []
        if not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return rows


def fetch_full(client: Client, memory_id: str) -> dict | None:
    """Return the full row, or ``None`` if the server returns 404.

    A 404 here means the row is no longer in the source tenant — usually
    because Memra cascaded the move when its parent event was migrated
    earlier in the same run. Callers should treat ``None`` as "already
    handled server-side," not as an error.
    """
    r = client.request("GET", f"/memories/{memory_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def create_copy(client: Client, src: dict, src_tenant: str,
                dest_tenant: str, project_id: str) -> tuple[str | None, str | None]:
    content = src.get("content") or ""
    if not content:
        return None, "empty content"
    tag = f"migrated:from:{src_tenant}"
    tags = list(src.get("tags") or [])
    if tag not in tags:
        tags.append(tag)
    body = {
        "content": content,
        "tenant_id": dest_tenant,
        "project_id": project_id,
        "type": src.get("type") or "fact",
        "importance": src.get("importance") or 5,
        "source": src.get("source") or f"migrate:from:{src_tenant}",
        "tags": tags,
        "metadata": {
            **(src.get("metadata") or {}),
            "migrated_from_tenant": src_tenant,
            "migrated_from_id": src.get("id"),
            "original_created_at": src.get("created_at"),
        },
    }
    r = client.request("POST", "/memories", json=body)
    if r.status_code >= 300:
        return None, f"POST {r.status_code}: {r.text[:300]}"
    return (r.json() or {}).get("id"), None


def verify(client: Client, new_id: str, expected: str) -> tuple[bool, str | None]:
    r = client.request("GET", f"/memories/{new_id}")
    if r.status_code != 200:
        return False, f"GET {r.status_code}"
    got = (r.json() or {}).get("content") or ""
    if got != expected:
        return False, f"content mismatch (got {len(got)} chars, expected {len(expected)})"
    return True, None


def delete_row(client: Client, memory_id: str) -> tuple[bool, str | None]:
    r = client.request("DELETE", f"/memories/{memory_id}")
    if r.status_code not in (200, 204):
        return False, f"DELETE {r.status_code}: {r.text[:200]}"
    return True, None


def main() -> int:
    ap = argparse.ArgumentParser(description="Move Memra rows between tenants.")
    ap.add_argument("--from-tenant", required=True,
                    help="Source tenant_id (where rows currently live).")
    ap.add_argument("--to-tenant", required=True,
                    help="Destination tenant_id (canonical store).")
    ap.add_argument("--project-id",
                    help="Project id (default: from memra.json / env).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Walk rows and report what would migrate; no writes.")
    ap.add_argument("--keep-source", action="store_true",
                    help="Copy rows but don't delete the originals.")
    ap.add_argument("--pace", type=float, default=0.5,
                    help="Sleep N seconds between successful migrations (default 0.5).")
    args = ap.parse_args()

    if args.from_tenant == args.to_tenant:
        print("--from-tenant and --to-tenant must differ.", file=sys.stderr)
        return 2

    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    _load_dotenv_into(home / ".env")
    cfg = _load_config(home)
    project_id = args.project_id or cfg.get("project_id") or ""
    if not cfg.get("api_key") or not project_id:
        print("ERROR: missing MEMRA_API_KEY or project_id.", file=sys.stderr)
        return 2

    try:
        client = Client(cfg["base_url"], cfg["api_key"])
    except ImportError:
        print("ERROR: httpx not installed. Run with the Hermes venv python:",
              file=sys.stderr)
        print("  ~/.hermes/hermes-agent/venv/bin/python " + __file__,
              file=sys.stderr)
        return 2

    print(f"  source:      tenant_id={args.from_tenant}")
    print(f"  destination: tenant_id={args.to_tenant}")
    print(f"  project:     {project_id}")
    print(f"  mode:        {'DRY-RUN' if args.dry_run else 'WRITE'}"
          f"{', keep-source' if args.keep_source else ''}")
    print()

    src_rows = list_source(client, args.from_tenant, project_id)
    print(f"Found {len(src_rows)} rows in source tenant.")

    ok = cascaded = skipped = failed = 0
    migrated_tag = f"migrated:from:{args.from_tenant}"
    for i, meta in enumerate(src_rows, 1):
        src_id = meta.get("id")
        try:
            full = fetch_full(client, src_id)
        except Exception as e:
            failed += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  FETCH FAILED  {e}")
            continue
        if full is None:
            cascaded += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  already moved "
                  f"(server-side cascade) — counted as migrated")
            continue
        content = full.get("content") or ""
        if not content:
            skipped += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  empty content — skip")
            continue
        if migrated_tag in (full.get("tags") or []):
            skipped += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  already migrated — skip")
            continue

        if args.dry_run:
            ok += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  WOULD MIGRATE")
            continue

        new_id, err = create_copy(client, full, args.from_tenant,
                                  args.to_tenant, project_id)
        if not new_id:
            failed += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  CREATE FAILED  {err}")
            continue
        verified, verr = verify(client, new_id, content)
        if not verified:
            failed += 1
            print(f"  [{i:>3}/{len(src_rows)}] {src_id}  VERIFY FAILED  "
                  f"(copy={new_id}) {verr}")
            continue
        if not args.keep_source:
            deleted, derr = delete_row(client, src_id)
            if not deleted:
                failed += 1
                print(f"  [{i:>3}/{len(src_rows)}] {src_id}  DELETE FAILED  "
                      f"(copy={new_id}) {derr}")
                continue
        ok += 1
        print(f"  [{i:>3}/{len(src_rows)}] {src_id} -> {new_id}")
        time.sleep(max(args.pace, 0.0))

    print()
    print(f"Summary: ok={ok} cascaded={cascaded} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
