"""Compatibility alias for dsec.storage.publication; canonical implementation lives there."""
import sys
from dsec.storage import publication as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
