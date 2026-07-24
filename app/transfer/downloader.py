from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from app.config import get_settings

log = logging.getLogger("panbridge.download")

ProgressCB = Callable[[int, int], Awaitable[None]]  # done, total

# Network stalls (Baidu/CDN) must not hang forever. read = silence between socket data.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=90.0, write=60.0, pool=30.0)
# If no progress_cb-visible growth for this long while streaming, force reconnect.
_STALL_SECONDS = 120.0


def _dl_timeout() -> httpx.Timeout:
    return _DEFAULT_TIMEOUT


async def _probe_size(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bool]:
    """Return (size, supports_range). size 0 if unknown."""
    try:
        r = await client.head(url, headers=headers)
        if r.status_code < 400:
            size = int(r.headers.get("content-length") or 0)
            accept = "bytes" in (r.headers.get("accept-ranges") or "").lower()
            if size:
                return size, accept or True
    except Exception:
        pass
    # fallback: Range 0-0
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        r = await client.get(url, headers=h)
        if r.status_code in (200, 206):
            cr = r.headers.get("content-range") or ""
            if "/" in cr:
                try:
                    return int(cr.rsplit("/", 1)[-1]), True
                except ValueError:
                    pass
            if r.status_code == 206:
                return 0, True
            return int(r.headers.get("content-length") or 0), False
    except Exception:
        pass
    return 0, False


async def resumable_download(
    url: str,
    dest: Path,
    headers: dict[str, str] | None = None,
    expected_size: int = 0,
    progress_cb: ProgressCB | None = None,
    max_retries: int = 40,
    connections: int | None = None,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
) -> Path:
    """Download with resume. Uses multi-connection Range when beneficial.

    url_refresh_cb: optional async () -> new_url, called before retry after stall/HTTP expiry.
    """
    settings = get_settings()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest if str(dest).endswith(".part") else Path(str(dest) + ".part")
    if str(dest).endswith(".part"):
        part = dest

    headers = dict(headers or {})
    conns = connections if connections is not None else settings.download_connections
    conns = max(1, min(conns, 8))

    # small files or single connection: sequential
    async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
        size, can_range = await _probe_size(client, url, headers)
        if expected_size and not size:
            size = expected_size
        if expected_size and size and abs(expected_size - size) > 0:
            size = expected_size or size

    # BUG-9: multi-conn cannot refresh expired dlinks — force single when refresh needed
    if url_refresh_cb is not None:
        conns = 1
    # threshold: multi only if >= 8MB and range ok and conns>1
    if conns <= 1 or not can_range or (size and size < 8 * 1024 * 1024) or size == 0:
        return await _single_stream_download(
            url,
            part,
            headers,
            size or expected_size,
            progress_cb,
            max_retries,
            url_refresh_cb=url_refresh_cb,
        )
    try:
        return await _multi_conn_download(
            url, part, headers, size, progress_cb, max_retries, conns
        )
    except Exception:
        # fallback single stream
        return await _single_stream_download(
            url,
            part,
            headers,
            size or expected_size,
            progress_cb,
            max_retries,
            url_refresh_cb=url_refresh_cb,
        )


async def _single_stream_download(
    url: str,
    part: Path,
    headers: dict[str, str],
    expected_size: int,
    progress_cb: ProgressCB | None,
    max_retries: int,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
) -> Path:
    settings = get_settings()
    attempt = 0
    current_url = url
    while attempt < max_retries:
        attempt += 1
        existing = part.stat().st_size if part.exists() else 0
        if expected_size and existing >= expected_size > 0:
            if progress_cb:
                await progress_cb(existing, expected_size)
            return part

        # refresh dlink every few retries (Baidu links expire / go silent)
        if attempt > 1 and url_refresh_cb and (attempt % 3 == 1 or attempt <= 3):
            try:
                current_url = await url_refresh_cb()
                log.info("refreshed download url on attempt %s (have %s bytes)", attempt, existing)
            except Exception as e:
                log.warning("url refresh failed: %s", e)

        req_headers = dict(headers)
        if existing > 0:
            req_headers["Range"] = f"bytes={existing}-"

        try:
            async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
                async with client.stream("GET", current_url, headers=req_headers) as resp:
                    if resp.status_code in (401, 403, 404):
                        raise RuntimeError(f"download URL expired or forbidden: HTTP {resp.status_code}")
                    if existing > 0 and resp.status_code == 200:
                        # server ignored Range — restart from 0
                        existing = 0
                        part.write_bytes(b"")
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"download failed HTTP {resp.status_code}")

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
                            # flush periodically so crash/restart keeps more progress
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
                            # Soft stall: no byte growth for too long (belt + httpx read timeout)
                            if now - last_progress_t > _STALL_SECONDS:
                                f.flush()
                                raise TimeoutError(
                                    f"download stalled for {_STALL_SECONDS:.0f}s at {downloaded} bytes"
                                )

            final_size = part.stat().st_size
            if expected_size and final_size < expected_size:
                log.warning(
                    "incomplete download %s/%s, retry %s",
                    final_size,
                    expected_size,
                    attempt,
                )
                continue
            # BUG-4: unknown expected size — refuse empty completion; require positive body
            if not expected_size:
                if final_size <= 0:
                    log.warning("empty download with unknown size, retry %s", attempt)
                    continue
                # if we learned total from headers and fell short, already handled above
            return part
        except RuntimeError as e:
            msg = str(e).lower()
            # expired URL → refresh and retry (don't burn all attempts without refresh)
            if any(x in msg for x in ("expired", "forbidden", "403", "401", "404")):
                if url_refresh_cb:
                    try:
                        current_url = await url_refresh_cb()
                    except Exception:
                        pass
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


