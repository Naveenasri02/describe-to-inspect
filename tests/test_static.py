import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.mark.parametrize("page", ["index.html", "lamp.html"])
def test_inline_scripts_parse(page, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", (STATIC / page).read_text(encoding="utf-8"), re.S)
    for i, code in enumerate(scripts):
        f = tmp_path / f"{page}.{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("page", ["index.html", "lamp.html"])
def test_banner_colours(page):
    text = (STATIC / page).read_text(encoding="utf-8")
    assert 'PASS:"#118a3a"' in text and '"NO STRIP":"#888"' in text
