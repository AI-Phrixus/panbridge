from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from app.config import get_settings

log = logging.getLogger("panbridge.download")

ProgressCB = Callable[[int, int], Awaitable[None]]  # done, total
RequestRefreshCB = Callable[[], Awaitable[tuple[str, dict[str, str]]]]

# Network stalls (Baidu/CDN) must not hang forever. read = silence between socket data.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=90.0, write=60.0, pool=30.0)
_STALL_SECONDS = 120.0
_REQUEST_REFRESH_SECONDS = 20 * 60.0
_SLICE_MIN = 8 * 1024 * 1024
_SLICE_MAX = 64 * 1024 * 1024
_MAX_CONNECTIONS = 8


class RangeNotSupportedError(RuntimeError):
    """The server cannot safely satisfy independent byte-range requests."""


class DownloadRequestRejected(RuntimeError):
    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"download request rejected: HTTP {status_code} {body[:120]}")


def _recover_legacy_ranges(
    metadata: dict, size: int
) -> tuple[list[tuple[int, int]], set[str]] | None:
    """Recover v1 slice indexes only when their byte boundaries are unambiguous.

    v0.3.x did not store the connection count. For a tail larger than the
    maximum possible connection count times 64 MiB, every supported connection
    count nevertheless used identical capped 64 MiB slices.
    """
    if int(metadata.get("version") or 1) != 1:
        return None
    prefix = max(0, min(size, int(metadata.get("prefix") or 0)))
    remain = size - prefix
    if remain <= _MAX_CONNECTIONS * _SLICE_MAX:
        return None
    ranges: list[tuple[int, int]] = []
    position = prefix
    while position < size:
        end = min(size - 1, position + _SLICE_MAX - 1)
        ranges.append((position, end))
        position = end + 1
    try:
        indexes = {int(value) for value in (metadata.get("done") or [])}
    except (TypeError, ValueError):
        return None
    if any(index < 0 or index >= len(ranges) for index in indexes):
        return None
    done = {f"{ranges[index][0]}-{ranges[index][1]}" for index in indexes}
    return ranges, done


def _metadata_state(
    metadata: dict, expected_size: int = 0
) -> tuple[int, int, list[tuple[int, int]], set[str]] | None:
    """Return (size, prefix, ranges, done) for valid v1/v2 resume metadata."""
    size = int(metadata.get("size") or 0)
    if size <= 0 or (expected_size > 0 and size != expected_size):
        return None
    prefix = max(0, min(size, int(metadata.get("prefix") or 0)))
    if int(metadata.get("version") or 0) < 2:
        recovered = _recover_legacy_ranges(metadata, size)
        if not recovered:
            return size, prefix, [], set()
        ranges, done = recovered
        return size, prefix, ranges, done
    try:
        ranges = [(int(item[0]), int(item[1])) for item in (metadata.get("ranges") or [])]
    except (TypeError, ValueError, IndexError):
        return None
    cursor = prefix
    for start, end in ranges:
        if start != cursor or end < start or end >= size:
            return None
        cursor = end + 1
    if ranges and cursor != size:
        return None
    valid_keys = {f"{start}-{end}" for start, end in ranges}
    done = {str(key) for key in (metadata.get("done") or []) if str(key) in valid_keys}
    return size, prefix, ranges, done


def _verified_contiguous_prefix(metadata: dict, expected_size: int = 0) -> int:
    state = _metadata_state(metadata, expected_size)
    if not state:
        return 0
    _size, prefix, ranges, done = state
    cursor = prefix
    for start, end in ranges:
        key = f"{start}-{end}"
        if start != cursor or key not in done:
            break
        cursor = end + 1
    return cursor


def _dl_timeout() -> httpx.Timeout:
    return _DEFAULT_TIMEOUT


def _fsync_file(path: Path) -> None:
    """Flush file contents before resume metadata claims those bytes exist."""
    with open(path, "r+b") as file_obj:
        file_obj.flush()
        os.fsync(file_obj.fileno())


