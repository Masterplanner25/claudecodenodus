"""Durable, tag-indexed memory for the research agent.

Nodus's ``std:memory`` is a flat key/value store held in a per-runtime
``MemoryStore`` (and, by default, a *process-global* one — VM-001).  That is
enough for within-run bookkeeping, but the plan's memory schema asks for two
things it does not provide:

1. **Durability** — ``research/{session}/final`` written in one process must
   still be there in the next one, the way approvals and effect records are.
2. **Cross-session recall** — "what did I already learn about this topic?",
   answered by tag, across every session in the workspace.

``SqliteMemoryStore`` supplies both while staying a drop-in ``MemoryStore``:
the workflow keeps using plain ``mem.put`` / ``mem.get`` / ``mem.tag``, and the
host injects this store via ``NodusRuntime(memory_store=...)``.

**Tags ride on the stdlib.** ``std:memory``'s ``tag(key, tags)`` is just
``put("__nodus_tags__:<key>", tags)``.  This store recognises that key shape and
mirrors the tags into a searchable index, so tagging from ``.nd`` code needs no
new builtin — and the raw value is still stored, so ``mem.get`` of the tag key
keeps working.

**Why a subclass rather than a Protocol implementation.**
``memory_runtime.recall_from`` / ``recall_all`` reach into ``store._values``
directly, so the in-memory dict has to stay authoritative.  The SQLite file is
a write-through mirror, loaded back into ``_values`` on construction.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from nodus.services.memory_runtime import MemoryStore

# std:memory's `tag()` writes the tag list under this prefix.
TAG_KEY_PREFIX = "__nodus_tags__:"

# Words carrying no topical signal.  Deliberately short: this is a recall
# heuristic, not a search engine, and an over-eager stoplist silently drops
# the one word two sessions had in common.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from by with without
is are was were be been being am do does did doing have has had having
what which who whom whose when where why how
i you he she it we they me him her us them my your his its our their
can could shall should will would may might must
about into over under again further once here there all any both each few more most other some such
no nor not only own same so too very s t just don now
""".split())

# Keep recall keys short and stable; a question is not a document.
_MAX_TOPIC_TAGS = 8


def topic_tags(question: str, *, max_tags: int = _MAX_TOPIC_TAGS) -> list[str]:
    """Derive ``topic:<word>`` tags from a question, deterministically.

    Lowercase, split on non-alphanumerics, drop stopwords and words shorter
    than three characters, de-duplicate preserving order, cap the count.
    Recall then matches on *any* overlap, so two differently-phrased questions
    about the same subject still find each other:

        "What is LLM safety?"                     -> topic:llm, topic:safety
        "Recent advances in LLM alignment"        -> topic:recent, topic:advances,
                                                     topic:llm, topic:alignment

    Deterministic on purpose — the tests stay hermetic and a recall is
    reproducible.  An LLM-extracted canonical topic would group synonyms
    better; that is a drop-in replacement for this function, not a change to
    anything downstream.
    """
    seen: list[str] = []
    for word in re.split(r"[^a-z0-9]+", question.lower()):
        if len(word) < 3 or word in _STOPWORDS or word in seen:
            continue
        seen.append(word)
        if len(seen) >= max_tags:
            break
    return [f"topic:{w}" for w in seen]


def session_of(key: str) -> str | None:
    """The session id in a ``research/{session_id}/...`` memory key, if any."""
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "research":
        return parts[1]
    return None


