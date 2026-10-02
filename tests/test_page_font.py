"""The participant page's typeface (#75): served by this app, as a font.

The page uses Inter from static/fonts/ rather than a font CDN, so a
participant's browser contacts nobody else. On 2026-10-01 the production image
served it as application/octet-stream: StaticFiles names a file's type from
Python's mimetypes, which knows .woff2 only where the system's MIME table does.
macOS has one and the python:3.12-slim image has none, so a test run on a Mac
passed while production sent the wrong type. The first test empties the
system tables to stand where production stands.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "static" / "v2.html"
FONT = "/static/fonts/inter-latin-wght-normal.woff2"


def test_the_font_is_served_as_a_font_where_the_system_knows_no_mime_types():
    code = (
        "import mimetypes\n"
        "mimetypes.knownfiles[:] = []\n"   # the slim image: no /etc/mime.types
        "mimetypes.init()\n"
        "assert mimetypes.guess_type('x.woff2')[0] is None, 'this stand-in still knows .woff2'\n"
        "import server.app\n"
        "from starlette.responses import FileResponse\n"
        f"print(FileResponse({str(ROOT / FONT.lstrip('/'))!r}).media_type)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "font/woff2", proc.stdout + proc.stderr


def test_the_page_loads_its_typeface_from_this_app_and_nowhere_else():
    src = V2.read_text(encoding="utf-8")
    head = src[:src.index("</style>")]
    assert re.search(rf'<link rel="preload" href="{re.escape(FONT)}" as="font" '
                     r'type="font/woff2" crossorigin />', head), "the font is not preloaded"
    face = re.search(r"@font-face \{([^}]*)\}", head)
    assert face and f'url("{FONT}")' in face.group(1), "the @font-face does not use this app's file"
    # The preload is reused only when the font request matches it, and an
    # off-site font would tell a third party who opened the page.
    assert not re.search(r"https?://", re.sub(r"<!--.*?-->|/\*.*?\*/", "", head, flags=re.S)), \
        "the page's head fetches from another origin"

    font = ROOT / FONT.lstrip("/")
    assert font.read_bytes()[:4] == b"wOF2", "the font file is not a woff2"
    assert "SIL Open Font License" in (font.parent / "LICENSE-Inter.txt").read_text(encoding="utf-8")
