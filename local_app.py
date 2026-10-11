"""
Deprecated alias. The offline desktop app is now **OmniLink App**
(``omniilink_app.py``). This module re-exports it so older scripts and docs
keep working; prefer ``python omniilink_app.py``.
"""
from omniilink_app import *          # noqa: F401,F403
from omniilink_app import main       # noqa: F401

if __name__ == "__main__":
    import sys
    sys.exit(main())
