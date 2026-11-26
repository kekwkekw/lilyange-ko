from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024  # 256 KB
UNITYFS_MAGIC = b"UnityFS"
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_USER_AGENT = "lily_crawler/1.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_stamp_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _sha256_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _load_json_safe(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read state JSON %s: %s", path, exc)
    return None


def _is_cancelled(
    cancel_event: threading.Event,
    cancel_file: Path | None,
) -> bool:
    if cancel_event.is_set():
        return True
    if cancel_file is not None and cancel_file.exists():
        cancel_event.set()
        return True
    return False


def _sleep_with_cancel(
    seconds: float,
    cancel_event: threading.Event,
    cancel_file: Path | None,
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _is_cancelled(cancel_event, cancel_file):
            return True
        remaining = deadline - time.monotonic()
        time.sleep(min(0.2, max(0.0, remaining)))
    return _is_cancelled(cancel_event, cancel_file)


def _extract_header_magic(header_bytes: bytes) -> str:
    if not header_bytes:
        return ""
    if header_bytes.startswith(UNITYFS_MAGIC):
        return "UnityFS"
    sample = header_bytes[:8]
    if all(32 <= b < 127 for b in sample):
        return sample.decode("ascii", errors="replace")
    return sample.hex()


def _raw_bundle_path(work_dir: Path, raw_sha256: str) -> Path:
    return work_dir / "raw" / "bundles" / raw_sha256[:2] / f"{raw_sha256}.bundle"


def _asset_state_path(work_dir: Path, platform: str, key_sha: str) -> Path:
    return (
        work_dir
        / "state"
        / platform
        / "assets"
        / key_sha[:2]
        / f"{key_sha}.json"
    )


def _asset_history_dir(work_dir: Path, platform: str, key_sha: str) -> Path:
    return work_dir / "state" / platform / "assets" / key_sha[:2] / "history"


def _partial_dir(work_dir: Path, key_sha: str) -> Path:
    return work_dir / "partial" / key_sha[:2]


def _encode_request_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    quoted_path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, quoted_path, parts.query, parts.fragment))


def _normalize_bundle_entry(bundle: dict[str, Any]) -> dict[str, Any]:
    internal_id = str(bundle.get("internal_id") or "")
    url = str(bundle.get("url") or bundle.get("resolved_url") or "")
    group_guess = bundle.get("group_guess")
    meta = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
    unity_hash = bundle.get("m_Hash")
    if unity_hash is None:
        unity_hash = bundle.get("unity_hash")
    if unity_hash is None:
        unity_hash = meta.get("m_Hash")
    unity_crc = bundle.get("m_Crc")
    if unity_crc is None:
        unity_crc = bundle.get("unity_crc")
    if unity_crc is None:
        unity_crc = meta.get("m_Crc")
    declared_bytes = bundle.get("m_BundleSize")
    if declared_bytes is None:
        declared_bytes = bundle.get("declared_bytes")
    if declared_bytes is None:
        declared_bytes = meta.get("m_BundleSize")
    if declared_bytes is not None:
        try:
            declared_bytes = int(declared_bytes)
        except (TypeError, ValueError):
            declared_bytes = None
    return {
        "internal_id": internal_id,
        "url": url,
        "group_guess": group_guess,
        "unity_hash": str(unity_hash) if unity_hash is not None else None,
        "unity_crc": unity_crc,
        "declared_bytes": declared_bytes,
    }


def _preserve_history_if_changed(
    work_dir: Path,
    platform: str,
    key_sha: str,
    existing_state: dict[str, Any],
    new_unity_hash: str | None,
    new_url: str,
) -> None:
    old_hash = existing_state.get("unity_hash")
    old_url = existing_state.get("url")
    if old_hash != new_unity_hash or old_url != new_url:
        hist_dir = _asset_history_dir(work_dir, platform, key_sha)
        hist_path = hist_dir / f"{key_sha}_{_utc_stamp_compact()}.json"
        _atomic_write_json(hist_path, existing_state)


