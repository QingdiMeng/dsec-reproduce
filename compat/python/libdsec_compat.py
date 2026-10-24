"""Compatibility alias for dsec.compat.libdsec; canonical implementation lives there."""
import sys
from dsec.compat import libdsec as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
