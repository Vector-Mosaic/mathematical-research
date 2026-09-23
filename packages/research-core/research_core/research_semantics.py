"""Resolution-kind vocabulary retained by the exact Evidence relation reader.

These labels preserve recorded scope; they authorize no scheduling or effect.
"""

from __future__ import annotations

from enum import Enum


class ResolutionRequirementKind(str, Enum):
    NORMALIZATION = "normalization"
    COLLISION = "collision"
    SOURCE = "source"
    REVIEW = "review"
    SYNTHESIS = "synthesis"
    STRATEGY = "strategy"