def _fsync_directory(path: Path) -> None:
    """Best-effort directory flush for atomic metadata replacement/deletion."""
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # Some filesystems/platforms do not permit fsync on directories.
        pass
    finally:
        os.close(directory_fd)


def downloaded_bytes_on_disk(part: Path, expected_size: int = 0) -> int:
    """Return verified downloaded bytes without trusting a sparse file's st_size."""
    if not part.exists():
        return 0
    meta_path = Path(str(part) + ".ranges.json")
    if not meta_path.exists():
        value = part.stat().st_size
        return min(value, expected_size) if expected_size > 0 else value
    try:
        raw = json.loads(meta_path.read_text())
        state = _metadata_state(raw, expected_size)
        if not state:
            return 0
        size, prefix, stored_ranges, done = state
        ranges = {f"{start}-{end}": (start, end) for start, end in stored_ranges}
        completed = prefix + sum(
            end - start + 1
            for key, (start, end) in ranges.items()
            if key in done and start >= prefix and start <= end < size
        )
        return min(size, completed)
    except Exception:
        return 0


def _prepare_single_stream_resume(part: Path, expected_size: int = 0) -> int:
    """Collapse sparse parallel state into a verified contiguous prefix.

    A parallel ``.part`` is normally pre-sized to the final length.  Its
    ``st_size`` must therefore never be handed directly to the single-stream
    downloader, including when the initial range probe says that Range is not
    available.  Keep only the ranges verified continuously from byte zero,
    then remove the parallel metadata.
    """
    meta_path = Path(str(part) + ".ranges.json")
    if not meta_path.exists():
        return part.stat().st_size if part.exists() else 0

    prefix = 0
    try:
        metadata = json.loads(meta_path.read_text())
        prefix = _verified_contiguous_prefix(metadata, expected_size)
    except Exception:
        prefix = 0

    if not part.exists() or part.stat().st_size < prefix:
        # truncate() would otherwise extend a shortened file with NUL bytes and
        # turn a stale sidecar claim into a corrupt "verified" prefix.
        prefix = 0
    if part.exists():
        try:
            with open(part, "r+b") as file_obj:
                file_obj.truncate(prefix)
            _fsync_file(part)
        except OSError as error:
            # If the sparse file cannot be made safe, do not let the following
            # single-stream path mistake its apparent full size for success.
            try:
                part.write_bytes(b"")
                _fsync_file(part)
                prefix = 0
            except OSError as reset_error:
                raise RuntimeError("cannot make parallel resume data safe") from reset_error
    try:
        meta_path.unlink(missing_ok=True)
        _fsync_directory(meta_path.parent)
    except OSError:
        # Leaving stale metadata is safer than treating a sparse st_size as a
        # prefix; fail rather than silently upload a file with holes.
        raise RuntimeError("cannot clear parallel download resume metadata")
    return prefix


async def _probe_size(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bool]:
    """Return (size, supports_range). size 0 if unknown."""
    head_size = 0
    try:
        async with client.stream("HEAD", url, headers=headers) as r:
            if r.status_code < 400:
                head_size = int(r.headers.get("content-length") or 0)
                accept = "bytes" in (r.headers.get("accept-ranges") or "").lower()
                if head_size and accept:
                    return head_size, True
    except Exception:
        pass
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        # Stream only the response headers. A server that ignores Range may
        # otherwise make this tiny probe buffer the entire multi-GB file.
        async with client.stream("GET", url, headers=h) as r:
            if r.status_code == 206:
                cr = r.headers.get("content-range") or ""
                match = re.match(r"bytes\s+0-0/(\d+)$", cr, re.IGNORECASE)
                if match:
                    return int(match.group(1)), True
                return head_size, False
            if r.status_code == 200:
                return head_size or int(r.headers.get("content-length") or 0), False
    except Exception:
        pass
    return head_size, False


async def _refresh_request(
    current_url: str,
    current_headers: dict[str, str],
    url_refresh_cb: Callable[[], Awaitable[str]] | None,
    request_refresh_cb: RequestRefreshCB | None,
) -> tuple[str, dict[str, str]]:
    if request_refresh_cb:
        new_url, new_headers = await request_refresh_cb()
        return new_url, dict(new_headers)
    if url_refresh_cb:
        return await url_refresh_cb(), dict(current_headers)
    return current_url, dict(current_headers)


