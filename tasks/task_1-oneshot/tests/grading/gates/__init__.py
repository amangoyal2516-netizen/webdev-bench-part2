"""Reward gates — multiplicative factors applied to the weighted_mean of
Track A criteria. `framework_compliance` is the canonical example: 1.0 if
compliant, 0.3 if violated (caps reward at 30%).
"""

from . import framework_compliance

__all__ = ["framework_compliance"]
