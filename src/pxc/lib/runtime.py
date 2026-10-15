"""
Host functions for sandboxed WASM plugins.

These functions are injected into the plugin runtime and can be called
by WebAssembly code. They provide controlled access to server capabilities.
"""

from collections.abc import Callable
import json
import logging
from pathlib import Path
from typing import Any, TypedDict
from time import time
import urllib.error
import urllib.parse
import urllib.request

from pxc.lib.actions import ActionChecker, ActionValidationError
from pxc.lib.capabilities import CapabilityChecker, CapabilityError, InterfaceName
from pxc.lib.events import EventChecker
from pxc.lib.field_store import FieldStore
from pxc.lib.file_storage import FileStorage, FileStorageError
from pxc.lib.permission import Permission
from pxc.lib.fields import FieldChecker, FieldType, FieldValidationError
from pxc.lib.manifest_types import LogField, Scope, PxcActivityManifest
from pxc.lib.sandbox import SandboxExecutor, SandboxRuntimeError, get_sandbox_executor

logger = logging.getLogger(__file__)


HostContext = TypedDict(
    "HostContext",
    {
        "activity_id": str,
        "course_id": str,
        "user_id": str,
    },
    total=False,
)
SandboxContext = TypedDict(
    "SandboxContext",
    {
        "activity-id": str | None,
        "course-id": str | None,
        "user-id": str | None,
    },
)


class PendingEvent(TypedDict):
    name: str
    value: str
    context: HostContext
    permission: str


def sandbox_to_host_context(sandbox_context: SandboxContext) -> HostContext:
    host_context: HostContext = {}
    if sandbox_context["activity-id"] is not None:
        host_context["activity_id"] = sandbox_context["activity-id"]
    if sandbox_context["course-id"] is not None:
        host_context["course_id"] = sandbox_context["course-id"]
    if sandbox_context["user-id"] is not None:
        host_context["user_id"] = sandbox_context["user-id"]
    return host_context


class AssetAccessError(Exception):
    """Raised when attempting to access an activity asset that does not exist or without the right permissions"""