class SqliteMemoryStore(MemoryStore):
    """A ``MemoryStore`` that persists to SQLite and indexes tags.

    Values must be JSON-safe (the base class enforces this for us by cloning
    through ``clone_json_value``).  Every mutation is committed before it
    returns, so a value written in one process is visible to the next.

    Thread-safe the same way the rest of the host is: one connection
    (``check_same_thread=False``) behind a lock.
    """

    def __init__(self, path: str | Path, *, table_prefix: str = "memory") -> None:
        super().__init__()
        if not table_prefix.isidentifier():
            raise ValueError(f"table_prefix must be a valid identifier, got {table_prefix!r}")
        self._path = str(path)
        self._values_table = f"{table_prefix}_values"
        self._tags_table = f"{table_prefix}_tags"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._values_table} ("
            "key TEXT PRIMARY KEY, value_json TEXT NOT NULL)"
        )
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._tags_table} ("
            "key TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (key, tag))"
        )
        self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self._tags_table}_tag ON {self._tags_table} (tag)"
        )
        self._conn.commit()
        self._load()

    # ── MemoryStore overrides ────────────────────────────────────────────

    def put(self, key: str, value):
        stored = super().put(key, value)          # validates + clones into _values
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._values_table} (key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (key, json.dumps(stored)),
            )
            if key.startswith(TAG_KEY_PREFIX):
                self._index_tags(key[len(TAG_KEY_PREFIX):], stored)
            self._conn.commit()
        return stored

    def delete(self, key: str):
        existed, value = super().delete(key)
        with self._lock:
            self._conn.execute(f"DELETE FROM {self._values_table} WHERE key = ?", (key,))
            target = key[len(TAG_KEY_PREFIX):] if key.startswith(TAG_KEY_PREFIX) else key
            self._conn.execute(f"DELETE FROM {self._tags_table} WHERE key = ?", (target,))
            self._conn.commit()
        return existed, value

    def load_snapshot(self, values: dict | None) -> None:
        """Replace the whole store.  Used by test/reset paths — it truncates."""
        super().load_snapshot(values)
        with self._lock:
            self._conn.execute(f"DELETE FROM {self._values_table}")
            self._conn.execute(f"DELETE FROM {self._tags_table}")
            for key, value in self._values.items():
                self._conn.execute(
                    f"INSERT INTO {self._values_table} (key, value_json) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
                if key.startswith(TAG_KEY_PREFIX):
                    self._index_tags(key[len(TAG_KEY_PREFIX):], value)
            self._conn.commit()

    # ── tag index ────────────────────────────────────────────────────────

    def _index_tags(self, target_key: str, tags) -> None:
        """Replace *target_key*'s tags.  Caller holds the lock and commits."""
        self._conn.execute(f"DELETE FROM {self._tags_table} WHERE key = ?", (target_key,))
        if not isinstance(tags, list):
            return
        for tag in tags:
            if isinstance(tag, str) and tag:
                self._conn.execute(
                    f"INSERT OR IGNORE INTO {self._tags_table} (key, tag) VALUES (?, ?)",
                    (target_key, tag),
                )

    def tags_for(self, key: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT tag FROM {self._tags_table} WHERE key = ? ORDER BY tag", (key,)
            ).fetchall()
        return [r[0] for r in rows]

    def search_by_tags(
        self,
        tags,
        *,
        match: str = "any",
        require=None,
        exclude_session: str | None = None,
    ) -> list[str]:
        """Keys carrying these tags — ``match="any"`` (default) or ``"all"``.

        ``require`` is an additional set of tags a key must carry *all* of,
        independent of ``match``.  That combination is what a useful recall
        needs: overlap on *any* topic word, but only among nodes that are, say,
        ``status:final`` — an unfinished draft is not prior research.

        ``exclude_session`` drops keys belonging to one session, which is how a
        run recalls prior work without recalling itself.
        """
        wanted = [t for t in (tags or []) if isinstance(t, str) and t]
        if not wanted:
            return []
        required = [t for t in (require or []) if isinstance(t, str) and t]
        placeholders = ",".join("?" * len(wanted))
        sql = (
            f"SELECT key, COUNT(DISTINCT tag) AS hits FROM {self._tags_table} "
            f"WHERE tag IN ({placeholders})"
        )
        params: list = list(wanted)
        if required:
            req_placeholders = ",".join("?" * len(required))
            sql += (
                f" AND key IN (SELECT key FROM {self._tags_table} "
                f"WHERE tag IN ({req_placeholders}) GROUP BY key HAVING COUNT(DISTINCT tag) = ?)"
            )
            params.extend(required)
            params.append(len(required))
        sql += " GROUP BY key"
        if match == "all":
            sql += " HAVING hits = ?"
            params.append(len(wanted))
        # Most overlap first, then by key so the order is stable.
        sql += " ORDER BY hits DESC, key ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        keys = [r[0] for r in rows]
        if exclude_session is not None:
            keys = [k for k in keys if session_of(k) != exclude_session]
        return keys

    def recall_by_tags(
        self,
        tags,
        *,
        match: str = "any",
        require=None,
        exclude_session: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """``search_by_tags`` plus each key's value and tags, best match first.

        Keys whose value has since been deleted are skipped rather than
        returned as nulls — a dangling tag is not a memory.
        """
        out: list[dict] = []
        for key in self.search_by_tags(tags, match=match, require=require, exclude_session=exclude_session):
            if key not in self._values:
                continue
            out.append({
                "key": key,
                "session_id": session_of(key),
                "value": self.get(key),
                "tags": self.tags_for(key),
            })
            if len(out) >= max(0, limit):
                break
        return out

    # ── lifecycle ────────────────────────────────────────────────────────

    def _load(self) -> None:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT key, value_json FROM {self._values_table}"
            ).fetchall()
        for key, value_json in rows:
            try:
                # Straight into _values: MemoryStore.put would re-commit every
                # row we just read back, and these are already validated.
                self._values[key] = json.loads(value_json)
            except ValueError:
                continue

    def close(self) -> None:
        """Close the connection.  Safe to call more than once."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteMemoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
