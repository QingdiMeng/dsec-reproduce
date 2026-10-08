"""Compatibility alias for dsec.sdk.scheduled; canonical implementation lives there."""
import sys
from dsec.sdk import scheduled as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
