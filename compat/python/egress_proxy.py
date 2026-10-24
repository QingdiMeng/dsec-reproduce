"""Compatibility alias for dsec.runtime.isolation.proxy; canonical implementation lives there."""
import sys
from dsec.runtime.isolation import proxy as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
