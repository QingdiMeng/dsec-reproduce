"""Compatibility alias for dsec.control.local_api; canonical implementation lives there."""
import sys
from dsec.control import local_api as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