async def _multi_conn_download(
    url: str,
    part: Path,
    headers: dict[str, str],
    size: int,
    progress_cb: ProgressCB | None,
    max_retries: int,
    connections: int,
) -> Path:
    """Segmented multi-connection download with per-segment resume."""
    settings = get_settings()
    meta_path = Path(str(part) + ".segments.json")
    seg_dir = Path(str(part) + ".segs")
    seg_dir.mkdir(parents=True, exist_ok=True)

    # segment size ~ max(4MB, size/connections) but cap 16MB for low-mem
    n = connections
    seg_size = max(4 * 1024 * 1024, min(16 * 1024 * 1024, (size + n - 1) // n))
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(size - 1, start + seg_size - 1)
        ranges.append((start, end))
        start = end + 1

    # load progress
    done_map: dict[str, int] = {}
    if meta_path.exists():
        try:
            done_map = json.loads(meta_path.read_text()).get("done") or {}
        except Exception:
            done_map = {}

    progress = {"bytes": 0}
    # count already complete segments
    for i, (s, e) in enumerate(ranges):
        key = str(i)
        need = e - s + 1
        seg_file = seg_dir / f"{i:05d}.part"
        have = seg_file.stat().st_size if seg_file.exists() else int(done_map.get(key) or 0)
        if have >= need:
            progress["bytes"] += need
            done_map[key] = need
        else:
            progress["bytes"] += have
            done_map[key] = have

    lock = asyncio.Lock()
    last_cb = {"t": time.monotonic()}

    async def report() -> None:
        if not progress_cb:
            return
        now = time.monotonic()
        if now - last_cb["t"] < 0.4 and progress["bytes"] < size:
            return
        last_cb["t"] = now
        await progress_cb(progress["bytes"], size)

    async def save_meta() -> None:
        meta_path.write_text(json.dumps({"size": size, "done": done_map}))

    async def fetch_seg(idx: int, s: int, e: int) -> None:
        need = e - s + 1
        seg_file = seg_dir / f"{idx:05d}.part"
        key = str(idx)
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            have = seg_file.stat().st_size if seg_file.exists() else 0
            if have >= need:
                async with lock:
                    done_map[key] = need
                return
            byte_start = s + have
            req = dict(headers)
            req["Range"] = f"bytes={byte_start}-{e}"
            try:
                async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
                    async with client.stream("GET", url, headers=req) as resp:
                        if resp.status_code in (401, 403, 404):
                            raise RuntimeError(f"download URL expired: HTTP {resp.status_code}")
                        if resp.status_code not in (200, 206):
                            raise RuntimeError(f"segment HTTP {resp.status_code}")
                        mode = "ab" if have > 0 and resp.status_code == 206 else "wb"
                        if mode == "wb":
                            # reset progress for this segment
                            async with lock:
                                progress["bytes"] -= have
                                have = 0
                                done_map[key] = 0
                        last_data = time.monotonic()
                        with open(seg_file, mode) as f:
                            async for chunk in resp.aiter_bytes(settings.download_chunk_size):
                                if not chunk:
                                    if time.monotonic() - last_data > _STALL_SECONDS:
                                        raise TimeoutError(f"segment {idx} stalled")
                                    continue
                                f.write(chunk)
                                last_data = time.monotonic()
                                async with lock:
                                    progress["bytes"] += len(chunk)
                                    done_map[key] = seg_file.stat().st_size
                                await report()
                # validate
                final = seg_file.stat().st_size
                if final >= need:
                    async with lock:
                        done_map[key] = need
                        await save_meta()
                    return
            except RuntimeError:
                raise
            except Exception:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(min(20, 1.2 * attempt))
        raise RuntimeError(f"segment {idx} failed")

    # limit concurrency
    sem = asyncio.Semaphore(connections)

    async def wrapped(i: int, s: int, e: int) -> None:
        async with sem:
            await fetch_seg(i, s, e)

    await asyncio.gather(*[wrapped(i, s, e) for i, (s, e) in enumerate(ranges)])

    # assemble
    with open(part, "wb") as out:
        for i, (s, e) in enumerate(ranges):
            seg_file = seg_dir / f"{i:05d}.part"
            need = e - s + 1
            data = seg_file.read_bytes()
            if len(data) < need:
                raise RuntimeError(f"incomplete segment {i}")
            out.write(data[:need])

    # cleanup segments
    try:
        for f in seg_dir.glob("*.part"):
            f.unlink(missing_ok=True)
        seg_dir.rmdir()
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass

    if progress_cb:
        await progress_cb(size, size)
    return part