async def resumable_download(
    url: str,
    dest: Path,
    headers: dict[str, str] | None = None,
    expected_size: int = 0,
    progress_cb: ProgressCB | None = None,
    max_retries: int = 40,
    connections: int | None = None,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
    request_refresh_cb: RequestRefreshCB | None = None,
) -> Path:
    """Download with resume. Multi-connection in-place Range when beneficial.

    Multi-conn writes into a single ``.part`` (no 2× disk for segment assemble),
    so large Baidu files on ~50GB VPS stay feasible.
    """
    settings = get_settings()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest if str(dest).endswith(".part") else Path(str(dest) + ".part")
    if str(dest).endswith(".part"):
        part = dest

    headers = dict(headers or {})
    conns = connections if connections is not None else settings.download_connections
    conns = max(1, min(conns, 8))

    async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
        size, can_range = await _probe_size(client, url, headers)
        if expected_size and not size:
            size = expected_size
        if expected_size and size:
            size = expected_size

    # Multi needs known size + range.  A prior parallel attempt may have left a
    # sparse file whose apparent size already equals the final size, so collapse
    # it before *any* direct single-stream path.
    if conns <= 1 or not can_range or not size or size < 8 * 1024 * 1024:
        _prepare_single_stream_resume(part, size or expected_size)
        return await _single_stream_download(
            url,
            part,
            headers,
            size or expected_size,
            progress_cb,
            max_retries,
            url_refresh_cb,
            request_refresh_cb,
        )

    contiguous = part.stat().st_size if part.exists() else 0
    # only treat as contiguous prefix when no parallel meta (single-stream .part)
    meta_path = Path(str(part) + ".ranges.json")
    if meta_path.exists():
        # Previous parallel attempt: st_size may describe a sparse tail. Only
        # the explicitly recorded prefix is safe if we must fall back to one stream.
        contiguous = 0
        try:
            metadata = json.loads(meta_path.read_text())
            if int(metadata.get("size") or 0) == size:
                contiguous = max(0, min(size, int(metadata.get("prefix") or 0)))
        except Exception:
            contiguous = 0
    try:
        return await _parallel_range_download(
            url,
            part,
            headers,
            size,
            progress_cb,
            max_retries,
            conns,
            url_refresh_cb=url_refresh_cb,
            request_refresh_cb=request_refresh_cb,
            contiguous_prefix=part.stat().st_size if part.exists() and not meta_path.exists() else 0,
        )
    except RangeNotSupportedError as e:
        log.warning("parallel range unsupported (%s); fallback single stream", e)
        # Absorb completed ranges at the head before discarding sparse tails.
        # This preserves verified work while still giving single-stream resume a
        # safe contiguous prefix.
        _prepare_single_stream_resume(part, size or expected_size)
        return await _single_stream_download(
            url,
            part,
            headers,
            size or expected_size,
            progress_cb,
            max_retries,
            url_refresh_cb,
            request_refresh_cb,
        )


