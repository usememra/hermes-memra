"""Memra memory plugin — MemoryProvider interface.

Memra (https://usememra.com) is a self-hosted, EU-native memory API for AI
agents: hybrid semantic + structured recall, async embeddings, typed memories
(semantic / episodic / procedural / working), importance ranking, and
server-side compression of long-lived memories.

This provider is self-contained — it talks to the Memra REST API directly over
HTTPS and has no Memra client dependency.

Config via environment variables:
  MEMRA_API_KEY      — Memra API key, e.g. memra_live_xxx (required)
  MEMRA_PROJECT_ID   — Memra project id, e.g. proj_xxx (required)
  MEMRA_TENANT_ID    — Tenant/user scope (default: derived from user_id, else "hermes-user")
  MEMRA_BASE_URL     — API base (default: https://usememra.com/api/v1)

Or via $HERMES_HOME/memra.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120

_DEFAULT_BASE_URL = "https://usememra.com/api/v1"
# Memra rejects content over 10,000 chars; leave headroom for framing.
_MAX_CONTENT_CHARS = 9500


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/memra.json overrides.

    Environment variables provide defaults; memra.json (if present) overrides
    individual keys. This avoids a silent failure when the JSON file exists
    but is missing fields like ``api_key`` that the user set in ``.env``.
    """
    from hermes_constants import get_hermes_home

    config = {
        "api_key": os.environ.get("MEMRA_API_KEY", ""),
        "project_id": os.environ.get("MEMRA_PROJECT_ID", ""),
        "tenant_id": os.environ.get("MEMRA_TENANT_ID", ""),
        "base_url": os.environ.get("MEMRA_BASE_URL", _DEFAULT_BASE_URL),
    }

    config_path = get_hermes_home() / "memra.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    return config


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "memra_profile",
    "description": (
        "Retrieve stored memories about the user — preferences, facts, project "
        "context. Fast, no ranking. Use at conversation start to orient."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "memra_search",
    "description": (
        "Search long-term memory by meaning. Returns relevant memories ranked "
        "by a fused semantic + structured score."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
            "min_importance": {"type": "integer", "description": "Only return memories at/above this importance (1-10)."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "memra_remember",
    "description": (
        "Store a durable fact about the user or project. Use for explicit "
        "preferences, corrections, or decisions worth recalling later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
            "importance": {"type": "integer", "description": "How important (1-10, default: 6)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels."},
        },
        "required": ["content"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class MemraMemoryProvider(MemoryProvider):
    """Memra REST-backed memory: hybrid recall, typed memories, importance ranking."""

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._http = None
        self._http_lock = threading.Lock()
        self._api_key = ""
        self._project_id = ""
        self._tenant_id = "hermes-user"
        self._base_url = _DEFAULT_BASE_URL
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None
        self._write_thread: Optional[threading.Thread] = None
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "memra"

    def is_available(self) -> bool:
        cfg = _load_config()
        return bool(cfg.get("api_key") and cfg.get("project_id"))

    # -- Config ------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "api_key", "description": "Memra API key (memra_live_...)", "secret": True,
             "required": True, "env_var": "MEMRA_API_KEY", "url": "https://usememra.com/dashboard/keys"},
            {"key": "project_id", "description": "Memra project id (proj_...)", "required": True},
            {"key": "tenant_id", "description": "Tenant/user scope", "default": "hermes-user"},
            {"key": "base_url", "description": "API base URL (self-hosted or cloud)", "default": _DEFAULT_BASE_URL},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to $HERMES_HOME/memra.json."""
        from pathlib import Path
        config_path = Path(hermes_home) / "memra.json"
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2))

    # -- Lifecycle ---------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._api_key = self._config.get("api_key", "")
        self._project_id = self._config.get("project_id", "")
        self._base_url = (self._config.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
        # Prefer gateway-provided user_id for per-user memory scoping;
        # fall back to config/env default for CLI (single-user) sessions.
        self._tenant_id = (
            kwargs.get("user_id")
            or self._config.get("tenant_id")
            or "hermes-user"
        )

    def _get_http(self):
        """Thread-safe lazy httpx client."""
        with self._http_lock:
            if self._http is not None:
                return self._http
            try:
                import httpx
            except ImportError:
                raise RuntimeError("httpx not installed. Run: pip install httpx")
            self._http = httpx.Client(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            return self._http

    # -- Circuit breaker ---------------------------------------------------

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "Memra circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.",
                self._consecutive_failures, _BREAKER_COOLDOWN_SECS,
            )

    # -- REST helpers ------------------------------------------------------

    def _api_add(self, content: str, *, type: str = "semantic",
                 importance: int = 5, tags: Optional[List[str]] = None,
                 source: str = "hermes") -> None:
        body = {
            "content": content[:_MAX_CONTENT_CHARS],
            "tenant_id": self._tenant_id,
            "project_id": self._project_id,
            "type": type,
            "importance": importance,
            "source": source,
        }
        if tags:
            body["tags"] = tags
        resp = self._get_http().post("/memories", json=body)
        resp.raise_for_status()

    def _api_recall(self, query: str, *, limit: int = 10,
                    min_importance: Optional[int] = None) -> List[dict]:
        body: Dict[str, Any] = {
            "query": query,
            "tenant_id": self._tenant_id,
            "project_id": self._project_id,
            "limit": limit,
        }
        if min_importance is not None:
            body["min_importance"] = min_importance
        resp = self._get_http().post("/memories/recall", json=body)
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    def _api_list(self, *, limit: int = 50) -> List[dict]:
        params = {
            "tenant_id": self._tenant_id,
            "project_id": self._project_id,
            "limit": limit,
            "sort": "importance",
            "order": "desc",
        }
        resp = self._get_http().get("/memories", params=params)
        resp.raise_for_status()
        return resp.json().get("memories", []) or []

    # -- System prompt -----------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Memra Memory\n"
            f"Active. Tenant: {self._tenant_id}.\n"
            "Use memra_search to recall by meaning, memra_remember to store a "
            "durable fact, memra_profile for an overview of what you know."
        )

    # -- Prefetch ----------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Memra Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open() or not query:
            return

        def _run():
            try:
                results = self._api_recall(query, limit=5)
                if results:
                    lines = [r.get("content", "") for r in results if r.get("content")]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Memra prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="memra-prefetch")
        self._prefetch_thread.start()

    # -- Turn sync ---------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Persist the completed turn as an episodic memory (non-blocking)."""
        if self._is_breaker_open() or not user_content:
            return

        def _sync():
            try:
                content = f"User: {user_content}\nAssistant: {assistant_content}"
                self._api_add(content, type="episodic", importance=4,
                              tags=["hermes:turn"], source="hermes:turn")
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Memra sync failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="memra-sync")
        self._sync_thread.start()

    # -- Compression hook (differentiator) ---------------------------------

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Persist context that's about to be discarded into Memra long-term
        memory, so it survives compression and stays recallable.

        Memra applies its own server-side compression to stored memories, so
        we hand off the raw span and let the backend distill it. Returns a
        short note for Hermes's compression summary prompt.
        """
        if self._is_breaker_open() or not messages:
            return ""

        parts: List[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
                parts.append(f"{role}: {content.strip()}")
        if not parts:
            return ""
        digest = "\n".join(parts)

        def _store():
            try:
                self._api_add(digest, type="episodic", importance=5,
                              tags=["hermes:compression"], source="hermes:compression")
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Memra pre-compress store failed: %s", e)

        threading.Thread(target=_store, daemon=True, name="memra-precompress").start()
        return (
            "Older conversation context has been persisted to Memra long-term "
            "memory and remains recallable via memra_search."
        )

    # -- Mirror built-in memory writes -------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror Hermes built-in MEMORY.md / USER.md writes into Memra."""
        if self._is_breaker_open() or action not in ("add", "replace") or not content:
            return

        def _mirror():
            try:
                self._api_add(content, type="semantic", importance=6,
                              tags=[f"hermes:{target}"], source=f"hermes:builtin:{target}")
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Memra memory-write mirror failed: %s", e)

        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5.0)
        self._write_thread = threading.Thread(target=_mirror, daemon=True, name="memra-mirror")
        self._write_thread.start()

    # -- Tools -------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, REMEMBER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({
                "error": "Memra API temporarily unavailable (multiple consecutive failures). Will retry automatically."
            })

        if tool_name == "memra_profile":
            try:
                memories = self._api_list(limit=50)
                self._record_success()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                lines = [m.get("content", "") for m in memories if m.get("content")]
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to fetch profile: {e}")

        elif tool_name == "memra_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            top_k = min(int(args.get("top_k", 10)), 50)
            min_importance = args.get("min_importance")
            try:
                results = self._api_recall(
                    query, limit=top_k,
                    min_importance=int(min_importance) if min_importance is not None else None,
                )
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"content": r.get("content", ""), "score": r.get("score", 0),
                          "importance": r.get("importance")} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Search failed: {e}")

        elif tool_name == "memra_remember":
            content = args.get("content", "")
            if not content:
                return tool_error("Missing required parameter: content")
            importance = int(args.get("importance", 6))
            tags = args.get("tags") or None
            try:
                self._api_add(content, type="semantic", importance=importance,
                              tags=tags, source="hermes:remember")
                self._record_success()
                return json.dumps({"result": "Fact stored."})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to store: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    # -- Shutdown ----------------------------------------------------------

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread, self._write_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._http_lock:
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
                self._http = None


def register(ctx) -> None:
    """Register Memra as a memory provider plugin."""
    ctx.register_memory_provider(MemraMemoryProvider())
