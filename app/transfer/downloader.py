from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from app.config import get_settings

log = logging.getLogger("panbridge.download")

ProgressCB = Callable[[int, int], Awaitable[None]]  # done, total

# Network stalls (Baidu/CDN) must not hang forever. read = silence between socket data.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=90.0, write=60.0, pool=30.0)
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

    # multi needs known size + range
    if conns <= 1 or not can_range or not size or size < 8 * 1024 * 1024:
        return await _single_stream_download(
            url, part, headers, size or expected_size, progress_cb, max_retries, url_refresh_cb
        )

    contiguous = part.stat().st_size if part.exists() else 0
    # only treat as contiguous prefix when no parallel meta (single-stream .part)
    meta_path = Path(str(part) + ".ranges.json")
    if meta_path.exists():
        # previous parallel attempt — keep file; parallel will resume via meta
        contiguous = 0  # progress from meta + prefix handled inside
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
            contiguous_prefix=part.stat().st_size if part.exists() and not meta_path.exists() else 0,
        )
    except Exception as e:
        log.warning("parallel range failed (%s); fallback single stream", e)
        # shrink to contiguous prefix so st_size is not a sparse full-size lie
        prefix = contiguous
        if meta_path.exists():
            try:
                meta_path.unlink(missing_ok=True)
            except OSError:
                pass
        if part.exists() and prefix >= 0:
            try:
                with open(part, "r+b") as f:
                    f.truncate(prefix)
            except OSError:
                pass
        return await _single_stream_download(
            url, part, headers, size or expected_size, progress_cb, max_retries, url_refresh_cb
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
                    # 412 = Quark CDN often means cookie/auth invalid for download domain
                    if resp.status_code in (401, 403, 404, 412):
                        try:
                            err_body = (await resp.aread())[:400].decode("utf-8", "ignore")
                        except Exception:
                            err_body = ""
                        low = err_body.lower()
                        # HTTP 412 alone is enough to hard-fail for Quark CDN auth
                        if (
                            resp.status_code == 412
                            or "auth expired" in low
                            or "require login" in low
                            or "auth not found" in low
                            or "requestdeniedbycallback" in low
                        ):
                            raise RuntimeError(
                                "夸克登录已失效（CDN 403/412 require login），请到设定页重新连接夸克 Cookie"
                            )
                        raise RuntimeError(
                            f"download URL expired or forbidden: HTTP {resp.status_code} {err_body[:120]}"
                        )
                    if existing > 0 and resp.status_code == 200:
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
            return part
        except RuntimeError as e:
            msg = str(e).lower()
            # hard cookie/login failures — never spin refresh forever
            if "登录已失效" in str(e) or "require login" in msg or "auth expired" in msg:
                raise
            if any(x in msg for x in ("expired", "forbidden", "403", "401", "404", "412")):
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


async def _parallel_range_download(
    url: str,
    part: Path,
    headers: dict[str, str],
    size: int,
    progress_cb: ProgressCB | None,
    max_retries: int,
    connections: int,
    url_refresh_cb: Callable[[], Awaitable[str]] | None = None,
    contiguous_prefix: int = 0,
) -> Path:
    """Multi-connection Range fill into one file via seek+write (no 2× disk).

    Resume: contiguous prefix from prior single-stream is kept; remaining tail is
    split across connections. Finished slices stored in ``.ranges.json``.
    """
    settings = get_settings()
    meta_path = Path(str(part) + ".ranges.json")

    existing = max(0, int(contiguous_prefix))
    if part.exists() and meta_path.exists():
        # parallel resume: do not trust st_size (may include sparse holes)
        existing = 0
        try:
            raw = json.loads(meta_path.read_text())
            if int(raw.get("size") or 0) == size and "prefix" in raw:
                existing = int(raw.get("prefix") or 0)
        except Exception:
            existing = 0
    elif part.exists() and not meta_path.exists():
        existing = min(part.stat().st_size, size)

    if existing >= size > 0 and not meta_path.exists():
        if progress_cb:
            await progress_cb(size, size)
        return part

    if not part.exists():
        part.touch()

    remain_start = existing
    n = max(1, min(connections, 8))
    remain = size - remain_start
    if remain <= 0:
        if progress_cb:
            await progress_cb(size, size)
        return part
    slice_size = max(8 * 1024 * 1024, min(64 * 1024 * 1024, (remain + n - 1) // n))
    ranges: list[tuple[int, int]] = []
    pos = remain_start
    while pos < size:
        end = min(size - 1, pos + slice_size - 1)
        ranges.append((pos, end))
        pos = end + 1

    done_set: set[str] = set()
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text())
            if int(raw.get("size") or 0) == size:
                done_set = set(str(x) for x in (raw.get("done") or []))
                remain_start = int(raw.get("prefix") or remain_start)
        except Exception:
            done_set = set()

    # Rebuild ranges if prefix from meta differs
    if remain_start != existing:
        existing = remain_start
        ranges = []
        pos = existing
        remain = size - existing
        slice_size = max(8 * 1024 * 1024, min(64 * 1024 * 1024, (remain + n - 1) // max(1, n)))
        while pos < size:
            end = min(size - 1, pos + slice_size - 1)
            ranges.append((pos, end))
            pos = end + 1

    progress = {"bytes": existing}
    for i, (s, e) in enumerate(ranges):
        if str(i) in done_set:
            progress["bytes"] += e - s + 1

    url_state = {"url": url}
    url_lock = asyncio.Lock()
    file_lock = asyncio.Lock()  # serialize seeks/writes on one FD family
    meta_lock = asyncio.Lock()
    last_cb = {"t": time.monotonic()}
    # If any segment gets 403 without refresh success, signal fallback
    fatal_multi = {"err": None}

    async def report() -> None:
        if not progress_cb:
            return
        now = time.monotonic()
        if now - last_cb["t"] < 0.5 and progress["bytes"] < size:
            return
        last_cb["t"] = now
        await progress_cb(min(progress["bytes"], size), size)

    async def save_meta() -> None:
        meta_path.write_text(
            json.dumps(
                {
                    "size": size,
                    "prefix": existing,
                    "done": sorted(done_set, key=lambda x: int(x)),
                }
            )
        )

    async def refresh_url_if_needed() -> str:
        async with url_lock:
            if not url_refresh_cb:
                return url_state["url"]
            try:
                url_state["url"] = await url_refresh_cb()
                log.info("parallel-range refreshed dlink")
            except Exception as e:
                log.warning("parallel-range url refresh failed: %s", e)
            return url_state["url"]

    async def fetch_slice(idx: int, s: int, e: int) -> None:
        key = str(idx)
        need = e - s + 1
        if key in done_set:
            return
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            if fatal_multi["err"]:
                raise fatal_multi["err"]
            # resume mid-slice: check written bytes in file for this range via meta only
            # (we mark done only when full slice written)
            byte_start = s
            req = dict(headers)
            req["Range"] = f"bytes={byte_start}-{e}"
            try:
                current = url_state["url"]
                async with httpx.AsyncClient(timeout=_dl_timeout(), follow_redirects=True) as client:
                    async with client.stream("GET", current, headers=req) as resp:
                        if resp.status_code in (401, 403, 404, 412):
                            if url_refresh_cb:
                                await refresh_url_if_needed()
                                raise RuntimeError(f"download URL expired: HTTP {resp.status_code}")
                            # Baidu multi often 403 — signal outer fallback to single
                            err = RuntimeError(f"range rejected HTTP {resp.status_code}")
                            fatal_multi["err"] = err
                            raise err
                        if resp.status_code == 200 and byte_start > 0:
                            # server ignored Range — cannot multi safely
                            err = RuntimeError("server ignored Range (HTTP 200)")
                            fatal_multi["err"] = err
                            raise err
                        if resp.status_code not in (200, 206):
                            raise RuntimeError(f"segment HTTP {resp.status_code}")

                        offset = byte_start
                        got = 0
                        last_data = time.monotonic()
                        buf = bytearray()
                        flush_every = max(settings.download_chunk_size, 256 * 1024)

                        async for chunk in resp.aiter_bytes(settings.download_chunk_size):
                            if not chunk:
                                if time.monotonic() - last_data > _STALL_SECONDS:
                                    raise TimeoutError(f"slice {idx} stalled")
                                continue
                            last_data = time.monotonic()
                            buf.extend(chunk)
                            if len(buf) >= flush_every:
                                async with file_lock:
                                    with open(part, "r+b") as f:
                                        f.seek(offset)
                                        f.write(buf)
                                offset += len(buf)
                                got += len(buf)
                                async with meta_lock:
                                    progress["bytes"] += len(buf)
                                buf.clear()
                                await report()

                        if buf:
                            async with file_lock:
                                with open(part, "r+b") as f:
                                    f.seek(offset)
                                    f.write(buf)
                            got += len(buf)
                            async with meta_lock:
                                progress["bytes"] += len(buf)
                            buf.clear()
                            await report()

                if got < need:
                    # incomplete slice — rewind progress for retry
                    async with meta_lock:
                        progress["bytes"] = max(existing, progress["bytes"] - got)
                    await asyncio.sleep(min(15, 1.2 * attempt))
                    continue

                async with meta_lock:
                    done_set.add(key)
                    await save_meta()
                return
            except RuntimeError as e:
                msg = str(e).lower()
                if "rejected" in msg or "ignored range" in msg:
                    raise
                if any(x in msg for x in ("expired", "forbidden", "403", "401", "404")):
                    if url_refresh_cb:
                        await refresh_url_if_needed()
                    if attempt >= max_retries:
                        raise
                    await asyncio.sleep(min(20, 1.2 * attempt))
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError) as e:
                log.warning("slice %s network attempt %s: %s", idx, attempt, e)
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(min(20, 1.2 * attempt))
            except Exception as e:
                log.warning("slice %s error attempt %s: %s", idx, attempt, e)
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(min(20, 1.2 * attempt))
        raise RuntimeError(f"slice {idx} failed")

    sem = asyncio.Semaphore(connections)

    async def wrapped(i: int, s: int, e: int) -> None:
        async with sem:
            await fetch_slice(i, s, e)

    try:
        await asyncio.gather(*[wrapped(i, s, e) for i, (s, e) in enumerate(ranges)])
    except Exception:
        # leave partial .part + ranges meta for resume
        raise

    if len(done_set) < len(ranges):
        missing = [i for i in range(len(ranges)) if str(i) not in done_set]
        raise RuntimeError(f"incomplete slices: {missing[:10]}")

    # ensure logical size (sparse OK)
    with open(part, "r+b") as f:
        f.truncate(size)

    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass

    if progress_cb:
        await progress_cb(size, size)
    log.info("parallel-range complete %s bytes conns=%s slices=%s", size, connections, len(ranges))
    return part
