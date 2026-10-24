"""Compatibility alias for dsec.runtime.isolation.network; canonical implementation lives there."""
import sys
from dsec.runtime.isolation import network as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
