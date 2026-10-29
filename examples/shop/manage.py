#!/usr/bin/env python
"""Select compose mode before settings load, then use Django's command lifecycle."""

import os
import sys

from django_compose.management import execute

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    execute(sys.argv)