async def _single_stream_download(
    url: str,
    part: Path,
    headers: dict[str, str],
    expected_size: int,
    progress_cb: ProgressCB | None,
    max_retries: int,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
    request_refresh_cb: RequestRefreshCB | None = None,
) -> Path:
    settings = get_settings()
    attempt = 0
    current_url = url
    current_headers = dict(headers)
    rejected = 0
    while attempt < max_retries:
        attempt += 1
        existing = part.stat().st_size if part.exists() else 0
        if expected_size and existing > expected_size > 0:
            # An oversized partial is not a valid prefix and must not be treated
            # as success. Start clean rather than upload silently corrupted data.
            part.write_bytes(b"")
            existing = 0
        if expected_size and existing == expected_size > 0:
            await asyncio.to_thread(_fsync_file, part)
            if progress_cb:
                await progress_cb(existing, expected_size)
            return part

        if attempt > 1 and (request_refresh_cb or url_refresh_cb):
            try:
                current_url, current_headers = await _refresh_request(
                    current_url,
                    current_headers,
                    url_refresh_cb,
                    request_refresh_cb,
                )
                log.info("refreshed download url on attempt %s (have %s bytes)", attempt, existing)
            except Exception as e:
                # A refresh API can tell us definitively that the account login
                # expired. Do not mask that result with dozens of CDN retries.
                log.warning("download request refresh failed: %s", e)
                raise

        req_headers = dict(current_headers)
        if existing > 0:
            req_headers["Range"] = f"bytes={existing}-"

        try:
            async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
                async with client.stream("GET", current_url, headers=req_headers) as resp:
                    if resp.status_code in (401, 403, 404, 412, 416):
                        try:
                            err_body = (await resp.aread())[:400].decode("utf-8", "ignore")
                        except Exception:
                            err_body = ""
                        raise DownloadRequestRejected(resp.status_code, err_body)
                    if existing > 0 and resp.status_code == 200:
                        # A fresh signed URL can briefly land on a node that
                        # ignores Range. Refresh before sacrificing the prefix.
                        if (request_refresh_cb or url_refresh_cb) and attempt <= 2:
                            raise DownloadRequestRejected(200, "server ignored Range")
                        existing = 0
                        part.write_bytes(b"")
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"download failed HTTP {resp.status_code}")

                    if existing > 0 and resp.status_code == 206:
                        content_range = resp.headers.get("content-range") or ""
                        match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)$", content_range, re.IGNORECASE)
                        if not match or int(match.group(1)) != existing:
                            raise RuntimeError(
                                f"resume range mismatch: requested {existing}, got {content_range or 'none'}"
                            )

                    total = expected_size
                    cr = resp.headers.get("content-range")
                    if cr and "/" in cr:
                        try:
                            total = int(cr.rsplit("/", 1)[-1])
                        except ValueError:
                            pass
                    elif resp.headers.get("content-length") and resp.status_code == 200:
                        total = int(resp.headers["content-length"])
                    elif resp.headers.get("content-length") and resp.status_code == 206:
                        total = existing + int(resp.headers["content-length"])

                    mode = "ab" if existing > 0 and resp.status_code == 206 else "wb"
                    if mode == "wb":
                        existing = 0
                    downloaded = existing
                    last_cb = time.monotonic()
                    last_progress_t = time.monotonic()
                    last_progress_bytes = downloaded
                    with open(part, mode) as f:
                        last_flush = time.monotonic()
                        async for chunk in resp.aiter_bytes(settings.download_chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if now - last_flush >= 5.0:
                                f.flush()
                                last_flush = now
                            if downloaded > last_progress_bytes:
                                last_progress_t = now
                                last_progress_bytes = downloaded
                            if progress_cb and (
                                now - last_cb >= 0.5 or downloaded >= (total or downloaded)
                            ):
                                last_cb = now
                                await progress_cb(downloaded, total or expected_size or downloaded)
                            if now - last_progress_t > _STALL_SECONDS:
                                f.flush()
                                raise TimeoutError(
                                    f"download stalled for {_STALL_SECONDS:.0f}s at {downloaded} bytes"
                                )

            final_size = part.stat().st_size
            if expected_size and final_size < expected_size:
                log.warning("incomplete download %s/%s, retry %s", final_size, expected_size, attempt)
                continue
            if not expected_size and final_size <= 0:
                continue
            if expected_size and final_size > expected_size:
                part.write_bytes(b"")
                raise RuntimeError(
                    f"download response exceeded expected size: {final_size}/{expected_size}"
                )
            await asyncio.to_thread(_fsync_file, part)
            return part
        except DownloadRequestRejected as e:
            rejected += 1
            low = e.body.lower()
            if request_refresh_cb or url_refresh_cb:
                if rejected <= 3 and attempt < max_retries:
                    await asyncio.sleep(min(3, 0.5 * rejected))
                    continue
            if (
                e.status_code == 412
                or "auth expired" in low
                or "require login" in low
                or "auth not found" in low
                or "requestdeniedbycallback" in low
            ):
                raise RuntimeError(
                    "夸克登入已失效（CDN require login），請到設定頁重新掃碼；已下載進度會保留"
                ) from e
            if attempt >= max_retries:
                raise
            await asyncio.sleep(min(20, 1.2 * attempt))
            continue
        except RuntimeError as e:
            msg = str(e).lower()
            if "登入已失效" in str(e) or "登录已失效" in str(e) or "require login" in msg or "auth expired" in msg:
                raise
            if any(x in msg for x in ("expired", "forbidden", "403", "401", "404", "412", "range mismatch")):
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(min(20, 1.2 * attempt))
                continue
            raise
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError) as e:
            log.warning(
                "download network issue attempt %s/%s at %s bytes: %s",
                attempt,
                max_retries,
                part.stat().st_size if part.exists() else 0,
                e,
            )
            if attempt >= max_retries:
                raise RuntimeError(f"download stalled after {max_retries} retries: {e}") from e
            await asyncio.sleep(min(30, 1.5 * attempt))
        except Exception as e:
            log.warning("download error attempt %s: %s", attempt, e)
            if attempt >= max_retries:
                raise
            await asyncio.sleep(min(30, 1.5 * attempt))
    raise RuntimeError("download exceeded max retries")


