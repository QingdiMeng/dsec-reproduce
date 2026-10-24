"""Compatibility alias for dsec.runtime.lifecycle; canonical implementation lives there."""
import sys
from dsec.runtime import lifecycle as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
