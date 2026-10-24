"""Compatibility alias for dsec.runtime.fork; canonical implementation lives there."""
import sys
from dsec.runtime import fork as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
