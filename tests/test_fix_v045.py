from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from app.db import Database
from app.sinks.onedrive import (
    OneDrivePermanentUploadError,
    OneDriveSink,
    normalize_onedrive_target,
    sanitize_onedrive_segment,
)
from app.workers.runner import Worker


_INVALID = set('"*:<>?/\\|#%')


@pytest.mark.parametrize(
    "original",
    [
        "关于化妆水你值得知道的事 | 女神进化论.pdf",
        "加拿大科学家突破屏障：Why it matters?.pdf",
        "怎样炒出很嫩的肉丝:肉片:肉丁.pdf",
        "如何把家装修成\x08舒服的地方。.pdf",
    ],
)
def test_onedrive_live_invalid_names_are_deterministic_and_readable(original: str):
    safe = sanitize_onedrive_segment(original, preserve_extension=True)
    assert safe == sanitize_onedrive_segment(original, preserve_extension=True)
    assert safe.endswith(".pdf")
    assert "[pb-" in safe
    assert not any(char in _INVALID or ord(char) < 0x20 for char in safe)
    assert not safe.startswith(" ")
    assert not safe.endswith((" ", "."))


def test_onedrive_normalization_does_not_collide_with_existing_fullwidth_name():
    invalid = sanitize_onedrive_segment("a|b.pdf", preserve_extension=True)
    existing = sanitize_onedrive_segment("a｜b.pdf", preserve_extension=True)
    assert invalid != existing
    assert "[pb-" in invalid
    assert existing == "a｜b.pdf"


def test_onedrive_unicode_normalization_cannot_collapse_distinct_source_names():
    composed = sanitize_onedrive_segment("Café.mkv", preserve_extension=True)
    decomposed = sanitize_onedrive_segment("Café.mkv", preserve_extension=True)
    assert composed == "Café.mkv"
    assert decomposed != composed
    assert decomposed.startswith("Café [pb-")
    assert decomposed.endswith(".mkv")


@pytest.mark.parametrize(
    "original",
    [
        "CON.txt",
        "CON.foo.bar",
        "desktop.ini",
        ".lock",
        "~$budget.xlsx",
        "_vti_report.pdf",
        " trailing. ",
    ],
)
def test_onedrive_reserved_names_are_neutralized(original: str):
    safe = sanitize_onedrive_segment(original, preserve_extension=True)
    assert safe != original
    assert "[pb-" in safe
    assert not safe.startswith("~")
    assert not safe.endswith((" ", "."))
    assert "_vti_" not in safe.lower()


def test_onedrive_deep_long_target_is_unique_and_below_cloud_limit():
    folder = "/" + "/".join((f"資料夾{i}-" + "長" * 100) for i in range(12))
    filename = "影片" + "很" * 400 + ".mkv"
    safe_folder, safe_filename = normalize_onedrive_target(folder, filename)
    complete = safe_folder.strip("/") + "/" + safe_filename
    assert safe_folder.startswith("/")
    assert "縮短路徑 [pb-" in safe_folder
    assert safe_filename.endswith(".mkv")
    assert len(complete) <= 380
    assert normalize_onedrive_target(folder, filename) == (safe_folder, safe_filename)


def test_onedrive_backslash_in_source_folder_is_not_mistaken_for_separator():
    backslash_folder, _ = normalize_onedrive_target("/課程\\資料", "影片.mkv")
    nested_folder, _ = normalize_onedrive_target("/課程/資料", "影片.mkv")
    assert backslash_folder != nested_folder
    assert "＼" in backslash_folder
    assert "[pb-" in backslash_folder


@pytest.mark.asyncio
async def test_onedrive_case_equivalent_folders_are_kept_distinct():
    sink = OneDriveSink("access")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "upper", "name": "Movie", "folder": {}},
                    ]
                },
            )
        assert method == "POST"
        return httpx.Response(201, json={"id": "lower"})

    sink._request = fake_request  # type: ignore[method-assign]
    folder_id, actual_name = await sink._ensure_child_folder_target("root", "movie")

    assert folder_id == "lower"
    created_name = calls[-1][2]["json"]["name"]
    assert created_name.startswith("movie [pb-")
    assert actual_name == created_name
    assert created_name != "Movie"
    assert calls[-1][2]["json"]["@microsoft.graph.conflictBehavior"] == "fail"


