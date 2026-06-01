"""Regression tests for migrate_tenant.py cascade handling.

Pin the 2026-06 fix: when a parent event is migrated and Memra cascades
the move to its extracted-fact children, the script's subsequent GET of
each child id returns 404 — the work is already done. Those rows must
count as ``cascaded`` (a success), not ``failed`` (the old behavior,
which produced an exit-1 false negative for fully-successful runs).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "memra" / "scripts" / "migrate_tenant.py"
)


def _load_migrate_tenant():
    spec = importlib.util.spec_from_file_location(
        "migrate_tenant_under_test", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resp(status_code, body=None):
    """Build a minimal httpx-Response-like object for the FakeClient."""
    def _raise():
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")
    return SimpleNamespace(
        status_code=status_code,
        headers={},
        json=lambda: body or {},
        text="",
        raise_for_status=_raise,
    )


class FakeClient:
    """Scripted client. ``request(method, url, **kw)`` pops the next matching
    entry from ``queue`` in order. Unexpected calls raise — that's what we
    want, to keep tests honest about the call sequence."""

    def __init__(self, queue):
        self._queue = list(queue)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        for i, (m, u, resp) in enumerate(self._queue):
            if m == method and u == url:
                self._queue.pop(i)
                return resp
        raise AssertionError(
            f"unexpected call: {method} {url}; remaining queue={self._queue}"
        )


def _common_setup(tmp_path, monkeypatch):
    (tmp_path / "memra.json").write_text(
        '{"project_id": "proj_test", "tenant_id": "hermes-user"}'
    )
    monkeypatch.setenv("MEMRA_API_KEY", "memra_live_test_key")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "migrate_tenant.py",
        "--from-tenant", "1897837528",
        "--to-tenant", "hermes-user",
        "--pace", "0",
    ])
    return _load_migrate_tenant()


def test_cascade_404_counts_as_success(tmp_path, monkeypatch, capsys):
    """A parent + a cascaded child: parent migrates normally; child GET
    returns 404 (server already moved it). Exit must be 0 and the summary
    must show ``cascaded=1`` rather than ``failed=1``."""
    mig = _common_setup(tmp_path, monkeypatch)

    parent_full = {
        "id": "mem_parent", "type": "event", "content": "transcript",
        "importance": 4, "tags": [], "metadata": {},
        "source": "hermes:turn", "created_at": "2026-06-01T10:38:44Z",
    }
    queue = [
        ("GET", "/memories", _resp(200, {"memories": [
            {"id": "mem_parent"},
            {"id": "mem_child_cascaded"},
        ]})),
        ("GET", "/memories/mem_parent", _resp(200, parent_full)),
        ("POST", "/memories", _resp(200, {"id": "mem_parent_new"})),
        ("GET", "/memories/mem_parent_new",
            _resp(200, {"content": "transcript"})),
        ("DELETE", "/memories/mem_parent", _resp(204)),
        # The cascaded child — the bug under test.
        ("GET", "/memories/mem_child_cascaded", _resp(404)),
    ]
    monkeypatch.setattr(mig, "Client", lambda *a, **kw: FakeClient(queue))

    exit_code = mig.main()
    out = capsys.readouterr().out

    assert exit_code == 0, f"cascaded rows must not produce exit 1:\n{out}"
    assert "cascaded=1" in out, f"summary line missing cascaded count:\n{out}"
    assert "failed=0" in out, f"cascade must not count as failure:\n{out}"
    assert "ok=1" in out
    assert "server-side cascade" in out, (
        "user-facing message must explain why the row is counted as ok"
    )


def test_real_fetch_error_still_counts_as_failed(tmp_path, monkeypatch, capsys):
    """Don't paper over real errors: a non-404 GET (e.g. 500) must still
    increment ``failed`` and exit non-zero. The cascade-handling change
    is specifically scoped to 404."""
    mig = _common_setup(tmp_path, monkeypatch)

    queue = [
        ("GET", "/memories", _resp(200, {"memories": [
            {"id": "mem_will_500"},
        ]})),
        ("GET", "/memories/mem_will_500", _resp(500)),
    ]
    monkeypatch.setattr(mig, "Client", lambda *a, **kw: FakeClient(queue))

    exit_code = mig.main()
    out = capsys.readouterr().out

    assert exit_code == 1, f"a real fetch error must still fail the run:\n{out}"
    assert "failed=1" in out
    assert "cascaded=0" in out
    assert "FETCH FAILED" in out
