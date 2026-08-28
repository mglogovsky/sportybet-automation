"""SportyPilot licensing — client side.

The license server (betradar-clone) is the single judge: this package only
asks it and remembers the key locally. See PLAN-licensing.md, section REPO A.
"""

from . import client, store, config  # noqa: F401
from .gate import LicenseGate  # noqa: F401
