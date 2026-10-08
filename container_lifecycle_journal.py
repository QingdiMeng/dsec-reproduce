"""Compatibility alias for dsec.runtime.container_journal; canonical implementation lives there."""
import sys
from dsec.runtime import container_journal as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
