const $=id=>document.getElementById(id);
const BAND={READY:["READY","ink"],PASS:["PASSED","pass"],REJECT:["REJECTED","reject"],HOLD:["HELD","hold"],
  LOCKED:["LOCKED","reject"],"NO STRIP":["NO STRIP","grey"],"NOT RELEASED":["NOT RELEASED","hold"]};
const SENT={PASS:"Pass counted",REJECT:"Reject signal",HOLD:"Hold signal",LOCKED:"Lock signal",NO_STRIP:"Nothing sent"};
const WAIT=["Waiting: run it on the Setup screen, step 4","Waiting: Check print (foil side up), below","Waiting for the QA signature"];
const REL=["Self-test for this batch","Start-up print check","QA signature on the start-up specimen"];
const mk=(tag,text,cls)=>{const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e};
const cap=t=>t?t.charAt(0).toUpperCase()+t.slice(1):"";
const hhmm=at=>at?String(at).slice(11,16):"";
// shortcut: shift letter from the clock (A 06-14, B 14-22, C 22-06); upgrade when the plant's shift times are set up
const shift=()=>{const h=new Date().getHours();return h>=6&&h<14?"A":h>=14&&h<22?"B":"C"};
let S={},tab="run";

function go(t){tab=t;document.querySelectorAll("main>.tab").forEach(x=>x.hidden=x.id!==t);
  document.querySelectorAll("nav button").forEach(b=>{b.classList.toggle("on",b.dataset.tab===t);b.setAttribute("aria-current",b.dataset.tab===t?"page":"false")});
  refresh()}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>go(b.dataset.tab));

// an answer shows in the card (or tab) whose button was pressed
let where=null,clicked=null,running=false,ticking=null,tickText=null;  // ticking: the box a long action counts its seconds in; tickText: its last count
document.addEventListener("click",e=>{const b=e.target.closest("button");if(b){where=b.closest(".card,.tab");clicked=b}},true);
const box=()=>(where&&!where.closest("[hidden]")&&where.querySelector(".msg"))||[...document.querySelectorAll("main .msg")].find(m=>!m.closest("[hidden]"));
const show=(o,own)=>{const m=own||box(),t=typeof o==="string"?o:(o.error||o.message||JSON.stringify(o,null,1));  // own: a long action passes its card
  if(own&&m===ticking&&tickText!==null&&m.textContent!==tickText){if(t)m.textContent+="\n"+t;return refresh()}  // another message (e.g. Line held) took the long action's card: it stays and the answer goes below; a later message replaces it
  document.querySelectorAll("main .msg").forEach(x=>{if(x!==m&&x!==ticking){x.textContent="";x.className="msg";x.style.cssText=""}});  // only the latest message stays
  m.className="msg";m.style.cssText="";
  m.textContent=t;if(o&&o.error)m.classList.add("err");refresh()};
const red=(vs,m=box())=>{if(vs.some(v=>(v.reasons||[]).some(r=>r.severity==="critical")))m.classList.add("crit")};
window.addEventListener("unhandledrejection",e=>show({error:"Could not finish: "+((e.reason&&e.reason.message)||e.reason)}));
async function j(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});return r.json()}
async function form(url,o){const r=await fetch(url,{method:"POST",body:new URLSearchParams(o||{})});return r.json()}
// a long action (SAM 3.1, DINOv2): its button is off, its own card shows the station's step and the seconds (until another
// message takes that card), and a second long action does nothing meanwhile. Hold, resume and approvals never wait here.
async function busy(fn,b=clicked){if(running)return;running=true;const m=ticking=box(),t0=Date.now();tickText=null;if(b)b.disabled=true;
  const tick=()=>{if(tickText!==null&&m.textContent!==tickText)return;m.className="msg";m.textContent=tickText=(S.step||"Working")+"… "+Math.round((Date.now()-t0)/1000)+" s"};
  tick();const iv=setInterval(tick,1000);
  try{return await fn(m)}finally{clearInterval(iv);running=false;ticking=tickText=null;if(b)b.disabled=false}}
async function post(url,b,ok,m){const r=await j(url,b);show(r.error?r:typeof ok==="function"?ok(r):ok||r,m)}  // sent at once, always
const long=(url,b,ok,btn)=>busy(m=>post(url,b,ok,m),btn);
const taught=r=>r.taught?"Taught from "+r.taught+" photos."+(r.skipped.length?" Unreadable, skipped: "+r.skipped.join(", "):""):"Saved "+r.saved+" of "+r.need+" photos.";
const selftestSaid=r=>r.not_run||(r.passed?"Self-test passed.":"Self-test missed: "+r.failed.join(", "));
const uncovered=s=>(s.selftest_skipped||[]).length?"Self-test did not cover: "+s.selftest_skipped.map(k=>k.replace(/_/g," ")).join(", "):"";
const reasonText=r=>r.text+(r.severity?" ["+r.severity+"]":"");

