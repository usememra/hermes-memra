"""Regression tests for tenant_id resolution in the Memra plugin.

Pin the 2026-06 fix: an explicit ``tenant_id`` in config must win over the
gateway-provided ``user_id`` kwarg. Without this priority a single-user
Hermes install silently fragments memory across platforms (CLI writes to
``tenant=hermes-user``, Telegram writes to ``tenant=<chat_id>``, etc.).

These tests run standalone — they stub out the hermes-agent imports the
plugin reaches for, so ``pytest tests/`` works without a full hermes
checkout.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


PLUGIN_PATH = Path(__file__).resolve().parent.parent / "memra" / "__init__.py"


def _stub_hermes_imports() -> None:
    """The plugin does ``from agent.memory_provider import MemoryProvider``
    and ``from tools.registry import tool_error``. Neither lives in this
    repo. Inject minimal shims into ``sys.modules`` before importing."""
    if "agent.memory_provider" not in sys.modules:
        pkg = types.ModuleType("agent")
        sub = types.ModuleType("agent.memory_provider")

        class _MemoryProvider:
            pass

        sub.MemoryProvider = _MemoryProvider
        sys.modules["agent"] = pkg
        sys.modules["agent.memory_provider"] = sub
    if "tools.registry" not in sys.modules:
        pkg = types.ModuleType("tools")
        sub = types.ModuleType("tools.registry")
        sub.tool_error = lambda msg: json.dumps({"error": msg})
        sys.modules["tools"] = pkg
        sys.modules["tools.registry"] = sub
    if "hermes_constants" not in sys.modules:
        mod = types.ModuleType("hermes_constants")
        mod.get_hermes_home = lambda: Path.cwd()  # overridden per-test by fixture
        sys.modules["hermes_constants"] = mod


def _import_memra():
    """Load the plugin module fresh so each test sees a clean class state."""
    _stub_hermes_imports()
    spec = importlib.util.spec_from_file_location("memra_under_test", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Redirect ``get_hermes_home()`` at a tmp dir and clear MEMRA_* env vars
    so each test starts from a clean slate."""
    for k in ("MEMRA_API_KEY", "MEMRA_PROJECT_ID", "MEMRA_TENANT_ID", "MEMRA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMRA_API_KEY", "memra_live_test_key")

    _stub_hermes_imports()
    sys.modules["hermes_constants"].get_hermes_home = lambda: tmp_path
    return tmp_path


class TestTenantPrecedence:
    """The bug we are pinning: gateway ``user_id`` must NOT override an
    explicit ``tenant_id`` from config. A single-user install pins
    ``tenant_id`` and expects every platform (CLI, Telegram, cron) to bind
    the same tenant."""

    def test_config_tenant_wins_over_gateway_user_id(self, fake_hermes_home):
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
            "tenant_id": "canonical-store",
        }))
        memra = _import_memra()
        p = memra.MemraMemoryProvider()
        p.initialize(session_id="s", user_id="telegram-chat-12345")
        assert p._tenant_id == "canonical-store", (
            "Config tenant_id must override gateway user_id — otherwise "
            "single-user installs silently fragment across platforms."
        )

    def test_gateway_user_id_used_when_config_tenant_unset(self, fake_hermes_home):
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
        }))
        memra = _import_memra()
        p = memra.MemraMemoryProvider()
        p.initialize(session_id="s", user_id="discord-9876")
        assert p._tenant_id == "discord-9876", (
            "Without explicit tenant_id, multi-user gateways need per-session "
            "user_id scoping — don't break that fallback."
        )

    def test_default_when_neither_config_nor_user_id(self, fake_hermes_home):
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
        }))
        memra = _import_memra()
        p = memra.MemraMemoryProvider()
        p.initialize(session_id="s")  # no user_id, no config tenant
        assert p._tenant_id == "hermes-user"

    def test_empty_string_tenant_in_config_falls_through(self, fake_hermes_home):
        """A falsy ``tenant_id`` in config (multi-user gateway opt-out) must
        not block the gateway fallback."""
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
            "tenant_id": "",
        }))
        memra = _import_memra()
        p = memra.MemraMemoryProvider()
        p.initialize(session_id="s", user_id="gateway-user-42")
        assert p._tenant_id == "gateway-user-42"

    def test_env_tenant_id_wins_over_gateway_user_id(self, fake_hermes_home,
                                                     monkeypatch):
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
        }))
        monkeypatch.setenv("MEMRA_TENANT_ID", "env-pinned-tenant")
        memra = _import_memra()
        p = memra.MemraMemoryProvider()
        p.initialize(session_id="s", user_id="should-be-ignored")
        assert p._tenant_id == "env-pinned-tenant"


