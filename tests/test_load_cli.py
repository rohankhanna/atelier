"""Tests for the 'atelier load' credential management subcommands."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from atelier.load_cli import _validate_path, load_main


class TestValidatePath:
    def test_valid_path(self):
        assert (
            _validate_path("llm/cloud-provider-a/accounts/primary/cloud-key")
            == "llm/cloud-provider-a/accounts/primary/cloud-key"
        )

    def test_valid_path_with_leading_slash(self):
        assert (
            _validate_path("/llm/cloud-provider-a/accounts/primary/cloud-key")
            == "llm/cloud-provider-a/accounts/primary/cloud-key"
        )

    def test_valid_path_with_trailing_slash(self):
        assert (
            _validate_path("llm/cloud-provider-a/accounts/primary/cloud-key/")
            == "llm/cloud-provider-a/accounts/primary/cloud-key"
        )

    def test_rejects_empty_path(self):
        try:
            _validate_path("")
            assert False, "should have raised"
        except ValueError as exc:
            assert "empty" in str(exc)

    def test_rejects_non_llm_path(self):
        try:
            _validate_path("other/provider/key")
            assert False, "should have raised"
        except ValueError as exc:
            assert "llm/" in str(exc)

    def test_rejects_dotdot(self):
        try:
            _validate_path("llm/../etc/passwd")
            assert False, "should have raised"
        except ValueError as exc:
            assert ".." in str(exc)

    def test_rejects_whitespace_only(self):
        try:
            _validate_path("   ")
            assert False, "should have raised"
        except ValueError as exc:
            assert "empty" in str(exc)


class TestLoadPut:
    def test_put_calls_wrapper_with_insert(self):
        mock_result = MagicMock(returncode=0)
        with (
            patch("sys.stdin", io.StringIO("secret-key-value\n")),
            patch(
                "atelier.load_cli.subprocess.run", return_value=mock_result
            ) as mock_run,
        ):
            rc = load_main(["put", "llm/cloud-provider-a/accounts/primary/cloud-key"])
        assert rc == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[:4] == [
            "sudo",
            "-u",
            "atelier",
            "/usr/local/bin/atelier-pass-wrapper",
        ]
        assert cmd[4:] == [
            "insert",
            "-m",
            "-f",
            "llm/cloud-provider-a/accounts/primary/cloud-key",
        ]
        assert mock_run.call_args[1]["input"] == "secret-key-value\n"

    def test_put_rejects_empty_stdin(self):
        with patch("sys.stdin", io.StringIO("")):
            rc = load_main(["put", "llm/cloud-provider-a/accounts/primary/cloud-key"])
        assert rc == 2

    def test_put_rejects_invalid_path(self):
        with patch("sys.stdin", io.StringIO("secret\n")):
            rc = load_main(["put", "other/path"])
        assert rc == 2

    def test_put_custom_wrapper_and_user(self):
        mock_result = MagicMock(returncode=0)
        with (
            patch("sys.stdin", io.StringIO("secret\n")),
            patch(
                "atelier.load_cli.subprocess.run", return_value=mock_result
            ) as mock_run,
        ):
            rc = load_main(
                [
                    "--wrapper",
                    "/custom/wrapper",
                    "--service-user",
                    "custom_user",
                    "put",
                    "llm/test/key",
                ]
            )
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[3] == "/custom/wrapper"
        assert cmd[2] == "custom_user"


class TestLoadRm:
    def test_rm_calls_wrapper_with_rm(self):
        mock_result = MagicMock(returncode=0)
        with patch(
            "atelier.load_cli.subprocess.run", return_value=mock_result
        ) as mock_run:
            rc = load_main(["rm", "llm/cloud-provider-a/accounts/primary/cloud-key"])
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[4:] == ["rm", "-f", "llm/cloud-provider-a/accounts/primary/cloud-key"]

    def test_rm_rejects_invalid_path(self):
        rc = load_main(["rm", "bad/path"])
        assert rc == 2


class TestLoadMv:
    def test_mv_calls_wrapper_with_mv(self):
        mock_result = MagicMock(returncode=0)
        with patch(
            "atelier.load_cli.subprocess.run", return_value=mock_result
        ) as mock_run:
            rc = load_main(["mv", "llm/old/path", "llm/new/path"])
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[4:] == ["mv", "-f", "llm/old/path", "llm/new/path"]

    def test_mv_rejects_invalid_source(self):
        rc = load_main(["mv", "bad/source", "llm/dest"])
        assert rc == 2

    def test_mv_rejects_invalid_dest(self):
        rc = load_main(["mv", "llm/source", "bad/dest"])
        assert rc == 2


class TestLoadCp:
    def test_cp_calls_wrapper_with_cp(self):
        mock_result = MagicMock(returncode=0)
        with patch(
            "atelier.load_cli.subprocess.run", return_value=mock_result
        ) as mock_run:
            rc = load_main(["cp", "llm/source", "llm/dest"])
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[4:] == ["cp", "-f", "llm/source", "llm/dest"]

    def test_cp_rejects_invalid_source(self):
        rc = load_main(["cp", "bad/source", "llm/dest"])
        assert rc == 2


class TestLoadPropagation:
    def test_nonzero_returncode_propagates(self):
        mock_result = MagicMock(returncode=1)
        with patch("atelier.load_cli.subprocess.run", return_value=mock_result):
            rc = load_main(["rm", "llm/test/key"])
        assert rc == 1
