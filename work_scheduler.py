"""Compatibility alias for dsec.runtime.scheduler; canonical implementation lives there."""
import sys
from dsec.runtime import scheduler as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
