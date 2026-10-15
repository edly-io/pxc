import codecs
import logging
from pathlib import Path
from typing import Any, Callable

import wasmtime
import wasmtime.component

logger = logging.getLogger(__file__)


class SandboxRuntimeError(RuntimeError):
    """
    Raised whenever there is an error in a sandbox executor at runtime.

    ``stderr`` holds what the sandbox wrote to stderr during the failing call, or "" if nothing.
    When it is not empty, it is also appended to the error message, so logging the error shows it.
    """

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}\nSandbox stderr:\n{stderr}" if stderr else message)
        self.stderr = stderr


def stderr_collector(chunks: list[str]) -> Callable[[bytes], int]:
    """
    Return a callback that decodes sandbox stderr output and appends it to ``chunks``.

    The decoder is incremental, so a multi-byte character split across two writes is
    not garbled; invalid bytes are replaced.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def collect(data: bytes) -> int:
        chunks.append(decoder.decode(data))
        return len(data)

    return collect


class ForbiddenImportError(ValueError):
    """
    Raised when a WASM component imports a forbidden interface (e.g. wasi:http).
    """


class RecordArg:
    """
    This is used to convert from dict arguments/return values to WIT's 'record' type.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class SandboxExecutor:
    """
    Abstract base class implementation for all sandbox executors.

    ``host_functions`` is keyed by WIT interface name (e.g. "state",
    "grading") mapping to kebab-case host function names.
    """

    def __init__(
        self,
        plugin_path: Path,
        host_functions: dict[str, dict[str, Callable[..., Any]]],
    ) -> None:
        self._plugin_path = plugin_path
        self._host_functions = host_functions

    def call_function(self, function_name: str, *args: Any) -> bytes:
        raise NotImplementedError


class SandboxComponentExecutor(SandboxExecutor):
    """
    Sandboxed WASM Component Model execution via wasmtime.
    """

    # Set to -1 to disable memory limits
    # We need at least 20MB for Python and 10MB for JS activities
    MEMORY_LIMIT_BYTES: int = 20 * 10**6

    def __init__(
        self,
        plugin_path: Path,
        host_functions: dict[str, dict[str, Callable[..., Any]]],
    ) -> None:
        super().__init__(plugin_path, host_functions)

        self._plugin_path = plugin_path
        self._host_functions = host_functions

    def _create_store_instance(
        self, stderr: list[str]
    ) -> tuple[wasmtime.Store, wasmtime.component.Instance]:
        """
        Create a wasmtime store, engine with wasi features, component, etc.

        What the sandbox writes to stderr is appended to ``stderr`` as text and not forwarded anywhere.
        """
        engine = create_engine()

        # Create WASI config
        wasi_config = wasmtime.WasiConfig()
        wasi_config.inherit_stdout()
        wasi_config.stderr_custom = stderr_collector(stderr)

        # Create store
        store = wasmtime.Store(engine)
        store.set_limits(memory_size=self.MEMORY_LIMIT_BYTES)
        store.set_wasi(wasi_config)

        # Create linker
        linker = wasmtime.component.Linker(store.engine)
        linker.add_wasip2()

        # Create component
        component = load_component(store.engine, self._plugin_path)

        # Register host functions, one wasmtime instance per WIT interface.
        with linker.root() as root:
            for interface_name, funcs in self._host_functions.items():
                with root.add_instance(f"pxc:sandbox/{interface_name}") as ctx:
                    for wit_name, func in funcs.items():
                        ctx.add_func(wit_name, make_host_function(func))

        # Create instance
        instance = linker.instantiate(store, component)

        return store, instance

    def call_function(self, function_name: str, *args: Any) -> bytes:
        """
        Call an exported function on the WASM component.

        A SandboxRuntimeError raised while running the function carries what the sandbox wrote
        to stderr.
        """
        stderr: list[str] = []
        try:
            store, instance = self._create_store_instance(stderr)
        except wasmtime.WasmtimeError as e:
            raise SandboxRuntimeError(
                f"Component {self._plugin_path}: failed to create instance"
            ) from e
        func = instance.get_func(store, function_name)
        if func is None:
            raise SandboxRuntimeError(
                f"Component {self._plugin_path}: export '{function_name}' not found"
            )
        try:
            result = call_sandbox_function(store, func, *args)
        except wasmtime.WasmtimeError as e:
            raise SandboxRuntimeError(
                f"Component {self._plugin_path}: error running '{function_name}' with arguments: {args}",
                "".join(stderr),
            ) from e
        except Exception as e:
            raise SandboxRuntimeError(
                f"Component {self._plugin_path}: error running '{function_name}'",
                "".join(stderr),
            ) from e
        return result.encode("utf-8") if isinstance(result, str) else b""


def create_engine() -> wasmtime.Engine:
    # Create config
    engine_config = wasmtime.Config()
    # engine_config.cache = True # Cache to directory. Do we need this?

    # Create engine
    engine = wasmtime.Engine(engine_config)
    return engine


def load_component(
    engine: wasmtime.Engine, plugin_path: Path
) -> wasmtime.component.Component:
    """
    Load a component and cache the result in a <plugin_path>.bin file. Automatically
    load this file if it's more recent than the plugin file. Else, update it.
    """
    bin_path = plugin_path.parent / (plugin_path.name + ".bin")
    load_cache = (
        bin_path.exists() and bin_path.stat().st_mtime > plugin_path.stat().st_mtime
    )
    if load_cache:
        try:
            # Load serialized file
            return wasmtime.component.Component.deserialize_file(engine, str(bin_path))
        except wasmtime.WasmtimeError as e:
            # This might happen for files that were serialized with a different version.
            # In such cases we need to re-serialize it.
            logger.exception(e)
            logger.warning("Failed to load serialized file, serializing again")

    # Create new component and serialize
    component = wasmtime.component.Component.from_file(engine, str(plugin_path))
    bin_path.write_bytes(component.serialize())
    return component


def make_host_function(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Convert our host functions to a format that is understandable by wasmtime.

    1. All host functions take a Store as a first argument
    2. We need to convert Record args to dict
    """

    def host_function(_store: wasmtime.Store, *args: Any) -> Any:
        func_args = []
        for arg in args:
            # Convert Record args to dict
            if isinstance(arg, wasmtime.component.Record):
                arg_dict = arg.__dict__.copy()
                func_args.append(arg_dict)
            else:
                func_args.append(arg)
        result = func(*func_args)
        if isinstance(result, dict):
            return RecordArg(result)
        return result

    return host_function


def call_sandbox_function(
    store: wasmtime.Store, func: wasmtime.component.Func, *args: Any
) -> Any:
    """
    To call a sandbox function we must:

    1. add the store as the first argument
    2. convert dict arguments to records
    3. call "post_return" after every call, to avoid memory leaks
    """
    func_args = []
    for arg in args:
        if isinstance(arg, dict):
            record_arg = RecordArg(arg)
            func_args.append(record_arg)
        else:
            func_args.append(arg)
    try:
        return func(store, *func_args)
    finally:
        func.post_return(store)


def get_sandbox_executor(
    plugin_path: Path,
    host_functions: dict[str, dict[str, Callable[..., Any]]],
) -> SandboxExecutor:
    """
    Return a sandbox executor for this plugin.
    """
    return SandboxComponentExecutor(plugin_path, host_functions)
