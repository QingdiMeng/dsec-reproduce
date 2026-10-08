"""Compatibility alias for dsec.observability.shared; canonical implementation lives there."""
import sys
from dsec.observability import shared as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
