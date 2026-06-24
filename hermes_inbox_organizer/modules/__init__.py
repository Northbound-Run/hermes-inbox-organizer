"""Inbox feature modules + the registry that runs them.

See :mod:`hermes_inbox_organizer.modules.base` for the module contract and
:mod:`hermes_inbox_organizer.modules.registry` for how they are scheduled.
"""

from __future__ import annotations

from .base import InboundEvent, Module, PeriodicJob, SentEvent, ToolSpec
from .registry import InlineExecutor, ModuleRegistry

__all__ = [
    "InboundEvent",
    "InlineExecutor",
    "Module",
    "ModuleRegistry",
    "PeriodicJob",
    "SentEvent",
    "ToolSpec",
]
