"""
In-memory context store.

Holds every context the judge pushes (category, merchant, customer, trigger).
Rules (from challenge-testing-brief.md §2.1):
  - Same or lower version for a context_id  -> rejected as stale (409)
  - Higher version                          -> replaces the old one atomically
"""
import threading
from datetime import datetime, timezone

VALID_SCOPES = ("category", "merchant", "customer", "trigger")

# Which field inside each payload holds the object's own id.
# Used as a backup lookup in case context_id and the payload id differ.
ID_FIELDS = {
    "category": "slug",
    "merchant": "merchant_id",
    "customer": "customer_id",
    "trigger": "id",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ContextStore:
    def __init__(self):
        self._lock = threading.Lock()
        # scope -> context_id -> {"version", "payload", "stored_at"}
        self._data = {scope: {} for scope in VALID_SCOPES}
        # scope -> id found inside payload -> context_id
        self._alias = {scope: {} for scope in VALID_SCOPES}

    def put(self, scope: str, context_id: str, version: int, payload: dict):
        """Returns (accepted: bool, current_version: int, stored_at: str | None)."""
        with self._lock:
            current = self._data[scope].get(context_id)
            if current is not None and version <= current["version"]:
                return False, current["version"], None

            stored_at = utc_now_iso()
            self._data[scope][context_id] = {
                "version": version,
                "payload": payload,
                "stored_at": stored_at,
            }
            inner_id = payload.get(ID_FIELDS[scope])
            if isinstance(inner_id, str) and inner_id != context_id:
                self._alias[scope][inner_id] = context_id
            return True, version, stored_at

    def get(self, scope: str, some_id: str | None) -> dict | None:
        """Look up a payload by context_id, falling back to the id inside the payload."""
        if not some_id or scope not in self._data:
            return None
        with self._lock:
            entry = self._data[scope].get(some_id)
            if entry is None:
                real_id = self._alias[scope].get(some_id)
                entry = self._data[scope].get(real_id) if real_id else None
            return entry["payload"] if entry else None

    def all(self, scope: str) -> dict:
        """All payloads in a scope, as {context_id: payload}."""
        with self._lock:
            return {cid: e["payload"] for cid, e in self._data[scope].items()}

    def counts(self) -> dict:
        with self._lock:
            return {scope: len(items) for scope, items in self._data.items()}

    def clear(self):
        with self._lock:
            for scope in VALID_SCOPES:
                self._data[scope].clear()
                self._alias[scope].clear()


# One shared store for the whole app
store = ContextStore()