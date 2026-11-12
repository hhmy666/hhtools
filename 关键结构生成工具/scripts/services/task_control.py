"""Thread-safe cooperative controls for long-running local operations.

Workers opt in by calling :meth:`CooperativeTaskControl.checkpoint` between
bounded units of work.  Pause never interrupts a file write or solver call in
the middle; cancel is raised only at a checkpoint, leaving all prior durable
work and completed result rows intact.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass

from puzzle_core.errors import OperationCancelledError


@dataclass(frozen=True)
class TaskControlSnapshot:
    state: str
    phase: str | None
    can_pause: bool
    can_resume: bool
    can_cancel: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class CooperativeTaskControl:
    """A small state machine shared by generation and export workers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._state = "running"
        self._phase: str | None = None

    def request_pause(self) -> bool:
        with self._condition:
            if self._state != "running":
                return False
            self._state = "pause_requested"
            return True

    def resume(self) -> bool:
        with self._condition:
            if self._state not in {"pause_requested", "paused"}:
                return False
            self._state = "running"
            self._condition.notify_all()
            return True

    def request_cancel(self) -> bool:
        with self._condition:
            if self._state in {
                "cancel_requested",
                "cancelled",
                "committing",
                "completed",
            }:
                return False
            self._state = "cancel_requested"
            self._condition.notify_all()
            return True

    def begin_uninterruptible(self, phase: str) -> None:
        """Cross a commit boundary after honoring all pending controls.

        New pause/cancel requests are rejected until the caller marks the
        operation completed.  This is used for the crash-safe export publish
        window, where voluntary interruption could create orphan staging.
        """

        self.checkpoint(phase)
        with self._condition:
            self._phase = phase
            self._state = "committing"

    def checkpoint(self, phase: str = "checkpoint") -> None:
        """Pause or cancel at a safe boundary selected by the worker."""

        with self._condition:
            self._phase = phase
            if self._state == "pause_requested":
                self._state = "paused"
            while self._state == "paused":
                self._condition.wait()
            if self._state in {"cancel_requested", "cancelled"}:
                self._state = "cancelled"
                raise OperationCancelledError(
                    "任务已在安全检查点取消",
                    evidence={"phase": phase},
                )

    def mark_completed(self) -> None:
        with self._condition:
            if self._state not in {"cancel_requested", "cancelled"}:
                self._state = "completed"
            self._condition.notify_all()

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    def snapshot(self) -> TaskControlSnapshot:
        with self._condition:
            return TaskControlSnapshot(
                state=self._state,
                phase=self._phase,
                can_pause=self._state == "running",
                can_resume=self._state in {"pause_requested", "paused"},
                can_cancel=self._state
                not in {
                    "cancel_requested",
                    "cancelled",
                    "committing",
                    "completed",
                },
            )
