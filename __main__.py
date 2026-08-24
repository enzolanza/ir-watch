"""Entry point: python -m ir_monitor <command>"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
