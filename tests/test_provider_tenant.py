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
