"""Compatibility alias: work dispatch now belongs to the rollout layer."""
import sys
from dsec.rollout import scheduler as _implementation

sys.modules[__name__] = _implementation
