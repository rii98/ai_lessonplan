"""The event stream a chat turn yields — shared by the pipeline and every skill.

Kept in its own module so skills (which yield events) and the pipeline (which
imports skills) don't import each other. The API renders each event as one SSE frame.

Event types:

- ``token``     — a text delta of the assistant's reply
- ``sources``   — the citation list under a grounded answer
- ``intent``    — what the router decided this turn is (shown so the user sees WHY a
                  quiz appeared, and the hook for analytics / evals)
- ``status``    — a short progress line while a slow skill works ("Writing your quiz…")
- ``artifact``  — a structured interactive thing the turn produced (quiz, …); the
                  pipeline persists it and forwards a summary to the UI
- ``awaiting``  — the skill needs one more thing from the user; the pipeline records
                  it so the NEXT message is routed back to that skill
- ``done`` / ``error``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ChatEvent:
    """``type`` is one of the event types above; ``data`` its payload."""

    type: str
    data: Any
