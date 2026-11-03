"""Tests for back.core.databricks.uc.VolumeFileService."""

import pytest
from unittest.mock import MagicMock

from back.core.databricks.DatabricksAuth import DatabricksAuth
from back.core.databricks.uc import VolumeFileService


@pytest.fixture
def clean_databricks_env(monkeypatch):
    for key in (
        "DATABRICKS_APP_PORT",
        "DATABRICKS_TOKEN",
        "DATABRICKS_HOST",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


class TestVolumeFileServiceInit:
    def test_host_token(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        assert svc._auth.host == "https://h.com"
        assert svc._auth.token == "tok"

    def test_auth_kwarg(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        assert svc._auth is auth


class TestIsConfigured:
    def test_true_with_valid_auth(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        assert svc.is_configured() is True

    def test_false_without_auth(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="")
        svc = VolumeFileService(auth=auth)
        assert svc.is_configured() is False


class TestGetVolumePath:
    def test_static_path(self):
        assert VolumeFileService.get_volume_path("c", "s", "v") == "/Volumes/c/s/v"


class TestListFiles:
    def test_not_configured(self, clean_databricks_env):
        svc = VolumeFileService(auth=DatabricksAuth(host="https://h.com", token=""))
        ok, files, msg = svc.list_files("c", "s", "v")
        assert ok is False
        assert files == []
        assert "configuration missing" in msg

    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "contents": [
                    {
                        "name": "a.txt",
                        "is_directory": False,
                        "file_size": 5,
                    }
                ]
            },
        )
        ok, files, msg = svc.list_files("c", "s", "v")
        assert ok is True
        assert len(files) == 1
        assert files[0]["name"] == "a.txt"
        assert "Found" in msg

    def test_404(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=404)
        ok, files, msg = svc.list_files("c", "s", "missing")
        assert ok is False
        assert files == []
        assert "not found" in msg.lower()


class TestListDirectory:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "contents": [
                    {"name": "d/", "is_directory": True},
                    {"name": "f.txt", "is_directory": False},
                ]
            },
        )
        ok, items, msg = svc.list_directory("/Volumes/c/s/v")
        assert ok is True
        assert len(items) == 2

    def test_dirs_only(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "contents": [
                    {"name": "d/", "is_directory": True},
                    {"name": "f.txt", "is_directory": False},
                ]
            },
        )
        ok, items, _ = svc.list_directory("/Volumes/c/s/v", dirs_only=True)
        assert ok is True
        assert len(items) == 1
        assert items[0]["is_directory"] is True


class TestReadFile:
    def test_not_configured(self, clean_databricks_env):
        svc = VolumeFileService(auth=DatabricksAuth(host="https://h.com", token=""))
        ok, content, msg = svc.read_file("/Volumes/c/s/v/f.txt")
        assert ok is False
        assert content == ""
        assert "configuration missing" in msg

    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=200, text="hello")
        ok, content, msg = svc.read_file("/Volumes/c/s/v/f.txt")
        assert ok is True
        assert content == "hello"
        assert "success" in msg.lower()

    def test_404(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=404)
        ok, content, msg = svc.read_file("/missing")
        assert ok is False
        assert "not found" in msg.lower()

    def test_403(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=403)
        ok, content, msg = svc.read_file("/secret")
        assert ok is False
        assert "denied" in msg.lower()