def _can_reuse_existing(
    work_dir: Path,
    existing_state: dict[str, Any],
    unity_hash: str | None,
    url: str,
    integrity: bool,
) -> tuple[bool, Path | None]:
    if existing_state.get("status") != "ACQUIRED":
        return False, None
    if existing_state.get("unity_hash") != unity_hash:
        return False, None
    if existing_state.get("url") != url:
        return False, None

    raw_sha256 = existing_state.get("raw_sha256")
    actual_bytes = existing_state.get("actual_bytes")
    if not isinstance(raw_sha256, str) or len(raw_sha256) < 4:
        return False, None
    if not isinstance(actual_bytes, int) or actual_bytes <= 0:
        return False, None

    raw_path = _raw_bundle_path(work_dir, raw_sha256)
    if not raw_path.exists():
        return False, None

    try:
        st = raw_path.stat()
    except OSError:
        return False, None

    if st.st_size != actual_bytes:
        logger.warning(
            "Existing raw bundle size mismatch for %s: expected=%d disk=%d",
            raw_path,
            actual_bytes,
            st.st_size,
        )
        return False, None

    if integrity:
        try:
            computed = _sha256_file(raw_path)
        except OSError as exc:
            logger.warning("Integrity check failed reading %s: %s", raw_path, exc)
            return False, None
        if computed != raw_sha256:
            logger.warning(
                "Integrity SHA-256 mismatch for %s: expected=%s actual=%s",
                raw_path,
                raw_sha256,
                computed,
            )
            return False, None

    return True, raw_path


def _save_partial_failure(
    work_dir: Path,
    key_sha: str,
    attempt: int,
    tmp_path: Path | None,
    meta: dict[str, Any],
) -> None:
    p_dir = _partial_dir(work_dir, key_sha)
    p_dir.mkdir(parents=True, exist_ok=True)
    part_path = p_dir / f"{key_sha}_attempt{attempt}.part"
    meta_path = p_dir / f"{key_sha}_attempt{attempt}.meta.json"

    if tmp_path is not None and tmp_path.exists():
        try:
            os.replace(tmp_path, part_path)
            meta["part_path"] = str(part_path)
        except OSError as exc:
            logger.warning("Failed to preserve partial file %s: %s", part_path, exc)
            meta["part_path"] = None
    else:
        meta["part_path"] = None

    _atomic_write_json(meta_path, meta)


