"""Compatibility alias for dsec.rollout.environment; canonical implementation lives there."""
import sys
from dsec.rollout import environment as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
