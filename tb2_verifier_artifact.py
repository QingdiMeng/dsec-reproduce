"""Legacy opt-in alias for the TB2.1 verifier tool artifact."""
import sys
from dsec.compat.applications import require_tb21
sys.modules[__name__] = require_tb21("verifier_artifact")
