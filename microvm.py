"""Compatibility alias for dsec.runtime.backends.firecracker; canonical implementation lives there."""
import sys
from dsec.runtime.backends import firecracker as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
