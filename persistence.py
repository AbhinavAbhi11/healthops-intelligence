"""Durable local persistence for the single-user HealthOps development build.

This module stores DataFrame snapshots as pandas JSON-table documents (never pickle)
and keeps workspace metadata/job status in a local SQLite database. It is intended
for a loopback-only, single-user development installation, not multi-tenant production.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


_STORAGE_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FRAME_GROUPS = ("tables", "source_tables", "cleaned")
_DEFAULT_JOB_RETENTION = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    """Convert common pandas/numpy objects recursively into strict JSON values."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, pd.DataFrame):
        raise TypeError(
            "DataFrames must be written as table snapshots, not JSON metadata."
        )
    if isinstance(value, pd.Series):
        return [_json_safe(item) for item in value.tolist()]
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if pd.isna(value) or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


class LocalPersistence:
    """Persist one local workspace and its recent jobs to disk."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        enabled: bool = True,
    ):
        configured_root = root or os.environ.get("HEALTHOPS_DATA_DIR")
        self.root = (
            Path(configured_root).expanduser().resolve()
            if configured_root
            else None
        )
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._ready = False

        if self.enabled:
            if self.root is None:
                raise ValueError(
                    "A storage root is required when persistence is enabled."
                )
            self.snapshots_dir = self.root / "snapshots"
            self.database_path = self.root / "healthops.sqlite3"
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            self.root.mkdir(parents=True, exist_ok=True)
            self._initialise()
        else:
            self.snapshots_dir = None
            self.database_path = None

    def _connect(self) -> sqlite3.Connection:
        if not self.enabled or self.database_path is None:
            raise RuntimeError("Local persistence is disabled.")

        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialise(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS workspace_state (
                        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                        snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_updated_at
                        ON jobs(updated_at DESC);
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES(?, ?)
                    """,
                    (_STORAGE_SCHEMA_VERSION, _utc_now()),
                )
                connection.commit()
                self._ready = True
            finally:
                connection.close()

    def _write_frame(self, destination: Path, frame: pd.DataFrame) -> None:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(
                "Persisted table values must be pandas DataFrames."
            )

        # JSON-table format avoids unsafe Python pickle deserialisation and records
        # column schema/dtypes. Source indices are not treated as business data.
        content = frame.to_json(
            orient="table",
            index=False,
            date_format="iso",
            date_unit="us",
            double_precision=15,
            force_ascii=False,
        )
        destination.write_text(content, encoding="utf-8")

    def _read_frame(self, source: Path) -> pd.DataFrame:
        if not source.is_file():
            raise ValueError(
                f"Persisted table snapshot is missing: {source.name}"
            )
        return pd.read_json(source, orient="table")

    def save_workspace(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Write a new immutable snapshot, then atomically point metadata at it."""
        if not self.enabled:
            return None

        with self._lock:
            snapshot_id = uuid.uuid4().hex
            final_dir = self.snapshots_dir / snapshot_id
            temp_dir = self.snapshots_dir / f".tmp-{snapshot_id}"
            temp_dir.mkdir(parents=True, exist_ok=False)

            try:
                manifest: dict[str, Any] = {
                    "schema_version": _STORAGE_SCHEMA_VERSION,
                    "snapshot_id": snapshot_id,
                    "saved_at": _utc_now(),
                    "state": {},
                    "frame_groups": {},
                }
                frame_root = temp_dir / "frames"
                frame_root.mkdir()

                for group_name in _FRAME_GROUPS:
                    group_value = state.get(group_name)

                    if group_value is None:
                        manifest["frame_groups"][group_name] = None
                        continue

                    if not isinstance(group_value, Mapping):
                        raise TypeError(
                            f"Workspace field {group_name!r} must be a "
                            "mapping or None."
                        )

                    group_entries = []
                    group_dir = frame_root / group_name
                    group_dir.mkdir()

                    for position, (table_name, frame) in enumerate(
                        group_value.items()
                    ):
                        filename = f"table_{position:05d}.json"
                        self._write_frame(group_dir / filename, frame)
                        group_entries.append(
                            {
                                "name": str(table_name),
                                "file": (
                                    f"frames/{group_name}/{filename}"
                                ),
                            }
                        )

                    manifest["frame_groups"][group_name] = group_entries

                metadata = {
                    key: value
                    for key, value in state.items()
                    if key not in _FRAME_GROUPS
                    and not str(key).startswith("_")
                }
                manifest["state"] = _json_safe(metadata)

                manifest_path = temp_dir / "workspace.json"
                manifest_path.write_text(
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        allow_nan=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                # The snapshot directory is complete before it becomes visible.
                os.replace(temp_dir, final_dir)

                connection = self._connect()
                try:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO workspace_state(
                                singleton_id, snapshot_id, updated_at
                            )
                            VALUES(1, ?, ?)
                            ON CONFLICT(singleton_id) DO UPDATE SET
                                snapshot_id=excluded.snapshot_id,
                                updated_at=excluded.updated_at
                            """,
                            (snapshot_id, manifest["saved_at"]),
                        )
                finally:
                    connection.close()

                self._prune_snapshots(keep={snapshot_id})
                return {
                    "snapshot_id": snapshot_id,
                    "saved_at": manifest["saved_at"],
                }

            except Exception:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                # If metadata commit fails after the final directory rename, leave
                # the unreferenced directory for safe later pruning/inspection.
                raise

    def load_workspace(self) -> dict[str, Any] | None:
        """Load and validate the last committed snapshot, if one exists."""
        if not self.enabled:
            return None

        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT snapshot_id
                    FROM workspace_state
                    WHERE singleton_id=1
                    """
                ).fetchone()
            finally:
                connection.close()

            if row is None:
                return None

            snapshot_id = row["snapshot_id"]
            if (
                not isinstance(snapshot_id, str)
                or not _SAFE_ID.fullmatch(snapshot_id)
            ):
                raise ValueError(
                    "The stored workspace snapshot identifier is invalid."
                )

            snapshot_dir = self.snapshots_dir / snapshot_id
            manifest_path = snapshot_dir / "workspace.json"

            if not manifest_path.is_file():
                raise ValueError(
                    "The latest workspace snapshot is missing its manifest."
                )

            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )

            if manifest.get("snapshot_id") != snapshot_id:
                raise ValueError(
                    "Workspace snapshot manifest does not match its database record."
                )

            if manifest.get("schema_version") != _STORAGE_SCHEMA_VERSION:
                raise ValueError(
                    "Workspace snapshot uses an unsupported schema version."
                )

            state = dict(manifest.get("state", {}))

            for group_name in _FRAME_GROUPS:
                entries = manifest.get("frame_groups", {}).get(group_name)

                if entries is None:
                    state[group_name] = None
                    continue

                loaded_frames: dict[str, pd.DataFrame] = {}

                for entry in entries:
                    filename = entry.get("file", "")
                    relative = Path(filename)

                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError(
                            "Workspace snapshot contains an unsafe table path."
                        )

                    source = (snapshot_dir / relative).resolve()

                    if not source.is_relative_to(snapshot_dir.resolve()):
                        raise ValueError(
                            "Workspace snapshot table escaped the snapshot directory."
                        )

                    table_name = entry.get("name")
                    if (
                        not isinstance(table_name, str)
                        or table_name in loaded_frames
                    ):
                        raise ValueError(
                            "Workspace snapshot contains an invalid or duplicate table name."
                        )

                    loaded_frames[table_name] = self._read_frame(source)

                state[group_name] = loaded_frames

            return state

    def save_job(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not self.enabled:
            return

        if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
            raise ValueError("Invalid job identifier.")

        now = _utc_now()
        safe_payload = dict(_json_safe(payload))

        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    existing = connection.execute(
                        "SELECT created_at FROM jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    created_at = existing["created_at"] if existing else now

                    connection.execute(
                        """
                        INSERT INTO jobs(
                            job_id, status, payload_json, created_at, updated_at
                        )
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            status=excluded.status,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            job_id,
                            str(safe_payload.get("status", "unknown")),
                            json.dumps(
                                safe_payload,
                                ensure_ascii=False,
                                allow_nan=False,
                            ),
                            created_at,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM jobs
                        WHERE job_id IN (
                            SELECT job_id FROM jobs
                            ORDER BY updated_at DESC
                            LIMIT -1 OFFSET ?
                        )
                        """,
                        (_DEFAULT_JOB_RETENTION,),
                    )
            finally:
                connection.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if (
            not self.enabled
            or not isinstance(job_id, str)
            or not _SAFE_ID.fullmatch(job_id)
        ):
            return None

        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT payload_json FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            finally:
                connection.close()

        return json.loads(row["payload_json"]) if row else None

    def recover_interrupted_jobs(self) -> int:
        """Mark previously running jobs as interrupted after a process restart."""
        if not self.enabled:
            return 0

        interrupted = 0
        now = _utc_now()

        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT job_id, payload_json
                    FROM jobs
                    WHERE status IN ('processing','queued','running')
                    """
                ).fetchall()

                with connection:
                    for row in rows:
                        payload = json.loads(row["payload_json"])
                        payload["status"] = "error"
                        payload["error"] = (
                            "The server restarted before this job completed. "
                            "Run the analysis again to create a new job."
                        )
                        payload["recovered_after_restart"] = True
                        connection.execute(
                            """
                            UPDATE jobs
                            SET status='error', payload_json=?, updated_at=?
                            WHERE job_id=?
                            """,
                            (
                                json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                ),
                                now,
                                row["job_id"],
                            ),
                        )
                        interrupted += 1
            finally:
                connection.close()

        return interrupted

    def _prune_snapshots(self, keep: set[str]) -> None:
        if not self.enabled:
            return

        # Keep the current snapshot and the most recent previous complete snapshot
        # for local recovery. Never delete the active snapshot.
        directories = sorted(
            (
                p for p in self.snapshots_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".tmp-")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        retained = set(keep)
        previous = [
            p.name for p in directories if p.name not in retained
        ]

        if previous:
            retained.add(previous[0])

        for directory in directories:
            if directory.name not in retained:
                shutil.rmtree(directory, ignore_errors=True)

        for path in self.snapshots_dir.glob(".tmp-*"):
            shutil.rmtree(path, ignore_errors=True)