def _download_single_bundle(
    work_dir: Path,
    platform: str,
    bundle: dict[str, Any],
    max_retries: int,
    timeout: int,
    cancel_event: threading.Event,
    cancel_file: Path | None,
    integrity: bool,
) -> tuple[dict[str, Any], bool]:
    norm = _normalize_bundle_entry(bundle)
    internal_id = norm["internal_id"]
    url = norm["url"]
    group_guess = norm["group_guess"]
    unity_hash = norm["unity_hash"]
    unity_crc = norm["unity_crc"]
    declared_bytes = norm["declared_bytes"]

    key_source = internal_id if internal_id else url
    key_sha = _sha256_str(key_source)
    state_path = _asset_state_path(work_dir, platform, key_sha)
    existing_state = _load_json_safe(state_path)

    if existing_state is not None:
        _preserve_history_if_changed(
            work_dir=work_dir,
            platform=platform,
            key_sha=key_sha,
            existing_state=existing_state,
            new_unity_hash=unity_hash,
            new_url=url,
        )
        reusable, raw_path = _can_reuse_existing(
            work_dir=work_dir,
            existing_state=existing_state,
            unity_hash=unity_hash,
            url=url,
            integrity=integrity,
        )
        if reusable and raw_path is not None:
            reused_state = dict(existing_state)
            reused_state["group_guess"] = group_guess
            reused_state["unity_crc"] = unity_crc
            reused_state["declared_bytes"] = declared_bytes
            reused_state["raw_path"] = str(raw_path)
            reused_state["updated_at"] = _utc_now_iso()
            _atomic_write_json(state_path, reused_state)
            return reused_state, True

    base_record: dict[str, Any] = {
        "internal_id": internal_id,
        "url": url,
        "group_guess": group_guess,
        "unity_hash": unity_hash,
        "unity_crc": unity_crc,
        "declared_bytes": declared_bytes,
        "status": "PENDING",
        "http_status": None,
        "last_modified": None,
        "actual_bytes": 0,
        "size_matches_declared": None,
        "header_magic": None,
        "raw_sha256": None,
        "raw_path": None,
        "attempts": 0,
        "error_code": None,
        "error_message": None,
        "updated_at": _utc_now_iso(),
    }

    if _is_cancelled(cancel_event, cancel_file):
        return base_record, False

    if not url:
        base_record["status"] = "FAILED"
        base_record["error_code"] = "MISSING_URL"
        base_record["error_message"] = "Bundle entry has empty URL"
        base_record["updated_at"] = _utc_now_iso()
        _atomic_write_json(state_path, base_record)
        return base_record, False

    p_dir = _partial_dir(work_dir, key_sha)
    p_dir.mkdir(parents=True, exist_ok=True)

    attempts = 0
    last_http_status: int | None = None
    last_modified: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_actual_bytes = 0

    for attempt in range(1, max(1, max_retries) + 1):
        if _is_cancelled(cancel_event, cancel_file):
            base_record.update(
                {
                    "status": "CANCELLED",
                    "http_status": last_http_status,
                    "last_modified": last_modified,
                    "actual_bytes": last_actual_bytes,
                    "attempts": attempts,
                    "error_code": "CANCELLED",
                    "error_message": "Download cancelled before attempt",
                    "updated_at": _utc_now_iso(),
                }
            )
            _atomic_write_json(state_path, base_record)
            return base_record, False

        attempts = attempt
        tmp_path = p_dir / f"{key_sha}_attempt{attempt}.{os.getpid()}.{threading.get_ident()}.tmp"
        bytes_written = 0
        header_buf = bytearray()
        sha = hashlib.sha256()
        http_status: int | None = None
        resp_last_modified: str | None = None
        retryable = False
        cancelled_mid_stream = False

        try:
            req = urllib.request.Request(
                _encode_request_url(url),
                headers={"User-Agent": DEFAULT_USER_AGENT},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                http_status = getattr(resp, "status", None) or resp.getcode()
                resp_last_modified = resp.headers.get("Last-Modified")
                last_http_status = http_status
                last_modified = resp_last_modified

                if http_status != 200:
                    retryable = http_status in RETRYABLE_HTTP_STATUSES
                    raise urllib.error.HTTPError(
                        url=url,
                        code=http_status,
                        msg=f"Unexpected HTTP status {http_status}",
                        hdrs=resp.headers,
                        fp=None,
                    )

                with tmp_path.open("wb") as out_f:
                    while True:
                        if _is_cancelled(cancel_event, cancel_file):
                            cancelled_mid_stream = True
                            break
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        sha.update(chunk)
                        bytes_written += len(chunk)
                        if len(header_buf) < 16:
                            needed = 16 - len(header_buf)
                            header_buf.extend(chunk[:needed])
                    out_f.flush()
                    try:
                        os.fsync(out_f.fileno())
                    except OSError:
                        pass

            last_actual_bytes = bytes_written

            if cancelled_mid_stream:
                _save_partial_failure(
                    work_dir=work_dir,
                    key_sha=key_sha,
                    attempt=attempt,
                    tmp_path=tmp_path,
                    meta={
                        "internal_id": internal_id,
                        "url": url,
                        "attempt": attempt,
                        "http_status": http_status,
                        "bytes_written": bytes_written,
                        "error_code": "CANCELLED",
                        "error_message": "Download cancelled during streaming",
                        "timestamp": _utc_now_iso(),
                    },
                )
                base_record.update(
                    {
                        "status": "CANCELLED",
                        "http_status": http_status,
                        "last_modified": resp_last_modified,
                        "actual_bytes": bytes_written,
                        "attempts": attempts,
                        "error_code": "CANCELLED",
                        "error_message": "Download cancelled during streaming",
                        "updated_at": _utc_now_iso(),
                    }
                )
                _atomic_write_json(state_path, base_record)
                return base_record, False

            if bytes_written <= 0:
                last_error_code = "EMPTY_BODY"
                last_error_message = "Downloaded 0 bytes with HTTP 200"
                retryable = True
                _save_partial_failure(
                    work_dir=work_dir,
                    key_sha=key_sha,
                    attempt=attempt,
                    tmp_path=tmp_path,
                    meta={
                        "internal_id": internal_id,
                        "url": url,
                        "attempt": attempt,
                        "http_status": http_status,
                        "bytes_written": bytes_written,
                        "error_code": last_error_code,
                        "error_message": last_error_message,
                        "timestamp": _utc_now_iso(),
                    },
                )
            else:
                raw_sha256 = sha.hexdigest()
                header_magic = _extract_header_magic(bytes(header_buf))
                if header_magic != "UnityFS":
                    logger.warning(
                        "Non-UnityFS header magic '%s' for %s (%s)",
                        header_magic,
                        internal_id,
                        url,
                    )

                size_matches_declared: bool | None = None
                if declared_bytes is not None and declared_bytes > 0:
                    size_matches_declared = bytes_written == declared_bytes
                    if not size_matches_declared:
                        logger.warning(
                            "Bundle size mismatch for %s: declared=%d actual=%d",
                            internal_id,
                            declared_bytes,
                            bytes_written,
                        )

                raw_path = _raw_bundle_path(work_dir, raw_sha256)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                if raw_path.exists() and raw_path.stat().st_size == bytes_written:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                else:
                    os.replace(tmp_path, raw_path)

                base_record.update(
                    {
                        "status": "ACQUIRED",
                        "http_status": http_status,
                        "last_modified": resp_last_modified,
                        "actual_bytes": bytes_written,
                        "size_matches_declared": size_matches_declared,
                        "header_magic": header_magic,
                        "raw_sha256": raw_sha256,
                        "raw_path": str(raw_path),
                        "attempts": attempts,
                        "error_code": None,
                        "error_message": None,
                        "updated_at": _utc_now_iso(),
                    }
                )
                _atomic_write_json(state_path, base_record)
                return base_record, False

        except urllib.error.HTTPError as exc:
            http_status = exc.code
            last_http_status = http_status
            last_error_code = f"HTTP_{exc.code}"
            last_error_message = str(exc)
            last_actual_bytes = bytes_written
            retryable = exc.code in RETRYABLE_HTTP_STATUSES
            _save_partial_failure(
                work_dir=work_dir,
                key_sha=key_sha,
                attempt=attempt,
                tmp_path=tmp_path,
                meta={
                    "internal_id": internal_id,
                    "url": url,
                    "attempt": attempt,
                    "http_status": http_status,
                    "bytes_written": bytes_written,
                    "error_code": last_error_code,
                    "error_message": last_error_message,
                    "timestamp": _utc_now_iso(),
                },
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error_code = type(exc).__name__
            last_error_message = str(exc)
            last_actual_bytes = bytes_written
            retryable = True
            _save_partial_failure(
                work_dir=work_dir,
                key_sha=key_sha,
                attempt=attempt,
                tmp_path=tmp_path,
                meta={
                    "internal_id": internal_id,
                    "url": url,
                    "attempt": attempt,
                    "http_status": http_status,
                    "bytes_written": bytes_written,
                    "error_code": last_error_code,
                    "error_message": last_error_message,
                    "timestamp": _utc_now_iso(),
                },
            )
        except Exception as exc:
            last_error_code = type(exc).__name__
            last_error_message = str(exc)
            last_actual_bytes = bytes_written
            retryable = False
            _save_partial_failure(
                work_dir=work_dir,
                key_sha=key_sha,
                attempt=attempt,
                tmp_path=tmp_path,
                meta={
                    "internal_id": internal_id,
                    "url": url,
                    "attempt": attempt,
                    "http_status": http_status,
                    "bytes_written": bytes_written,
                    "error_code": last_error_code,
                    "error_message": last_error_message,
                    "timestamp": _utc_now_iso(),
                },
            )

        if not retryable or attempt >= max(1, max_retries):
            break

        backoff = min(30.0, float(2 ** (attempt - 1)))
        if _sleep_with_cancel(backoff, cancel_event, cancel_file):
            base_record.update(
                {
                    "status": "CANCELLED",
                    "http_status": last_http_status,
                    "last_modified": last_modified,
                    "actual_bytes": last_actual_bytes,
                    "attempts": attempts,
                    "error_code": "CANCELLED",
                    "error_message": "Download cancelled during retry backoff",
                    "updated_at": _utc_now_iso(),
                }
            )
            _atomic_write_json(state_path, base_record)
            return base_record, False

    base_record.update(
        {
            "status": "FAILED",
            "http_status": last_http_status,
            "last_modified": last_modified,
            "actual_bytes": last_actual_bytes,
            "attempts": attempts,
            "error_code": last_error_code or "DOWNLOAD_FAILED",
            "error_message": last_error_message or "Download failed after retries",
            "updated_at": _utc_now_iso(),
        }
    )
    _atomic_write_json(state_path, base_record)
    return base_record, False


def download_inventory(
    work_dir: str | Path,
    platform: str,
    bundles: list[dict[str, Any]],
    snapshot_meta: dict[str, Any] | None = None,
    workers: int = 4,
    max_retries: int = 3,
    timeout: int = 60,
    cancel_file: str | Path | None = None,
    integrity: bool = False,
    max_items: int = 0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    work_path = Path(work_dir)
    cancel_path = Path(cancel_file) if cancel_file is not None else None
    state_platform_dir = work_path / "state" / platform
    state_platform_dir.mkdir(parents=True, exist_ok=True)
    progress_path = state_platform_dir / "download_progress.json"
    report_path = state_platform_dir / "download_report.json"

    target_bundles = bundles[:max_items] if max_items and max_items > 0 else list(bundles)
    total = len(target_bundles)
    started_at = _utc_now_iso()
    start_mono = time.monotonic()

    cancel_event = threading.Event()
    prev_sigint = None
    in_main_thread = threading.current_thread() is threading.main_thread()

    if in_main_thread:
        def _handle_sigint(signum: int, frame: Any) -> None:
            logger.warning("SIGINT received; cancelling remaining downloads...")
            cancel_event.set()

        try:
            prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _handle_sigint)
        except ValueError:
            prev_sigint = None

    lock = threading.Lock()
    counts: dict[str, int] = {
        "ACQUIRED": 0,
        "REUSED": 0,
        "FAILED": 0,
        "CANCELLED": 0,
        "PENDING": total,
    }
    bytes_downloaded = 0
    bytes_reused = 0
    completed = 0
    results: list[dict[str, Any]] = []
    last_progress_write = 0.0

    def _build_progress(final: bool = False) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - start_mono)
        return {
            "platform": platform,
            "started_at": started_at,
            "updated_at": _utc_now_iso(),
            "elapsed_seconds": round(elapsed, 3),
            "total": total,
            "completed": completed,
            "acquired": counts["ACQUIRED"],
            "reused": counts["REUSED"],
            "failed": counts["FAILED"],
            "cancelled": counts["CANCELLED"],
            "pending": counts["PENDING"],
            "bytes_downloaded": bytes_downloaded,
            "bytes_reused": bytes_reused,
            "is_cancelled": _is_cancelled(cancel_event, cancel_path),
            "final": final,
        }

    def _flush_progress(force: bool = False) -> None:
        nonlocal last_progress_write
        now = time.monotonic()
        if not force and (now - last_progress_write) < 2.0:
            return
        last_progress_write = now
        prog = _build_progress(final=force)
        _atomic_write_json(progress_path, prog)
        if on_progress is not None:
            try:
                on_progress(prog)
            except Exception as exc:
                logger.warning("on_progress callback error: %s", exc)

    _flush_progress(force=True)

    try:
        effective_workers = max(1, int(workers))
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures: dict[concurrent.futures.Future[tuple[dict[str, Any], bool]], dict[str, Any]] = {}
            for bundle in target_bundles:
                if _is_cancelled(cancel_event, cancel_path):
                    break
                fut = executor.submit(
                    _download_single_bundle,
                    work_path,
                    platform,
                    bundle,
                    max_retries,
                    timeout,
                    cancel_event,
                    cancel_path,
                    integrity,
                )
                futures[fut] = bundle

            for fut in concurrent.futures.as_completed(futures):
                try:
                    record, was_reused = fut.result()
                except Exception as exc:
                    b = _normalize_bundle_entry(futures[fut])
                    record = {
                        "internal_id": b["internal_id"],
                        "url": b["url"],
                        "group_guess": b["group_guess"],
                        "unity_hash": b["unity_hash"],
                        "unity_crc": b["unity_crc"],
                        "declared_bytes": b["declared_bytes"],
                        "status": "FAILED",
                        "http_status": None,
                        "last_modified": None,
                        "actual_bytes": 0,
                        "size_matches_declared": None,
                        "header_magic": None,
                        "raw_sha256": None,
                        "raw_path": None,
                        "attempts": 0,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                        "updated_at": _utc_now_iso(),
                    }
                    was_reused = False

                with lock:
                    results.append(record)
                    status = record.get("status", "FAILED")
                    if status == "PENDING":
                        pass
                    else:
                        completed += 1
                        counts["PENDING"] = max(0, counts["PENDING"] - 1)
                        if status == "ACQUIRED":
                            counts["ACQUIRED"] += 1
                            if was_reused:
                                counts["REUSED"] += 1
                                bytes_reused += int(record.get("actual_bytes") or 0)
                            else:
                                bytes_downloaded += int(record.get("actual_bytes") or 0)
                        elif status == "FAILED":
                            counts["FAILED"] += 1
                        elif status == "CANCELLED":
                            counts["CANCELLED"] += 1
                    _flush_progress(force=False)
    finally:
        if in_main_thread and prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, prev_sigint)
            except ValueError:
                pass

    cancelled_flag = _is_cancelled(cancel_event, cancel_path)
    overall_status = "CANCELLED" if cancelled_flag else ("COMPLETED" if counts["FAILED"] == 0 else "COMPLETED_WITH_ERRORS")

    _flush_progress(force=True)

    report: dict[str, Any] = {
        "platform": platform,
        "status": overall_status,
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "elapsed_seconds": round(max(0.0, time.monotonic() - start_mono), 3),
        "snapshot_meta": snapshot_meta or {},
        "config": {
            "workers": workers,
            "max_retries": max_retries,
            "timeout": timeout,
            "integrity": integrity,
            "max_items": max_items,
        },
        "summary": {
            "total": total,
            "completed": completed,
            "acquired": counts["ACQUIRED"],
            "reused": counts["REUSED"],
            "downloaded": counts["ACQUIRED"] - counts["REUSED"],
            "failed": counts["FAILED"],
            "cancelled": counts["CANCELLED"],
            "pending": counts["PENDING"],
            "bytes_downloaded": bytes_downloaded,
            "bytes_reused": bytes_reused,
        },
        "failed_items": [r for r in results if r.get("status") == "FAILED"],
        "size_mismatches": [
            {
                "internal_id": r.get("internal_id"),
                "url": r.get("url"),
                "declared_bytes": r.get("declared_bytes"),
                "actual_bytes": r.get("actual_bytes"),
                "raw_sha256": r.get("raw_sha256"),
            }
            for r in results
            if r.get("status") == "ACQUIRED" and r.get("size_matches_declared") is False
        ],
    }

    _atomic_write_json(report_path, report)
    return report