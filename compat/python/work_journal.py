"""Compatibility alias for dsec.rollout.work_journal; canonical implementation lives there."""
import sys
from dsec.rollout import work_journal as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
