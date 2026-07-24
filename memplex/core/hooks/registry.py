"""HookRegistry -- registry for lifecycle hook handlers.

Design spec §4.2: HookRegistry is the central event dispatch system that
adapters register with.  Hooks are synchronous and thread-safe.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from memplex.core.hooks.hook_event import HookEvent

logger = logging.getLogger(__name__)


class HookRegistry:
    """Central registry for lifecycle hook handlers.

    Adapters register handlers for specific events; the registry dispatches
    to all registered handlers when an event is triggered.

    Thread-safe: uses a ``threading.RLock`` to protect handler registration
    and dispatch.

    Parameters
    ----------
    on_error:
        Called when a handler raises an exception.  If ``None``, exceptions
        are logged and swallowed.  Default: ``None``.
    """

    def __init__(self, on_error: Optional[Callable[[HookEvent, Exception], None]] = None) -> None:
        self._handlers: Dict[HookEvent, List[Callable]] = defaultdict(list)
        self._lock = threading.RLock()
        self._on_error = on_error

    # ── Registration ────────────────────────────────────────────────

    def register(self, event: HookEvent, handler: Callable) -> None:
        """Register *handler* to be called when *event* is triggered.

        Parameters
        ----------
        event:
            The event type to listen for.
        handler:
            A callable that accepts a ``dict`` context parameter.
            The dict contains event-specific data (e.g. ``tool_name``,
            ``tool_result`` for ``POST_TOOL_USE``).
        """
        with self._lock:
            if handler not in self._handlers[event]:
                self._handlers[event].append(handler)

    def unregister(self, event: HookEvent, handler: Callable) -> None:
        """Remove *handler* from *event*.  Silently succeeds if not registered."""
        with self._lock:
            try:
                self._handlers[event].remove(handler)
            except ValueError:
                pass

    # ── Dispatch ─────────────────────────────────────────────────

    def trigger(self, event: HookEvent, context: Optional[dict] = None) -> None:
        """Fire *event* with *context* to all registered handlers.

        Handlers are called sequentially in registration order.  If a handler
        raises, ``on_error`` is invoked; the default (``None``) logs the error
        and continues to the next handler.

        Parameters
        ----------
        event:
            The event type to fire.
        context:
            Arbitrary data passed to each handler.  Shape is event-specific.
        """
        context = context or {}
        with self._lock:
            handlers = list(self._handlers.get(event, []))

        for handler in handlers:
            try:
                handler(context)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(event, exc)
                else:
                    logger.warning(
                        "Hook handler %s raised for event %s: %s",
                        getattr(handler, "__name__", repr(handler)),
                        event.value,
                        exc,
                    )

    # ── Introspection ────────────────────────────────────────────

    def list_events(self) -> List[HookEvent]:
        """Return all event types that have at least one registered handler."""
        with self._lock:
            return [e for e in HookEvent if self._handlers[e]]

    def list_handlers(self, event: HookEvent) -> List[Callable]:
        """Return a snapshot of handlers registered for *event*."""
        with self._lock:
            return list(self._handlers.get(event, []))


# ── Global default registry ────────────────────────────────────────────


_default_registry: Optional[HookRegistry] = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> HookRegistry:
    """Return the process-wide default HookRegistry (lazily created)."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = HookRegistry()
        return _default_registry
