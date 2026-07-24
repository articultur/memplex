"""Test core/hooks: HookEvent, HookRegistry, and ObservationCollector.

Previously zero coverage (evaluation: core/hooks/ at 0%). Covers the
hook event enum, registry register/unregister/trigger/error-handler
contract, and the ObservationCollector rate-limit + dedup + attach logic
against a stub store.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.core.hooks.collector import ObservationCollector  # noqa: E402
from memplex.core.hooks.hook_event import HookEvent  # noqa: E402
from memplex.core.hooks.registry import HookRegistry, get_default_registry  # noqa: E402

# ── HookEvent enum ───────────────────────────────────────────────────


def test_hook_event_values_are_strings():
    for e in HookEvent:
        assert isinstance(e.value, str)


def test_hook_event_has_post_tool_use():
    assert HookEvent.POST_TOOL_USE.value == "post_tool_use"


# ── HookRegistry ─────────────────────────────────────────────────────


def test_registry_register_and_trigger():
    reg = HookRegistry()
    seen = []
    reg.register(HookEvent.SESSION_START, lambda ctx: seen.append(ctx))
    reg.trigger(HookEvent.SESSION_START, {"x": 1})
    assert seen == [{"x": 1}]


def test_registry_unregister_stops_calls():
    reg = HookRegistry()

    def handler(ctx):
        seen.append(ctx)

    seen = []
    reg.register(HookEvent.AGENT_STOP, handler)
    reg.unregister(HookEvent.AGENT_STOP, handler)
    reg.trigger(HookEvent.AGENT_STOP)
    assert seen == []


def test_registry_multiple_handlers_all_called_in_order():
    reg = HookRegistry()
    order = []
    reg.register(HookEvent.USER_PROMPT_SUBMIT, lambda ctx: order.append("a"))
    reg.register(HookEvent.USER_PROMPT_SUBMIT, lambda ctx: order.append("b"))
    reg.trigger(HookEvent.USER_PROMPT_SUBMIT)
    assert order == ["a", "b"]


def test_registry_trigger_with_no_handlers_does_not_raise():
    reg = HookRegistry()
    reg.trigger(HookEvent.SESSION_END)  # no handlers registered


def test_registry_on_error_callback_receives_exception():
    errors = []
    reg = HookRegistry(on_error=lambda evt, exc: errors.append((evt, exc)))

    def boom(ctx):
        raise ValueError("boom")

    reg.register(HookEvent.PRE_TOOL_USE, boom)
    reg.trigger(HookEvent.PRE_TOOL_USE)  # must not propagate
    assert errors and isinstance(errors[0][1], ValueError)


def test_registry_list_events_and_handlers():
    reg = HookRegistry()

    def h(ctx):
        pass

    reg.register(HookEvent.SESSION_START, h)
    events = reg.list_events()
    assert HookEvent.SESSION_START in events
    assert h in reg.list_handlers(HookEvent.SESSION_START)


def test_get_default_registry_is_singleton():
    assert get_default_registry() is get_default_registry()


# ── ObservationCollector ─────────────────────────────────────────────


class _StubStore:
    def __init__(self):
        self.observations = []

    def add_observation(self, obs):
        self.observations.append(obs)


def test_collector_constructs_with_stub_store():
    c = ObservationCollector(store=_StubStore())
    assert c is not None


def test_collector_attach_detach_on_registry():
    reg = HookRegistry()
    store = _StubStore()
    c = ObservationCollector(store=store, registry=reg)
    c.attach()
    assert reg.list_handlers(HookEvent.POST_TOOL_USE)
    c.detach()
    assert not reg.list_handlers(HookEvent.POST_TOOL_USE)


def test_collector_rate_limit_blocks_after_max():
    """Above max_per_minute, _check_rate_limit returns False."""
    c = ObservationCollector(store=_StubStore(), max_per_minute=2)
    assert c._check_rate_limit() is True  # 1st
    assert c._check_rate_limit() is True  # 2nd
    assert c._check_rate_limit() is False  # 3rd blocked


def test_collector_dedup_skips_consecutive_identical():
    store = _StubStore()
    c = ObservationCollector(store=store)
    ctx = {"tool_name": "Read", "tool_input": {"path": "x"}, "tool_result": "ok"}
    c._on_tool_use(dict(ctx))
    first_count = len(store.observations)
    c._on_tool_use(dict(ctx))  # identical consecutive -> deduped
    assert len(store.observations) == first_count
