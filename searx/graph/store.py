# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local SQLite store for the personal knowledge graph (entities/relations/projects).

The store keeps a directed, weighted graph of ``entity -[relation]-> entity``
triples plus user-defined **projects** that entities can be linked to.  It is
local-only (a single SQLite file with WAL) and **fail-safe**: any error is logged
at ``debug`` level and yields an empty result / no-op.

Writes are performed by the graph background worker (see
:py:mod:`searx.graph.context`); reads happen on the request thread.  Each thread
uses its own connection (``threading.local``) guarded by a re-entrant lock for
the write paths.
"""

__all__ = ["GraphStore", "get_store", "reset_store"]

import os
import sqlite3
import tempfile
import threading
import time
import typing as t

from searx import get_setting, logger
from searx.nextgen import edge_id, node_id, normalize_key

log = logger.getChild("searx.graph.store")

_DEFAULT_DB_PATH: str = os.path.join(tempfile.gettempdir(), "searx_graph.db")
_DEFAULT_MAX_ROWS: int = 20000
_KEY_MAX_LEN: int = 120
_MESH_WEIGHT_CAP: float = 5.0

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS entity (
      id      TEXT PRIMARY KEY,
      key     TEXT NOT NULL,
      name    TEXT NOT NULL,
      kind    TEXT NOT NULL DEFAULT '',
      created REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS entity_key_idx ON entity(key)",
    """
    CREATE TABLE IF NOT EXISTS relation (
      id       TEXT PRIMARY KEY,
      src_id   TEXT NOT NULL,
      rel      TEXT NOT NULL,
      dst_id   TEXT NOT NULL,
      weight   REAL NOT NULL DEFAULT 1.0,
      created  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS relation_src_idx ON relation(src_id)",
    "CREATE INDEX IF NOT EXISTS relation_dst_idx ON relation(dst_id)",
    """
    CREATE TABLE IF NOT EXISTS project (
      id          TEXT PRIMARY KEY,
      name        TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      created     REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS project_entity (
      project_id TEXT NOT NULL,
      entity_id  TEXT NOT NULL,
      weight     REAL NOT NULL DEFAULT 1.0,
      created    REAL NOT NULL,
      PRIMARY KEY (project_id, entity_id)
    )""",
)

_SQL_UPSERT_ENTITY: str = (
    "INSERT INTO entity (id, key, name, kind, created) VALUES (?, ?, ?, ?, ?)"
    "    ON CONFLICT(id) DO UPDATE SET name = entity.name"
)
_SQL_UPSERT_RELATION: str = (
    "INSERT INTO relation (id, src_id, rel, dst_id, weight, created) VALUES (?, ?, ?, ?, ?, ?)"
    "    ON CONFLICT(id) DO UPDATE SET weight = relation.weight + 1.0, created = excluded.created"
)
_SQL_UPSERT_PROJECT: str = "INSERT INTO project (id, name, description, created) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING"
_SQL_LINK: str = (
    "INSERT INTO project_entity (project_id, entity_id, weight, created) VALUES (?, ?, ?, ?)"
    "    ON CONFLICT(project_id, entity_id) DO UPDATE SET weight = project_entity.weight + excluded.weight"
)
_SQL_PURGE_RELATIONS: str = "DELETE FROM relation WHERE id NOT IN (SELECT id FROM relation ORDER BY created DESC LIMIT ?)"
_SQL_EXPORT_RELATIONS: str = "SELECT src_id, rel, dst_id, weight FROM relation ORDER BY weight DESC LIMIT ?"
_SQL_REINFORCE_RELATION: str = "UPDATE relation SET weight = weight + ? WHERE src_id = ? AND rel = ? AND dst_id = ?"


class _Triple(t.NamedTuple):
    source: str
    relation: str
    target: str


def _clean(value: t.Any, limit: int = _KEY_MAX_LEN) -> str:
    """Trim/collapse whitespace and cap length; never raises."""
    try:
        text = " ".join(str(value or "").split())
    except Exception:  # pylint: disable=broad-except
        return ""
    return text[:limit]


