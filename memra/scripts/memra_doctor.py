#!/usr/bin/env python3
"""Memra doctor — detect tenant fragmentation and verify write/read health.

Why this exists
---------------
Memra rows are partitioned by ``tenant_id``. Sessions only see rows tagged
with the tenant they were initialized with. The plugin's ``initialize()``
resolves the tenant in this priority order:

  1. Explicit ``tenant_id`` in env (``MEMRA_TENANT_ID``) or
     ``$HERMES_HOME/memra.json``.
  2. Gateway-provided ``user_id`` kwarg (Telegram chat_id, Discord user id, …).
  3. Built-in default ``"hermes-user"``.

For single-user installs this is a footgun: forget to pin (1) and Telegram
sessions silently write to a different tenant than CLI/cron sessions, so the
same person ends up with N split memory stores keyed by platform identity.
This doctor surfaces the split before it becomes a multi-day mystery.

What it checks
--------------
- Config: project_id, tenant_id, and where they came from (env vs JSON).
- Active session tenant: what the plugin resolves to with no gateway kwargs.
- Storage tenants: every distinct ``tenant_id`` that owns rows for the
  configured project (the only way to spot existing fragmentation).
- Round-trip: write a sentinel and read it back to prove the path is live.

Usage::

  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/memra/scripts/memra_doctor.py
  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/memra/scripts/memra_doctor.py --json
  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/memra/scripts/memra_doctor.py --skip-write

Exit codes::

  0 = healthy
  1 = tenant fragmentation detected (rows in >1 tenant for project)
  2 = round-trip failed (write or read didn't work)
  3 = config incomplete (no project_id or api_key)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _load_plugin_config(hermes_home: Path) -> dict:
    """Mirror MemraMemoryProvider._load_config without importing the plugin
    (so this script works even if the plugin module is broken)."""
    cfg = {
        "api_key": os.environ.get("MEMRA_API_KEY", ""),
        "project_id": os.environ.get("MEMRA_PROJECT_ID", ""),
        "tenant_id": os.environ.get("MEMRA_TENANT_ID", ""),
        "base_url": os.environ.get("MEMRA_BASE_URL", "https://usememra.com/api/v1"),
    }
    cfg_path = hermes_home / "memra.json"
    if cfg_path.exists():
        try:
            file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in file_cfg.items()
                        if v is not None and v != ""})
        except Exception:
            pass
    return cfg


def _load_dotenv_into(env_path: Path) -> None:
    """Hydrate MEMRA_* env vars from $HERMES_HOME/.env if not already set
    (the gateway does this for live sessions; we replicate it for CLI use)."""
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


def discover_tenants(http, project_id: str) -> dict[str, int]:
    """Walk all rows in the project, grouped by tenant_id.

    The Memra list endpoint requires a ``tenant_id`` filter, so we can't just
    GET /memories without one. Instead we probe a list of candidate tenant ids:
    the configured one, the built-in default, and any user_id-shaped strings
    discovered from agent.log (chat ids, emails). Returns a dict of
    tenant_id → row_count for ones with at least one row.
    """
    candidates: set[str] = set()
    candidates.add("hermes-user")
    if os.environ.get("MEMRA_TENANT_ID"):
        candidates.add(os.environ["MEMRA_TENANT_ID"])

    # Pull recent inbound message identities from agent.log to catch
    # platform-derived tenants like Telegram chat_ids and Discord user ids.
    log = Path.home() / ".hermes" / "logs" / "agent.log"
    if log.exists():
        try:
            text = log.read_text(errors="ignore")
        except Exception:
            text = ""
        import re
        for m in re.finditer(r"chat=(\d+)\b", text):
            candidates.add(m.group(1))
        for m in re.finditer(r"\buser=([A-Za-z0-9._+-]+)", text):
            candidates.add(m.group(1))

    found: dict[str, int] = {}
    for tenant in sorted(candidates):
        try:
            r = http.get(
                "/memories",
                params={
                    "tenant_id": tenant,
                    "project_id": project_id,
                    "limit": 100,
                },
            )
            if r.status_code != 200:
                continue
            rows = r.json().get("memories", []) or []
            if rows:
                # Probe deeper to count beyond the page cap.
                total = len(rows)
                if total >= 100:
                    offset = 100
                    while True:
                        rr = http.get(
                            "/memories",
                            params={
                                "tenant_id": tenant,
                                "project_id": project_id,
                                "limit": 100,
                                "offset": offset,
                            },
                        )
                        if rr.status_code != 200:
                            break
                        page = rr.json().get("memories", []) or []
                        if not page:
                            break
                        total += len(page)
                        if len(page) < 100:
                            break
                        offset += 100
                found[tenant] = total
        except Exception:
            continue
    return found


def write_read_probe(http, project_id: str, tenant_id: str) -> tuple[bool, str]:
    """Write a sentinel row in the active tenant; recall it; delete it.
    Returns (ok, detail)."""
    sentinel = f"memra-doctor-probe-{int(time.time())}"
    body = {
        "content": f"{sentinel} — memra_doctor write/read probe",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "type": "fact",
        "importance": 1,
        "source": "memra-doctor",
        "tags": ["memra-doctor"],
    }
    t0 = time.monotonic()
    w = http.post("/memories", json=body)
    elapsed_w = time.monotonic() - t0
    if w.status_code >= 300:
        return False, f"write failed: HTTP {w.status_code} body={w.text[:200]}"
    new_id = (w.json() or {}).get("id")
    if not new_id:
        return False, f"write returned no id: {w.text[:200]}"

    # Small delay — content writes through to storage but the read endpoint
    # is sometimes serving cached metadata for a few ms.
    time.sleep(0.4)
    g = http.get(f"/memories/{new_id}")
    if g.status_code != 200:
        return False, f"verify GET failed: HTTP {g.status_code}"
    got = (g.json() or {}).get("content") or ""
    if sentinel not in got:
        return False, f"content mismatch: sentinel missing from {got[:120]!r}"

    # Clean up
    try:
        http.delete(f"/memories/{new_id}")
    except Exception:
        pass
    return True, f"wrote+verified in {elapsed_w:.2f}s (id={new_id})"


def run(args) -> int:
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Run with the Hermes venv python:",
              file=sys.stderr)
        print("  ~/.hermes/hermes-agent/venv/bin/python " + __file__,
              file=sys.stderr)
        return 3

    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    _load_dotenv_into(home / ".env")
    cfg = _load_plugin_config(home)

    if not cfg.get("api_key") or not cfg.get("project_id"):
        print("ERROR: Memra not configured. MEMRA_API_KEY and project_id required.",
              file=sys.stderr)
        return 3

    base_url = (cfg.get("base_url") or "https://usememra.com/api/v1").rstrip("/")
    cfg_tenant = cfg.get("tenant_id") or ""
    active_tenant = cfg_tenant or "hermes-user"  # CLI session, no user_id kwarg
    tenant_source = "config" if cfg_tenant else "default (no MEMRA_TENANT_ID set)"

    report: dict = {
        "hermes_home": str(home),
        "base_url": base_url,
        "project_id": cfg["project_id"],
        "config": {
            "tenant_id": cfg_tenant or None,
            "tenant_id_source": tenant_source,
        },
        "active_tenant_for_cli_session": active_tenant,
        "issues": [],
        "ok": True,
    }

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    with httpx.Client(base_url=base_url, headers=headers, timeout=15.0) as http:
        tenants = discover_tenants(http, cfg["project_id"])
        report["tenants_with_rows"] = tenants

        if not tenants:
            report["issues"].append(
                "No tenants found with rows. Either the project is empty or "
                "writes haven't happened yet."
            )

        if len(tenants) > 1:
            report["ok"] = False
            others = [t for t in tenants if t != active_tenant]
            report["issues"].append(
                f"FRAGMENTATION: rows live in {len(tenants)} distinct tenants "
                f"({sorted(tenants.keys())}). Active CLI tenant is "
                f"{active_tenant!r}; rows in {others!r} are invisible to your "
                f"CLI/cron sessions. Run migrate_tenant.py to merge."
            )

        if cfg_tenant and cfg_tenant not in tenants:
            report["issues"].append(
                f"Active tenant {cfg_tenant!r} has no rows yet — first write "
                "will populate it."
            )

        if not args.skip_write:
            ok, detail = write_read_probe(http, cfg["project_id"], active_tenant)
            report["write_read_probe"] = {"ok": ok, "detail": detail}
            if not ok:
                report["ok"] = False
                report["issues"].append(f"WRITE/READ PROBE FAILED: {detail}")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("=== Memra Doctor ===")
        print(f"  HERMES_HOME:     {report['hermes_home']}")
        print(f"  base_url:        {report['base_url']}")
        print(f"  project_id:      {report['project_id']}")
        print(f"  config tenant:   {report['config']['tenant_id']!r} "
              f"(source: {report['config']['tenant_id_source']})")
        print(f"  active tenant:   {report['active_tenant_for_cli_session']} "
              f"(what CLI/cron sessions will use)")
        print()
        print(f"  Tenants with rows for this project:")
        if not tenants:
            print("    (none)")
        for t, n in sorted(tenants.items(), key=lambda kv: -kv[1]):
            marker = "  <-- active" if t == active_tenant else ""
            print(f"    {t:30s}  rows={n}{marker}")
        print()
        if not args.skip_write:
            probe = report.get("write_read_probe", {})
            mark = "OK" if probe.get("ok") else "FAIL"
            print(f"  Write/read probe: {mark}  {probe.get('detail', '')}")
            print()
        if report["issues"]:
            print("  Issues:")
            for issue in report["issues"]:
                print(f"    - {issue}")
        else:
            print("  No issues detected. Tenant is consistent and write/read works.")

    if not report["ok"]:
        return 1 if len(tenants) > 1 else 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Memra plugin tenant/health doctor")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of a text report.")
    ap.add_argument("--skip-write", action="store_true",
                    help="Skip the write/read probe (read-only check).")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
