"""Tests for back.core.databricks.document.VolumeDocumentSource."""

from __future__ import annotations

import pytest

from back.core.databricks import VolumeDocument, VolumeDocumentSource
from back.core.errors import AuthorizationError, NotFoundError, ValidationError

pytestmark = pytest.mark.unit

MB = 1024 * 1024
ROOT = "/Volumes/main/docs/raw"


class _FakeFiles:
    """In-memory Files API: ``files`` maps path -> bytes, ``dirs`` maps path -> children."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        dirs: dict[str, list[dict]] | None = None,
        status: dict[str, int] | None = None,
        sizes: dict[str, int | None] | None = None,
    ) -> None:
        self.files = files or {}
        self.dirs = dirs or {}
        self.status = status or {}
        self.sizes = sizes or {}
        self.reads: list[str] = []

    def head_file(self, path: str) -> tuple[int, int | None]:
        if path in self.status:
            return self.status[path], None
        if path in self.files:
            return 200, self.sizes.get(path, len(self.files[path]))
        return 404, None

    def list_directory_page(self, path: str, max_entries: int):
        if path in self.status:
            return self.status[path], [], False
        if path not in self.dirs:
            return 404, [], False
        items = self.dirs[path]
        return 200, items[:max_entries], len(items) > max_entries

    def read_binary_file_capped(self, path: str, max_bytes: int):
        self.reads.append(path)
        if path in self.status:
            return self.status[path], None
        data = self.files.get(path)
        if data is None:
            return 404, None
        return 200, None if len(data) > max_bytes else data


def _file(name: str, size: int = 10, base: str = ROOT) -> dict:
    return {"name": name, "path": f"{base}/{name}", "is_directory": False, "size": size}


def _dir(name: str, base: str = ROOT) -> dict:
    return {"name": name, "path": f"{base}/{name}", "is_directory": True, "size": 0}


# -- path validation -----------------------------------------------


class TestNormalizePath:
    @pytest.mark.parametrize(
        "path,expected",
        [
            (f"{ROOT}/a.md", f"{ROOT}/a.md"),
            (f"  {ROOT}/dir/  ", f"{ROOT}/dir"),
            (ROOT, ROOT),
            (f"{ROOT}/보고서 v2.pdf", f"{ROOT}/보고서 v2.pdf"),
        ],
    )
    def test_accepts_volume_paths(self, path, expected):
        assert VolumeDocumentSource.normalize_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/Volumes",
            "/Volumes/",
            "/Volumes/main",
            "/Volumes/main/docs",
            "/Workspace/Users/a/file.md",
            "dbfs:/Volumes/main/docs/raw/a.md",
            "Volumes/main/docs/raw/a.md",
            f"{ROOT}/../other/a.md",
            f"{ROOT}/./a.md",
            "/Volumes/main//raw/a.md",
            f"{ROOT}\\a.md",
            f"{ROOT}/a\x00.md",
            f"{ROOT}/a\n.md",
            ROOT + "/" + "x" * 1100,
        ],
    )
    def test_rejects_invalid_paths(self, path):
        with pytest.raises(ValidationError):
            VolumeDocumentSource.normalize_path(path)


class TestIsSupported:
    @pytest.mark.parametrize("name", ["a.md", "b.PDF", "c.docx", "d.txt", "e.json"])
    def test_supported(self, name):
        assert VolumeDocumentSource.is_supported(name) is True

    @pytest.mark.parametrize("name", ["a.exe", "b.zip", "noext", "c.parquet"])
    def test_unsupported(self, name):
        assert VolumeDocumentSource.is_supported(name) is False


# -- plan: single files --------------------------------------------


class TestPlanFiles:
    def test_single_file_is_planned_with_size(self):
        files = _FakeFiles(files={f"{ROOT}/a.md": b"# hi"})
        docs, truncated = VolumeDocumentSource(files).plan([f"{ROOT}/a.md"])
        assert docs == [VolumeDocument(f"{ROOT}/a.md", "a.md", 4)]
        assert truncated is False

    def test_oversize_file_is_rejected_from_metadata_without_download(self):
        path = f"{ROOT}/big.pdf"
        files = _FakeFiles(files={path: b"x"}, sizes={path: 10 * MB + 1})
        docs, _ = VolumeDocumentSource(files).plan([path])
        assert "10 MB" in docs[0].error
        assert files.reads == []

    def test_unsupported_type_is_rejected_without_download(self):
        path = f"{ROOT}/tool.exe"
        files = _FakeFiles(files={path: b"MZ"})
        docs, _ = VolumeDocumentSource(files).plan([path])
        assert docs[0].error == "Unsupported document type"
        assert files.reads == []

    def test_invalid_path_is_reported_per_item(self):
        files = _FakeFiles(files={f"{ROOT}/a.md": b"a"})
        docs, _ = VolumeDocumentSource(files).plan(
            [f"{ROOT}/../x.md", f"{ROOT}/a.md"]
        )
        assert docs[0].error.startswith("Path must look like")
        assert docs[1].error == ""

    def test_missing_path_is_not_found(self):
        docs, _ = VolumeDocumentSource(_FakeFiles()).plan([f"{ROOT}/nope.md"])
        assert docs[0].error.startswith("Path not found")

    def test_forbidden_path_reports_permission_and_scope_hint(self):
        path = f"{ROOT}/secret.md"
        docs, _ = VolumeDocumentSource(_FakeFiles(status={path: 403})).plan([path])
        assert "permission" in docs[0].error
        assert "files.files" in docs[0].error

    def test_requires_a_non_empty_list(self):
        with pytest.raises(ValidationError):
            VolumeDocumentSource(_FakeFiles()).plan([])
        with pytest.raises(ValidationError):
            VolumeDocumentSource(_FakeFiles()).plan(f"{ROOT}/a.md")

    def test_too_many_paths_is_rejected(self):
        paths = [f"{ROOT}/{i}.md" for i in range(VolumeDocumentSource.MAX_IMPORT_PATHS + 1)]
        with pytest.raises(ValidationError):
            VolumeDocumentSource(_FakeFiles()).plan(paths)

    def test_same_name_from_two_folders_is_flagged_not_overwritten(self):
        a, b = f"{ROOT}/x/r.md", f"{ROOT}/y/r.md"
        files = _FakeFiles(files={a: b"1", b: b"2"})
        docs, _ = VolumeDocumentSource(files).plan([a, b])
        assert docs[0].error == ""
        assert docs[1].error == "Another selected file has the same name"

    def test_duplicate_path_is_dropped(self):
        path = f"{ROOT}/a.md"
        docs, _ = VolumeDocumentSource(_FakeFiles(files={path: b"a"})).plan([path, path])
        assert len(docs) == 1


# -- plan: folders -------------------------------------------------


class TestPlanFolders:
    def test_folder_imports_supported_direct_children_only(self):
        files = _FakeFiles(
            dirs={
                ROOT: [
                    _file("b.pdf"),
                    _file("a.md"),
                    _file("skip.exe"),
                    _dir("nested"),
                ]
            }
        )
        docs, truncated = VolumeDocumentSource(files).plan([ROOT])
        assert [d.filename for d in docs] == ["a.md", "b.pdf"]
        assert all(not d.error for d in docs)
        assert truncated is False

    def test_folder_flags_oversize_children(self):
        files = _FakeFiles(dirs={ROOT: [_file("big.pdf", 11 * MB)]})
        docs, _ = VolumeDocumentSource(files).plan([ROOT])
        assert "10 MB" in docs[0].error

    def test_folder_without_supported_files_reports_it(self):
        files = _FakeFiles(dirs={ROOT: [_file("x.exe"), _dir("sub")]})
        docs, _ = VolumeDocumentSource(files).plan([ROOT])
        assert docs[0].error == "No supported documents directly inside this folder"

    def test_folder_import_is_capped(self):
        cap = VolumeDocumentSource.MAX_IMPORT_FILES
        children = [_file(f"doc{i:03d}.md") for i in range(cap + 7)]
        docs, truncated = VolumeDocumentSource(_FakeFiles(dirs={ROOT: children})).plan(
            [ROOT]
        )
        assert len(docs) == cap
        assert truncated is True

    def test_listing_truncation_is_reported(self):
        many = [_file(f"f{i}.md") for i in range(VolumeDocumentSource.MAX_BROWSE_ENTRIES + 1)]
        _docs, truncated = VolumeDocumentSource(_FakeFiles(dirs={ROOT: many})).plan(
            [ROOT]
        )
        assert truncated is True

    def test_folder_is_detected_when_file_head_answers_non_404(self):
        class _DirHead400(_FakeFiles):
            def head_file(self, path):
                return (400, None) if path in self.dirs else super().head_file(path)

        files = _DirHead400(dirs={ROOT: [_file("a.md")]})
        docs, _ = VolumeDocumentSource(files).plan([ROOT])
        assert [d.filename for d in docs] == ["a.md"]

    def test_files_api_exception_is_reported_per_path(self):
        class _Broken(_FakeFiles):
            def head_file(self, path):
                raise ConnectionError("boom")

        docs, _ = VolumeDocumentSource(_Broken()).plan([f"{ROOT}/a.md"])
        assert docs[0].error == "Could not reach the Unity Catalog Files API"


# -- browse --------------------------------------------------------


class TestBrowse:
    def test_lists_dirs_first_and_flags_files(self):
        files = _FakeFiles(
            dirs={ROOT: [_file("z.md"), _file("a.exe"), _file("big.pdf", 11 * MB), _dir("sub")]}
        )
        listing = VolumeDocumentSource(files).browse(ROOT + "/")
        names = [e["name"] for e in listing["items"]]
        assert names == ["sub", "a.exe", "big.pdf", "z.md"]
        by_name = {e["name"]: e for e in listing["items"]}
        assert by_name["a.exe"]["supported"] is False
        assert by_name["big.pdf"]["too_large"] is True
        assert by_name["z.md"]["supported"] is True
        assert listing["path"] == ROOT

    def test_forbidden_folder_raises_authorization_error(self):
        with pytest.raises(AuthorizationError):
            VolumeDocumentSource(_FakeFiles(status={ROOT: 403})).browse(ROOT)

    def test_missing_folder_raises_not_found(self):
        with pytest.raises(NotFoundError):
            VolumeDocumentSource(_FakeFiles()).browse(ROOT)

    def test_invalid_path_raises_validation_error(self):
        with pytest.raises(ValidationError):
            VolumeDocumentSource(_FakeFiles()).browse("/Volumes/main")


# -- read ----------------------------------------------------------


class TestRead:
    def test_returns_bytes(self):
        path = f"{ROOT}/a.md"
        source = VolumeDocumentSource(_FakeFiles(files={path: b"abc"}))
        assert source.read(VolumeDocument(path, "a.md")) == b"abc"

    def test_stream_over_limit_raises_even_when_metadata_lied(self):
        path = f"{ROOT}/a.pdf"
        source = VolumeDocumentSource(_FakeFiles(files={path: b"x" * 11}), max_bytes=10)
        with pytest.raises(ValidationError):
            source.read(VolumeDocument(path, "a.pdf", 5))

    def test_forbidden_read_raises_authorization_error(self):
        path = f"{ROOT}/a.md"
        source = VolumeDocumentSource(_FakeFiles(status={path: 403}))
        with pytest.raises(AuthorizationError):
            source.read(VolumeDocument(path, "a.md"))
