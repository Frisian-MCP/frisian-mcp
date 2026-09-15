"""
GH #67 — cross-carve required-relation discoverability (W017 / W019).

Every fixture here uses **real Django models with real required foreign keys**.
That is deliberate and it is the point of the file.

The suite already owned FK tests, and none of them could exhibit this shape.
``test_required_field_detection.py`` builds its model with ``MagicMock()``, whose
``_meta.get_field`` answers for *any* name, so the fixture cannot tell the model
that *declares* a relation from the model it *points at* — the exact distinction
this check turns on.  ``test_fk_m2m_schemas.py`` passes ``queryset=[]``, a bare
list with no ``.model``, so resolution bails before it reaches introspection at
all.  Measured before this file was written: of the seven modules that carve a
route and the four that attach model identity, exactly one does both, and it
carries a single ``perm_app_label`` and no serializer.  **Zero existing fixtures
could produce a W017.**

So the models below are ``auth.Permission.content_type -> contenttypes.ContentType``
— a genuinely required (``null=False``, no default) cross-app FK between two
built-ins that ship with Django.  Nothing is mocked that the check reads.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest
from django.test import override_settings

from frisian_mcp._relations import (
    RelationOutcome,
    RequiredRelation,
    clear_required_relations,
    record_required_relations,
)
from frisian_mcp.registry import ToolRegistry, tool_registry
from frisian_mcp.route_audit import (
    CrossCarveReport,
    audit_cross_carve_surface,
    audit_route_surface,
)

AUDIT_LOGGER = "frisian_mcp.route_audit"

#: The owning side: a real write action over a model with a real required FK.
OWNER_LABEL = "auth.permission"
#: The target the FK points at.  A different app, so a carve can separate them.
TARGET_LABEL = "contenttypes.contenttype"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _fn(name: str) -> Any:
    """Return a no-op tool callable."""

    def handler(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        return {"tool": name}

    return handler


def _register(
    name: str,
    *,
    action: str | None,
    app_label: str | None = None,
    model: str | None = None,
    is_write: bool = False,
    view_class: type | None = None,
) -> None:
    """Register one tool into the live registry with DRF metadata attached."""
    tool_registry.register(
        name=name,
        fn=_fn(name),
        description=f"tool {name}",
        input_schema={"type": "object", "properties": {}},
        permission_tier="read_write" if is_write else "read",
        is_write=is_write,
        perm_app_label=app_label,
        perm_model=model,
        perm_drf_action=action,
    )
    if view_class is not None:
        tool_registry.get_entry(name).view_class = view_class  # type: ignore[union-attr]


def _relation(
    field: str = "content_type",
    target: str | None = TARGET_LABEL,
    outcome: RelationOutcome = RelationOutcome.RESOLVED,
    *,
    schema_required: bool = False,
) -> RequiredRelation:
    """
    Return one recorded relation for a write action.

    *schema_required* is the emitted schema's verdict and *outcome* is the
    resolver's; they are independent inputs here because they are independent
    facts in the code, and the gate reads the first one first.
    """
    return RequiredRelation(
        field_name=field,
        target_label=target,
        outcome=outcome,
        schema_required=schema_required,
    )


def _permission_viewset() -> type:
    """
    A real DRF ``ModelViewSet`` over ``auth.Permission`` — no hand-built relations.

    Everything else in this file records a :class:`RequiredRelation` directly,
    which is the right instrument for grading the audit but means the audit is
    graded on a tuple a test wrote.  This one goes through discovery instead, so
    the schema's ``required`` array and the resolver's outcome are both whatever
    the real introspection produces.
    """
    from django.contrib.auth.models import Permission
    from rest_framework import serializers, viewsets

    class _PermissionSerializer(serializers.ModelSerializer):
        class Meta:
            model = Permission
            fields = "__all__"

    class _PermissionViewSet(viewsets.ModelViewSet):
        queryset = Permission.objects.all()
        serializer_class = _PermissionSerializer

    return _PermissionViewSet


def _logentry_viewset() -> type:
    """
    A real ``ModelViewSet`` over ``admin.LogEntry``, for the override cases.

    ``LogEntry`` is used rather than ``Permission`` because it carries both
    shapes this file needs and ``Permission`` carries neither: a relation the
    emitted schema does *not* require (``content_type``), and -- through the
    explicit serializer below -- a relation that resolves **RESOLVED and
    optional**, which is the one ``narrow_relations`` deletes outright.
    """
    from django.contrib.admin.models import LogEntry
    from rest_framework import serializers, viewsets

    class _LogEntrySerializer(serializers.ModelSerializer):
        class Meta:
            model = LogEntry
            fields = "__all__"

    class _LogEntryViewSet(viewsets.ModelViewSet):
        queryset = LogEntry.objects.all()
        serializer_class = _LogEntrySerializer

    return _LogEntryViewSet


def _resolved_optional_viewset() -> type:
    """
    A viewset whose only relation resolves as genuinely optional.

    ``LogEntry.action_time`` is ``null=False`` **with a default**, so
    ``resolve_relation`` reaches a real verdict of "not required" — outcome
    ``RESOLVED``, ``required=False``.  That is the one combination
    ``narrow_relations`` deletes, which is why it is the fixture for the silent
    half of the override defect.  Nothing is mocked; the ``null``/``has_default``
    premise is asserted in :class:`TestFixturesCanActuallyFail`.
    """
    from django.contrib.admin.models import LogEntry
    from rest_framework import serializers, viewsets

    class _OptionalRelationSerializer(serializers.Serializer):  # pylint: disable=abstract-method
        action_time = serializers.PrimaryKeyRelatedField(
            queryset=LogEntry.objects.all(), required=False
        )

    class _OptionalRelationViewSet(viewsets.ModelViewSet):
        queryset = LogEntry.objects.all()
        serializer_class = _OptionalRelationSerializer

    return _OptionalRelationViewSet


def _routes(**routes: dict[str, Any]) -> Any:
    """Return an ``override_settings`` context for ``FRISIAN_MCP_ROUTES``."""
    return override_settings(FRISIAN_MCP_ROUTES=routes)


def _codes(report: CrossCarveReport) -> list[str]:
    """Return the finding codes in *report*, in order."""
    return [f.code for f in report.findings]


def _message(report: CrossCarveReport, code: str) -> str:
    """Return the single message for *code*."""
    (finding,) = [f for f in report.findings if f.code == code]
    return str(finding.message)


def _raise_injected(*_args: Any, **_kwargs: Any) -> Any:
    """Fail the way a bug in the check would, for injection tests."""
    raise RuntimeError("injected")


def _run_doctor(*, strict: bool = False, **routes: dict[str, Any]) -> tuple[str, str]:
    """
    Run ``mcp_doctor`` under *routes* and return its ``(stdout, stderr)``.

    ``handle()`` is called directly, matching ``tests/test_mcp_doctor.py`` — Django's
    command discovery needs a populated ``INSTALLED_APPS`` and ``execute()`` wants an
    options dict, neither of which this fixture supplies.
    """
    from io import StringIO

    from frisian_mcp.management.commands.mcp_doctor import Command

    out, err = StringIO(), StringIO()
    with override_settings(FRISIAN_MCP_ROUTES=routes or None):
        Command(stdout=out, stderr=err).handle(strict=strict)
    return out.getvalue(), err.getvalue()


@pytest.fixture()
def clean_registry() -> Generator[None, None, None]:
    """
    Give each test an empty registry, then put the live one back exactly as it was.

    The registry is **emptied**, not merely snapshotted.  Every assertion here turns
    on which tools a route can see, so a tool left behind by another module could
    satisfy a relation's target and silently suppress a W017 — the finding would go
    missing for a reason that has nothing to do with the code under test, and only
    when the suite runs in a particular order.

    This is not hypothetical: one full-suite run failed here exactly that way and
    then would not reproduce in four attempts, including the same command sequence.
    The cause was never pinned down.  Emptying the registry makes the whole class of
    failure impossible rather than leaving it unexplained and intermittent.
    """
    saved = dict(tool_registry._tools)  # noqa: SLF001
    tool_registry._tools.clear()  # noqa: SLF001
    clear_required_relations()
    yield
    tool_registry._tools.clear()  # noqa: SLF001
    tool_registry._tools.update(saved)  # noqa: SLF001
    clear_required_relations()


@pytest.fixture()
def carved_surface(clean_registry: None) -> None:
    """
    Register the canonical shape: a write on the owner, reads on both models.

    ``permission_create`` carries a required relation to ``contenttypes.contenttype``.
    Whether that target is *readable* is then decided purely by the route carve.
    """
    _register("permission_list", action="list", app_label="auth", model="permission")
    _register(
        "permission_create",
        action="create",
        app_label="auth",
        model="permission",
        is_write=True,
    )
    _register(
        "contenttype_list",
        action="list",
        app_label="contenttypes",
        model="contenttype",
    )
    record_required_relations("permission_create", [_relation()])


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------


class TestUnreadableTargetIsFound:
    """W017 fires when the carve hides the relation's target."""

    def test_carve_hiding_the_target_emits_w017(self, carved_surface: None) -> None:
        """Route exposing only the owner flags the unreachable target."""
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]

    def test_w017_names_route_action_field_and_target(self, carved_surface: None) -> None:
        """The message carries every fact an operator needs to act on it."""
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")
        assert "default" in message
        assert "permission_create" in message
        assert "content_type" in message
        assert TARGET_LABEL in message

    def test_report_counts_the_action_it_examined(self, carved_surface: None) -> None:
        """``actions_checked`` proves the write was examined, not skipped."""
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.evaluated is True
        assert report.routes_checked == 1
        assert report.actions_checked == 1

    def test_w017_does_not_claim_the_write_will_fail(self, carved_surface: None) -> None:
        """
        The text must never predict the host's 422.

        A carve removes tools from a door; it does not remove a permission from a
        principal.  On the normal deployment the write succeeds, so predicting a
        failure would name something the operator cannot reproduce — and there is
        no way to suppress this log.
        """
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")
        assert "neither predicts nor rules out" in message
        assert "422" not in message

    def test_w017_makes_no_absolute_capability_claim(self, carved_surface: None) -> None:
        """
        The message must not say the caller cannot obtain a value.  It used to.

        Measured on a live 1,879-tool host: of 34 W017 findings on a carved door,
        **28 of the writes succeeded**.  Nested FK representations are not
        permission-filtered, so an identifier this route exposes no read for is
        still handed back inside another list's rows — and whether it is depends
        on what those rows hold at the time, which is runtime state this pass is
        contractually barred from reading.

        So the claim shrank to the part that is provable from the carve alone.
        The banned sentence is asserted absent by its own words, because a
        rewrite that reintroduces the same promise in new phrasing is the
        failure this test exists to catch.
        """
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")

        # What it must never say again, asserted FIRST so a regression reds on
        # the claim itself rather than on a phrasing mismatch.
        lowered = message.lower()
        assert "cannot look up" not in lowered
        assert "cannot" not in lowered
        assert "unable" not in lowered

        # What it must now say instead.
        assert "exposes no list/retrieve action for" in message
        assert "A valid value may still be obtainable" in message
        assert "does not examine response shapes" in message


