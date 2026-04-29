"""支持 `uv run python -m lelamp.companion`。"""
import sys
from lelamp.companion.daemon import main

if __name__ == "__main__":
    sys.exit(main())