const inspectNow=btn=>busy(async m=>{const v=await (await fetch("/inspect",{method:"POST"})).json();
  if(v.error)return show(v,m);
  if(v.status==="NO_CHECKLIST")return show(v.reasons[0].text,m);
  show(v.reasons.length>1?"All reasons: "+v.reasons.map(reasonText).join("; "):"",m);red([v],m)},btn);
const printNow=()=>busy(async m=>{const r=await j("/print");
  if(r.error)return show(r,m);const mine=m.textContent===tickText;  // no other message took this card meanwhile
  show((r.status==="PRINT_OK"?"PRINT OK":r.by_eye?"HOLD — Check the print by eye":"HOLD")+(r.reasons.length?"\n"+r.reasons.join("\n"):""),m);
  if(mine){m.classList.add("print");m.style.background=r.status==="PRINT_OK"?"var(--pass)":"var(--hold)"}});
const trayNow=()=>busy(async m=>{const r=await j("/tray");
  show(r.verdicts?r.verdicts.map(v=>"Strip "+(v.strip_id??"-")+": "+v.status+" "+v.reasons.map(reasonText).join("; ")).join("\n"):r,m);if(r.verdicts)red(r.verdicts,m)});
const clearance=()=>busy(async m=>{const r=await form("/clearance");
  $("clr").textContent=r.error?"":(r.clear?"Clear":"Not clear: "+r.found.map(f=>f.count+" "+f.prompt).join(", "));show(r.error?r:"Line clearance: "+(r.clear?"clear":"NOT clear"),m)});
async function batchStart(){const r=await form("/batch/start",{batch:$("bn").value,mfg:$("bm").value,exp:$("be").value,mrp:$("bp").value,product:$("bpn").value,strength:$("bst").value});
  show(r.error?r:"Batch "+r.batch+" started.")}
async function leak(x){const r=await form("/leak",{result:x,note:$("bnote").value,samples:$("bsamp").value||0});
  show(r.error?r:"Leak test "+x+" recorded ("+r.samples+" samples).")}
// a flagged print read: QA looks at the strip and decides (the model only flags); sent at once, like a resume
async function printEye(x){const r=await j("/print/eye",{result:x,password:$("epw").value,note:$("enote").value});$("epw").value="";
  if(!r.error)$("enote").value="";show(r.error?r:x==="matches"?"Print matches: PRINT OK recorded.":"Wrong print: line held.")}
async function specimen(){const r=await j("/specimen/sign",{password:$("spw").value});$("spw").value="";show(r.error?r:"Start-up specimen signed.")}
async function foil(){const r=await form("/foil-change");show(r.error?r:"Foil roll change recorded.")}
async function batchEnd(){if(!confirm("End this batch? Its batch number cannot be used again."))return;
  const r=await form("/batch/end",{received:$("e-received").value,good_tray:$("e-good").value,reject_box:$("e-reject").value,samples:$("e-samples").value,unused:$("e-unused").value,
    foil_issued:$("e-fissued").value,foil_used:$("e-fused").value,foil_returned:$("e-freturned").value,foil_destroyed:$("e-fdestroyed").value,
    strips_limit:$("e-slimit").value,foil_limit:$("e-flimit").value,cleared:$("e-cleared").checked,password:$("e-pw").value,reason:$("e-reason").value});
  $("e-pw").value="";show(r.error?r:"Batch "+r.batch+" ended ("+r.totals.status+").")}
const describe=()=>busy(async m=>shown(await j("/describe",{text:$("text").value}),m));
function shown(r,m){if(r.error)return show(r,m);if(r.transcript)$("text").value=r.transcript;show("Check the rules, then Approve.",m)}
let recId="";
async function rec(){const q=new URLSearchParams({batch:$("batch").value,operator:$("op").value});
  const r=await fetch("/record?"+q);recId=r.headers.get("X-Record-Id")||"";const html=await r.text();
  if(document.querySelector('nav button[data-tab="record"]').hidden)return window.open(URL.createObjectURL(new Blob([html],{type:"text/html"})));
  $("recframe").srcdoc=html;$("recframe").hidden=false;$("recempty").hidden=true;go("record")}
