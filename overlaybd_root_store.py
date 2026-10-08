"""Compatibility alias for dsec.storage.overlaybd; canonical implementation lives there."""
import sys
from dsec.storage import overlaybd as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
