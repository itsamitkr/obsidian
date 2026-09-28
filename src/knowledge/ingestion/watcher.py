"""Filesystem watcher for the inbox.

Drop a file into ``00-Inbox/`` and walk away -- the watcher debounces
rapid events, then triggers the ingestion pipeline. Handles create, modify,
delete and rename (move) events. On startup it also sweeps any files that
were dropped while the watcher was not running (restart safety), so no drop
is ever missed.

Uses ``watchdog`` when available; the ``sweep_once`` path works without it
(e.g. for cron-style invocation).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..models import JobStage
from ..system import KnowledgeSystem
from .pipeline import IngestionPipeline


class InboxWatcher:
    def __init__(self, ks: KnowledgeSystem, on_event=None):
        self.ks = ks
        self.pipeline = IngestionPipeline(ks)
        self.inbox = ks.settings.inbox_path
        self.debounce = ks.settings.ingest.watch_debounce_seconds
        self.on_event = on_event or (lambda outcome: None)
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._marker = ks.settings.ingest.processed_marker

    # ---- shared helpers --------------------------------------------------

    def _is_ingestable(self, path: Path) -> bool:
        if path.is_dir():
            return False
        if self._marker in path.parts:
            return False
        return path.suffix.lower() in self.ks.settings.ingest.supported_extensions

    def sweep_once(self, force: bool = False) -> list:
        """Process everything currently in the inbox. Restart-safe entry."""
        outcomes = self.pipeline.process_inbox(force=force)
        for o in outcomes:
            self.on_event(o)
        return outcomes

    # ---- debounced processing -------------------------------------------

    def _schedule(self, path: str) -> None:
        with self._lock:
            self._pending[path] = time.monotonic()

    def _process_ready(self) -> None:
        now = time.monotonic()
        ready: list[str] = []
        with self._lock:
            for path, ts in list(self._pending.items()):
                if now - ts >= self.debounce:
                    ready.append(path)
                    del self._pending[path]
        for path_str in ready:
            path = Path(path_str)
            if path.exists() and self._is_ingestable(path):
                outcome = self.pipeline.process_file(path)
                # Filesystem events often arrive in pairs (create + modify).
                # Drop any event queued for this path while we were working,
                # otherwise the follow-up sees the now-consumed file as deleted.
                with self._lock:
                    self._pending.pop(path_str, None)
                self.on_event(outcome)
            elif not path.exists():
                if self._was_consumed(path):
                    # The pipeline archives the original and clears it from the
                    # inbox, so a missing *completed* source is our own cleanup,
                    # not a user deletion. De-indexing here would wipe the note
                    # we just created.
                    continue
                outcome = self.pipeline.handle_deleted(path)
                self.on_event(outcome)

    def _was_consumed(self, path: Path) -> bool:
        """True if this source was already ingested successfully."""
        entry = self.ks.state.get_manifest(self.pipeline._rel(path))
        return bool(entry and entry.stage == JobStage.completed)

    # ---- run loops -------------------------------------------------------

    def run(self, poll_interval: float = 1.0) -> None:
        """Blocking watch loop. Uses watchdog if installed, else polling."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        # startup sweep for restart safety
        self.sweep_once()

        observer = self._start_watchdog()
        try:
            while not self._stop.is_set():
                if observer is None:
                    self._poll_scan()
                self._process_ready()
                # Wait on the stop event instead of sleeping blindly, so stop()
                # is honoured immediately rather than up to poll_interval later.
                if self._stop.wait(poll_interval):
                    break
        finally:
            if observer is not None:
                observer.stop()
                observer.join()

    def _poll_scan(self) -> None:
        # Fallback when watchdog is unavailable: scan the inbox.
        for path in self.inbox.rglob("*"):
            if self._is_ingestable(path):
                self._schedule(str(path))

    def _start_watchdog(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return None

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    watcher._schedule(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    watcher._schedule(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    watcher.pipeline.handle_deleted(Path(event.src_path))
                    watcher._schedule(event.dest_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    watcher._schedule(event.src_path)

        observer = Observer()
        observer.schedule(_Handler(), str(self.inbox), recursive=True)
        observer.start()
        return observer

    def stop(self) -> None:
        self._stop.set()
