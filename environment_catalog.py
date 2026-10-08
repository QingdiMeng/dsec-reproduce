"""Compatibility alias for dsec.storage.catalog; canonical implementation lives there."""
import sys
from dsec.storage import catalog as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