class ActivityRuntime:

    # Valid override keys for each scope
    _VALID_SCOPE_KEYS: dict[Scope, set[str]] = {
        Scope.activity: {"course_id", "activity_id"},
        Scope.user_activity: {"course_id", "activity_id", "user_id"},
        Scope.course: {"course_id"},
        Scope.user_course: {"course_id", "user_id"},
        Scope.global_: set(),
        Scope.user_global: {"user_id"},
    }

    def __init__(
        self,
        activity_dir: Path,
        field_store: FieldStore,
        file_storage: FileStorage,
        activity_id: str,
        course_id: str,
        user_id: str,
        permission: Permission,
    ) -> None:
        self._activity_dir = activity_dir
        self._file_storage = file_storage
        self._user_id: str = user_id
        self._permission: Permission = permission
        self._course_id: str = course_id
        self._activity_id: str = activity_id

        # Field storage backend
        self.field_store: FieldStore = field_store

        # Events posted by sandbox during execution
        self._pending_events: list[PendingEvent] = []

        # Manifest capabilities and fields (validated by Pydantic)
        # TODO we don't need to validate the manifest for every action
        with open(self._activity_dir / "manifest.json", encoding="utf8") as f:
            self.manifest = PxcActivityManifest.model_validate_json(f.read())
        self.capability_checker = CapabilityChecker(self.manifest.capabilities)
        self.field_checker = FieldChecker(self.manifest.fields)
        self.action_checker = ActionChecker(self.manifest.actions)
        self.event_checker = EventChecker(self.manifest.events)

        # Sandboxed code
        self.sandbox: SandboxExecutor | None = None
        sandbox_path = self.manifest.sandbox
        if sandbox_path is not None:
            # Note: we know that this is a safe path thanks to path constraints in the manifests
            wasm_path = self._activity_dir / sandbox_path
            self.sandbox = get_sandbox_executor(wasm_path, self.host_functions())

        # Storage directories are created on demand by storage_write.

    @property
    def user_id(self) -> str:
        return self._user_id

    @user_id.setter
    def user_id(self, value: str) -> None:
        self._user_id = value

    @property
    def course_id(self) -> str:
        return self._course_id

    @course_id.setter
    def course_id(self, value: str) -> None:
        self._course_id = value

    @property
    def activity_id(self) -> str:
        return self._activity_id

    @activity_id.setter
    def activity_id(self, value: str) -> None:
        self._activity_id = value

    @property
    def permission(self) -> Permission:
        return self._permission

    @permission.setter
    def permission(self, value: Permission) -> None:
        self._permission = value

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def ui_path(self) -> str:
        """
        Path to UI script, relative to activity directory.

        Note that this is a safe path thanks to schema constraints.
        """
        return self.manifest.ui

    def host_functions(self) -> dict[str, dict[str, Callable[..., Any]]]:
        """
        Host functions that will be made available to the sandbox, grouped by
        WIT interface name.

        The outer dict is keyed by interface (e.g. "state", "grading"); the
        inner dict is keyed by kebab-case function name matching pxc.wit.
        Interfaces beyond `state` are only included when declared in the
        manifest capabilities.
        """
        interfaces: dict[str, dict[str, Callable[..., Any]]] = {
            InterfaceName.state.value: {
                "send-event": self.send_event,
                "get-field": self.get_field,
                "set-field": self.set_field,
                "log-get": self.log_get,
                "log-get-after": self.log_get_after,
                "log-get-before": self.log_get_before,
                "log-append": self.log_append,
                "log-delete": self.log_delete,
                "log-delete-before": self.log_delete_before,
                "log-clear": self.log_clear,
                "get-usernames": self.get_usernames,
            },
        }
        if self.capability_checker.is_interface_requested(InterfaceName.grading):
            interfaces[InterfaceName.grading.value] = {
                "report-completed": self.report_completed,
                "report-passed": self.report_passed,
                "report-failed": self.report_failed,
                "report-progressed": self.report_progressed,
                "report-scored": self.report_scored,
            }
        if self.capability_checker.is_interface_requested(InterfaceName.http):
            interfaces[InterfaceName.http.value] = {
                "http-request": self.http_request,
            }
        if self.capability_checker.is_interface_requested(InterfaceName.storage):
            interfaces[InterfaceName.storage.value] = {
                "storage-read": self.storage_read,
                "storage-exists": self.storage_exists,
                "storage-url": self.storage_url,
                "storage-list": self.storage_list,
                "storage-write": self.storage_write,
                "storage-delete": self.storage_delete,
            }
        return interfaces

    def get_ui_path(self) -> Path:
        full_path = self._activity_dir / self.manifest.ui
        if not full_path.exists() or not full_path.is_file():
            raise AssetAccessError("UI script does not exist")
        return full_path

    def get_asset_path(self, file_path: str) -> Path:
        full_path = self._activity_dir / file_path
        try:
            full_path.resolve().relative_to(self._activity_dir.resolve())
        except ValueError as e:
            raise AssetAccessError("Incorrect path") from e

        if file_path not in [item.root for item in (self.manifest.assets or [])]:
            raise AssetAccessError("Undeclared asset")

        if not full_path.exists() or not full_path.is_file():
            raise AssetAccessError("Asset does not exist")

        return full_path

    def load_field(
        self,
        activity_id: str,
        course_id: str,
        user_id: str,
        name: str,
    ) -> FieldType:
        """Get a declared field.

        Returns the stored value, or the default if not set.

        Raises:
            FieldValidationError: If the field is not declared in manifest.
        """
        # Check that field is declared (raises if not)
        default = self.field_checker.get_default(name)

        stored = self.field_store.get(course_id, self.name, activity_id, user_id, name)
        if stored is None:
            return default
        return stored

    def store_field(
        self,
        course_id: str,
        activity_id: str,
        user_id: str,
        name: str,
        value: FieldType,
    ) -> None:
        """Set a declared field.

        Raises:
            FieldValidationError: If the field is not declared or invalid.
        """
        # Validate against manifest definition
        self.field_checker.validate(name, value)

        self.field_store.set(course_id, self.name, activity_id, user_id, name, value)

    def _scope_key_segments(
        self, scope: Scope, sandbox_context: SandboxContext | None = None
    ) -> tuple[str, str, str]:
        """Return (activity_id, course_id, user_id) key segments for a scope.

        Args:
            scope: The field scope.
            overrides: Optional dict of dimension overrides (e.g. {"user_id": "bob"}).

        Raises:
            FieldValidationError: If an override key is not valid for the scope.
        """
        overrides = sandbox_to_host_context(sandbox_context) if sandbox_context else {}

        if overrides:
            valid_keys = self._VALID_SCOPE_KEYS[scope]
            invalid = set(overrides.keys()) - valid_keys
            if invalid:
                raise FieldValidationError(
                    f"Invalid scope override keys {invalid} for scope '{scope.value}'. "
                    f"Valid keys: {valid_keys}"
                )

        overrides = overrides or {}
        scope_map: dict[Scope, tuple[str, str, str]] = {
            Scope.activity: (
                overrides.get("activity_id", self._activity_id),
                overrides.get("course_id", self._course_id),
                "",
            ),
            Scope.user_activity: (
                overrides.get("activity_id", self._activity_id),
                overrides.get("course_id", self._course_id),
                overrides.get("user_id", self._user_id),
            ),
            Scope.course: (
                "",
                overrides.get("course_id", self._course_id),
                "",
            ),
            Scope.user_course: (
                "",
                overrides.get("course_id", self._course_id),
                overrides.get("user_id", self._user_id),
            ),
            Scope.global_: ("", "", ""),
            Scope.user_global: (
                "",
                "",
                overrides.get("user_id", self._user_id),
            ),
        }
        return scope_map[scope]

    def _log_scope_segments(
        self, name: str, context: SandboxContext | None
    ) -> tuple[str, str, str]:
        """Validate log field and return scope key segments."""
        self.field_checker.require_log_type(name)
        field_scope = self.field_checker.get_scope(name)
        return self._scope_key_segments(field_scope, context)

    def get_all_fields(self) -> dict[str, FieldType]:
        """Get all declared fields for a user.

        Returns:
            A dict of field names to their current values.
        """
        result: dict[str, FieldType] = {}
        for name in self.field_checker.field_names:
            if isinstance(self.field_checker.get_definition(name), LogField):
                continue
            scope = self.field_checker.get_scope(name)
            activity_id, course_id, user_id = self._scope_key_segments(scope)
            result[name] = self.load_field(activity_id, course_id, user_id, name)
        return result

    def clear_pending_events(self) -> list[PendingEvent]:
        """Return and clear all pending events."""
        events = self._pending_events
        self._pending_events = []
        return events

    def get_state(self) -> dict[str, FieldType]:
        """Get the activity state to send to the client.

        If the sandbox exports a get-state function, calls it with a
        ``{context, permission}`` input dict and returns the result.
        Otherwise falls back to returning all fields.
        """
        if self.sandbox is None:
            return self.get_all_fields()
        try:
            result = self.sandbox.call_function(
                "get-state",
                SandboxContext(
                    {
                        "activity-id": self._activity_id,
                        "course-id": self._course_id,
                        "user-id": self._user_id,
                    }
                ),
                self._permission.value,
            )
        except SandboxRuntimeError as e:
            # TODO we should prevent displaying the activity in the frontend
            logger.exception(e)
            raise
        state: dict[str, FieldType] = json.loads(result)
        return state

    def on_action_or_raise(self, action_name: str, action_value: Any) -> None:
        """
        Call the sandbox on-action callback.

        The sandbox receives the action name, the action value, a context dict
        with the current identifiers (user_id, course_id, activity_id),
        and the current permission level.

        Raise ActionValidationError if the action is not valid and SandboxRuntimeError if
        the sandbox fails, with the error's ``stderr`` holding what the sandbox wrote. Hosts
        that report failures to their users call it.
        """
        if self._permission == Permission.view:
            raise ActionValidationError("Actions are not allowed in view mode")
        self.action_checker.validate(action_name, action_value)
        if self.sandbox is not None:
            time_start = time()
            self.sandbox.call_function(
                "on-action",
                action_name,
                json.dumps(action_value),
                SandboxContext(
                    {
                        "activity-id": self._activity_id,
                        "course-id": self._course_id,
                        "user-id": self._user_id,
                    }
                ),
                self._permission.value,
            )
            time_end = time()
            logger.info(
                "Activity ID=%s call to 'on-action' took %d ms",
                self.activity_id,
                (time_end - time_start) * 1000,
            )

    def on_action(self, action_name: str, action_value: Any) -> None:
        """
        Call the sandbox on-action callback.

        The sandbox receives the action name, the action value, a context dict
        with the current identifiers (user_id, course_id, activity_id),
        and the current permission level.

        Raise ActionValidationError if the action is not valid. A sandbox failure is
        logged and swallowed.
        """
        try:
            self.on_action_or_raise(action_name, action_value)
        except SandboxRuntimeError as e:
            logger.exception(e)

    def send_event(
        self, name: str, value: str, context: SandboxContext | None, permission: str
    ) -> str:
        """Send an event back to the client.

        Called by sandbox code to send events (e.g., field changes) to the frontend.

        Args:
            name: Event name.
            value: JSON-encoded event value.
            context: JSON-encoded context dict (e.g. '{"activity_id": "..."}').
                     Empty dict {} means use current context.
            permission: Minimum permission to receive the event ("view", "play", or "edit").

        Raises:
            EventValidationError: If the event is not declared in manifest.
        """
        self.event_checker.validate(name, json.loads(value))
        if context:
            host_context = sandbox_to_host_context(context)
        else:
            # By default, send event to all users
            host_context = HostContext(
                {
                    "activity_id": self._activity_id,
                    "course_id": self._course_id,
                }
            )
        self._pending_events.append(
            {
                "name": name,
                "value": value,
                "context": host_context,
                "permission": permission,
            }
        )
        # We have to return something, otherwise wasmtime complains with "TypeError:
        # expected Variant type"
        return ""

    def get_usernames(self, ids: list[str]) -> list[tuple[str, str]]:
        """Resolve user IDs to display names.

        By default returns (id, id) for every requested ID. Subclasses may
        override this to look up human-readable names from a user directory.
        """
        return [(uid, uid) for uid in ids]

    def get_field(self, name: str, context: SandboxContext | None = None) -> str:
        """Get a field, resolving scope from manifest.

        Args:
            name: Field name.
            context: JSON-encoded dict of context overrides (e.g. '{"user_id": "bob"}').

        Returns JSON-encoded value (e.g., "42" for integer, "true" for boolean).
        Returns the default value if not set.
        """
        if isinstance(self.field_checker.get_definition(name), LogField):
            raise FieldValidationError(
                f"Field '{name}' is of type 'log'; use log_get/log_get_after/log_get_before instead"
            )
        field_scope = self.field_checker.get_scope(name)
        activity_id, course_id, user_id = self._scope_key_segments(field_scope, context)
        value = self.load_field(activity_id, course_id, user_id, name)
        return json.dumps(value)

    def set_field(
        self, name: str, value: str, context: SandboxContext | None = None
    ) -> bool:
        """Set a field, resolving scope from manifest. Takes JSON-encoded value.

        Args:
            name: Field name.
            value: JSON-encoded value.
            context: JSON-encoded dict of context overrides.
        """
        if isinstance(self.field_checker.get_definition(name), LogField):
            raise FieldValidationError(
                f"Field '{name}' is of type 'log'; use log_append instead"
            )
        parsed_value: FieldType = json.loads(value)
        field_scope = self.field_checker.get_scope(name)
        activity_id, course_id, user_id = self._scope_key_segments(field_scope, context)
        self.store_field(course_id, activity_id, user_id, name, parsed_value)
        return True

    def log_get(
        self, name: str, entry_id: int, context: SandboxContext | None = None
    ) -> str:
        """Get a single log entry by id.

        Returns JSON-encoded value if found, JSON null otherwise.
        """
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        value = self.field_store.log_get(
            course_id, self.name, activity_id, user_id, name, entry_id
        )
        return json.dumps(value)

    def log_get_after(
        self,
        name: str,
        after_id: int | None,
        count: int,
        context: SandboxContext | None = None,
    ) -> str:
        """Get up to `count` log entries with id > after_id, ascending.

        If after_id is None, start from the oldest entry. Returns JSON-encoded
        list of {id, value} dicts.
        """
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        result = self.field_store.log_get_after(
            course_id, self.name, activity_id, user_id, name, after_id, count
        )
        return json.dumps(result)

    def log_get_before(
        self,
        name: str,
        before_id: int | None,
        count: int,
        context: SandboxContext | None = None,
    ) -> str:
        """Get up to `count` log entries with id < before_id, descending.

        If before_id is None, start from the newest entry. Returns JSON-encoded
        list of {id, value} dicts (newest first).
        """
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        result = self.field_store.log_get_before(
            course_id, self.name, activity_id, user_id, name, before_id, count
        )
        return json.dumps(result)

    def log_append(
        self, name: str, value: str, context: SandboxContext | None = None
    ) -> int:
        """Append a value to a log field. Returns the assigned id."""
        parsed_value: FieldType = json.loads(value)
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        self.field_checker.validate_log_item(name, parsed_value)
        return self.field_store.log_append(
            course_id, self.name, activity_id, user_id, name, parsed_value
        )

    def log_delete(
        self, name: str, entry_id: int, context: SandboxContext | None = None
    ) -> bool:
        """Delete a single log entry by id. Returns True if the entry existed."""
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        return self.field_store.log_delete(
            course_id, self.name, activity_id, user_id, name, entry_id
        )

    def log_delete_before(
        self, name: str, before_id: int, context: SandboxContext | None = None
    ) -> int:
        """Delete all log entries with id < before_id. Returns count deleted."""
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        return self.field_store.log_delete_before(
            course_id, self.name, activity_id, user_id, name, before_id
        )

    def log_clear(self, name: str, context: SandboxContext | None = None) -> int:
        """Delete all entries in a log field. Returns count deleted."""
        activity_id, course_id, user_id = self._log_scope_segments(name, context)
        return self.field_store.log_clear(
            course_id, self.name, activity_id, user_id, name
        )

    def http_request(
        self, url: str, method: str, body: str, headers: list[tuple[str, str]]
    ) -> str:
        """Make an HTTP request.

        Returns a JSON string: {"status": int, "headers": [[k,v],...], "body": str}

        - 2xx: returns status, headers, body
        - 4xx/5xx: returns status, headers, error body (no exception)
        - Connection error: returns status=0, empty headers, error message
        - Capability error: returns status=0, empty headers, error message
        """
        try:
            self.capability_checker.check_http_request(url)
        except CapabilityError as e:
            return json.dumps({"status": 0, "headers": [], "body": str(e)})

        body_bytes = body.encode("utf-8") if body else None

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=dict(headers),
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                resp_headers = list(response.getheaders())
                resp_body: str = response.read().decode("utf-8")
                return json.dumps(
                    {
                        "status": response.status,
                        "headers": resp_headers,
                        "body": resp_body,
                    }
                )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            err_headers = list(e.headers.items()) if e.headers else []
            logger.warning(
                "HTTP request error: code=%d reason=%s body=%s",
                e.code,
                e.reason,
                err_body,
            )
            return json.dumps(
                {
                    "status": e.code,
                    "headers": err_headers,
                    "body": err_body,
                }
            )
        except urllib.error.URLError as e:
            logger.warning("HTTP URL error: reason=%s", e.reason)
            return json.dumps(
                {
                    "status": 0,
                    "headers": [],
                    "body": str(e.reason),
                }
            )

    # ── Report host functions ──────────────────────────────────────────

    def report_completed(self) -> bool:
        logger.info(
            "report completed: user=%s activity=%s",
            self._user_id,
            self._activity_id,
        )
        return True

    def report_passed(self, score: float | None) -> bool:
        logger.info(
            "report passed: user=%s activity=%s score=%s",
            self._user_id,
            self._activity_id,
            score,
        )
        return True

    def report_failed(self, score: float | None) -> bool:
        logger.info(
            "report failed: user=%s activity=%s score=%s",
            self._user_id,
            self._activity_id,
            score,
        )
        return True

    def report_progressed(self, progress: float) -> bool:
        logger.info(
            "report progressed: user=%s activity=%s progress=%f",
            self._user_id,
            self._activity_id,
            progress,
        )
        return True

    def report_scored(self, score: float) -> bool:
        logger.info(
            "report scored: user=%s activity=%s score=%f",
            self._user_id,
            self._activity_id,
            score,
        )
        return True

    # ── Storage host functions ──────────────────────────────────────────

    def _storage_path(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> str:
        """Build the full scoped storage path and validate the storage name."""
        scope = self.capability_checker.get_storage_scope(name)
        activity_id, course_id, user_id = self._scope_key_segments(scope, context)
        segments: list[str] = [self.manifest.name, name]
        segments.extend(s for s in (course_id, activity_id, user_id) if s)
        if path:
            segments.append(path)
        return "/".join(segments)

    def storage_read(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> bytes:
        """Read a file from storage. Returns raw bytes."""
        try:
            return self._file_storage.read(self._storage_path(name, path, context))
        except FileStorageError as e:
            raise AssetAccessError(str(e)) from e

    def storage_exists(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> bool:
        """Check whether a path exists in storage."""
        try:
            return self._file_storage.exists(self._storage_path(name, path, context))
        except FileStorageError as e:
            raise AssetAccessError(str(e)) from e

    def storage_url(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> str:
        """Return the HTTP URL path for a storage file.

        Context overrides are encoded as query parameters so the serving
        endpoint can reconstruct the same scoped path via the runtime.
        """
        self.capability_checker.check_storage(name)
        clean = path.strip("/")
        base = f"/activity/{self._activity_id}/storage/{name}/{clean}"
        if context is None:
            return base
        params: dict[str, str] = {}
        if context.get("activity-id") is not None:
            params["activity_id"] = context["activity-id"]  # type: ignore[assignment]
        if context.get("course-id") is not None:
            params["course_id"] = context["course-id"]  # type: ignore[assignment]
        if context.get("user-id") is not None:
            params["user_id"] = context["user-id"]  # type: ignore[assignment]
        if params:
            return f"{base}?{urllib.parse.urlencode(params)}"
        return base

    def storage_list(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> tuple[list[str], list[str]]:
        """List files and directories at a storage path.

        Returns a tuple with ``directories`` and ``files``.
        """
        try:
            files, directories = self._file_storage.list(
                self._storage_path(name, path, context)
            )
        except FileStorageError as e:
            raise AssetAccessError(str(e)) from e
        return (directories, files)

    def storage_write(
        self,
        name: str,
        path: str,
        content: bytes,
        context: SandboxContext | None = None,
    ) -> bool:
        """Write content to a storage file. Creates parent directories."""
        try:
            self._file_storage.write(self._storage_path(name, path, context), content)
        except FileStorageError as e:
            raise AssetAccessError(str(e)) from e
        return True

    def storage_delete(
        self, name: str, path: str, context: SandboxContext | None = None
    ) -> bool:
        """Delete a storage file. Returns True if the file existed."""
        try:
            return self._file_storage.delete(self._storage_path(name, path, context))
        except FileStorageError as e:
            raise AssetAccessError(str(e)) from e
