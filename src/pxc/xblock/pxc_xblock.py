"""PXC XBlock — runs PXC activities inside Open edX."""

import json
import logging
import mimetypes
from datetime import timedelta
from importlib.resources import files as pkg_files
from typing import Any

from django.db.models import Max
from django.template import Context, Template
from django.utils import timezone

from web_fragments.fragment import Fragment
from webob import Response
from xblock.core import XBlock
from xblock.fields import Scope, String
from xblock.validation import Validation

from pxc.lib.actions import ActionValidationError
from pxc.lib.capabilities import CapabilityError
from pxc.lib.permission import Permission
from pxc.lib.runtime import AssetAccessError, SandboxContext
from pxc.lib.runtime import PendingEvent as RuntimeEvent
from pxc.xblock.activities import get_activity_dir, list_activities
from pxc.xblock.field_store import DjangoFieldStore
from pxc.xblock.file_storage import DjangoFileStorage
from pxc.xblock.models import PendingEvent
from pxc.xblock.permissions import resolve_permission
from pxc.xblock.runtime import XBlockActivityRuntime

logger = logging.getLogger(__name__)

_PERM_RANK: dict[str, int] = {"view": 0, "play": 1, "edit": 2}

# Cross-user events are persisted in PendingEvent so polling clients can
# replay them with cursor-based catch-up. Anything older than this is unlikely
# to be useful and we drop it on every action handler call to keep the table
# bounded.
PENDING_EVENT_TTL = timedelta(hours=24)


def _event_visible(
    event_context: dict[str, str],
    event_permission: str,
    course_id: str,
    activity_id: str,
    user_id: str,
    permission: Permission,
) -> bool:
    """Return True if this user/permission should receive the event."""
    if "user_id" in event_context and event_context["user_id"] != user_id:
        return False
    if "activity_id" in event_context and event_context["activity_id"] != activity_id:
        return False
    if "course_id" in event_context and event_context["course_id"] != course_id:
        return False
    return _PERM_RANK.get(permission.value, 0) >= _PERM_RANK.get(event_permission, 0)


def _json_response(data: Any) -> Response:
    return Response(json.dumps(data), content_type="application/json", charset="utf8")