@pytest.mark.asyncio
async def test_onedrive_resolved_collision_folder_is_reported_in_upload_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"clip")
    sink = OneDriveSink("access")

    async def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/root/children"):
            return httpx.Response(
                200,
                json={"value": [{"id": "upper", "name": "Movie", "folder": {}}]},
            )
        if method == "POST" and url.endswith("/root/children"):
            requested = kwargs["json"]["name"]
            return httpx.Response(201, json={"id": "lower", "name": requested})
        assert method == "POST" and url.endswith(":/createUploadSession")
        return httpx.Response(200, json={"uploadUrl": "https://upload.example/folder"})

    class UploadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def put(self, _url: str, content: bytes, headers: dict[str, str]):
            return httpx.Response(
                201,
                json={
                    "id": "clip",
                    "size": len(content),
                    "name": "clip.mkv",
                    "parentReference": {"driveId": "drive"},
                },
            )

    monkeypatch.setattr(sink, "_request", fake_request)
    monkeypatch.setattr("app.sinks.onedrive.httpx.AsyncClient", UploadClient)
    result = await sink.upload_file(source, "/movie", "clip.mkv")

    assert result["_panbridge_remote_folder"].startswith("/movie [pb-")


@pytest.mark.asyncio
async def test_onedrive_exact_folder_is_reused_without_create():
    sink = OneDriveSink("access")
    methods: list[str] = []

    async def fake_request(method: str, _url: str, **_kwargs):
        methods.append(method)
        return httpx.Response(
            200,
            json={"value": [{"id": "existing", "name": "movie", "folder": {}}]},
        )

    sink._request = fake_request  # type: ignore[method-assign]
    assert await sink._ensure_child_folder("root", "movie") == "existing"
    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_onedrive_small_upload_uses_safe_graph_name_and_reports_actual_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "small.pdf"
    source.write_bytes(b"pdf")
    sink = OneDriveSink("access")
    calls: list[tuple[str, str, dict]] = []

    async def fake_folder(path: str) -> str:
        assert "|" not in path
        return "folder"

    async def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(
            200, json={"uploadUrl": "https://upload.example/small-session"}
        )

    class UploadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def put(self, url: str, content: bytes, headers: dict[str, str]):
            assert url == "https://upload.example/small-session"
            assert content == b"pdf"
            return httpx.Response(
                201,
                json={
                    "id": "item",
                    "size": 3,
                    "name": "Microsoft adopted name.pdf",
                    "parentReference": {"driveId": "drive"},
                },
            )

    monkeypatch.setattr(sink, "ensure_folder_path", fake_folder)
    monkeypatch.setattr(sink, "_request", fake_request)
    monkeypatch.setattr("app.sinks.onedrive.httpx.AsyncClient", UploadClient)
    result = await sink.upload_file(
        source,
        "/PanBridge/女神:资料",
        "关于化妆水 | 女神进化论.pdf",
    )

    assert len(calls) == 1
    decoded_url = unquote(calls[0][1])
    assert calls[0][0] == "POST"
    assert "|" not in decoded_url and ":资料" not in decoded_url
    assert "｜" in decoded_url
    assert calls[0][2]["json"]["item"]["@microsoft.graph.conflictBehavior"] == "rename"
    assert result["_panbridge_remote_folder"].startswith("/PanBridge/")
    assert result["_panbridge_remote_name"] == "Microsoft adopted name.pdf"


@pytest.mark.asyncio
async def test_onedrive_case_conflict_uses_rename_and_records_graph_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"video")
    sink = OneDriveSink("access")
    create_body: dict = {}

    async def fake_folder(_path: str) -> str:
        return "folder"

    async def fake_request(method: str, url: str, **kwargs):
        assert method == "POST"
        assert url.endswith(":/createUploadSession")
        create_body.update(kwargs["json"])
        return httpx.Response(200, json={"uploadUrl": "https://upload.example/case"})

    class UploadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def put(self, _url: str, content: bytes, headers: dict[str, str]):
            assert content == b"video"
            return httpx.Response(
                201,
                json={
                    "id": "item-2",
                    "size": 5,
                    "name": "movie 1.mkv",
                    "parentReference": {"driveId": "drive"},
                },
            )

    monkeypatch.setattr(sink, "ensure_folder_path", fake_folder)
    monkeypatch.setattr(sink, "_request", fake_request)
    monkeypatch.setattr("app.sinks.onedrive.httpx.AsyncClient", UploadClient)
    result = await sink.upload_file(source, "/PanBridge", "movie.mkv")

    assert create_body["item"]["@microsoft.graph.conflictBehavior"] == "rename"
    assert result["_panbridge_remote_name"] == "movie 1.mkv"