# ---------------------------------------------------------------------------
# Negatives — each one is a distinct way to ship a check that cries wolf
# ---------------------------------------------------------------------------


class TestNegativesStaySilent:
    """Configurations that must produce no cross-carve finding at all."""

    def test_routes_unset_audits_nothing(self, carved_surface: None) -> None:
        """The implicit legacy route carves nothing, so nothing can be hidden."""
        with override_settings():
            from django.conf import settings

            if hasattr(settings, "FRISIAN_MCP_ROUTES"):
                del settings.FRISIAN_MCP_ROUTES  # type: ignore[misc]
            report = audit_cross_carve_surface()
        assert report.evaluated is True
        assert report.findings == ()
        assert report.routes_checked == 0

    def test_wildcard_allow_list_carves_nothing(self, carved_surface: None) -> None:
        """``["*"]`` exposes the target's read, so it is discoverable."""
        with _routes(default={"path": "mcp", "allow_list": ["*"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()

    def test_target_readable_via_a_different_tool_is_silent(self, carved_surface: None) -> None:
        """The read that satisfies the relation need not be the tool carrying it."""
        with _routes(default={"path": "mcp", "allow_list": ["permission", "contenttype"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()

    def test_relation_resolved_optional_is_never_recorded(self, clean_registry: None) -> None:
        """
        An optional FK produces no finding because it is never recorded.

        ``null=True`` and ``has_default()`` both resolve as not-required, and a
        relation resolved as genuinely optional is not written to the map at all.
        """
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations("permission_create", [])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 1

    def test_read_only_action_carrying_a_relation_is_skipped(self, clean_registry: None) -> None:
        """Writes only — a relation recorded against a read action is ignored."""
        _register("permission_list", action="list", app_label="auth", model="permission")
        record_required_relations("permission_list", [_relation()])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 0

    def test_patch_is_out_of_scope(self, clean_registry: None) -> None:
        """
        PATCH does not enforce ``required``, so an unreachable target is harmless.

        This case is not in FK-5's original brief; it exists because the design
        narrowed the acting side to POST/PUT after the brief was written.  It is
        the shape an implementation gets wrong by reusing ``is_write``, which is
        true for all four write verbs.
        """
        _register(
            "permission_partial_update",
            action="partial_update",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations("permission_partial_update", [_relation()])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 0

    def test_delete_is_out_of_scope(self, clean_registry: None) -> None:
        """DELETE carries no body, so it cannot be made unsatisfiable either."""
        _register(
            "permission_destroy",
            action="destroy",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations("permission_destroy", [_relation()])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()


class TestWriteReachabilityIsNotDiscoverability:
    """A write on the target is not a way to learn a valid identifier."""

    def test_target_reachable_only_by_a_write_still_fires(self, clean_registry: None) -> None:
        """
        Exposing ``contenttype_create`` does not make the target discoverable.

        This is the half of "target on the surface via a different tool" that an
        implementation gets accidentally right for reads and accidentally wrong
        for writes: you cannot learn an id by creating a new object.
        """
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        _register(
            "contenttype_create",
            action="create",
            app_label="contenttypes",
            model="contenttype",
            is_write=True,
        )
        record_required_relations("permission_create", [_relation()])
        with _routes(default={"path": "mcp", "allow_list": ["permission", "contenttype"]}):
            report = audit_cross_carve_surface()
        assert "W017" in _codes(report)
        assert TARGET_LABEL in _message(report, "W017")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundaries where the check must not over- or under-report."""

    def test_target_exposed_by_no_tool_anywhere_still_fires_w017(
        self, clean_registry: None
    ) -> None:
        """
        A target no tool exposes is unreadable on this route, so W017 covers it.

        W018 ("exposed nowhere at all") was cut as a separate feature; it must
        not leave this case silent in the meantime.
        """
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations("permission_create", [_relation(target="ghost.model")])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert "W017" in _codes(report)
        assert "ghost.model" in _message(report, "W017")

    def test_deny_list_removing_the_target_read_fires_w017(self, carved_surface: None) -> None:
        """Deny is part of the net surface, so removing the read hides the target."""
        with _routes(
            default={
                "path": "mcp",
                "allow_list": ["*"],
                "deny_list": ["contenttype"],
            }
        ):
            report = audit_cross_carve_surface()
        assert "W017" in _codes(report)

    def test_fully_denied_route_does_not_double_report(self, carved_surface: None) -> None:
        """
        A route exposing nothing exposes no writes, so W017 cannot also fire.

        W008 already covers "allow selected tools and deny removed all of them";
        emitting W017 beside it would report one misconfiguration twice.
        """
        with _routes(
            default={
                "path": "mcp",
                "allow_list": ["permission"],
                "deny_list": ["permission"],
            }
        ):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 0

    def test_each_route_is_reported_separately(self, carved_surface: None) -> None:
        """Two carved routes produce two findings, each naming its own route."""
        with _routes(
            default={"path": "mcp/one", "allow_list": ["permission"]},
            elevated={"path": "mcp/two", "allow_list": ["permission"]},
        ):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017", "W017"]
        assert {f.route_name for f in report.findings} == {"default", "elevated"}
        assert report.routes_checked == 2


# ---------------------------------------------------------------------------
# W019 — "not evaluated" is never "clean"
# ---------------------------------------------------------------------------


class TestUnevaluatedIsNotClean:
    """The three ways a verdict goes missing, and the one that must stay silent."""

    def test_hand_registered_write_is_silent_not_w019(self, clean_registry: None) -> None:
        """
        A ``@mcp_tool`` write has no serializer, so there is no question to answer.

        This matters because on the ``AUTODISCOVER=False`` path *every* registered
        write looks like this — no ``perm_drf_action``, no ``view_class``.  Routing
        it to W019 would emit a could-not-evaluate for a host's entire write
        surface on every boot, once per worker, and bury the real alarm below.
        "Not applicable" is not "could not evaluate".
        """
        _register("thing_create", action=None, is_write=True)
        with _routes(default={"path": "mcp", "allow_list": ["thing"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 0

    def test_custom_action_without_view_class_is_w019(self, clean_registry: None) -> None:
        """A custom ``@action`` we cannot place on an HTTP method is unevaluated."""
        _register(
            "permission_sync",
            action="sync",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W019"]
        assert "method undetermined" in _message(report, "W019")

    def test_custom_action_with_view_class_resolves_its_methods(self, clean_registry: None) -> None:
        """
        With ``view_class`` present the decorator's ``mapping`` supplies the method.

        This is the positive control for the case above: same custom action, the
        only difference is that the class survived registration.
        """
        from rest_framework.decorators import action as drf_action

        class _ViewSet:
            @drf_action(detail=True, methods=["post"])
            def sync(self) -> None:
                """Custom POST action; the decorator parks its methods on ``mapping``."""

        _register(
            "permission_sync",
            action="sync",
            app_label="auth",
            model="permission",
            is_write=True,
            view_class=_ViewSet,
        )
        record_required_relations("permission_sync", [_relation()])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]

    def test_drf_write_never_examined_is_w019(self, clean_registry: None) -> None:
        """A DRF write with no recorded relations was not examined, not cleared."""
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W019"]
        assert "not examined" in _message(report, "W019")

    def test_unevaluated_relation_is_w019_not_w017(self, clean_registry: None) -> None:
        """
        An unresolved relation must not be graded as though it had been resolved.

        ``required`` is ``False`` on an ``UNEVALUATED`` outcome as a safe default,
        so a check that reads the bool alone would silently clear this.
        """
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [_relation(outcome=RelationOutcome.UNEVALUATED)],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W019"]

    def test_unresolved_target_is_w019_even_when_required(self, clean_registry: None) -> None:
        """A resolved requirement whose target is unknown cannot be graded either."""
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations("permission_create", [_relation(target=None)])
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W019"]

    def test_both_buckets_report_side_by_side(self, clean_registry: None) -> None:
        """One unreadable and one unevaluated relation produce both findings."""
        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [
                _relation(field="content_type"),
                _relation(field="owner", outcome=RelationOutcome.UNEVALUATED),
            ],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert sorted(_codes(report)) == ["W017", "W019"]
        assert "content_type" in _message(report, "W017")
        assert "owner" in _message(report, "W019")


# ---------------------------------------------------------------------------
# The gate — the emitted schema's ``required`` array, not the resolver's outcome
# ---------------------------------------------------------------------------


class TestSchemaRequiredIsWhatDecides:
    """
    A field the schema obliges the caller to send is a W017 question, always.

    The resolver's outcome answers a different question: whether model
    introspection reached a verdict about the model field behind the relation.
    Reading that first is what made this check inert — measured on a real host,
    ``1,241`` of ``1,245`` relations came back ``UNEVALUATED`` while their targets
    resolved ``1,245`` of ``1,245``, so every one of them was filed as a
    could-not-evaluate and W017 never fired at all.  The two facts are kept
    separate and the schema's is consulted first.
    """

    def test_schema_required_unevaluated_relation_is_w017_not_w019(
        self, clean_registry: None
    ) -> None:
        """The schema requires it and the target resolved: that is decidable."""
        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [_relation(outcome=RelationOutcome.UNEVALUATED, schema_required=True)],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]
        assert f"content_type -> {TARGET_LABEL}" in _message(report, "W017")

    def test_schema_required_unevaluated_relation_stays_silent_when_readable(
        self, clean_registry: None
    ) -> None:
        """
        And it goes quiet when the carve is widened — the control for the test above.

        A signal that does not stop when its cause is removed is not a signal.
        Same registry, same relation, the only difference is the allow_list.
        """
        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        _register(
            "contenttype_list",
            action="list",
            app_label="contenttypes",
            model="contenttype",
        )
        record_required_relations(
            "permission_create",
            [_relation(outcome=RelationOutcome.UNEVALUATED, schema_required=True)],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission", "contenttype"]}):
            report = audit_cross_carve_surface()
        assert report.findings == ()
        assert report.actions_checked == 1

    def test_schema_required_with_unresolved_target_is_still_w019(
        self, clean_registry: None
    ) -> None:
        """Obliged to send it, but we do not know where it points: still undecidable."""
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [_relation(target=None, outcome=RelationOutcome.UNEVALUATED, schema_required=True)],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W019"]

    def test_widening_w017_did_not_empty_w019(self, clean_registry: None) -> None:
        """
        Both buckets still fill, from one action, under the new gate.

        The risk in moving a population from one bucket to the other is that the
        emptied bucket stops being a real signal.  ``content_type`` is
        schema-required and unevaluated (now W017); ``owner`` is neither
        (still W019).  Both must appear.
        """
        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [
                _relation(
                    field="content_type",
                    outcome=RelationOutcome.UNEVALUATED,
                    schema_required=True,
                ),
                _relation(field="owner", outcome=RelationOutcome.UNEVALUATED),
            ],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert sorted(_codes(report)) == ["W017", "W019"]
        assert "content_type" in _message(report, "W017")
        assert "owner" in _message(report, "W019")
        assert "owner" not in _message(report, "W017")


class TestNarrowRelationsCarriesTheFact:
    """
    ``narrow_relations`` is the only place holding the schema's ``required`` array.

    It decides which relations survive, and now records *why* each one did.  A
    fact not stamped here is gone: the audit downstream sees the relation and the
    outcome, and nothing else.
    """

    def test_a_required_name_is_kept_and_stamped(self) -> None:
        """In the schema's array: kept, and marked as such."""
        from frisian_mcp._relations import narrow_relations

        relations = {"content_type": _relation(outcome=RelationOutcome.UNEVALUATED)}
        narrow_relations(relations, {"content_type"})
        assert relations["content_type"].schema_required is True
        assert relations["content_type"].outcome is RelationOutcome.UNEVALUATED

    def test_an_optional_resolved_relation_is_still_dropped(self) -> None:
        """Absent from the array and resolved: nothing to say, so it goes."""
        from frisian_mcp._relations import narrow_relations

        relations = {"content_type": _relation()}
        narrow_relations(relations, set())
        assert relations == {}

    def test_an_unevaluated_relation_is_kept_unstamped(self) -> None:
        """
        Absent from the array but unevaluated: kept, and NOT marked.

        This is the pair that must stay a could-not-evaluate.  Stamping it would
        turn every field we failed to examine into a confident W017.
        """
        from frisian_mcp._relations import narrow_relations

        relations = {"content_type": _relation(outcome=RelationOutcome.UNEVALUATED)}
        narrow_relations(relations, set())
        assert relations["content_type"].schema_required is False


class TestTheEmittedSchemaDoesNotMove:
    """
    This change *consumes* the schema's ``required`` array and never writes it.

    The audit is advisory and ``mcp_doctor``-only; the emitted schema is enforced
    by ``dispatch`` before the host ever sees the call.  So the boundary between
    them is the property that keeps this change non-caller-visible, and it is
    asserted here rather than argued.
    """

    def test_collecting_relations_does_not_change_what_discovery_emits(self) -> None:
        """Same action, with and without the collector: the same schema."""
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        view_class = _permission_viewset()
        without = DRFSyncDiscovery().get_input_schema(view_class, "create")
        collected: dict[str, RequiredRelation] = {}
        with_collector = DRFSyncDiscovery().get_input_schema(view_class, "create", collected)

        assert with_collector["properties"] == without["properties"]
        # The array's ORDER is not stable between processes -- discovery builds it
        # through a set -- so it is compared as the set it is.
        assert sorted(with_collector["required"]) == sorted(without["required"])
        assert collected  # the collector really did run, so the comparison means something

    def test_the_real_discovery_path_produces_w017_on_a_carve(self, clean_registry: None) -> None:
        """
        End to end on real models: discovery emits, the audit reads, W017 fires.

        Nothing here hand-writes a relation.  This is the shape a real host
        presents and the one the live re-run has to match.
        """
        from frisian_mcp._relations import record_write_relations
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        collected: dict[str, RequiredRelation] = {}
        schema = DRFSyncDiscovery().get_input_schema(_permission_viewset(), "create", collected)
        record_write_relations("permission_create", "post", collected)

        # The premise, asserted rather than assumed: this is the exact shape the
        # old gate mis-filed -- the schema requires the field, and the resolver
        # declined to answer for it.
        assert "content_type" in schema["required"]
        assert collected["content_type"].outcome is RelationOutcome.UNEVALUATED
        assert collected["content_type"].target_label == TARGET_LABEL

        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]


# ---------------------------------------------------------------------------
# Operator overrides — a supported setting must not produce a silent clean
# ---------------------------------------------------------------------------


class TestOperatorOverridesReachTheGate:
    """
    ``FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES`` is part of the emitted required set.

    It is the operator's escape hatch for relations introspection cannot see as
    required, so it names exactly the fields most likely to be undiscoverable.
    It used to be applied *after* the collector had already been narrowed, which
    made it invisible to the gate — and for a relation that resolved as
    optional, deleted before the override ran, the result was **neither W017 nor
    W019**.  A silent clean on a field the operator declared required themselves.
    """

    def test_an_override_forced_relation_is_stamped(self) -> None:
        """The override's names are in the set the collector is narrowed against."""
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"logentry_create": ["content_type"]}
        ):
            DRFSyncDiscovery().get_input_schema(
                _logentry_viewset(), "create", collected, "logentry_create"
            )
        assert collected["content_type"].schema_required is True

    def test_without_the_override_that_same_relation_is_not_stamped(self) -> None:
        """The control: the stamp comes from the override, not from the field."""
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        DRFSyncDiscovery().get_input_schema(
            _logentry_viewset(), "create", collected, "logentry_create"
        )
        assert collected["content_type"].schema_required is False

    def test_an_override_forced_relation_produces_w017_on_a_carve(
        self, clean_registry: None
    ) -> None:
        """End to end: the operator declares it required, the audit says it is unreachable."""
        from frisian_mcp._relations import record_write_relations
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        _register("logentry_list", action="list", app_label="admin", model="logentry")
        _register(
            "logentry_create",
            action="create",
            app_label="admin",
            model="logentry",
            is_write=True,
        )
        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"logentry_create": ["content_type"]}
        ):
            DRFSyncDiscovery().get_input_schema(
                _logentry_viewset(), "create", collected, "logentry_create"
            )
        record_write_relations("logentry_create", "post", collected)

        with _routes(default={"path": "mcp", "allow_list": ["logentry"]}):
            report = audit_cross_carve_surface()
        assert "W017" in _codes(report)
        assert f"content_type -> {TARGET_LABEL}" in _message(report, "W017")

    def test_a_resolved_optional_relation_stays_dropped_without_an_override(self) -> None:
        """
        Property that must survive: widening the collector must not sweep in optionals.

        ``action_time`` resolves to a real verdict of "not required".  Nobody is
        obliged to send it, so it is not a discoverability problem and it goes.
        """
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        DRFSyncDiscovery().get_input_schema(
            _resolved_optional_viewset(), "create", collected, "logentry_create"
        )
        assert collected == {}

    def test_an_override_rescues_a_relation_that_was_being_deleted(self) -> None:
        """
        The silent half, closed.

        Same fixture as the test above — a relation resolved as optional, which
        ``narrow_relations`` deletes.  Naming it in the override makes it
        required in the emitted schema, so it must survive the narrowing and
        arrive at the gate.  Before the ordering fix it was deleted first and the
        override landed too late: the operator got neither finding, on the one
        field they had gone out of their way to declare.
        """
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"logentry_create": ["action_time"]}
        ):
            schema = DRFSyncDiscovery().get_input_schema(
                _resolved_optional_viewset(), "create", collected, "logentry_create"
            )
        # The collector is asserted first deliberately: it is what the defect
        # emptied, and a failure here says "the relation was deleted" rather
        # than pointing at the schema, which was never the broken half.
        assert collected["action_time"].schema_required is True
        assert collected["action_time"].outcome is RelationOutcome.RESOLVED
        assert "action_time" in schema["required"]

    def test_the_rescued_relation_reaches_a_real_finding(self, clean_registry: None) -> None:
        """
        Surviving the narrowing is not the deliverable; being *graded* is.

        The rescued relation points at ``admin.logentry`` itself, so a resource
        carve cannot separate the two — the write and the read share a prefix.
        A ``deny_list`` can, and it is the same net surface either way.  W017 is
        the right bucket: the target resolved, the route exposes no read for it,
        and the operator has declared the caller must send it.
        """
        from frisian_mcp._relations import record_write_relations
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        _register("logentry_list", action="list", app_label="admin", model="logentry")
        _register(
            "logentry_create",
            action="create",
            app_label="admin",
            model="logentry",
            is_write=True,
        )
        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"logentry_create": ["action_time"]}
        ):
            DRFSyncDiscovery().get_input_schema(
                _resolved_optional_viewset(), "create", collected, "logentry_create"
            )
        record_write_relations("logentry_create", "post", collected)

        with _routes(default={"path": "mcp", "allow_list": ["*"], "deny_list": ["logentry_list"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]
        assert "logentry_create.action_time -> admin.logentry" in _message(report, "W017")

    def test_an_override_naming_a_non_relational_field_stamps_nothing(self) -> None:
        """An override on a plain field is not a relation and must not become one."""
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"logentry_create": ["change_message"]}
        ):
            DRFSyncDiscovery().get_input_schema(
                _logentry_viewset(), "create", collected, "logentry_create"
            )
        assert "change_message" not in collected
        assert collected["content_type"].schema_required is False

    def test_an_override_for_a_different_tool_does_not_leak(self) -> None:
        """Overrides are keyed by tool name and stay there."""
        from frisian_mcp.backends.discovery import DRFSyncDiscovery

        collected: dict[str, RequiredRelation] = {}
        with override_settings(
            FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES={"permission_create": ["content_type"]}
        ):
            DRFSyncDiscovery().get_input_schema(
                _logentry_viewset(), "create", collected, "logentry_create"
            )
        assert collected["content_type"].schema_required is False

    def test_the_emitted_schema_is_what_the_old_ordering_produced(self) -> None:
        """
        Applying the overrides earlier changed *when*, never *what*.

        The old path was ``get_input_schema()`` then ``_apply_required_overrides()``
        on its result; the new one folds the second into the first.  Both are run
        here and compared, so the equivalence is asserted rather than reasoned
        about.  Compared as a SET: the array's order is not stable between
        processes, because discovery builds it through a set.
        """
        from frisian_mcp.backends.discovery import DRFSyncDiscovery, _apply_required_overrides

        overrides = {"logentry_create": ["content_type"]}
        view_class = _logentry_viewset()
        with override_settings(FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES=overrides):
            new_path = DRFSyncDiscovery().get_input_schema(
                view_class, "create", None, "logentry_create"
            )
            old_path = DRFSyncDiscovery().get_input_schema(view_class, "create")
            _apply_required_overrides(old_path, "logentry_create")

        assert new_path["properties"] == old_path["properties"]
        assert sorted(new_path["required"]) == sorted(old_path["required"])
        assert "content_type" in new_path["required"]


