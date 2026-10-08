"""Compatibility alias for dsec.runtime.backends.container; canonical implementation lives there."""
import sys
from dsec.runtime.backends import container as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
