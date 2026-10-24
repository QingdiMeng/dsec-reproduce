"""Legacy local-development alias; the product SDK uses the API Server."""
import sys
from dsec.sdk import development as _implementation
sys.modules[__name__] = _implementation