async function ask(){const r=await j("/ask",{question:$("q").value});show(r.error?r:r.answer+"\n(written by the model — check before acting)")}
async function login(){const r=await j("/login",{name:$("ln").value,password:$("lp").value});$("lp").value="";show(r.error?r:"Logged in as "+r.name)}
async function logout(){await j("/logout");refresh()}
async function release(){const r=await j("/release",{password:$("rp").value,reason:$("rr").value});$("rp").value="";show(r.error?r:"Quarantine released.")}
async function sign(){const r=await j("/sign",{password:$("sp").value,record_id:recId,meaning:$("smean").value});$("sp").value="";
  show(r.error?r:"Record signed by "+r.signed_by+" — "+r.meaning+".");if(!r.error)rec()}  // re-open: the record now shows the signed state
async function investigate(){const r=await j("/investigate",{trigger:$("trig").value});if(r.error)show(r);invRefresh()}
async function answer(){const r=await j("/ask-operator-answer",{id:qId,answer:$("ans").value});$("ans").value="";show(r.error?r:"Answer sent.")}
async function approveReq(id,pw){show(await j("/approve-request",{id:id,password:pw.value}));pw.value=""}
async function declineReq(id,pw,why){show(await j("/decline-request",{id:id,password:pw.value,reason:why.value}));pw.value=""}
async function resumeLine(){show(await j("/resume",{password:$("rsp").value,reason:$("rsr").value,waiver:$("rsw").value}));$("rsp").value=""}

async function refresh(){
  let s;try{s=await (await fetch("/status")).json()}catch(e){return}  // station not answering: keep the last screen
  S=s;roles(s.user);batchBar(s);runTab(s);
  $("notrel").hidden=!(s.batch&&s.blockers.length);$("notrel").textContent="Batch not released: "+s.blockers.join(", ");
  if(tab==="setup")setupTab(s);
  if(tab==="qa")qaTab(s);
  if(tab==="system"){$("spent").textContent=s.spent_usd==null?"?":s.spent_usd.toFixed(2);traceRows()}
  quarantine(s.quarantine_count,s.quarantine_error);invRefresh();chalRefresh();recheckRefresh();
}
function roles(u){
  const w=$("who");w.replaceChildren();if(u)w.append("Signed in as ",mk("b",u.name),u.role==="qa"?" (QA)":" (Operator)");
  $("loginbox").hidden=!!u;$("logoutbtn").hidden=!u;
  document.querySelectorAll("nav button[data-role]").forEach(b=>b.hidden=!(u&&(b.dataset.role==="user"||u.role==="qa")));
  if(document.querySelector('nav button[data-tab="'+tab+'"]').hidden)go("run");  // a hidden tab is not security: the server checks QA
}
function batchBar(s){
  const b=s.batch,cl=s.checklist,cell=(id,v,none)=>{$(id).textContent=v||none;$(id).parentNode.classList.toggle("none",!v)};
  cell("b-product",cl&&cap(cl.product)+", strip of "+cl.count,"No checklist yet");
  cell("b-batch",b&&b.batch,"No batch open");
  cell("b-mfg",b&&b.mfg,"Not set");cell("b-exp",b&&b.exp,"Not set");cell("b-mrp",b&&"₹ "+b.mrp,"Not set");
  $("b-shift").textContent=shift();
  cell("b-ver",s.checklist_version&&"Version "+s.checklist_version,"Not approved");
}
// a stack light under the camera: lamp colour (ok / due / bad / none), small label, bold value
const light=(id,cls,label,value)=>{const e=$(id);e.className=cls;e.replaceChildren(mk("i",""),mk("small",label),mk("b",value))};
function runTab(s){
  const v=s.last_verdict,[word,cls]=BAND[s.state]||[s.state,"grey"];
  $("label").className="label "+cls;$("band").textContent=word;$("band").classList.toggle("long",word.length>8);
  const own=v&&!s.held&&["PASS","REJECT","HOLD","NO STRIP"].includes(s.state),r=own&&v.reasons[0];
  let why=s.text,tag="",crit=false;
  if(s.held&&s.hold_reason){why=cap(s.hold_reason);crit=/^Critical/.test(why)}
  else if(r){why=cap(r.text.replace(/^Rule \d+ failed: /,""));tag=r.rule?"Rule "+r.rule+(r.severity?", "+r.severity:""):r.severity||"";crit=r.severity==="critical"}
  else if(s.state==="PASS")why="Every checked rule passed.";
  else if(s.state==="NO STRIP")why="No strip in view.";
  else if(s.state==="READY")why="Ready for the next strip.";
  const w=$("why");w.replaceChildren(why);if(tag)w.append(mk("span",tag,"sev "+(r.severity||"")));w.classList.toggle("crit",crit);
  const notes=[];
  if(own&&v.reasons.length>1)notes.push("Also: "+v.reasons.slice(1).map(x=>x.text).join("; "));
  if(own&&(v.skipped||[]).length)notes.push("Not checked: "+v.skipped.join(", "));
  if(s.held&&s.hold_reason)notes.push(s.text);
  if(s.print_by_eye)notes.push("Print check: QA checks the print by eye");
  $("skipped").textContent=notes.join("\n");
  $("vfacts").hidden=!v&&!s.held;  // nothing judged yet: no empty Strip / Time cells
  $("stripno").textContent=v?(v.strip_id!=null?"Tray strip "+v.strip_id:s.totals?"No. "+s.totals.inspected:"No batch"):"None yet";
  $("vtime").textContent=v?String(v.at).slice(11,19):"None yet";
  $("sent").textContent=s.machine_link==null?"No machine link":s.held?"Hold signal":v?SENT[v.status]||"Nothing sent":"Nothing sent";
  const t=s.totals;[["c-pass","passed"],["c-rej","rejected"],["c-held","held"],["c-all","inspected"]].forEach(([id,k])=>$(id).textContent=t?t[k]:0);
  light("st-self",s.locked?"bad":"ok","Self-test",s.locked?"Station locked":"Passed "+hhmm(s.selftest&&s.selftest.at));
  $("st-self").title=uncovered(s);
  light("st-clear",s.batch?"ok":"","Line clearance",s.batch?"Clear "+hhmm(s.batch.clearance_at):"No batch open");
  $("st-due").hidden=!s.due.length;light("st-due","due","Hourly checks",s.due.join(" · "));
  light("st-link",s.machine_link?"ok":s.machine_link===false?"due":"","Machine link",
    s.machine_link?"Connected":s.machine_link===false?"No machine connected":"No machine link");
  const cam=$("cam");$("camres").textContent=cam.naturalWidth?cam.naturalWidth+" × "+cam.naturalHeight:"";
}

