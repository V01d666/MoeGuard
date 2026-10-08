"""Display the installed package version, including experiment suffixes."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from moeguard import __version__


def installed_version() -> str:
    if getattr(sys, "frozen", False):
        try:
            manifest = ET.parse(Path(sys.executable).parent / "sq.version")
            for node in manifest.iter():
                if node.tag.rsplit("}", 1)[-1] == "version" and node.text:
                    return node.text.strip()
        except (OSError, ET.ParseError):
            pass
    return __version__
