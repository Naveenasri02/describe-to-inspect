import json
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


def _node_run(code, f):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    f.write_text(code, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def _actions():
    """busy(), post() and long() from station.js, as they are."""
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    return re.search(r"^async function busy\(.*?^const long=.*?$", js, re.M | re.S).group(0)


# a page with one card: j() answers /teach after 2.3 s and anything else at once
PAGE = ('let S={step:"Finding the strip (SAM 3.1)"},running=false,ticking=null,clicked={disabled:false},sent=[];'
        'const el=()=>({textContent:"",className:"msg"}),card=el(),box=()=>card;'
        'const show=(o,m=box())=>{m.textContent=o};'
        'const j=url=>{sent.push(url);return new Promise(r=>setTimeout(()=>r({}),url==="/teach"?2300:10))};\n')


def test_long_action_shows_its_step_and_seconds_and_cannot_be_pressed_twice(tmp_path):
    """Rehearsal: inspect about 30 s, self-test about 4 min, and the card only said "Working..."; operators press
    twice. The pressed button is off while it runs and its card shows the station's step and the seconds."""
    out = _node_run(PAGE + _actions() + '''
(async()=>{const a=long("/teach",{},"Taught."),b=long("/selftest",{},"x");
  await new Promise(r=>setTimeout(r,1200));
  console.log(JSON.stringify([clicked.disabled,card.textContent,sent]));
  await a;await b;console.log(JSON.stringify([sent,clicked.disabled,card.textContent]))})()''', tmp_path / "busy.js")
    assert json.loads(out[0]) == [True, "Finding the strip (SAM 3.1)… 1 s", ["/teach"]]
    assert json.loads(out[1]) == [["/teach"], False, "Taught."]


def test_hold_and_approvals_are_sent_at_once_even_during_a_long_action(tmp_path):
    """Review: Hold line (button and voice) went through busy(), so it was dropped without a word while a teach or
    self-test ran. It is sent at once and its answer stays in the card; long actions alone wait their turn."""
    out = _node_run(PAGE + _actions() + '''
(async()=>{const a=long("/teach",{},"Taught.");
  await new Promise(r=>setTimeout(r,1100));
  await post("/hold",{},"Line held. QA must resume it.");
  await new Promise(r=>setTimeout(r,1100));
  console.log(JSON.stringify([sent,card.textContent]));await a})()''', tmp_path / "hold.js")
    assert json.loads(out[0]) == [["/teach", "/hold"], "Line held. QA must resume it."]
    html, js = ((STATIC / f).read_text(encoding="utf-8") for f in ("index.html", "station.js"))
    assert "onclick=\"post('/hold'" in html and "onclick=\"post('/approve'" in html
    assert html.count("onclick=\"long('/teach'") == 2 and "onclick=\"long('/selftest'" in html
    voice = re.search(r"^const ACTIONS=.*?\};$", js, re.M | re.S).group(0)
    assert "hold:()=>post('/hold'" in voice  # a voice "hold" is never held back either
    for a in ("long('/selftest',{},selftestSaid,$(\"vcmd\"))", "long('/teach',{capture:true},taught,$(\"vcmd\"))",
              "inspectNow($(\"vcmd\"))"):
        assert a in voice, a  # a voice action turns the Voice button off


def test_only_the_latest_message_stays_on_screen(tmp_path):
    """Rehearsal: the QA screen showed "Leak test pass recorded", "Line resumed" and "Quarantine released." at once,
    one in each card. Only the latest message stays."""
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    show = re.search(r"^const show=.*?refresh\(\)\};$", js, re.M | re.S).group(0)
    out = _node_run('const el=t=>({textContent:t,className:"msg",style:{cssText:""},classList:{add(){}}});'
                    'const msgs=[el("Leak test pass recorded"),el("Line resumed"),el(""),el("Teaching… 3 s")];'
                    'const ticking=msgs[3];'  # a long action's own card keeps its seconds
                    'const document={querySelectorAll:()=>msgs};const box=()=>msgs[2],refresh=()=>{};\n' + show +
                    '\nshow("Quarantine released.");console.log(JSON.stringify(msgs.map(m=>m.textContent)))',
                    tmp_path / "show.js")
    assert json.loads(out[0]) == ["", "", "Quarantine released.", "Teaching… 3 s"]


def test_open_batch_totals_line_does_not_claim_reconciled():
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    assert "Reconciled when the batch ends" in js and "Reconciled against the counts" not in js


def _show():
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    return re.search(r"^const show=.*?refresh\(\)\};$", js, re.M | re.S).group(0)


def test_a_finished_long_action_never_blanks_a_line_held_message_in_its_card(tmp_path):
    """Integration: Hold pressed in the same card while an Inspect runs said "Line held. QA must resume it."; the
    inspect then finished with an empty answer (one reason: the band says it) and blanked it. The held message stays
    and a non-empty answer goes below it; a long action alone in its card still replaces its own count."""
    out = _node_run('let S={step:"Finding the strip (SAM 3.1)"},running=false,ticking=null,tickText=null,'
                    'clicked={disabled:false};const el=t=>({textContent:t||"",className:"msg",style:{cssText:""},'
                    'classList:{add(){}}});const card=el(),other=el("Leak test pass recorded");'
                    'const document={querySelectorAll:()=>[card,other]};const box=()=>card,refresh=()=>{};'
                    'const j=url=>new Promise(r=>setTimeout(()=>r({}),url==="/hold"?10:1300));\n'
                    + _show() + "\n" + _actions() + '''
const held=async()=>{await new Promise(r=>setTimeout(r,300));await post("/hold",{},"Line held. QA must resume it.")};
(async()=>{let a=busy(async m=>{await j("/inspect");show("",m)});await held();await a;console.log(JSON.stringify(card.textContent));
  a=long("/teach",{},"Taught.");await held();await a;console.log(JSON.stringify(card.textContent));
  await long("/teach",{},"Taught.");console.log(JSON.stringify([card.textContent,other.textContent,running,ticking]))})()''',
                    tmp_path / "held.js")
    assert json.loads(out[0]) == "Line held. QA must resume it."
    assert json.loads(out[1]) == "Line held. QA must resume it.\nTaught."
    assert json.loads(out[2]) == ["Taught.", "", False, None]


def test_challenge_inspect_is_long_register_and_record_result_are_sent_at_once(tmp_path):
    """Challenge Inspect is a SAM 3.1 call: a busy() action in its own card (a second press sends nothing). Register
    (QA saves the camera frame) and Record result (the Ejected tick) are sent at once, like Hold, even meanwhile."""
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    chal = [re.search(rf"^{p}.*?(\}}\);|\}})$", js, re.M | re.S).group(0) for p in (
        r"const chalInspect=\(\)=>busy\(async m=>", r"async function chalRegister\(\)", r"async function chalEject\(\)")]
    out = _node_run('let S={step:"Finding the strip (SAM 3.1)"},running=false,ticking=null,tickText=null,calls=[];'
                    'const clicked={disabled:false},card={textContent:"",className:"msg"},box=()=>card;'
                    'const show=(o,m=box())=>{m.textContent=typeof o==="string"?o:o.message};const $=()=>({value:"C1",checked:true});'
                    'class FormData{append(){}};'
                    'const fetch=u=>{calls.push(u);return new Promise(r=>setTimeout(()=>r({json:()=>({message:"done "+u,'
                    'strips:[{label:"C1"}],result:{},next:"Place C2"})}),u==="/challenge/inspect"?1500:10))};'
                    'const j=async u=>(await fetch(u)).json();\n' + _actions() + "\n" + "\n".join(chal) + '''
(async()=>{const a=chalInspect(),b=chalInspect();
  await new Promise(r=>setTimeout(r,1100));const busyNow=[clicked.disabled,card.textContent];
  await chalEject();await chalRegister();
  console.log(JSON.stringify([busyNow,calls]));await a;await b;console.log(JSON.stringify([calls,clicked.disabled]))})()''',
                    tmp_path / "chal.js")
    assert json.loads(out[0]) == [[True, "Finding the strip (SAM 3.1)… 1 s"],
                                  ["/challenge/inspect", "/challenge/eject", "/challenge/register"]]
    assert json.loads(out[1]) == [["/challenge/inspect", "/challenge/eject", "/challenge/register"], False]


def test_missed_check_notes_keep_what_qa_types_and_are_sent_at_once(tmp_path):
    """Task 35 (b): the QA tab lists each missed hourly window with a note field and a button. The 2 s refresh must
    not wipe a note being typed; the note is a QA action, sent at once (never held back by a long action)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "station.js").read_text(encoding="utf-8")
    assert 'id="missedbox"' in html and 'id="missed"' in html and 'id="mpw"' in html and 'id="mbatch"' in html
    src = re.search(r"^let missedKey=.*?show\(r\.error\?r:\"Review note saved: \"\+m\.text\+\"\.\"\)\}$", js,
                    re.M | re.S).group(0)
    assert "busy(" not in src and "missedRefresh();" in js.split("function qaTab")[1].split("\n}\n")[0]
    out = _node_run('''const el=()=>({hidden:false,value:"",children:[],append(...k){this.children.push(...k)},
  replaceChildren(){this.rebuilt=(this.rebuilt||0)+1;this.children=[]},setAttribute(){}});
const els={missedbox:el(),missed:el(),mpw:el(),mbatch:el()},$=id=>els[id],mk=()=>el(),posted=[],shown=[],urls=[];
const W=(kind,text,s)=>({kind,text,start:s,end:s+3600,batch:"B1",window:kind+" "+s});
let list=[W("leak","Leak test missed",0),W("print","Print check missed",0)];
const fetch=async u=>{urls.push(u);return {json:async()=>({missed:list})}};
const j=async(u,b)=>{posted.push([u,b]);list=list.slice(1);return {}};const show=t=>shown.push(t);
''' + src + '''
(async()=>{await missedRefresh();await missedRefresh();
  const [a,b]=els.missed.children.map(li=>li.children[2]),btn=els.missed.children[0].children[3];
  a.value="tested 10:05";b.value="print read by hand";
  await missedRefresh();els.mpw.value="pw";await btn.onclick();await missedRefresh();
  els.mbatch.value=" B0 ";await missedRefresh();
  console.log(JSON.stringify([els.missed.rebuilt,b.value,els.missed.children.map(li=>li.children[2].value),posted,
    els.mpw.value,shown,urls.slice(-1)]))})()''', tmp_path / "missed.js")
    rebuilt, typed, rows, posted, pw, shown, url = json.loads(out[0])
    assert typed == "print read by hand" and rows == ["print read by hand"]  # another row's note survives a save
    assert rebuilt == 3  # built once, then only when the windows or the batch changed (a save; another batch)
    assert posted == [["/review-missed", {"batch": "B1", "window": "leak 0", "kind": "leak", "note": "tested 10:05",
                                          "password": "pw"}]]
    assert pw == "" and shown == ["Review note saved: Leak test missed."] and url == ["/missed?batch=B0"]