let stepWas=0,stepShown=0,slotsN=-1;
document.querySelectorAll("#steps button").forEach(b=>b.onclick=()=>{stepShown=+b.dataset.step;setupTab(S)});
function setupTab(s){
  const done=[!!(s.pending||s.checklist),!!s.checklist&&!s.pending,!!s.checklist&&s.taught,!s.locked,false];
  const now=done.indexOf(false)+1;
  if(now!==stepWas)stepWas=stepShown=now;  // follow the work, unless QA picked another step since
  document.querySelectorAll("#steps button").forEach((b,i)=>{b.className=done[i]?"done":i+1===now?"now":"";
    if(i+1===stepShown&&i+1!==now)b.classList.add("shown");b.querySelector("i").textContent=done[i]?"✓":i+1;
    b.setAttribute("aria-current",i+1===now?"step":"false")});
  document.querySelectorAll("[data-panel]").forEach(p=>p.hidden=+p.dataset.panel!==stepShown);
  const ol=$("rules");ol.replaceChildren();
  (s.pending||s.rules).forEach(t=>{const m=t.match(/^Rule (\d+)(?: \((\w+)\))?: (.*)$/)||[t,"",undefined,t];
    const li=mk("li","");li.append(mk("i",m[1]),mk("span",cap(m[3])));if(m[2])li.append(mk("em",cap(m[2]),m[2]));ol.append(li)});
  const sen=s.pending?s.pending_sentence:s.checklist&&s.checklist.sentence;
  $("sentencebox").hidden=!sen;$("sentence").textContent=sen||"";$("psentence").textContent=s.pending_sentence||"Nothing is waiting for approval.";
  const a=$("approved");a.className="approved";a.replaceChildren();
  if(s.pending){a.classList.add("wait");a.textContent="Waiting for approval"}
  else if(s.checklist)a.append("Version "+s.checklist.version+", approved by ",mk("b",s.checklist.approved_by)," at "+hhmm(s.checklist.approved_at));
  else a.textContent="No rules yet.";
  const n=s.good_photos||0;
  $("teachh").textContent=n<5?"Teach good strips: photo "+(n+1)+" of 5":"Teach good strips: "+n+" photos saved";
  if(n!==slotsN){slotsN=n;const sl=$("slots"),start=Math.max(0,n-5);sl.replaceChildren();
    for(let i=0;i<5;i++){const k=start+i,d=mk("div","","slot");
      if(k<n){const im=document.createElement("img");im.src="/good/"+k+"?n="+n;im.alt="Saved photo "+(k+1);d.append(im,mk("b","Photo "+(k+1)))}
      else{d.className="slot empty"+(k===n?" next":"");d.textContent="Photo "+(k+1)}
      sl.append(d)}}
  const t=s.selftest,ul=$("stresults");ul.replaceChildren();$("sth").hidden=!t;
  if(t){$("sth").textContent=(t.challenge?"Physical challenge "+t.challenge+" missed":t.passed?"Last self-test passed":"Last self-test missed")+" at "+hhmm(t.at);
    const row=(ok,text)=>{const li=mk("li","",ok?"":"bad");li.append(mk("span",ok?"✓":"✗"),mk("div",text));ul.append(li)};
    t.results.forEach(x=>row(x.caught&&x.named_right,cap(x.kind.replace(/_/g," "))+(x.pocket?", pocket "+x.pocket:"")+
      (x.caught?(x.named_right?": caught and named":": caught, named wrong"):": missed")));
    if(!t.challenge)row(t.good_passed,t.good_passed?"Good strip passed":"Good strip not passed")}
  $("uncovered").textContent=uncovered(s);
}
function qaTab(s){
  const b=s.batch,unreadable=s.blockers.some(x=>!REL.includes(x)&&x!==CHAL),clr=$("clr").textContent==="Clear";
  const ok=x=>!!b&&!unreadable&&!s.blockers.includes(x),wait=(x,k)=>ok(x)?"Done":b?WAIT[k]:"After the batch starts";
  const at=t=>t?new Date(t*1000).toTimeString().slice(0,5):"";
  // the release sequence: [step, done, what it says, when]; the first step not done is the current one
  const steps=[["q-clear",!!b||clr,b?"Clear at "+hhmm(b.clearance_at)+", before batch "+b.batch:clr?"Clear":"Run it before the batch starts",b&&hhmm(b.clearance_at)],
    ["q-batch",!!b,b?b.batch+", MFG "+b.mfg+", EXP "+b.exp+", MRP ₹ "+b.mrp:"Batch no., MFG, EXP, MRP, and the product as printed",b&&at(b.time)],
    ["q-self",ok(REL[0]),wait(REL[0],0),ok(REL[0])&&s.selftest&&hhmm(s.selftest.at)],
    ["q-chal",ok(CHAL),ok(CHAL)?"Done":b?"Waiting: Challenge run, on the Run screen":"After the batch starts"],
    ["q-print",ok(REL[1]),b&&!ok(REL[1])&&s.print_by_eye?"Waiting: Check the print by eye, below":wait(REL[1],1)],
    ["q-spec",ok(REL[2]),wait(REL[2],2)],
    ["q-ipqc",false,b?"Leak test and print check every hour; a missed one needs a QA review note":"After the batch starts"],
    ["q-end",false,b?"Type the counts at the bench, then End batch":"After the batch starts"]];
  $("q-chal").hidden=!C.set;
  let now=false,n=0;
  steps.forEach(([id,done,d,when])=>{const li=$(id);if(li.hidden)return;const cur=!done&&!now;now=now||cur;
    li.className=done?"done":cur?"now":"";li.setAttribute("aria-current",cur?"step":"false");
    li.querySelector(".n b").textContent=++n;li.querySelector(".d").textContent=d;li.querySelector(".when").textContent=when||""});
  $("spw").closest(".field").hidden=!(b&&s.blockers.includes(REL[2]));
  $("eyebox").hidden=!(b&&s.print_by_eye);$("eyewhy").textContent=(s.print_by_eye||[]).join("; ");
  $("nobatch").hidden=!!b;$("inbatch").hidden=!b;$("w-clear").hidden=!!b;$("endbox").hidden=!b;
  // the print check sits in the start-up step until it is done, then with the hourly checks
  const startup=!!b&&s.blockers.includes(REL[1]),home=startup?$("w-print"):$("inbatch");$("w-print").hidden=!startup;
  if($("printbox").parentNode!==home)home.prepend($("printbox"));
  $("due").hidden=!s.due.length;$("due").textContent=s.due.join(". ")+".";
  $("heldh").textContent=s.held?"Line held":"Line running";$("linecard").className="card line "+(s.held?"held":"running");
  $("heldbox").hidden=$("resumebox").hidden=!s.held;$("runningnote").hidden=s.held;
  $("heldby").textContent=s.text;$("heldwhy").textContent=s.hold_reason;$("heldbox").classList.toggle("crit",/^Critical/.test(s.hold_reason));
  const t=s.totals,tb=$("recon");tb.replaceChildren();
  if(t)[["Passed",t.passed],["Rejected",t.rejected],["Held for QA",t.held],["No strip found",t.no_strip],["Inspected",t.inspected,"sum"]]
    .forEach(([k,n,c])=>{const tr=mk("tr","",c);tr.append(mk("td",k),mk("td",n));tb.append(tr)});
  $("bal").className="bal";$("bal").textContent=t?"Reconciled when the batch ends":"No batch open";
  // the one main action on this screen: the next thing QA has to do (a supervisor request waiting for QA comes first)
  const main=reqN?"":s.held?"go-resume":!b?(clr?"go-start":"go-clear"):s.blockers.includes(REL[1])?"go-print":s.blockers.includes(REL[2])?"go-spec":"";
  document.querySelectorAll("#qa .go").forEach(x=>x.classList.toggle("primary",x.id===main));
  missedRefresh();
}
// missed hourly windows of a batch (blank = the last started): each needs a QA review note before its record is signed
let missedKey="",missedNotes={};  // missedNotes: the note fields by window, so a rebuild keeps what QA typed in other rows
async function missedRefresh(){
  const bt=$("mbatch").value.trim();
  let r;try{r=await (await fetch("/missed?batch="+encodeURIComponent(bt))).json()}catch(e){return}
  const ms=r.missed||[],key=bt+"|"+JSON.stringify(ms.map(m=>m.window));
  if(key===missedKey)return;missedKey=key;  // the same windows: keep the notes being typed
  const ul=$("missed"),old=missedNotes;ul.replaceChildren();missedNotes={};
  if(!ms.length){const li=mk("li","");li.append(mk("span","✓"),mk("div","No missed hourly check waits for a note"));ul.append(li)}
  ms.forEach((m,k)=>{const li=mk("li","","todo"),at=t=>new Date(t*1000).toTimeString().slice(0,5);
    const note=mk("input");note.id="missed-note-"+k;note.placeholder="Review note";note.setAttribute("aria-label","Review note: "+m.text+" "+at(m.start));
    note.value=old[m.window]?old[m.window].value:"";missedNotes[m.window]=note;
    const btn=mk("button","Add review note (QA)","btn");btn.onclick=()=>reviewMissed(m,note);
    const lab=mk("label","Batch "+m.batch+": "+m.text+" "+at(m.start)+"–"+at(m.end));lab.htmlFor=note.id;  // the visible label of the note
    li.append(mk("span","!"),lab,note,btn);ul.append(li)})}
