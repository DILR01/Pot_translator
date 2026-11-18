"""Allow `python -m pot_translator` to launch the GUI application."""
import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
