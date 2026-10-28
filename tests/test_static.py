import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGE_FILES = ["index.html", "station.css", "station.js"]
FONTS = ["IBMPlexSans-Regular", "IBMPlexSans-Medium", "IBMPlexSans-SemiBold", "IBMPlexSans-Bold",
         "IBMPlexSansCondensed-Bold"]


def _node_check(code, f):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    f.write_text(code, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("page", ["index.html", "lamp.html"])
def test_inline_scripts_parse(page, tmp_path):
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", (STATIC / page).read_text(encoding="utf-8"), re.S)
    for i, code in enumerate(scripts):
        _node_check(code, tmp_path / f"{page}.{i}.js")


@pytest.mark.parametrize("script", ["station.js"])
def test_script_files_parse(script, tmp_path):
    _node_check((STATIC / script).read_text(encoding="utf-8"), tmp_path / script)


def test_banner_colours():
    lamp = (STATIC / "lamp.html").read_text(encoding="utf-8")
    assert 'PASS:"#118a3a"' in lamp and '"NO STRIP":"#888"' in lamp
    js, css = (STATIC / "station.js").read_text(encoding="utf-8"), (STATIC / "station.css").read_text(encoding="utf-8")
    assert 'PASS:["PASSED","pass"]' in js and '"NO STRIP":["NO STRIP","grey"]' in js and 'READY:["READY","ink"]' in js
    assert "--pass:#10804A" in css and "--reject:#C22A2A" in css and "--hold:#D4940A" in css


def test_tabs_present():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nav = re.search(r"<nav[^>]*>(.*?)</nav>", html, re.S).group(1)
    assert re.findall(r"<button[^>]*>\s*(\w+)\s*</button>", nav) == ["Run", "Setup", "QA", "Record", "System"]


def test_no_external_fonts_or_scripts():
    for name in PAGE_FILES:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, name


def test_fonts_bundled_with_licence():
    css = (STATIC / "station.css").read_text(encoding="utf-8")
    for f in FONTS:
        assert (STATIC / "fonts" / f"{f}.woff2").read_bytes()[:4] == b"wOF2"
        assert f"fonts/{f}.woff2" in css
    assert "SIL OPEN FONT LICENSE" in (STATIC / "fonts" / "OFL.txt").read_text(encoding="utf-8")


def test_run_tab_has_no_spend_or_trace():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    run = re.search(r'<section[^>]*\bid="run"[^>]*>(.*?)</section>', html, re.S).group(1)
    assert 'id="trace"' in html and 'id="spent"' in html  # they live on System
    assert "spent" not in run.lower() and "trace" not in run.lower()


def test_record_frame_loads_photos_but_runs_no_scripts():
    import re
    html = open("static/index.html", encoding="utf-8").read()
    box = re.search(r'<iframe id="recframe"[^>]*sandbox="([^"]*)"', html).group(1).split()
    assert "allow-same-origin" in box and "allow-popups" in box and "allow-scripts" not in box


def test_setup_screen_says_when_the_self_test_could_not_run(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    line = re.search(r"^const selftestSaid=.*$", (STATIC / "station.js").read_text(encoding="utf-8"), re.M).group(0)
    f = tmp_path / "said.js"
    f.write_text(line + '\nfor(const r of [{passed:true,failed:[]},{passed:false,failed:["empty_pocket"]},'
                 '{passed:false,failed:["pocket finder did not answer"],not_run:"Self-test could not run: x"}])'
                 'console.log(selftestSaid(r))', encoding="utf-8")
    out = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    assert out == ["Self-test passed.", "Self-test missed: empty_pocket", "Self-test could not run: x"]


def test_end_batch_asks_first():  # a batch number cannot be used again
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    assert re.search(r"async function batchEnd\(\)\{if\(!confirm\(", js)


def test_end_batch_form_takes_counts_limits_and_reason():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    for i in ("e-received", "e-good", "e-reject", "e-samples", "e-unused", "e-fissued", "e-fused", "e-freturned",
              "e-fdestroyed", "e-cleared", "e-slimit", "e-flimit", "e-pw", "e-reason"):
        assert f'id="{i}"' in html and f'$("{i}")' in js, i
    assert "site policy" in html and "line cleared, leftover printed foil returned or destroyed" in html
    assert "confirm(" in js.split("async function batchEnd")[1].split("\n")[0] and "Totals balance" not in js