@pytest.mark.asyncio
async def test_onedrive_zero_byte_file_fails_closed_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    sink = OneDriveSink("access")

    async def fake_folder(_path: str) -> str:
        return "folder"

    async def forbidden_request(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("zero-byte safety failure must happen before upload")

    monkeypatch.setattr(sink, "ensure_folder_path", fake_folder)
    monkeypatch.setattr(sink, "_request", forbidden_request)
    with pytest.raises(OneDrivePermanentUploadError, match="0 bytes"):
        await sink.upload_file(source, "/PanBridge", "empty.txt")


@pytest.mark.asyncio
async def test_onedrive_large_invalid_request_is_permanent_before_sending_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "large.pdf"
    source.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    sink = OneDriveSink("access")
    seen: dict[str, object] = {}

    async def fake_folder(_path: str) -> str:
        return "folder"

    async def fake_request(method: str, url: str, **kwargs):
        seen.update(method=method, url=url, json=kwargs.get("json"))
        return httpx.Response(400, json={"error": {"code": "invalidRequest"}})

    monkeypatch.setattr(sink, "ensure_folder_path", fake_folder)
    monkeypatch.setattr(sink, "_request", fake_request)
    with pytest.raises(OneDrivePermanentUploadError):
        await sink.upload_file(source, "/PanBridge", "大檔 | 測試.pdf")

    decoded_url = unquote(str(seen["url"]))
    assert seen["method"] == "POST"
    assert "｜" in decoded_url and "|" not in decoded_url
    assert seen["json"]["item"]["name"] in decoded_url  # type: ignore[index]


class _UnusedSource:
    async def prepare_download(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("existing verified staging file must not be downloaded again")

    def get_download_headers(self):
        return {}


@pytest.mark.asyncio
async def test_worker_stores_actual_safe_onedrive_path_and_preserves_original_name(
    tmp_path: Path,
):
    db = Database(tmp_path / "safe-name.db")
    await db.connect()
    job_id = await db.create_job(
        "quark",
        "https://pan.quark.cn/s/x",
        destination="onedrive",
        pcloud_path="/PanBridge/课程:资料",
    )
    original = "内容 | 女神进化论.pdf"
    file_id = await db.create_file(
        job_id,
        original,
        relative_path=original,
        size=100,
        source_fid="f1",
    )
    row = await db.get_file(file_id)
    job_tmp = tmp_path / "job"
    job_tmp.mkdir()
    local_name = "".join(char if char not in '\\/:*?"<>|' else "_" for char in original)
    (job_tmp / f"{file_id}_{local_name}").write_bytes(b"z" * 100)

    class SuccessfulOneDrive(OneDriveSink):
        def __init__(self):
            super().__init__("access")

        async def upload_file(self, local_path, remote_dir, filename, progress_cb=None):
            assert local_path.stat().st_size == 100
            assert "|" not in filename and "｜" in filename and "[pb-" in filename
            assert ":资料" not in remote_dir
            if progress_cb:
                await progress_cb(100, 100)
            return {
                "id": "item",
                "size": 100,
                "name": filename,
                "parentReference": {"driveId": "drive"},
                "_panbridge_remote_folder": remote_dir,
                "_panbridge_remote_name": filename,
            }

    worker = Worker(db)
    await worker._process_file(
        _UnusedSource(),
        SuccessfulOneDrive(),
        job_id,
        row,
        "/PanBridge/课程:资料",
        job_tmp,
        {},
    )
    saved = await db.get_file(file_id)
    delivery = json.loads(saved["meta_json"])["onedrive_delivery"]
    assert saved["remote_name"] == original
    assert saved["status"] == "done"
    assert "｜" in saved["pcloud_path"] and "[pb-" in saved["pcloud_path"]
    assert delivery["name"] in saved["pcloud_path"]
    assert delivery["path"] == saved["pcloud_path"]
    await db.close()


@pytest.mark.asyncio
async def test_worker_does_not_retry_permanent_onedrive_request_four_times(
    tmp_path: Path,
):
    db = Database(tmp_path / "permanent.db")
    await db.connect()
    job_id = await db.create_job("quark", "https://pan.quark.cn/s/x")
    file_id = await db.create_file(
        job_id,
        "bad|name.pdf",
        relative_path="bad|name.pdf",
        size=10,
        source_fid="f1",
    )
    row = await db.get_file(file_id)
    job_tmp = tmp_path / "job"
    job_tmp.mkdir()
    (job_tmp / f"{file_id}_bad_name.pdf").write_bytes(b"x" * 10)

    class PermanentFailure(OneDriveSink):
        def __init__(self):
            super().__init__("access")
            self.calls = 0

        async def upload_file(self, *_args, **_kwargs):
            self.calls += 1
            raise OneDrivePermanentUploadError("invalidRequest")

    sink = PermanentFailure()
    worker = Worker(db)
    with pytest.raises(OneDrivePermanentUploadError):
        await worker._process_file(
            _UnusedSource(), sink, job_id, row, "/PanBridge", job_tmp, {}
        )
    assert sink.calls == 1
    saved = await db.get_file(file_id)
    assert saved["status"] == "failed"
    assert saved["downloaded_bytes"] == 10
    await db.close()