# ---------------------------------------------------------------------------
# Contract — advisory code must never break the gateway
# ---------------------------------------------------------------------------


class TestFailureContract:
    """The check is advisory: it degrades, it never raises and never lies."""

    def test_report_says_unevaluated_when_the_pass_throws(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An injected failure comes back as ``evaluated=False`` with a reason."""
        import frisian_mcp.route_audit as module

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(module, "_cross_carve_findings_for_route", _boom)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.evaluated is False
        assert report.reason is not None
        assert "injected" in report.reason
        assert report.findings == ()

    def test_audit_route_surface_still_returns_a_list_when_the_check_throws(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``audit_route_surface`` logs and returns rather than propagating."""
        import frisian_mcp.route_audit as module

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(module, "_cross_carve_findings_for_route", _boom)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            findings = audit_route_surface()
        assert isinstance(findings, list)

    def test_a_failing_check_does_not_discard_the_other_findings(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        C3 — a raise in the newest check must not silence W008.

        Without per-route isolation a bug here would take out the established
        security findings for every route, not just its own.
        """
        import frisian_mcp.route_audit as module

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(module, "_cross_carve_findings_for_route", _boom)
        with _routes(
            default={
                "path": "mcp",
                "allow_list": ["permission"],
                "deny_list": ["permission"],
            }
        ):
            findings = audit_route_surface()
        assert "W008" in {f.code for f in findings}

    def test_failure_is_logged_not_swallowed_silently(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        """The operator gets a log line; the failure is not invisible."""
        import frisian_mcp.route_audit as module

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(module, "_cross_carve_findings_for_route", _boom)
        with (
            _routes(default={"path": "mcp", "allow_list": ["permission"]}),
            caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER),
        ):
            audit_route_surface()
        assert any("cross-carve" in record.message for record in caplog.records)


class TestMetadataOnly:
    """C4 — the check must not touch a queryset."""

    def test_check_runs_without_database_access(self, carved_surface: None) -> None:
        """
        No ``pytest.mark.django_db``: any query would error, not pass quietly.

        The audit runs on the app-ready path, where the database may not be
        migrated yet, and touching a queryset would also reopen the existence
        oracle the design withdrew.
        """
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]


# ---------------------------------------------------------------------------
# C5 — every production path, asserted end to end
# ---------------------------------------------------------------------------


class TestEveryProductionPath:
    """
    The audit reaches production three ways; a guard proven on one proves nothing.

    Asserting on the helper alone would leave a path silently uncovered — the
    recurring failure this project has paid for before.
    """

    def test_path_one_deferred_discovery_tail(self, carved_surface: None) -> None:
        """``_run_deferred_discovery`` finishes by calling the shared finalizer."""
        from frisian_mcp.apps import FrisianMcpConfig

        assert hasattr(FrisianMcpConfig, "_finalize_route_surfaces")
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            findings = audit_route_surface()
        assert "W017" in {f.code for f in findings}

    def test_path_two_autodiscover_off_reaches_the_finalizer(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``ready()`` with autodiscovery off really does reach the finalizer.

        Driven rather than inspected: a fresh config instance with ``_mcp_ready``
        cleared, one substitution to record the call.  A manual-registration host is
        *more* likely to hand-carve routes, so this is the path that must not be the
        unaudited one — and an early return added above the call would be invisible
        to a source-substring assertion.
        """
        import frisian_mcp
        from frisian_mcp.apps import FrisianMcpConfig

        config = FrisianMcpConfig("frisian_mcp", frisian_mcp)
        config._mcp_ready = False  # noqa: SLF001
        calls: list[str] = []
        monkeypatch.setattr(config, "_finalize_route_surfaces", lambda: calls.append("finalized"))

        with override_settings(FRISIAN_MCP_AUTODISCOVER=False):
            config.ready()

        assert calls == ["finalized"]

    def test_path_two_finalizer_actually_runs_the_cross_carve_audit(
        self, carved_surface: None, caplog: Any
    ) -> None:
        """
        And the finalizer it reaches emits the finding, rather than merely existing.

        The two halves are separate tests because they fail for different reasons: an
        early return above the call breaks the first, and a finalizer that stops
        running the audit breaks the second.
        """
        import frisian_mcp
        from frisian_mcp.apps import FrisianMcpConfig
        from frisian_mcp.route_views import route_views

        config = FrisianMcpConfig("frisian_mcp", frisian_mcp)
        with route_views._lock:  # noqa: SLF001
            saved = dict(route_views._views)  # noqa: SLF001
        try:
            with (
                _routes(default={"path": "mcp", "allow_list": ["permission"]}),
                caplog.at_level(logging.INFO, logger=AUDIT_LOGGER),
            ):
                config._finalize_route_surfaces()  # noqa: SLF001
        finally:
            with route_views._lock:  # noqa: SLF001
                route_views._views = saved  # noqa: SLF001

        assert any("W017" in record.getMessage() for record in caplog.records)

    def test_path_three_findings_reach_the_command_output(self, carved_surface: None) -> None:
        """
        The doctor prints the finding, asserted on its **output** not its source.

        The previous version of this test matched ``audit_cross_carve_surface``
        against the module source.  That passes if the call is deleted and the
        name survives in an import or a comment — which is exactly what happened:
        neutralising the check left the doctor suite fully green.  C6 makes this
        command the *only* operator-visible surface for the check, so pinning it
        by substring left that whole surface unpinned.
        """
        out, _ = _run_doctor(default={"path": "mcp", "allow_list": ["permission"]})
        assert "W017" in out
        assert "permission_create" in out
        assert TARGET_LABEL in out

    def test_doctor_never_green_ticks_an_unevaluated_posture(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed pass must not be rendered as a clean one."""
        import frisian_mcp.route_audit as module

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(module, "_cross_carve_findings_for_route", _boom)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert report.evaluated is False
        assert report.findings == ()
        assert report.reason


# ---------------------------------------------------------------------------
# C6 — the doctor is not the primary operator surface, it is the only one
# ---------------------------------------------------------------------------


class TestDoctorIsTheOnlyOperatorSurface:
    """
    The command's rendered output, asserted behaviourally.

    SOFT findings map to ``logging.INFO`` and are not mirrored to stdout, so a host
    with the root logger at WARNING sees this check *nowhere else*.  That makes every
    assertion in this class a test of the feature's entire operator-visible surface,
    not a convenience wrapper over the report object.
    """

    def test_clean_run_states_the_scope_limitation(self, carved_surface: None) -> None:
        """
        The clean path carries the scope sentence, and it is the line most at risk.

        An operator reading a clean run is exactly the person about to conclude that
        scoped credentials can write.  The sentence reads as verbose padding next to
        a green tick, which is precisely why it gets trimmed — so it is pinned here
        rather than left to a reviewer's judgement.
        """
        out, _ = _run_doctor(default={"path": "mcp", "allow_list": ["*"]})
        assert "no findings" in out
        assert "This does NOT mean writes will succeed" in out
        assert "does not evaluate any principal's Django grants" in out

    def test_clean_run_reports_what_it_actually_examined(self, carved_surface: None) -> None:
        """A count, so "clean" cannot be confused with "examined nothing"."""
        out, _ = _run_doctor(default={"path": "mcp", "allow_list": ["*"]})
        assert "write action(s) across" in out
        assert "route(s) examined" in out

    def test_findings_run_also_states_the_scope_limitation(self, carved_surface: None) -> None:
        """The same limitation is restated beside findings, not only on the green path."""
        out, _ = _run_doctor(default={"path": "mcp", "allow_list": ["permission"]})
        assert "This does NOT mean writes will succeed" in out

    def test_no_routes_reports_not_applicable_rather_than_clean(self, carved_surface: None) -> None:
        """An unconfigured host is told the audit did not apply, not that it passed."""
        out, _ = _run_doctor()
        assert "not applicable" in out
        assert "nothing is carved" in out

    def test_unevaluated_is_never_rendered_as_a_pass(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A pass that could not run says so in the output, in those words.

        This command has shipped the opposite before — green-ticking a lockdown that
        was a no-op, and naming a cache TTL on a build where the cache was disabled.
        """
        import frisian_mcp.route_audit as module

        monkeypatch.setattr(
            module,
            "_cross_carve_findings_for_route",
            _raise_injected,
        )
        out, _ = _run_doctor(default={"path": "mcp", "allow_list": ["permission"]})
        assert "could not run" in out
        assert "NOT a clean result" in out
        assert "no findings" not in out

    def test_unevaluated_under_strict_is_an_error_not_a_warning(
        self, carved_surface: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--strict`` must not exit zero on a gate that proved nothing.

        A gate that could not evaluate has established nothing, so treating it as a
        warning would let CI pass on an unexamined posture.
        """
        import frisian_mcp.route_audit as module

        monkeypatch.setattr(
            module,
            "_cross_carve_findings_for_route",
            _raise_injected,
        )
        with pytest.raises(SystemExit):
            _run_doctor(strict=True, default={"path": "mcp", "allow_list": ["permission"]})


# ---------------------------------------------------------------------------
# C1 — the volume cap, on a route large enough to trigger it
# ---------------------------------------------------------------------------


class TestVolumeCap:
    """
    The truncation branch, which every other fixture in this file is too small to reach.

    C1 was raised against a measured multiplier: the *intended* carve puts several
    cross-carve relations on a single create action, and a per-relation finding
    would emit hundreds of lines per boot times every worker process.  Aggregation
    plus ``(+N more)`` is the mitigation, so it is the thing that has to hold.

    Every other W017 fixture here carries one or two relations, so none of them
    enters the ``extra > 0`` branch at all — the shape that is invisible on a small
    fixture and only appears on a real host.

    The count and the truncation are pinned in **separate** tests on purpose: they
    regress independently.  A change that capped the total to the shown names would
    keep a correct-looking ``(+N more)`` while silently under-reporting the total,
    and a test that asserted both at once could be satisfied by either half.
    """

    #: Comfortably above ``_SURFACE_LIST_CAP`` so truncation is unambiguous.
    RELATION_COUNT = 12

    @staticmethod
    def _many_relations(count: int) -> None:
        """Register one write carrying *count* distinct unreadable relations."""
        _register("permission_list", action="list", app_label="auth", model="permission")
        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [_relation(field=f"field_{index:02d}") for index in range(count)],
        )

    def test_many_relations_still_produce_exactly_one_finding(self, clean_registry: None) -> None:
        """Aggregation holds: twelve relations are one W017, not twelve."""
        self._many_relations(self.RELATION_COUNT)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        assert _codes(report) == ["W017"]

    def test_the_reported_total_is_the_true_total_not_the_shown_count(
        self, clean_registry: None
    ) -> None:
        """
        The count names every relation found, not the five that fit in the message.

        This is the half that matters to an operator deciding whether to act: a
        total silently capped at five would read as a small problem.
        """
        self._many_relations(self.RELATION_COUNT)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")
        assert f"{self.RELATION_COUNT} required relation(s)" in message
        assert "5 required relation(s)" not in message

    def test_truncation_is_explicit_and_counts_the_remainder(self, clean_registry: None) -> None:
        """Exactly five names are shown and the remainder is declared, not dropped."""
        from frisian_mcp.route_audit import _SURFACE_LIST_CAP

        self._many_relations(self.RELATION_COUNT)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")
        shown = [f"field_{index:02d}" for index in range(self.RELATION_COUNT)]
        assert sum(name in message for name in shown) == _SURFACE_LIST_CAP
        assert f"(+{self.RELATION_COUNT - _SURFACE_LIST_CAP} more)" in message

    def test_a_route_at_the_cap_is_not_truncated(self, clean_registry: None) -> None:
        """
        Exactly five relations names all five and adds no ``(+N more)``.

        The boundary in the other direction: ``extra > 0`` must not fire at equality,
        or every real finding gains a misleading ``(+0 more)``.
        """
        from frisian_mcp.route_audit import _SURFACE_LIST_CAP

        self._many_relations(_SURFACE_LIST_CAP)
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W017")
        assert f"{_SURFACE_LIST_CAP} required relation(s)" in message
        assert "more)" not in message

    def test_w019_is_capped_on_the_same_terms(self, clean_registry: None) -> None:
        """
        The unevaluated bucket truncates too — C1 covers both, and it floods alike.

        Scope note: FK-5's re-opening named W017.  W019 shares the helper and the
        same absence of coverage, so leaving it unpinned would re-open the identical
        gap on the other bucket.
        """
        from frisian_mcp.route_audit import _SURFACE_LIST_CAP

        _register(
            "permission_create",
            action="create",
            app_label="auth",
            model="permission",
            is_write=True,
        )
        record_required_relations(
            "permission_create",
            [
                _relation(field=f"field_{index:02d}", outcome=RelationOutcome.UNEVALUATED)
                for index in range(self.RELATION_COUNT)
            ],
        )
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            report = audit_cross_carve_surface()
        message = _message(report, "W019")
        assert f"{self.RELATION_COUNT} write relation(s)" in message
        assert f"(+{self.RELATION_COUNT - _SURFACE_LIST_CAP} more)" in message


# ---------------------------------------------------------------------------
# The fixture guard — this file's own premise, asserted
# ---------------------------------------------------------------------------


class TestFixturesCanActuallyFail:
    """
    Prove the fixtures exhibit the shape, rather than assuming they do.

    A sibling harness on this project went green over fixtures that could not
    exhibit the change under test.  These two tests are the guard against this
    file becoming that: they assert the *difference* between the carved and
    uncarved configurations, so a check that never fires and a check that always
    fires both fail here.
    """

    def test_the_same_registry_is_clean_when_uncarved(self, carved_surface: None) -> None:
        """Identical tools, wildcard route: silent.  The carve is what matters."""
        with _routes(default={"path": "mcp", "allow_list": ["*"]}):
            uncarved = audit_cross_carve_surface()
        with _routes(default={"path": "mcp", "allow_list": ["permission"]}):
            carved = audit_cross_carve_surface()
        assert uncarved.findings == ()
        assert _codes(carved) == ["W017"]

    def test_the_relation_uses_a_real_required_cross_app_fk(self) -> None:
        """
        The model this fixture is built on really does have the shape.

        If Django ever made ``Permission.content_type`` nullable or gave it a
        default, the fixture would stop representing the case under test and this
        file would quietly prove nothing.
        """
        from django.contrib.auth.models import Permission

        from frisian_mcp._relations import model_label

        field = Permission._meta.get_field("content_type")  # noqa: SLF001
        assert field.null is False
        assert field.has_default() is False
        assert model_label(Permission) == OWNER_LABEL
        assert model_label(field.related_model) == TARGET_LABEL

    def test_the_optional_relation_fixture_really_resolves_optional(self) -> None:
        """
        ``LogEntry.action_time`` is the shape the override fixture needs.

        ``null=False`` with a default means ``resolve_relation`` reaches a real
        verdict of "not required" rather than declining to answer.  If Django
        ever drops that default the fixture silently stops exercising the
        deleted-relation case and the override tests would prove nothing.
        """
        from django.contrib.admin.models import LogEntry
        from rest_framework import serializers

        from frisian_mcp._relations import resolve_relation

        field = LogEntry._meta.get_field("action_time")  # noqa: SLF001
        assert field.null is False
        assert field.has_default() is True

        resolved = resolve_relation(
            serializers.PrimaryKeyRelatedField(queryset=LogEntry.objects.all(), required=False),
            "action_time",
        )
        assert resolved.outcome is RelationOutcome.RESOLVED
        assert resolved.required is False

    def test_registry_fixture_restores_the_live_registry(self) -> None:
        """
        The snapshot fixture leaves the live registry exactly as it found it.

        Driven directly inside one test rather than by registering in one test
        and checking in a later one.  An ordering-based version passes when run
        alone no matter what the fixture does, which would make it precisely the
        kind of test this file exists to avoid.
        """
        before = set(tool_registry.list_names())

        generator = clean_registry.__wrapped__()  # type: ignore[attr-defined]
        next(generator)
        _register("scratch_create", action="create", is_write=True)
        assert tool_registry.get_entry("scratch_create") is not None
        assert set(tool_registry.list_names()) != before

        with pytest.raises(StopIteration):
            next(generator)
        assert tool_registry.get_entry("scratch_create") is None
        assert set(tool_registry.list_names()) == before
        assert isinstance(tool_registry, ToolRegistry)