async function reviewMissed(m,note){const r=await j("/review-missed",{batch:m.batch,window:m.window,kind:m.kind,note:note.value,password:$("mpw").value});  // QA action: sent at once
  $("mpw").value="";if(!r.error){missedKey="";note.value=""}show(r.error?r:"Review note saved: "+m.text+".")}
async function quarantine(n,err){
  $("qbanner").hidden=$("qrel").hidden=!n&&!err;
  if(err){$("qbanner").textContent="Quarantine status unknown — check the record";$("qlist").replaceChildren();return}
  if(!n)return;
  $("qbanner").textContent="Quarantine: "+n+" strips since the last passed self-test — QA release needed";
  if(tab!=="qa")return;
  const q=await (await fetch("/quarantine")).json(),ul=$("qlist");ul.replaceChildren();
  (q.strips||[]).slice(-10).forEach(v=>ul.appendChild(mk("li",(v.at||"")+" "+v.status)));
}
async function traceRows(){
  const rows=await (await fetch("/trace")).json(),tb=$("trace");tb.replaceChildren();
  rows.slice(-12).reverse().forEach(e=>{const tr=document.createElement("tr");
    [e.model+" — "+e.platform,e.job+(e.ok?"":" (failed)"),e.ms,(e.usd||0).toFixed(4)].forEach(x=>tr.appendChild(mk("td",x)));tb.appendChild(tr)});
}
let reqSig="",qId="",reqN=0;  // reqN: supervisor requests waiting for QA
// a tool result in words: "brightness 184.2, drift false"
const said=o=>o&&typeof o==="object"&&!Array.isArray(o)?Object.entries(o).map(([k,v])=>k.replace(/_/g," ")+" "+(v&&typeof v==="object"?JSON.stringify(v):v)).join(", "):JSON.stringify(o);
const labelled=(text,input)=>{const l=mk("label","","fl grow");l.append(mk("span",text),input);return l};
async function invRefresh(){
  const i=await (await fetch("/investigation")).json(),plat=i.result&&i.result.platform;
  $("invstate").textContent=i.running?"Investigating...":(i.result?(i.result.error||"Done"):"");
  if(plat)$("supmodel").textContent=  // the model that actually answered
    {"Meta API":"Muse Spark 1.3 on the Meta API, fallback",none:"no model answered"}[plat]||"Muse Spark Open, open weights";
  const note="Written by "+(plat==="Meta API"?"Muse Spark 1.3":"Muse Spark Open")+". Check before acting.";
  $("supnote").textContent=note;
  const ol=$("steplist");ol.replaceChildren();$("checked").hidden=!i.steps.length;$("supnote").hidden=!i.steps.length&&!(i.result&&i.result.conclusion);
  i.steps.forEach(t=>{const li=mk("li",""),sp=mk("span","");sp.append(mk("b",cap(t.tool.replace(/_/g," "))+": "),
    (Object.keys(t.args||{}).length?"("+said(t.args)+") ":"")+said(t.result).slice(0,160));li.append(sp);ol.appendChild(li)});
  $("askbox").hidden=!i.question;if(i.question){qId=i.question.id;$("askq").textContent=i.question.text}
  const c=$("concl");c.replaceChildren();if(i.result&&i.result.conclusion)c.append(mk("b","Conclusion: "),i.result.conclusion);
  reqN=i.pending.length;
  const r=$("reqs"),sig=JSON.stringify(i.pending)+note;if(sig===reqSig)return;reqSig=sig;r.replaceChildren();  // keep a typed password while polling
  i.pending.forEach(q=>{const d=mk("div","","req");d.append(mk("b",cap(q.kind.replace(/_/g," "))+": "),q.reason,mk("div",note,"model"));
    const f=mk("div","","field"),pw=document.createElement("input");pw.type="password";pw.setAttribute("aria-label","QA password");
    const b=mk("button","Approve (QA)","btn primary");b.onclick=()=>approveReq(q.id,pw);
    const why=document.createElement("input");why.setAttribute("aria-label","Reason to decline");
    const x=mk("button","Decline (QA)","btn");x.onclick=()=>declineReq(q.id,pw,why);f.append(labelled("QA password",pw),b,labelled("Reason to decline",why),x);d.append(f);r.appendChild(d)});
}
// Speak rule: mono audio, downsampled to 16 kHz 16-bit PCM WAV
let ctx,proc,chunks=[],recording=false,recMode="rule";
async function toggleRec(mode){
  if(recording){recording=false;proc.disconnect();ctx.close();$("rec").textContent="Speak rule";$("vcmd").textContent="Voice command";return send(wav(chunks,ctx.sampleRate),recMode)}
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  ctx=new AudioContext();chunks=[];
  const srcN=ctx.createMediaStreamSource(stream);proc=ctx.createScriptProcessor(4096,1,1);
  proc.onaudioprocess=e=>chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  srcN.connect(proc);proc.connect(ctx.destination);recording=true;recMode=mode;$(mode==="command"?"vcmd":"rec").textContent="Stop";
}
function wav(chunks,rate){
  const n=chunks.reduce((a,c)=>a+c.length,0),all=new Float32Array(n);let o=0;chunks.forEach(c=>{all.set(c,o);o+=c.length});
  const ratio=rate/16000,len=Math.floor(n/ratio),pcm=new DataView(new ArrayBuffer(44+len*2));
  const w=(p,s)=>[...s].forEach((c,i)=>pcm.setUint8(p+i,c.charCodeAt(0)));
  w(0,"RIFF");pcm.setUint32(4,36+len*2,true);w(8,"WAVEfmt ");pcm.setUint32(16,16,true);pcm.setUint16(20,1,true);pcm.setUint16(22,1,true);
  pcm.setUint32(24,16000,true);pcm.setUint32(28,32000,true);pcm.setUint16(32,2,true);pcm.setUint16(34,16,true);w(36,"data");pcm.setUint32(40,len*2,true);
  for(let i=0;i<len;i++){const s=Math.max(-1,Math.min(1,all[Math.floor(i*ratio)]));pcm.setInt16(44+i*2,s<0?s*0x8000:s*0x7fff,true)}
  return new Blob([pcm],{type:"audio/wav"});
}
const ACTIONS={self_test:()=>long('/selftest',{},selftestSaid,$("vcmd")),teach:()=>long('/teach',{capture:true},taught,$("vcmd")),
  inspect:()=>inspectNow($("vcmd")),record:rec,hold:()=>post('/hold',{reason:'operator voice command'},'Line held. QA must resume it.')};
