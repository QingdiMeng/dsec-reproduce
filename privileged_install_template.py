"""Compatibility alias for dsec.host.install_template; canonical implementation lives there."""
import sys
from dsec.host import install_template as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
