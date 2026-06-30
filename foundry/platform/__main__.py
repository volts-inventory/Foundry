"""Enable ``python -m foundry.platform``."""

from __future__ import annotations

from foundry.platform.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