async function send(blob,mode){
  show(mode==="command"?"Listening to the voice command…":"Turning the spoken rule into a checklist…");$("heard").textContent="";const f=new FormData();f.append("audio",blob,"rule.wav");
  const l=$("lang").value;if(l!=="Auto")f.append("languages",l);
  if(mode==="command"){
    let r;try{const res=await fetch("/command",{method:"POST",body:f});if(!res.ok)throw new Error("HTTP "+res.status);r=await res.json()}
    catch(e){return show({error:"Voice command failed: "+e.message+". Use the buttons."})}
    if(r.error)return show(r);
    if(!ACTIONS[r.command]){$("heard").textContent="Heard: "+r.transcript;return show("Command not understood — use the buttons")}
    $("heard").textContent="Heard: "+r.transcript+" — Command: "+r.command;return ACTIONS[r.command]()}
  shown(await (await fetch("/describe",{method:"POST",body:f})).json());
}
// Physical challenge: QA registers known-bad strips (QA screen); any logged-in user runs them (Run screen)
const CHAL="Physical challenge for this batch";
let C={};
async function chalRefresh(){
  if(!S.user){$("chal").hidden=true;return}
  let c;try{c=await (await fetch("/challenge")).json()}catch(e){return}
  if(c.error)return;C=c;
  const set=c.set?c.set.strips:[],ul=$("chset");ul.replaceChildren();
  set.forEach(x=>{const li=mk("li","");li.append(mk("span","✓"),mk("div",x.label+": "+x.kind.replace(/_/g," ")+", expect Rule "+x.rule));ul.append(li)});
  if(!set.length){const li=mk("li","","off");li.append(mk("span","!"),mk("div","No physical challenge set registered"));ul.append(li)}
  const k=$("ck"),cl=S.checklist;
  if(cl&&k.dataset.v!==String(cl.version)){k.dataset.v=cl.version;  // the checklist's kinds; Rule = position + 2
    k.replaceChildren(...cl.reject_if.map((x,i)=>{const o=mk("option",x.replace(/_/g," "));o.value=x;o.dataset.rule=i+2;return o}));
    k.onchange=()=>$("cr").value=k.selectedOptions[0]?k.selectedOptions[0].dataset.rule:"";k.onchange()}
  $("chal").hidden=!c.set;if(!c.set)return;
  const p=c.pending,lab=p?p.label:c.strip&&c.strip.label;
  $("chnext").textContent=p?(p.caught?p.label+" rejected for Rule "+p.expected_rule+": tick Ejected once it is out"
    :p.label+" missed ("+p.why+"): station locked. Take "+p.label+" out — it is a known-bad strip"):c.next;
  const im=$("chphoto"),src="/challenge/photo/"+lab+"?at="+encodeURIComponent(c.set.at);im.hidden=!lab;if(lab&&im.getAttribute("src")!==src)im.src=src;
  $("chgo").hidden=!!p;$("chrec").hidden=$("chejl").hidden=!p;
  const r=$("chres"),last=!c.results.length&&!p&&c.last;r.replaceChildren();
  if(last)r.append(mk("li","Last run "+(last.passed?"passed ":"failed ")+hhmm(last.at),last.passed?"ok":"bad"));
  (last?last.results:c.results).forEach(x=>r.append(mk("li",x.label+(x.passed?" ✓":" ✗"),x.passed?"ok":"bad")));
}
// Register (QA saves the camera frame) and Record result are sent at once, like Hold; only Inspect (SAM 3.1) is long
async function chalRegister(){const f=new FormData();f.append("label",$("cl").value);f.append("kind",$("ck").value);f.append("rule",$("cr").value);
  const r=await (await fetch("/challenge/register",{method:"POST",body:f})).json();show(r.error?r:"Challenge set: "+r.strips.map(x=>x.label).join(", ")+"."+(r.message?"\n"+r.message:""))}
const chalInspect=()=>busy(async m=>{const r=await (await fetch("/challenge/inspect",{method:"POST"})).json();show(r.error?r:r.message,m)});
async function chalClose(){const r=await j("/challenge/close",{password:$("ccp").value,reason:$("ccr").value});$("ccp").value="";
  show(r.error?r:"Failed challenge closed with a QA note.")}
async function chalEject(){const r=await j("/challenge/eject",{ejected:$("chej").checked});$("chej").checked=false;
  show(r.error?r:r.next?r.next:"Challenge run "+(r.run.passed?"passed.":"failed: station locked, quarantine open."))}
// Re-check window after a critical hold: each Inspect re-checks one strip; Resume needs it done or a waiver reason
const recheckSaid=w=>w.open?w.text+" · "+w.count+": inspect each one with Inspect strip":"";
async function recheckRefresh(){
  let w;try{w=await (await fetch("/recheck")).json()}catch(e){return}
  if(w.error)return;
  $("rcbanner").textContent=recheckSaid(w);$("rcbanner").hidden=$("rswbox").hidden=!w.open;
}
refresh();setInterval(refresh,2000);