class PxcXBlock(XBlock):  # type: ignore[misc]
    has_author_view = True

    display_name = String(
        display_name="Display Name",
        default="PXC Activity",
        scope=Scope.settings,
    )
    activity_slug = String(
        display_name="Activity",
        default="",
        scope=Scope.settings,
    )

    # ------------------------------------------------------------------ #
    # Context helpers
    # ------------------------------------------------------------------ #

    def _get_ids(self) -> tuple[str, str, str]:
        """Return (course_id, activity_id, user_id)."""
        user_id = str(self.scope_ids.user_id)
        activity_id = str(self.scope_ids.usage_id)
        course_key = getattr(self.scope_ids.usage_id, "course_key", None)
        course_id = str(course_key) if course_key else ""
        return course_id, activity_id, user_id

    def _resolve_permission(self) -> Permission:
        """Permission for this handler call, derived from XBlock runtime context."""
        # TODO how do we actually determine if a user is allowed to be in edit mode?
        # shouldn't it be getattr(self.runtime, "user_is_staff", False)? But this fails in studio view
        has_edit_permission = getattr(self.runtime, "is_author_mode", False)
        return resolve_permission(self.scope_ids.user_id, has_edit_permission)

    @staticmethod
    def resource_string(path: str) -> str:
        """Read a UTF-8 text resource bundled with this xblock."""
        return pkg_files("pxc.xblock").joinpath(path).read_text(encoding="utf-8")

    def _pxcjs_bundle(self) -> str:
        """pxc.js + xblock_pxc.js concatenated as one non-module classic script.

        Both source files are ES modules in their own packages; we strip the
        single `export class` keyword from each so PXC and XBlockPXC are
        top-level class declarations in the resulting script. Served via the
        `pxcjs` handler and loaded by the fragment with `add_javascript_url`
        — i.e. a real <script src="…"> tag, which puts the class bindings in
        the realm's global declarative record where the per-view init
        scripts (student.js / studio.js) can resolve them by bare name.
        """
        base = (
            pkg_files("pxc.lib")
            .joinpath("static/js/pxc.js")
            .read_text(encoding="utf-8")
            .replace("export class PXC ", "class PXC ", 1)
        )
        sub = self.resource_string("static/js/xblock_pxc.js").replace(
            "export class XBlockPXC ", "class XBlockPXC ", 1
        )
        return f"{base}\n{sub}\n"

    @staticmethod
    def _initial_events_cursor(course_id: str, activity_id: str) -> int:
        """Current max PendingEvent.id for this activity, or 0 if no events yet.

        Embedded in the rendered fragment so first-time clients start polling
        from "now" instead of replaying the entire retention window.
        """
        agg = PendingEvent.objects.filter(
            course_id=course_id, activity_id=activity_id
        ).aggregate(m=Max("id"))
        return int(agg["m"] or 0)

    def _make_runtime(self, permission: Permission) -> XBlockActivityRuntime:
        course_id, activity_id, user_id = self._get_ids()
        activity_dir = get_activity_dir(self.activity_slug)
        return XBlockActivityRuntime(
            activity_dir=activity_dir,
            field_store=DjangoFieldStore(),
            file_storage=DjangoFileStorage(f"pxc/{activity_id}/storage"),
            activity_id=activity_id,
            course_id=course_id,
            user_id=user_id,
            permission=permission,
            storage_base_url=self.runtime.handler_url(self, "storage").strip("?"),
        )

    def validate(self) -> Validation:
        # Open edX's LmsBlockMixin.validate() calls resources.files('pxc') to
        # find translations, which crashes because 'pxc' is a namespace package
        # shared between pxc-lib and pxc-xblock. Skip to XBlock.validate() to
        # avoid the broken i18n service path; PXC has no validation requirements
        # that need translation-aware messages.
        return XBlock.validate(self)

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    def _attach_view_javascript(self, frag: Fragment, view: str) -> None:
        """Wire the standard JS resources + initialize_js entrypoint for a view."""
        frag.add_javascript_url(self.runtime.handler_url(self, "pxcjs").strip("?"))
        frag.add_javascript(self.resource_string(f"static/js/{view}.js"))
        frag.initialize_js(f"Pxc{view.capitalize()}XBlock")

    def _render_pxc_activity(self, permission: Permission) -> str:
        """Render the <pxc-activity> element via the shared partial template.

        Used by both student_view and author_view. Django's template engine
        auto-escapes every `{{ var }}` for HTML attribute / text context, so
        the embedded JSON (data-context, data-state) ends up with proper
        `&quot;` for its inner double quotes without any manual escaping.
        """
        course_id, activity_id, user_id = self._get_ids()
        runtime = self._make_runtime(permission)
        state = runtime.get_state()
        ctx_json = json.dumps(
            {"activity_id": activity_id, "course_id": course_id, "user_id": user_id}
        )
        return self._render_template(
            "static/html/pxc_activity.html",
            {
                "ctx_json": ctx_json,
                "state_json": json.dumps(state),
                # Note that for some reason, in the studio the events url is
                # suffixed with a "?", which we strip
                "action_url": self.runtime.handler_url(self, "action").strip("?"),
                "events_url": self.runtime.handler_url(self, "events").strip("?"),
                "asset_base_url": self.runtime.handler_url(self, "asset").strip("?"),
                "src_url": self.runtime.handler_url(self, "uijs").strip("?"),
                "events_cursor": self._initial_events_cursor(course_id, activity_id),
                "permission": permission.value,
            },
        )

    def student_view(self, context: dict[str, Any] | None = None) -> Fragment:
        if not self.activity_slug:
            return Fragment(
                "<p>PXC activity not configured. Open Studio to select an activity.</p>"
            )

        frag = Fragment(self._render_pxc_activity(Permission.play))
        self._attach_view_javascript(frag, "student")
        return frag

    def author_view(self, context: dict[str, Any] | None = None) -> Fragment:
        # Studio's inline preview on the unit page. Without this, the runtime
        # falls back to student_view, which renders at `play` permission.
        if not self.activity_slug:
            return Fragment(
                "<p>PXC activity not configured. Click Edit to select an activity.</p>"
            )

        frag = Fragment(self._render_pxc_activity(Permission.edit))
        self._attach_view_javascript(frag, "student")
        return frag

    def studio_view(self, context: dict[str, Any] | None = None) -> Fragment:
        rendered = self._render_template(
            "static/html/studio_view.html",
            {
                "activities": list_activities(),
                "current_slug": self.activity_slug,
                "display_name": self.display_name,
            },
        )
        frag = Fragment(rendered)
        self._attach_view_javascript(frag, "studio")
        return frag

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    @XBlock.handler  # type: ignore[untyped-decorator]
    def pxcjs(self, request: Any, suffix: str = "") -> Response:
        """Serve pxc.js + xblock_pxc.js as a single non-module classic script.

        Loaded by the views with `frag.add_javascript_url(...)`, which produces
        a real <script src="…"> tag and reliably exposes the class declarations
        to the per-view init scripts (see `_pxcjs_bundle`).
        """
        return Response(
            self._pxcjs_bundle(),
            content_type="application/javascript",
            charset="utf8",
        )

    @XBlock.handler  # type: ignore[untyped-decorator]
    def action(self, request: Any, suffix: str = "") -> Response:
        if not self.activity_slug:
            return _json_response({"events": [], "cursor": 0})

        try:
            body: dict[str, Any] = json.loads(request.body)
        except (ValueError, KeyError):
            return Response(status=400)

        name = str(body.get("name", ""))
        value = body.get("value", "")
        permission = self._resolve_permission()

        course_id, activity_id, user_id = self._get_ids()
        runtime = self._make_runtime(permission)

        try:
            runtime.on_action(name, value)
        except ActionValidationError as e:
            return Response(str(e), status=400)

        # Retention sweep: drop pending events that are too old to be useful.
        PendingEvent.objects.filter(
            created_at__lt=timezone.now() - PENDING_EVENT_TTL
        ).delete()

        pending: list[RuntimeEvent] = runtime.clear_pending_events()
        caller_events = []
        last_id = 0

        for ev in pending:
            ctx: dict[str, str] = ev["context"]  # type: ignore[assignment]
            pe = PendingEvent.objects.create(
                course_id=course_id,
                activity_id=activity_id,
                event_name=ev["name"],
                event_value=ev["value"],
                event_context=ctx,
                event_permission=ev["permission"],
            )
            assert pe.id is not None
            last_id = pe.id
            if _event_visible(
                ctx, ev["permission"], course_id, activity_id, user_id, permission
            ):
                caller_events.append({"name": ev["name"], "value": ev["value"]})

        return _json_response({"events": caller_events, "cursor": last_id})

    @XBlock.handler  # type: ignore[untyped-decorator]
    def events(self, request: Any, suffix: str = "") -> Response:
        """Return buffered events since a cursor for cross-user polling."""
        course_id, activity_id, user_id = self._get_ids()
        permission = self._resolve_permission()

        # `since` is mandatory: a missing/invalid cursor would return every
        # event in the (24h-retention-bounded) PendingEvent table on every
        # poll, which we don't want clients to default into by accident.
        since_raw = request.GET.get("since")
        if since_raw is None:
            return Response("Missing `since` query parameter", status=400)
        try:
            since = int(since_raw)
        except ValueError:
            return Response("Invalid `since` query parameter", status=400)

        rows = PendingEvent.objects.filter(
            course_id=course_id,
            activity_id=activity_id,
            id__gt=since,
        ).order_by("id")

        result = []
        cursor = since
        for row in rows:
            assert row.id is not None
            cursor = row.id  # advance cursor even for filtered-out events
            ctx: dict[str, str] = row.event_context
            if _event_visible(
                ctx, row.event_permission, course_id, activity_id, user_id, permission
            ):
                result.append({"name": row.event_name, "value": row.event_value})

        return _json_response({"events": result, "cursor": cursor})

    @XBlock.handler  # type: ignore[untyped-decorator]
    def asset(self, request: Any, suffix: str = "") -> Response:
        """Serve a manifest-declared activity asset."""
        if not self.activity_slug:
            return Response(status=404)
        runtime = self._make_runtime(Permission.view)
        try:
            asset_path = runtime.get_asset_path(suffix.lstrip("/"))
        except AssetAccessError:
            return Response(status=404)
        content_type, _ = mimetypes.guess_type(str(asset_path))
        return Response(
            asset_path.read_bytes(),
            content_type=content_type or "application/octet-stream",
        )

    @XBlock.handler  # type: ignore[untyped-decorator]
    def storage(self, request: Any, suffix: str = "") -> Response:
        """Serve a file from activity storage.

        URL shape (matches `XBlockActivityRuntime.storage_url`):
            <handler>/<storage_name>/<file_path>[?activity_id=&course_id=&user_id=]
        Query params let an activity read another scope's file (e.g. an
        instructor reading a learner's submission) — they're forwarded as a
        `SandboxContext` so the runtime rebuilds the same scoped path.
        """
        if not self.activity_slug:
            return Response(status=404)
        clean = suffix.lstrip("/")
        storage_name, _, file_path = clean.partition("/")
        if not storage_name:
            return Response(status=404)
        context: SandboxContext | None = None
        activity_id_override = request.GET.get("activity_id")
        course_id_override = request.GET.get("course_id")
        user_id_override = request.GET.get("user_id")
        if activity_id_override or course_id_override or user_id_override:
            context = {
                "activity-id": activity_id_override,
                "course-id": course_id_override,
                "user-id": user_id_override,
            }
        runtime = self._make_runtime(Permission.view)
        try:
            content = runtime.storage_read(storage_name, file_path, context)
        except (CapabilityError, AssetAccessError):
            return Response(status=404)
        content_type, _ = mimetypes.guess_type(file_path)
        return Response(
            content,
            content_type=content_type or "application/octet-stream",
        )

    @XBlock.handler  # type: ignore[untyped-decorator]
    def uijs(self, request: Any, suffix: str = "") -> Response:
        """Serve the activity's ui.js entry point as an ES module.

        Targeted by data-src on <pxc-activity>; pxc.js dynamically imports it
        and calls its setup(activity) export. The ui.js entry isn't part of
        the manifest's `assets` list — it's the special `manifest.ui` path —
        so it gets its own handler instead of going through `asset`.
        """
        if not self.activity_slug:
            return Response(status=404)
        runtime = self._make_runtime(Permission.view)
        try:
            ui_path = runtime.get_ui_path()
        except AssetAccessError:
            return Response(status=404)
        return Response(
            ui_path.read_bytes(),
            content_type="application/javascript",
            charset="utf8",
        )

    @XBlock.handler  # type: ignore[untyped-decorator]
    def save_settings(self, request: Any, suffix: str = "") -> Response:
        """Save studio settings (activity slug selection)."""
        try:
            body: dict[str, Any] = json.loads(request.body)
        except ValueError:
            return Response(status=400)
        slug = str(body.get("activity_slug", "")).strip()
        if slug and slug not in list_activities():
            return Response("Unknown activity", status=400)
        display_name = str(body.get("display_name", "")).strip()
        self.activity_slug = slug
        if display_name:
            self.display_name = display_name
        self.save()
        return _json_response({"ok": True})

    # ------------------------------------------------------------------ #
    # Template helper
    # ------------------------------------------------------------------ #

    def _render_template(self, path: str, ctx: dict[str, Any]) -> str:
        """Render an HTML template bundled with this xblock through Django.

        Django auto-escapes `{{ var }}` for HTML attribute / text contexts —
        callers should NOT pre-escape values. Use `{{ var|safe }}` in the
        template body to opt out for raw-HTML fragments built upstream
        (e.g. `activity_options`, the rendered `<pxc-activity>` partial).
        """
        return str(Template(self.resource_string(path)).render(Context(ctx)))
