"""Compatibility alias for dsec.sdk.sandbox_transport; canonical implementation lives there."""
import sys
from dsec.sdk import sandbox_transport as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
