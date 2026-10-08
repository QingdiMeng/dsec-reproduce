"""Compatibility alias for dsec.runtime.registry; canonical implementation lives there."""
import sys
from dsec.runtime import registry as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
