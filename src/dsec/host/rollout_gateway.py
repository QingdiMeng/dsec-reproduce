"""Compatibility alias for the canonical rollout ownership gateway."""
import sys
from dsec.control import rollout_gateway as _implementation

sys.modules[__name__] = _implementation
