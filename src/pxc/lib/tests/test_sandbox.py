from pxc.lib.sandbox import SandboxRuntimeError, stderr_collector


class TestStderrCollector:
    """Tests for the callback that gathers what a sandbox writes to stderr."""

    def test_decodes_a_character_split_across_chunks(self) -> None:
        chunks: list[str] = []
        collect = stderr_collector(chunks)
        encoded = "café".encode("utf-8")

        collect(encoded[:-1])
        collect(encoded[-1:])

        assert "".join(chunks) == "café"

    def test_replaces_invalid_bytes(self) -> None:
        chunks: list[str] = []

        stderr_collector(chunks)(b"a\xffb")

        assert "".join(chunks) == "a�b"


class TestSandboxRuntimeError:
    """Tests for how the error carries the sandbox's stderr."""

    def test_message_includes_stderr_and_attribute_holds_it_alone(self) -> None:
        err = SandboxRuntimeError("msg", "boom")

        assert "msg" in str(err)
        assert "boom" in str(err)
        assert err.stderr == "boom"

    def test_message_is_unchanged_without_stderr(self) -> None:
        assert str(SandboxRuntimeError("msg")) == "msg"