class GraphStore:
    """SQLite-backed personal knowledge graph (entities, relations, projects)."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
        self._db_path: str = db_path or _DEFAULT_DB_PATH
        self._max_rows: int = max(1, int(max_rows))
        self._lock: threading.RLock = threading.RLock()
        self._local: threading.local = threading.local()

    # --- connections --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=3.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            with conn:
                for statement in _DDL:
                    conn.execute(statement)
            self._local.conn = conn
        return conn

    # --- writes -------------------------------------------------------------

    def upsert_entity(self, name: str, kind: str = "") -> str | None:
        """Insert (or touch) an entity and return its stable id, or ``None``."""
        display = _clean(name)
        key = normalize_key(display)
        if not key:
            return None
        kind_key = _clean(kind, 40)
        eid = node_id(kind_key, key)
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_UPSERT_ENTITY, (eid, key, display, kind_key, time.time()))
            return eid
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: upsert_entity failed: %s", e)
            return None

    def upsert_triples(self, triples: t.Iterable[t.Sequence[str] | _Triple], source_query: str = "") -> int:
        """Insert (or reinforce) ``(source, relation, target)`` triples.

        Returns the number of triples written.  ``source_query`` is stored only
        implicitly through entity/relation provenance and is not persisted raw.
        """
        written = 0
        try:
            conn = self._conn()
            with self._lock, conn:
                for triple in triples:
                    try:
                        src, rel, dst = triple[0], triple[1], triple[2]
                    except (TypeError, IndexError):
                        continue
                    src_d = _clean(src)
                    dst_d = _clean(dst)
                    rel_d = _clean(rel, 60)
                    if not src_d or not dst_d or not rel_d:
                        continue
                    src_key, dst_key = normalize_key(src_d), normalize_key(dst_d)
                    if src_key == dst_key:
                        continue
                    src_id = node_id("", src_key)
                    dst_id = node_id("", dst_key)
                    now = time.time()
                    conn.execute(_SQL_UPSERT_ENTITY, (src_id, src_key, src_d, "", now))
                    conn.execute(_SQL_UPSERT_ENTITY, (dst_id, dst_key, dst_d, "", now))
                    conn.execute(
                        _SQL_UPSERT_RELATION,
                        (edge_id(src_id, rel_d, dst_id), src_id, rel_d, dst_id, 1.0, now),
                    )
                    written += 1
                if written:
                    conn.execute(_SQL_PURGE_RELATIONS, (self._max_rows,))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: upsert_triples failed: %s", e)
        return written

    def add_project(self, name: str, description: str = "") -> str | None:
        """Create (or touch) a project and return its stable id, or ``None``."""
        display = _clean(name, 80)
        key = normalize_key(display)
        if not key:
            return None
        pid = node_id("project", key)
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_UPSERT_PROJECT, (pid, display, _clean(description, 400), time.time()))
            return pid
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: add_project failed: %s", e)
            return None

    def link_project_entity(self, project: str, entity: str, weight: float = 1.0) -> bool:
        """Link ``entity`` to project ``project`` (both by name), creating as needed."""
        pid = self.add_project(project)
        eid = self.upsert_entity(entity)
        if pid is None or eid is None:
            return False
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_LINK, (pid, eid, float(weight), time.time()))
            return True
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: link_project_entity failed: %s", e)
            return False

    # --- reads --------------------------------------------------------------

    def find_entities(self, keys: t.Iterable[str], limit: int = 10) -> list[tuple[str, str]]:
        """Return ``(id, name)`` for entities matching any normalized ``key``."""
        norm = [normalize_key(k) for k in keys]
        norm = [k for k in norm if k]
        if not norm:
            return []
        placeholders = ",".join("?" for _ in norm)
        sql = f"SELECT id, name FROM entity WHERE key IN ({placeholders}) ORDER BY created DESC LIMIT ?"
        try:
            conn = self._conn()
            rows = conn.execute(sql, (*norm, max(1, int(limit)))).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: find_entities failed: %s", e)
            return []

    def neighbors(self, entity_ids: t.Iterable[str], limit: int = 8) -> list[tuple[str, str, float]]:
        """Return ``(relation, other_name, weight)`` for 1-hop neighbours of the ids."""
        ids = [str(i) for i in entity_ids if i]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT r.rel, e.name, r.weight FROM relation r JOIN entity e ON e.id = r.dst_id"
            f" WHERE r.src_id IN ({placeholders})"
            " UNION ALL "
            "SELECT r.rel, e.name, r.weight FROM relation r JOIN entity e ON e.id = r.src_id"
            f" WHERE r.dst_id IN ({placeholders})"
            " ORDER BY 3 DESC LIMIT ?"
        )
        try:
            conn = self._conn()
            rows = conn.execute(sql, (*ids, *ids, max(1, int(limit)))).fetchall()
            return [(str(r[0]), str(r[1]), float(r[2])) for r in rows]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: neighbors failed: %s", e)
            return []

    def projects_for_entities(self, entity_ids: t.Iterable[str], limit: int = 5) -> list[tuple[str, str, float]]:
        """Return ``(project_name, description, weight)`` linked to the given entities."""
        ids = [str(i) for i in entity_ids if i]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT p.name, p.description, SUM(pe.weight) AS w FROM project_entity pe"
            " JOIN project p ON p.id = pe.project_id"
            f" WHERE pe.entity_id IN ({placeholders})"
            " GROUP BY p.id ORDER BY w DESC LIMIT ?"
        )
        try:
            conn = self._conn()
            rows = conn.execute(sql, (*ids, max(1, int(limit)))).fetchall()
            return [(str(r[0]), str(r[1]), float(r[2])) for r in rows]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: projects_for_entities failed: %s", e)
            return []

    def list_projects(self, limit: int = 50) -> list[tuple[str, str]]:
        """Return ``(name, description)`` for the known projects."""
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT name, description FROM project ORDER BY created DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: list_projects failed: %s", e)
            return []

    def count(self) -> tuple[int, int, int]:
        """Return ``(entities, relations, projects)`` counts, or zeros."""
        try:
            conn = self._conn()
            ent = int(conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0])
            rel = int(conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0])
            pro = int(conn.execute("SELECT COUNT(*) FROM project").fetchone()[0])
            return ent, rel, pro
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: count failed: %s", e)
            return 0, 0, 0

    # --- mesh fragments (anonymous, additive) -------------------------------

    def export_fragments(self, limit: int = 200) -> list[dict]:
        """Return up to ``limit`` anonymous relation fragments (hashed ids only)."""
        try:
            try:
                cap = max(1, int(limit))
            except (TypeError, ValueError):
                cap = 200
            conn = self._conn()
            rows = conn.execute(_SQL_EXPORT_RELATIONS, (cap,)).fetchall()
            return [
                {"s": str(row[0]), "r": str(row[1]), "t": str(row[2]), "w": round(float(row[3]), 3)}
                for row in rows
            ]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: export_fragments failed: %s", e)
            return []

    def import_fragment(self, fragment: dict) -> int:
        """Reinforce an existing local relation from a mesh fragment.

        Only **existing** relations are updated (weight += min(w, cap)); no entity
        is ever created, so remote fragments cannot pollute the local graph.
        """
        try:
            if not isinstance(fragment, dict):
                return 0
            src = str(fragment.get("s", ""))
            rel = _clean(fragment.get("r", ""), 60)
            dst = str(fragment.get("t", ""))
            if not src or not rel or not dst or src == dst:
                return 0
            try:
                weight = float(fragment.get("w", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            increment = min(max(weight, 0.0), _MESH_WEIGHT_CAP)
            if increment <= 0:
                return 0
            conn = self._conn()
            with self._lock, conn:
                cursor = conn.execute(_SQL_REINFORCE_RELATION, (increment, src, rel, dst))
                return int(cursor.rowcount or 0)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: import_fragment failed: %s", e)
            return 0

    def close(self) -> None:
        """Close the current thread's connection (best effort)."""
        try:
            conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
        except Exception:  # pylint: disable=broad-except
            pass


_STORE: GraphStore | None = None


def get_store() -> GraphStore | None:
    """Return the singleton :py:obj:`GraphStore`, building it on first use."""
    global _STORE  # pylint: disable=global-statement
    if _STORE is None:
        try:
            max_rows = int(get_setting("search.nextgen.graph.max_rows", _DEFAULT_MAX_ROWS) or _DEFAULT_MAX_ROWS)
            _STORE = GraphStore(_DEFAULT_DB_PATH, max_rows)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("graph store: initialization failed: %s", e)
            return None
    return _STORE


def reset_store() -> None:
    """Drop the singleton (mainly useful for tests)."""
    global _STORE  # pylint: disable=global-statement
    store = _STORE
    _STORE = None
    if store is not None:
        store.close()