class TestInitLogging:
    """The init log line is the cheapest in-prod fragmentation detector.
    Make sure it gets emitted with the resolved tenant + source."""

    def test_init_logs_resolved_tenant_and_source(self, fake_hermes_home, caplog):
        import logging
        (fake_hermes_home / "memra.json").write_text(json.dumps({
            "project_id": "proj_test",
            "tenant_id": "canonical",
        }))
        memra = _import_memra()
        with caplog.at_level(logging.INFO, logger="memra_under_test"):
            p = memra.MemraMemoryProvider()
            p.initialize(session_id="sess123", user_id="ignored-user")
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Memra initialized" in m for m in msgs), (
            f"Expected 'Memra initialized' log line, got: {msgs}"
        )
        joined = "\n".join(msgs)
        assert "tenant_id='canonical'" in joined
        assert "source=config" in joined
        assert "session='sess123'" in joined


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttp:
    """Stub the Memra REST surface the remember/supersede paths touch:
    GET /memories (list), GET /memories/{id} (hydrate), POST .../supersede,
    POST /memories (add)."""

    def __init__(self, rows=None, contents=None):
        self._rows = rows or []
        self._contents = contents or {}
        self.superseded = []  # list of (id, content)
        self.added = []       # list of request bodies

    def get(self, path, params=None):
        if path == "/memories":
            return _FakeResp({"memories": self._rows})
        if path.startswith("/memories/"):
            mid = path.rsplit("/", 1)[-1]
            return _FakeResp({"id": mid, "content": self._contents.get(mid, "")})
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json=None):
        if path.endswith("/supersede"):
            mid = path.split("/memories/", 1)[1].rsplit("/supersede", 1)[0]
            self.superseded.append((mid, (json or {}).get("content")))
            return _FakeResp({})
        if path == "/memories":
            self.added.append(json or {})
            return _FakeResp({})
        raise AssertionError(f"unexpected POST {path}")


def _provider_with_http(memra, http):
    p = memra.MemraMemoryProvider()
    p._project_id = "proj_test"
    p._tenant_id = "hermes-user"
    p._http = http  # _get_http() returns this directly when set
    return p


class TestSupersede:
    """Pin the 2026-06 supersede gap fix: memra_remember(action="supersede")
    must retire the matching durable fact in place — and must NOT mis-target an
    append-only transcript that merely quotes the fact."""

    def test_supersede_targets_durable_fact_not_transcript(self, fake_hermes_home):
        # The NEWEST row is a hermes:turn transcript that also contains the
        # old_text. If source-scoping were wrong, newest-first ordering would
        # supersede the transcript. It must skip it and hit the fact row.
        rows = [
            {"id": "mem_turn", "status": "active", "source": "hermes:turn"},
            {"id": "mem_fact", "status": "active", "source": "hermes:remember"},
        ]
        contents = {
            "mem_turn": "User: DNS still points to Inleed\nAssistant: noted",
            "mem_fact": "DNS still points to Inleed (185.189.48.4)",
        }
        http = _FakeHttp(rows, contents)
        memra = _import_memra()
        p = _provider_with_http(memra, http)
        out = json.loads(p.handle_tool_call("memra_remember", {
            "action": "supersede",
            "old_text": "DNS still points to Inleed",
            "content": "DNS now points to Hetzner (65.109.172.126)",
        }))
        assert out.get("result") == "Fact superseded."
        assert out.get("memory_id") == "mem_fact"
        assert http.superseded == [
            ("mem_fact", "DNS now points to Hetzner (65.109.172.126)")
        ]

    def test_supersede_also_matches_builtin_mirror_source(self, fake_hermes_home):
        rows = [{"id": "mem_b", "status": "active", "source": "hermes:builtin:memory"}]
        contents = {"mem_b": "old mirrored note"}
        http = _FakeHttp(rows, contents)
        memra = _import_memra()
        p = _provider_with_http(memra, http)
        out = json.loads(p.handle_tool_call("memra_remember", {
            "action": "supersede", "old_text": "old mirrored", "content": "new note",
        }))
        assert out.get("memory_id") == "mem_b"

    def test_supersede_requires_old_text(self, fake_hermes_home):
        memra = _import_memra()
        p = _provider_with_http(memra, _FakeHttp())
        out = json.loads(p.handle_tool_call("memra_remember", {
            "action": "supersede", "content": "x",
        }))
        assert "error" in out

    def test_supersede_no_match_errors_without_adding(self, fake_hermes_home):
        rows = [{"id": "mem_fact", "status": "active", "source": "hermes:remember"}]
        http = _FakeHttp(rows, {"mem_fact": "unrelated fact"})
        memra = _import_memra()
        p = _provider_with_http(memra, http)
        out = json.loads(p.handle_tool_call("memra_remember", {
            "action": "supersede", "old_text": "NO-SUCH-TEXT", "content": "x",
        }))
        assert "error" in out
        assert http.superseded == []
        assert http.added == []  # a failed supersede must not silently add

    def test_default_action_still_adds(self, fake_hermes_home):
        http = _FakeHttp()
        memra = _import_memra()
        p = _provider_with_http(memra, http)
        out = json.loads(p.handle_tool_call("memra_remember", {
            "content": "a brand new fact",
        }))
        assert out.get("result") == "Fact stored."
        assert len(http.added) == 1
        assert http.added[0]["source"] == "hermes:remember"
