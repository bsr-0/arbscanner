"""SQLite backup, restore, and retention helpers for the arbscanner database.

This module provides utilities for managing the scanner's SQLite database
(`arbscanner.db`). Because the scanner process may be actively writing
opportunities while a backup runs, we rely on SQLite's native
``Connection.backup()`` API, which performs an online, consistent snapshot
without blocking writers for long.

Capabilities:

* :func:`backup_database` creates a timestamped snapshot of the live DB in a
  backup directory (default ``<project>/backups``).
* :func:`restore_database` copies a backup file over the active database
  file. It refuses to clobber a running scanner (detected best-effort via
  the presence of ``*-journal`` / ``*-wal`` sidecar files) unless ``force``
  is set.
* :func:`list_backups` enumerates existing snapshot files, newest first.
* :func:`prune_backups` enforces a simple retention policy on the backup
  directory, deleting the oldest snapshots beyond ``keep``.
* :func:`prune_old_opportunities` trims the ``opportunities`` table itself,
  removing rows older than ``keep_days`` and reclaiming disk space via
  ``VACUUM``.

All functions log their operations via the module logger so that scheduled
backup/prune jobs leave an audit trail.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from arbscanner.config import DB_PATH, PROJECT_ROOT

logger = logging.getLogger(__name__)

BACKUP_DIR_DEFAULT: Path = PROJECT_ROOT / "backups"
BACKUP_FILENAME_FORMAT: str = "arbscanner-%Y%m%d%H%M%S.db"
BACKUP_GLOB: str = "arbscanner-*.db"


def backup_database(
    source_path: Path | None = None,
    dest_dir: Path | None = None,
) -> Path:
    """Create a consistent snapshot of the scanner database.

    Uses SQLite's native online backup API (:meth:`sqlite3.Connection.backup`)
    so the copy is safe even while the scanner is actively writing. The
    destination filename is of the form ``arbscanner-YYYYMMDDHHmmss.db``.

    Args:
        source_path: Path to the live database. Defaults to
            :data:`arbscanner.config.DB_PATH`.
        dest_dir: Directory that will hold the snapshot. Defaults to
            ``<PROJECT_ROOT>/backups``. Created if it does not exist.

    Returns:
        The absolute path to the newly written backup file.
    """
    src = Path(source_path) if source_path is not None else DB_PATH
    dst_dir = Path(dest_dir) if dest_dir is not None else BACKUP_DIR_DEFAULT

    dst_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime(BACKUP_FILENAME_FORMAT)
    dst_path = dst_dir / timestamp

    logger.info("Backing up database %s -> %s", src, dst_path)

    source_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dst_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    logger.info("Backup complete: %s (%d bytes)", dst_path, dst_path.stat().st_size)
    return dst_path


def restore_database(
    backup_path: Path,
    dest_path: Path | None = None,
    force: bool = False,
) -> None:
    """Restore a backup file over the active database.

    This is a destructive operation: the file at ``dest_path`` will be
    overwritten with the contents of ``backup_path``. As a best-effort guard
    against restoring while the scanner is still running, this function looks
    for ``*-journal`` or ``*-wal`` sidecar files next to ``dest_path`` and
    refuses to proceed if either is found, unless ``force=True``.

    Args:
        backup_path: Source snapshot file to restore.
        dest_path: Target database path. Defaults to
            :data:`arbscanner.config.DB_PATH`.
        force: If True, skip the sidecar-file safety check and overwrite
            unconditionally.

    Raises:
        FileNotFoundError: If ``backup_path`` does not exist.
        RuntimeError: If sidecar files suggest the scanner is active and
            ``force`` is False.
    """
    src = Path(backup_path)
    dst = Path(dest_path) if dest_path is not None else DB_PATH

    if not src.exists():
        raise FileNotFoundError(f"Backup file does not exist: {src}")

    journal = dst.with_name(dst.name + "-journal")
    wal = dst.with_name(dst.name + "-wal")
    sidecars = [p for p in (journal, wal) if p.exists()]

    if sidecars:
        message = (
            f"Detected active sidecar file(s): {[str(p) for p in sidecars]}. "
            "The scanner may still be running."
        )
        if not force:
            logger.error("Refusing to restore: %s Pass force=True to override.", message)
            raise RuntimeError(
                f"Refusing to restore {dst}: {message} Pass force=True to override."
            )
        logger.warning("Restoring despite active sidecars (force=True): %s", message)

    logger.info("Restoring database %s -> %s", src, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.info("Restore complete: %s (%d bytes)", dst, dst.stat().st_size)


def list_backups(backup_dir: Path | None = None) -> list[Path]:
    """Return backup snapshots in ``backup_dir`` sorted newest first.

    Args:
        backup_dir: Directory to scan. Defaults to ``<PROJECT_ROOT>/backups``.

    Returns:
        A list of backup file paths. Empty if the directory is missing or
        contains no matching files. Sorted by modification time, newest
        first.
    """
    directory = Path(backup_dir) if backup_dir is not None else BACKUP_DIR_DEFAULT
    if not directory.exists():
        logger.debug("Backup directory does not exist: %s", directory)
        return []

    backups = sorted(
        directory.glob(BACKUP_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    logger.debug("Found %d backup(s) in %s", len(backups), directory)
    return backups


def prune_backups(backup_dir: Path | None = None, keep: int = 10) -> int:
    """Delete the oldest backups beyond ``keep`` in the backup directory.

    Args:
        backup_dir: Directory to prune. Defaults to ``<PROJECT_ROOT>/backups``.
        keep: Number of most-recent backups to retain. Must be non-negative.

    Returns:
        The number of files actually deleted.
    """
    if keep < 0:
        raise ValueError(f"keep must be non-negative, got {keep}")

    backups = list_backups(backup_dir)
    if len(backups) <= keep:
        logger.info(
            "No backups to prune (have %d, keeping up to %d)", len(backups), keep
        )
        return 0

    to_delete = backups[keep:]
    deleted = 0
    for path in to_delete:
        try:
            path.unlink()
            logger.info("Deleted old backup: %s", path)
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to delete backup %s: %s", path, exc)

    logger.info("Pruned %d backup(s), kept %d", deleted, keep)
    return deleted


def prune_old_opportunities(
    db_path: Path | None = None,
    keep_days: int = 30,
) -> int:
    """Delete rows from ``opportunities`` older than ``keep_days``.

    Compares the stored ISO-8601 ``timestamp`` column against an ISO
    timestamp computed ``keep_days`` before now. After deletion, runs
    ``VACUUM`` to reclaim space.

    Args:
        db_path: Database to prune. Defaults to
            :data:`arbscanner.config.DB_PATH`.
        keep_days: Rows with ``timestamp`` older than this many days will be
            removed. Must be non-negative.

    Returns:
        The number of rows deleted.
    """
    if keep_days < 0:
        raise ValueError(f"keep_days must be non-negative, got {keep_days}")

    path = Path(db_path) if db_path is not None else DB_PATH
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()

    logger.info(
        "Pruning opportunities older than %s (keep_days=%d) from %s",
        cutoff,
        keep_days,
        path,
    )

    conn = sqlite3.connect(str(path))
    try:
        cursor = conn.execute(
            "DELETE FROM opportunities WHERE timestamp < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        logger.info("Deleted %d opportunity row(s); running VACUUM", deleted)
        # VACUUM cannot run inside a transaction; commit above ensures that.
        conn.execute("VACUUM")
    finally:
        conn.close()

    logger.info("Prune complete: removed %d row(s) from %s", deleted, path)
    return deleted
