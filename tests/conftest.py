"""Makes `import sdd_client` resolve to
custom_components/bnd_smart_hub/sdd_client.py from plain pytest, matching how
the canonical copy's own test suite imports it (see
../CLAUDE.md "Where the protocol write-up lives"). sdd_client.py has no
relative imports of its own, so - unlike helpers.py in test_helpers.py - a
simple sys.path insertion is enough; no stub package/importlib trick needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "bnd_smart_hub"))
