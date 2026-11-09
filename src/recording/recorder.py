"""Real-time market-data recorder for backtests and replay.

The bot already holds the two live WebSocket streams. This recorder taps them
instead of opening more connections, and appends newline-delimited JSON rotated
hourly. Names and payload shapes match ``research/scripts/record_live.py`` so
``research/scripts/replay_book.py`` can read the output directly.

Streams written under ``<recording_dir>/``:

  ``rtds_YYYYMMDD_HH.jsonl``           raw RTDS messages (Chainlink + TWAP)
  ``book_{ASSET}_YYYYMMDD_HH.jsonl``   1 Hz top-N snapshot + per-token updated_ts
  ``trades_{ASSET}_YYYYMMDD_HH.jsonl`` every ``last_trade_price`` event
  ``gamma_YYYYMMDD_HH.jsonl``          window metadata (p0, final, tokens)
  ``clob_YYYYMMDD_HH.jsonl``           optional raw CLOB messages

The feed callbacks must never block: producers use ``put_nowait`` and drop with
a counter if the writer cannot keep up.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.infra.logging import get_logger

log = get_logger(__name__)

_HOUR_RE = re.compile(r"_(\d{8}_\d{2})\.jsonl(?:\.gz)?$")
_HANDLE_REFRESH_WRITES = 1000


def _hour_stamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y%m%d_%H")


def _hour_from_name(name: str) -> Optional[str]:
    match = _HOUR_RE.search(name)
    return match.group(1) if match else None


class Recorder:
    """Tap the live feeds and persist their events as hourly JSONL."""

    def __init__(
        self,
        path: str | Path,
        assets: list[str],
        *,
        books: Any = None,
        windows: Any = None,
        book_hz: float = 1.0,
        book_levels: int = 5,
        raw_clob: bool = False,
        compress: bool = True,
        retention_days: int = 14,
        queue_size: int = 50_000,
    ) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.assets = [str(a).upper() for a in assets]
        self.books = books
        self.windows = windows
        self.book_hz = max(0.1, float(book_hz))
        self.book_levels = max(1, int(book_levels))
        self.raw_clob = bool(raw_clob)
        self.compress = bool(compress)
        self.retention_days = max(0, int(retention_days))
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(1000, int(queue_size)))

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._handles: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._token_asset: dict[str, str] = {}
        self._window_sig: dict[str, tuple] = {}
        self._writes_since_refresh = 0

        self.dropped = 0
        self.written = 0
        self.bytes = 0
        self.errors = 0
        self.last_write_ts = 0.0
        self.started_at = 0.0
        self._last_error_log = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.started_at = time.time()
        self._tasks = [
            asyncio.create_task(self._writer_loop(), name="recorder-writer"),
        ]
        if self.books is not None and self.windows is not None:
            self._tasks.append(
                asyncio.create_task(self._sampler_loop(), name="recorder-sampler")
            )
        if self.compress or self.retention_days:
            self._tasks.append(
                asyncio.create_task(self._maintenance_loop(), name="recorder-maintenance")
            )
        log.info(
            "recorder.started",
            path=str(self.path),
            assets=self.assets,
            book_hz=self.book_hz,
            book_levels=self.book_levels,
            raw_clob=self.raw_clob,
            compress=self.compress,
            retention_days=self.retention_days,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        # Flush whatever the writer did not get to before cancellation.
        while True:
            try:
                rec = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._write(rec)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.warning("recorder.flush_failed", error=repr(exc))
            finally:
                self.queue.task_done()
        self._close_all()
        log.info(
            "recorder.stopped",
            written=self.written,
            dropped=self.dropped,
            errors=self.errors,
            bytes=self.bytes,
        )

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self._running,
            "dir": str(self.path),
            "queued": self.queue.qsize(),
            "dropped": self.dropped,
            "written": self.written,
            "errors": self.errors,
            "bytes": self.bytes,
            "book_hz": self.book_hz,
            "book_levels": self.book_levels,
            "raw_clob": self.raw_clob,
            "compress": self.compress,
            "retention_days": self.retention_days,
            "last_write_ts": self.last_write_ts,
            "uptime_s": (time.time() - self.started_at) if self.started_at else 0.0,
        }

    def _log_error(self, event: str, **fields: Any) -> None:
        """Avoid flooding the log when the disk is full or a path is gone."""
        now = time.time()
        if now - self._last_error_log >= 60.0:
            self._last_error_log = now
            log.warning(event, **fields)

    # ── feed taps ────────────────────────────────────────────────────────
    def record_rtds(self, asset: str, recv_ts: float, data: Any) -> None:
        """Called for every parsed RTDS message (chainlink + TWAP)."""
        self._put("rtds", data, recv_ts)

    def record_raw_clob(self, recv_ts: float, data: Any) -> None:
        """Called for every parsed CLOB message when raw recording is on."""
        if self.raw_clob:
            self._put("clob", data, recv_ts)

    def record_trade(self, event: dict[str, Any]) -> None:
        """Called for every normalized CLOB trade print."""
        token = str(event.get("token_id") or "")
        asset = self._token_asset.get(token, "all")
        recv_ts = float(event.get("ts") or time.time())
        exchange_ts = event.get("exchange_ts")
        payload = {
            "asset_id": token,
            "price": event.get("price"),
            "size": event.get("size"),
            "side": event.get("side") or "",
            "timestamp": int(float(exchange_ts or recv_ts) * 1000),
            "recv_ts": recv_ts,
        }
        self._put(f"trades_{asset}", payload, recv_ts)

    def record_window(self, market: Any) -> None:
        """Called when a window is discovered and when late metadata changes."""
        payload = self._window_payload(market)
        self._remember_tokens(market)
        self._window_sig[str(market.asset).upper()] = self._signature(market)
        self._put("gamma", payload, payload["recv_ts"])

    # ── producers ────────────────────────────────────────────────────────
    def _put(self, source: str, payload: Any, recv_ts: float | None = None) -> None:
        if not self._running:
            return
        rec = {
            "recv_ts": float(recv_ts if recv_ts is not None else time.time()),
            "source": source,
            "payload": payload,
        }
        try:
            self.queue.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1

    # ── sampler ──────────────────────────────────────────────────────────
    async def _sampler_loop(self) -> None:
        interval = 1.0 / self.book_hz
        while self._running:
            started = time.time()
            try:
                self._sample_windows()
                self._sample_books()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self._log_error("recorder.sample_failed", error=repr(exc))
            await asyncio.sleep(max(0.02, interval - (time.time() - started)))

    def _sample_windows(self) -> None:
        for asset in self.assets:
            market = self.windows.current_window(asset)
            if market is None:
                continue
            self._remember_tokens(market)
            sig = self._signature(market)
            if self._window_sig.get(asset) == sig:
                continue
            self._window_sig[asset] = sig
            self._put("gamma", self._window_payload(market), time.time())

    def _sample_books(self) -> None:
        now = time.time()
        # The CLOB feed only subscribes to enabled coins (plus coins with an
        # open position), so do not write empty snapshots for disabled ones.
        active = getattr(self.books, "active_assets", None) if self.books is not None else None
        for asset in self.assets:
            if active is not None and asset not in active:
                continue
            market = self.windows.current_window(asset)
            if market is None:
                continue
            books: dict[str, dict[str, list]] = {}
            for token in (market.up_token, market.down_token):
                if not token:
                    continue
                book = self.books.book(token) if self.books is not None else None
                if book is None:
                    books[token] = {"bids": [], "asks": [], "updated_ts": None}
                    continue
                books[token] = {
                    "bids": [
                        [price, size]
                        for price, size in book.bids_descending()[: self.book_levels]
                    ],
                    "asks": [
                        [price, size]
                        for price, size in book.asks_ascending()[: self.book_levels]
                    ],
                    # Last CLOB update, not the sampler timestamp. Replay
                    # uses this to reproduce the live stale-book gate.
                    "updated_ts": book.updated_ts,
                }
            self._put(
                f"book_{asset}",
                {"ts": now, "slug": market.slug, "books": books},
                now,
            )

    # ── writer ───────────────────────────────────────────────────────────
    async def _writer_loop(self) -> None:
        while True:
            rec = await self.queue.get()
            try:
                self._write(rec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self._log_error("recorder.write_failed", error=repr(exc))
            finally:
                self.queue.task_done()

    def _write(self, rec: dict[str, Any]) -> None:
        recv_ts = float(rec.get("recv_ts") or time.time())
        hour = _hour_stamp(recv_ts)
        source = str(rec.get("source") or "unknown")
        name = f"{source}_{hour}.jsonl"
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            handle = self._handles.get(name)
            if handle is None:
                handle = self._open_append(self.path / name)
                self._handles[name] = handle
            try:
                handle.write(line)
            except Exception:
                # Reopen on the next record; a transient FS error should not
                # poison the handle for the rest of the hour.
                self._close_handle_locked(name)
                raise
            self.written += 1
            self.bytes += len(line)
            self.last_write_ts = recv_ts
            self._writes_since_refresh += 1
            if self._writes_since_refresh >= _HANDLE_REFRESH_WRITES:
                self._writes_since_refresh = 0
                self._close_stale_locked(hour)

    @staticmethod
    def _open_append(path: Path):
        """Open for append, healing a partial line left by a hard crash."""
        needs_newline = False
        try:
            if path.exists() and path.stat().st_size:
                with path.open("rb") as probe:
                    probe.seek(-1, os.SEEK_END)
                    needs_newline = probe.read(1) != b"\n"
        except OSError:
            pass
        handle = open(path, "a", encoding="utf-8", buffering=1)
        if needs_newline:
            handle.write("\n")
        return handle

    def _close_stale_locked(self, current_hour: str) -> None:
        suffix = f"_{current_hour}.jsonl"
        for name in [n for n in self._handles if not n.endswith(suffix)]:
            self._close_handle_locked(name)

    def _close_handle(self, name: str) -> None:
        with self._lock:
            self._close_handle_locked(name)

    def _close_handle_locked(self, name: str) -> None:
        handle = self._handles.pop(name, None)
        if handle is None:
            return
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass

    def _close_all(self) -> None:
        with self._lock:
            for name in list(self._handles):
                self._close_handle_locked(name)

    # ── maintenance ──────────────────────────────────────────────────────
    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._maintain)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.warning("recorder.maintenance_failed", error=repr(exc))
            await asyncio.sleep(3600)

    def _maintain(self) -> None:
        """Compress completed hours and delete files past retention."""
        current_hour = _hour_stamp(time.time())
        for path in sorted(self.path.glob("*.jsonl")):
            hour = _hour_from_name(path.name)
            if hour is None or hour >= current_hour:
                continue
            self._close_handle(path.name)
            if self.compress:
                self._gzip_file(path)
        if self.retention_days:
            cutoff = time.time() - self.retention_days * 86400.0
            for path in list(self.path.glob("*.jsonl")) + list(
                self.path.glob("*.jsonl.gz")
            ):
                hour = _hour_from_name(path.name)
                if hour is not None and hour >= current_hour:
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except FileNotFoundError:
                    continue

    def _gzip_file(self, path: Path) -> None:
        target = path.with_name(path.name + ".gz")
        if target.exists():
            # A previous pass or an old process already produced this hour.
            # Appending a new gzip member keeps both halves readable.
            with path.open("rb") as src, gzip.open(target, "ab") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
            return
        tmp = path.with_name(path.name + ".gz.tmp")
        try:
            with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            path.unlink()
        except Exception:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    # ── window helpers ───────────────────────────────────────────────────
    def _remember_tokens(self, market: Any) -> None:
        asset = str(market.asset).upper()
        for token in (market.up_token, market.down_token):
            if token:
                self._token_asset[str(token)] = asset

    @staticmethod
    def _signature(market: Any) -> tuple:
        return (
            int(market.start_ts),
            market.price_to_beat,
            market.final_price,
            market.outcome,
            bool(market.closed),
        )

    @staticmethod
    def _window_payload(market: Any) -> dict[str, Any]:
        return {
            "slug": market.slug,
            "start_ts": int(market.start_ts),
            "end_ts": int(market.end_ts),
            "condition_id": market.condition_id,
            "tokens": [market.up_token, market.down_token],
            "price_to_beat": market.price_to_beat,
            "final_price": market.final_price,
            "outcome": market.outcome,
            "volume": market.volume,
            "closed": bool(market.closed),
            "recv_ts": time.time(),
        }