async def _parallel_range_download(
    url: str,
    part: Path,
    headers: dict[str, str],
    size: int,
    progress_cb: ProgressCB | None,
    max_retries: int,
    connections: int,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
    request_refresh_cb: RequestRefreshCB | None = None,
    contiguous_prefix: int = 0,
) -> Path:
    """Multi-connection Range fill into one file via seek+write (no 2× disk).

    Resume: contiguous prefix from prior single-stream is kept; remaining tail is
    split across connections. Finished slices stored in ``.ranges.json``.
    """
    settings = get_settings()
    meta_path = Path(str(part) + ".ranges.json")

    existing = max(0, int(contiguous_prefix))
    ranges: list[tuple[int, int]] | None = None
    done_set: set[str] = set()
    raw: dict = {}

    if meta_path.exists():
        # A parallel file can be sparse, so stat().st_size is never a safe
        # contiguous-resume offset while metadata exists.
        try:
            raw = json.loads(meta_path.read_text())
        except Exception:
            raw = {}
        if int(raw.get("size") or 0) == size:
            existing = max(0, min(size, int(raw.get("prefix") or 0)))
            state = _metadata_state(raw, size)
            if state:
                _state_size, existing, parsed, recovered_done = state
                if parsed:
                    ranges = parsed
                    done_set = recovered_done
            # A sidecar is only evidence about bytes in its matching part file.
            # If that file was removed or truncated, trusting prefix/done would
            # create zero-filled holes and could mark a corrupt file complete.
            required_bytes = existing
            if ranges:
                completed_ends = [
                    end + 1
                    for start, end in ranges
                    if f"{start}-{end}" in done_set
                ]
                if completed_ends:
                    required_bytes = max(required_bytes, max(completed_ends))
            if not part.exists() or part.stat().st_size < required_bytes:
                existing = 0
                ranges = None
                done_set.clear()
                raw = {}
                if part.exists():
                    with open(part, "r+b") as file_obj:
                        file_obj.truncate(0)
            if ranges is None and part.exists():
                # v1 metadata stored only slice indexes. Connection-count changes
                # make those indexes ambiguous, so preserve the known prefix only.
                with open(part, "r+b") as file_obj:
                    file_obj.truncate(existing)
        else:
            existing = 0
            if part.exists():
                with open(part, "r+b") as file_obj:
                    file_obj.truncate(0)
    elif part.exists():
        disk_size = part.stat().st_size
        if disk_size <= size:
            existing = disk_size
        else:
            part.write_bytes(b"")
            existing = 0

    if existing >= size > 0 and not meta_path.exists():
        if progress_cb:
            await progress_cb(size, size)
        return part

    if not part.exists():
        part.touch()

    if ranges is None:
        remain = size - existing
        if remain <= 0:
            if progress_cb:
                await progress_cb(size, size)
            return part
        n = max(1, min(connections, _MAX_CONNECTIONS))
        slice_size = max(_SLICE_MIN, min(_SLICE_MAX, (remain + n - 1) // n))
        ranges = []
        position = existing
        while position < size:
            end = min(size - 1, position + slice_size - 1)
            ranges.append((position, end))
            position = end + 1

    range_by_key = {f"{start}-{end}": (start, end) for start, end in ranges}
    done_set.intersection_update(range_by_key)

    def save_meta() -> None:
        payload = {
            "version": 2,
            "size": size,
            "prefix": existing,
            "ranges": [[start, end] for start, end in ranges or []],
            "done": sorted(done_set, key=lambda key: range_by_key[key][0]),
        }
        tmp = Path(str(meta_path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(payload, separators=(",", ":")))
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(tmp, meta_path)
        _fsync_directory(meta_path.parent)

    # Write valid metadata before the first sparse seek. A crash before the
    # first completed slice can then never turn stat().st_size into a fake prefix.
    save_meta()

    file_lock = asyncio.Lock()
    meta_lock = asyncio.Lock()
    request_lock = asyncio.Lock()
    last_cb = {"t": 0.0}
    inflight: dict[str, int] = {}
    request_state: dict[str, object] = {
        "url": url,
        "headers": dict(headers),
        "refreshed_at": time.monotonic(),
        "generation": 0,
    }

    async def current_request(
        *, proactive: bool = False, rejected_generation: int | None = None
    ) -> tuple[str, dict[str, str], int]:
        async with request_lock:
            generation = int(request_state["generation"])
            if rejected_generation is not None and generation != rejected_generation:
                return str(request_state["url"]), dict(request_state["headers"]), generation
            age = time.monotonic() - float(request_state["refreshed_at"])
            should_refresh = rejected_generation is not None or (proactive and age >= _REQUEST_REFRESH_SECONDS)
            if should_refresh and (request_refresh_cb or url_refresh_cb):
                new_url, new_headers = await _refresh_request(
                    str(request_state["url"]),
                    dict(request_state["headers"]),
                    url_refresh_cb,
                    request_refresh_cb,
                )
                request_state.update(
                    url=new_url,
                    headers=new_headers,
                    refreshed_at=time.monotonic(),
                    generation=generation + 1,
                )
                generation += 1
                log.info("parallel-range refreshed download URL and headers")
            return str(request_state["url"]), dict(request_state["headers"]), generation

    async def report(force: bool = False) -> None:
        if not progress_cb:
            return
        now = time.monotonic()
        async with meta_lock:
            completed = existing + sum(
                end - start + 1 for key, (start, end) in range_by_key.items() if key in done_set
            )
            value = min(size, completed + sum(inflight.values()))
        if not force and now - last_cb["t"] < 0.5 and value < size:
            return
        last_cb["t"] = now
        await progress_cb(value, size)

    async def reset_inflight(key: str) -> None:
        async with meta_lock:
            inflight.pop(key, None)

    async def fetch_slice(start: int, end: int) -> None:
        key = f"{start}-{end}"
        need = end - start + 1
        if key in done_set:
            return
        attempt = 0
        rejections = 0
        while attempt < max_retries:
            attempt += 1
            current_url, request_headers, generation = await current_request(proactive=True)
            request_headers["Range"] = f"bytes={start}-{end}"
            got = 0
            offset = start
            try:
                async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
                    async with client.stream("GET", current_url, headers=request_headers) as response:
                        if response.status_code in (401, 403, 404, 412, 416):
                            try:
                                body = (await response.aread())[:400].decode("utf-8", "ignore")
                            except Exception:
                                body = ""
                            raise DownloadRequestRejected(response.status_code, body)
                        if response.status_code == 200:
                            raise RangeNotSupportedError("server ignored Range (HTTP 200)")
                        if response.status_code != 206:
                            raise RuntimeError(f"segment HTTP {response.status_code}")

                        content_range = response.headers.get("content-range") or ""
                        match = re.match(
                            r"bytes\s+(\d+)-(\d+)/(\d+|\*)$",
                            content_range,
                            re.IGNORECASE,
                        )
                        if (
                            not match
                            or int(match.group(1)) != start
                            or int(match.group(2)) != end
                            or (match.group(3) != "*" and int(match.group(3)) != size)
                        ):
                            raise RuntimeError(
                                f"segment Content-Range mismatch for {start}-{end}: {content_range or 'none'}"
                            )

                        buffer = bytearray()
                        flush_every = max(settings.download_chunk_size, 256 * 1024)

                        async def flush_buffer() -> None:
                            nonlocal got, offset
                            if not buffer:
                                return
                            if got + len(buffer) > need:
                                raise RuntimeError(f"segment {key} exceeded requested range")
                            data = bytes(buffer)
                            async with file_lock:
                                with open(part, "r+b") as file_obj:
                                    file_obj.seek(offset)
                                    file_obj.write(data)
                            offset += len(data)
                            got += len(data)
                            buffer.clear()
                            async with meta_lock:
                                inflight[key] = got
                            await report()

                        async for chunk in response.aiter_bytes(settings.download_chunk_size):
                            if not chunk:
                                continue
                            buffer.extend(chunk)
                            if len(buffer) >= flush_every:
                                await flush_buffer()
                        await flush_buffer()

                if got != need:
                    raise RuntimeError(f"incomplete segment {key}: {got}/{need}")
                # Do not persist a completed range until the bytes themselves
                # survive an abrupt host/power loss.
                async with file_lock:
                    await asyncio.to_thread(_fsync_file, part)
                async with meta_lock:
                    inflight.pop(key, None)
                    done_set.add(key)
                    save_meta()
                await report(force=True)
                return
            except asyncio.CancelledError:
                await reset_inflight(key)
                raise
            except RangeNotSupportedError:
                await reset_inflight(key)
                raise
            except DownloadRequestRejected as error:
                await reset_inflight(key)
                rejections += 1
                if (request_refresh_cb or url_refresh_cb) and rejections <= 3:
                    await current_request(rejected_generation=generation)
                    await asyncio.sleep(min(3.0, 0.5 * rejections))
                    continue
                low = error.body.lower()
                if (
                    error.status_code == 412
                    or "auth expired" in low
                    or "require login" in low
                    or "auth not found" in low
                    or "requestdeniedbycallback" in low
                ):
                    raise RuntimeError(
                        "夸克登入已失效（CDN require login），請到設定頁重新掃碼；已下載進度會保留"
                    ) from error
                raise RangeNotSupportedError(str(error)) from error
            except (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError) as error:
                await reset_inflight(key)
                log.warning("slice %s network attempt %s/%s: %s", key, attempt, max_retries, error)
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"slice {key} stalled after {max_retries} retries: {error}"
                    ) from error
                await asyncio.sleep(min(20, 1.2 * attempt))
            except RuntimeError as error:
                await reset_inflight(key)
                if "取消" in str(error) or "cancel" in str(error).lower():
                    raise
                log.warning("slice %s error attempt %s/%s: %s", key, attempt, max_retries, error)
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(min(20, 1.2 * attempt))
        raise RuntimeError(f"slice {key} failed")

    semaphore = asyncio.Semaphore(max(1, connections))

    async def wrapped(start: int, end: int) -> None:
        async with semaphore:
            await fetch_slice(start, end)

    tasks = [asyncio.create_task(wrapped(start, end)) for start, end in ranges]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Leave the sparse file plus atomic metadata for the worker's next retry.
        raise

    if len(done_set) < len(ranges):
        missing = [key for key in range_by_key if key not in done_set]
        raise RuntimeError(f"incomplete slices: {missing[:10]}")

    # ensure logical size (sparse OK)
    with open(part, "r+b") as f:
        f.truncate(size)

    try:
        meta_path.unlink(missing_ok=True)
        _fsync_directory(meta_path.parent)
    except OSError:
        pass

    if progress_cb:
        await progress_cb(size, size)
    log.info("parallel-range complete %s bytes conns=%s slices=%s", size, connections, len(ranges))
    return part
