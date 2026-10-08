"""Compatibility alias for dsec.storage.layers; canonical implementation lives there."""
import sys
from dsec.storage import layers as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
