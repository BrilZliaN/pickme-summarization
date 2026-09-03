"""Single-connection SQLite manager with WAL and file-based migrations."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite


def _dict_factory(cursor: object, row: tuple[object, ...]) -> dict[str, object]:
    """Convert a DB-API row tuple to a plain dict using cursor description."""
    # cursor.description is sequence of 7-tuples; first element is column name.
    desc = getattr(cursor, "description", None)  # type: ignore[attr-defined]
    if desc is None:
        return {}
    return {col[0]: row[idx] for idx, col in enumerate(desc)}


class Database:
    """Single shared aiosqlite connection with WAL, foreign keys, and migrations."""

    def __init__(self, db_path: str, migrations_dir: str | Path | None = None) -> None:
        """Initialize with a file path and optional migrations directory override.

        Args:
            db_path: Filesystem path to the SQLite database file.
            migrations_dir: Override for the migrations directory. When ``None``,
                resolves via package-relative and repo-relative fallbacks.
        """
        self._db_path = db_path
        self._migrations_dir: Path | None = Path(migrations_dir) if migrations_dir is not None else None
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database, apply PRAGMAs, and run pending file migrations."""
        # Ensure parent directory exists.
        parent = Path(self._db_path).parent
        parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = _dict_factory  # type: ignore[assignment]

        # Apply required PRAGMAs.
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")

        # Ensure schema_migrations tracking table exists.
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            "(name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        await conn.commit()

        # Discover migrations directory — robust order:
        # 1. explicit ctor arg
        # 2. package-relative: pickme/migrations (wheel/site-packages and editable via force-include)
        # 3. repo-relative: migrations beside src/ (dev checkout)
        migrations_dir: Path | None = None
        tried: list[str] = []

        if self._migrations_dir is not None:
            candidates = [self._migrations_dir]
            tried = [str(self._migrations_dir)]
            # Explicit arg must exist and contain .sql files — fail loudly otherwise
            for cand in candidates:
                if cand.is_dir():
                    migrations_dir = cand
                    break
            if migrations_dir is None:
                await conn.close()
                raise RuntimeError(f"migrations directory not found: {', '.join(tried)}")
        else:
            # Build candidate list in priority order
            pkg_relative = Path(__file__).resolve().parent.parent / "migrations"
            # Repo-relative candidates: spec says parents[2]/migrations, but actual
            # project root is parents[3] for this file (src/pickme/db/connection.py).
            # Include both to be robust across spec wording and real layout.
            repo_parents2 = Path(__file__).resolve().parents[2] / "migrations"
            repo_parents3 = Path(__file__).resolve().parents[3] / "migrations"
            candidates = [pkg_relative, repo_parents2, repo_parents3]
            tried = [str(c) for c in candidates]
            for cand in candidates:
                if cand.is_dir():
                    migrations_dir = cand
                    break
            if migrations_dir is None:
                await conn.close()
                raise RuntimeError(f"migrations directory not found: {', '.join(tried)}")

        # At this point migrations_dir is an existing directory
        sql_files = sorted(migrations_dir.glob("*.sql"))
        if not sql_files:
            await conn.close()
            raise RuntimeError(f"migrations directory not found: {migrations_dir} contains no .sql files (tried {', '.join(tried)})")

        # Apply pending .sql files in filename order.
        for sql_file in sql_files:
            name = sql_file.name
            cursor = await conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
            )
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is not None:
                continue
            sql_text = sql_file.read_text(encoding="utf-8")
            await conn.executescript(sql_text)
            await conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (name, int(time.time() * 1000)),
            )
            await conn.commit()

        self._conn = conn

    async def close(self) -> None:
        """Close the shared connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the single shared connection.

        Raises:
            RuntimeError: If :meth:`connect` has not been called.
        """
        if self._conn is None:
            raise RuntimeError("Database not connected — call await connect() first")
        return self._conn
