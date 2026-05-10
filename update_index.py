#!/usr/bin/env python3
"""Stamp the current build time into docs/index.html.

Replaces the `datetime` attribute on the `meta-bar__build` <time> element with
the current UTC ISO 8601 timestamp. The Pages site's inline JS reads that
attribute at load time and renders a "X minutes ago"-style relative string
relative to the visitor's clock.

Runs as part of the update_video_feeds workflow, but only after a real feed
change so the timestamp does not generate hourly commits on its own.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

INDEX_PATH = Path("docs/index.html")
BUILD_RE = re.compile(
    r'(<time class="meta-bar__build" datetime=")[^"]*(">)[^<]*(</time>)'
)


def main() -> int:
    now = datetime.now(timezone.utc)
    iso_timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    html = INDEX_PATH.read_text(encoding="utf-8")
    new_html, n = BUILD_RE.subn(
        rf"\g<1>{iso_timestamp}\g<2>just now\g<3>",
        html,
    )
    if n != 1:
        sys.stderr.write(
            f"meta-bar__build: expected exactly 1 replacement in {INDEX_PATH}, "
            f"made {n}. Did the markup change?\n"
        )
        return 1

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Stamped {INDEX_PATH}: build={iso_timestamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
