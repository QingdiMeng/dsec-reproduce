"""Compatibility alias for dsec.observability.meters; canonical implementation lives there."""
import sys
from dsec.observability import meters as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
