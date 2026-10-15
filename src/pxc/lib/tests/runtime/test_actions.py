from pathlib import Path
from typing import Any

import pytest

from pxc.lib.actions import ActionValidationError
from pxc.lib.permission import Permission
from pxc.lib.sandbox import SandboxExecutor, SandboxRuntimeError

from .utils import create_manifest, make_activity_runtime


class FailingSandbox(SandboxExecutor):
    """A sandbox whose every call fails the way a throwing onAction does."""

    def call_function(self, function_name: str, *args: Any) -> bytes:
        raise SandboxRuntimeError("onAction threw", "boom from onAction")


class TestOnAction:
    """Tests for on_action method."""

    def test_raises_in_view_mode(self, tmp_path: Path) -> None:
        """Should raise ActionValidationError when permission is view."""
        manifest = create_manifest(
            actions={"answer.submit": {"type": "object", "properties": {}}}
        )
        ctx = make_activity_runtime(tmp_path, manifest, permission=Permission.view)

        with pytest.raises(ActionValidationError, match="view mode"):
            ctx.on_action("answer.submit", {})

    def test_raises_in_view_mode_regardless_of_action(self, tmp_path: Path) -> None:
        """Should raise for any action in view mode, even undeclared ones."""
        manifest = create_manifest()
        ctx = make_activity_runtime(tmp_path, manifest, permission=Permission.view)

        with pytest.raises(ActionValidationError, match="view mode"):
            ctx.on_action("any.action", {})

    def test_allowed_in_play_mode(self, tmp_path: Path) -> None:
        """Should not raise for declared action in play mode."""
        manifest = create_manifest(
            actions={"answer.submit": {"type": "object", "properties": {}}}
        )
        ctx = make_activity_runtime(tmp_path, manifest, permission=Permission.play)
        ctx.on_action("answer.submit", {})

    def test_swallows_and_logs_a_sandbox_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing sandbox call is logged and does not reach the caller."""
        manifest = create_manifest(
            actions={"answer.submit": {"type": "object", "properties": {}}}
        )
        ctx = make_activity_runtime(tmp_path, manifest, permission=Permission.play)
        ctx.sandbox = FailingSandbox(tmp_path / "sandbox.wasm", {})

        ctx.on_action("answer.submit", {})

        assert "onAction threw" in caplog.text


class TestOnActionOrRaise:
    """Tests for on_action_or_raise method."""

    def test_raises_a_sandbox_failure_with_its_stderr(self, tmp_path: Path) -> None:
        """The failure reaches the caller, stderr attached."""
        manifest = create_manifest(
            actions={"answer.submit": {"type": "object", "properties": {}}}
        )
        ctx = make_activity_runtime(tmp_path, manifest, permission=Permission.play)
        ctx.sandbox = FailingSandbox(tmp_path / "sandbox.wasm", {})

        with pytest.raises(SandboxRuntimeError) as raised:
            ctx.on_action_or_raise("answer.submit", {})

        assert raised.value.stderr == "boom from onAction"
