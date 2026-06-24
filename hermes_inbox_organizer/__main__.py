"""Allow `python -m hermes_inbox_organizer` to run the setup/status CLI."""

import sys

from .cli import main

sys.exit(main())
