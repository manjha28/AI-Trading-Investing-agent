"""System-wide execution mode. Defaults to research and can never resolve to live today."""
from dataclasses import dataclass
from enum import Enum

from app.config import settings


class ExecutionMode(str, Enum):
    RESEARCH = "research"
    MONITOR = "monitor"
    LIVE = "live"


@dataclass(frozen=True)
class ModeState:
    configured: str
    effective: ExecutionMode
    live_blocked: bool
    reason: str | None


def current_mode() -> ModeState:
    """Resolve the mode. Live is never inferred from credentials, connectivity, a signal or startup.

    ``EXECUTION_MODE=live`` is treated as a configuration error that fails closed to research,
    because no live milestone has been approved and no live path exists.
    """
    configured = str(settings.execution_mode).lower()
    if configured == ExecutionMode.LIVE.value:
        return ModeState(
            configured=configured,
            effective=ExecutionMode.RESEARCH,
            live_blocked=True,
            reason="Live execution is not available: no live milestone has been approved.",
        )
    effective = ExecutionMode.MONITOR if configured == ExecutionMode.MONITOR.value else ExecutionMode.RESEARCH
    return ModeState(configured=configured, effective=effective, live_blocked=False, reason=None)