class TestReadBinaryFile:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=200, content=b"\x00\xff")
        ok, data, msg = svc.read_binary_file("/Volumes/c/s/v/bin")
        assert ok is True
        assert data == b"\x00\xff"

    def test_404(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.get.return_value = MagicMock(status_code=404)
        ok, data, msg = svc.read_binary_file("/nope")
        assert ok is False
        assert data == b""


class TestWriteFile:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.put.return_value = MagicMock(status_code=200, text="")
        ok, msg = svc.write_file("/Volumes/c/s/v/out.txt", "text")
        assert ok is True
        assert "saved" in msg.lower()

    def test_failure(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.put.return_value = MagicMock(status_code=400, text="bad")
        ok, msg = svc.write_file("/Volumes/c/s/v/out.txt", "text")
        assert ok is False
        assert "400" in msg


class TestWriteBinaryFile:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.put.return_value = MagicMock(status_code=204, text="")
        ok, msg = svc.write_binary_file("/Volumes/c/s/v/b.bin", b"\x01")
        assert ok is True
        assert "saved" in msg.lower()
        put_url = svc._session.put.call_args[0][0]
        assert "/api/2.0/fs/files" in put_url
        assert "Volumes" in put_url


class TestCreateDirectory:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.put.return_value = MagicMock(status_code=200)
        ok, msg = svc.create_directory("/Volumes/c/s/v/domains/p/documents")
        assert ok is True
        assert "documents" in msg

    def test_failure(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.put.return_value = MagicMock(status_code=403, text="no")
        ok, msg = svc.create_directory("/Volumes/c/s/v/doc")
        assert ok is False
        assert "403" in msg


class TestDeleteFile:
    def test_success(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.delete.return_value = MagicMock(status_code=204)
        ok, msg = svc.delete_file("/Volumes/c/s/v/f.txt")
        assert ok is True
        assert "deleted" in msg.lower()

    def test_404(self, clean_databricks_env):
        auth = DatabricksAuth(host="https://h.com", token="tok")
        svc = VolumeFileService(auth=auth)
        svc._session = MagicMock()
        svc._session.delete.return_value = MagicMock(status_code=404)
        ok, msg = svc.delete_file("/missing")
        assert ok is False
        assert "not found" in msg.lower()


class TestParseDirectoryContents:
    def test_extension_filter(self, clean_databricks_env):
        data = {
            "contents": [
                {"name": "a.txt", "is_directory": False},
                {"name": "b.pdf", "is_directory": False},
            ]
        }
        items = VolumeFileService._parse_directory_contents(
            data, "/Volumes/c/s/v", extensions=[".txt"]
        )
        assert len(items) == 1
        assert items[0]["name"] == "a.txt"

    def test_dirs_only(self, clean_databricks_env):
        data = {
            "contents": [
                {"name": "sub/", "is_directory": True},
                {"name": "f.txt", "is_directory": False},
            ]
        }
        items = VolumeFileService._parse_directory_contents(
            data, "/Volumes/c/s/v", dirs_only=True
        )
        assert len(items) == 1
        assert items[0]["name"] == "sub"


def _response(status=200, *, headers=None, payload=None, chunks=None):
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.json.return_value = payload or {}
    resp.iter_content.return_value = iter(chunks or [])
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _page(names, token=None):
    payload = {
        "contents": [{"name": n, "is_directory": False, "file_size": 1} for n in names]
    }
    if token:
        payload["next_page_token"] = token
    return _response(payload=payload)


class TestHeadFile:
    def test_returns_status_and_content_length(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.head.return_value = _response(headers={"content-length": "42"})
        assert svc.head_file("/Volumes/c/s/v/a.md") == (200, 42)
        url = svc._session.head.call_args.args[0]
        assert url == "https://h.com/api/2.0/fs/files/Volumes/c/s/v/a.md"

    def test_missing_length_is_none(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.head.return_value = _response(404)
        assert svc.head_file("/Volumes/c/s/v/a.md") == (404, None)


class TestListDirectoryPage:
    def test_follows_page_tokens_until_cap(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.get.side_effect = [_page(["a", "b"], "t1"), _page(["c", "d"], "t2")]
        status, items, truncated = svc.list_directory_page("/Volumes/c/s/v", 3)
        assert status == 200
        assert [i["name"] for i in items] == ["a", "b", "c"]
        assert truncated is True
        assert svc._session.get.call_args.kwargs["params"] == {"page_token": "t1"}

    def test_last_page_is_not_truncated(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.get.return_value = _page(["a"])
        assert svc.list_directory_page("/Volumes/c/s/v", 3)[2] is False

    def test_error_status_is_returned(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.get.return_value = _response(403)
        assert svc.list_directory_page("/Volumes/c/s/v", 10) == (403, [], False)


class TestReadBinaryFileCapped:
    def test_returns_bytes_under_cap(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.get.return_value = _response(chunks=[b"ab", b"c"])
        assert svc.read_binary_file_capped("/Volumes/c/s/v/a", 10) == (200, b"abc")
        assert svc._session.get.call_args.kwargs["stream"] is True

    def test_stops_reading_past_cap(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        consumed = []

        def chunks(**_kw):
            for chunk in (b"12345", b"67890", b"never"):
                consumed.append(chunk)
                yield chunk

        resp = _response()
        resp.iter_content.side_effect = chunks
        svc._session.get.return_value = resp
        assert svc.read_binary_file_capped("/Volumes/c/s/v/a", 8) == (200, None)
        assert consumed == [b"12345", b"67890"]

    def test_error_status(self, clean_databricks_env):
        svc = VolumeFileService(host="https://h.com", token="tok")
        svc._session = MagicMock()
        svc._session.get.return_value = _response(404)
        assert svc.read_binary_file_capped("/Volumes/c/s/v/a", 8) == (404, None)


class TestUserTokenInAppMode:
    """In Databricks Apps the SP credentials are always present; an explicit
    (forwarded user) token must still be the bearer for Files API calls."""

    @pytest.fixture
    def app_env(self, clean_databricks_env, monkeypatch):
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        monkeypatch.setattr(DatabricksAuth, "get_oauth_token", lambda self: "SP_TOKEN")

    def test_user_token_is_the_bearer_not_the_sp(self, app_env):
        svc = VolumeFileService(host="https://h.com", token="USER_TOKEN")
        svc._session = MagicMock()
        svc._session.head.return_value = _response(headers={"content-length": "1"})
        svc._session.get.side_effect = [_page(["a.md"]), _response(chunks=[b"x"])]
        svc.head_file("/Volumes/c/s/v/a.md")
        svc.list_directory_page("/Volumes/c/s/v", 10)
        svc.read_binary_file_capped("/Volumes/c/s/v/a.md", 10)
        calls = [svc._session.head.call_args, *svc._session.get.call_args_list]
        assert [c.kwargs["headers"]["Authorization"] for c in calls] == [
            "Bearer USER_TOKEN"
        ] * 3
