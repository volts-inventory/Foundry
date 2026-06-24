"use strict";
/* Foundry Workshop — a pixel factory floor driven by REAL build telemetry. */

const cv = document.getElementById("game");
const ctx = cv.getContext("2d");
ctx.imageSmoothingEnabled = false;
const W = cv.width, H = cv.height;
const FLOOR_TOP = 96, FLOOR_BOT = H - 70;

// ---- rooms (left -> right), weighted widths --------------------------------
const ROOM_DEFS = [
  ["reception",   "RECEPTION",    1.0, "#3a4a2e"],
  ["product",     "PRODUCT",      1.7, "#2e3b5a"],
  ["architecture","ARCHITECTURE", 1.5, "#3a2e5a"],
  ["engineering", "ENGINEERING",  1.5, "#5a3a2e"],
  ["qa",          "QA · REVIEW",  1.8, "#2e5a4a"],
  ["gate",        "THE GATE",     0.95,"#444"],
  ["vault",       "VAULT",        1.15,"#5a4a18"],
];
const ROOMS = {};
(function layout(){
  const total = ROOM_DEFS.reduce((s,r)=>s+r[2],0);
  let x = 0;
  for (const [key,label,w,color] of ROOM_DEFS){
    const px = Math.round((w/total)*W);
    ROOMS[key] = {key,label,color,x0:x,x1:x+px,cx:x+px/2};
    x += px;
  }
})();

// ---- roster: who idles in which room ---------------------------------------
const COLORS = {
  "Stakeholder Proxy":"#7fd1ff","Product Manager":"#ffd95e","Criteria Critic":"#ff8fae",
  "Program Manager":"#b59bff","Principal Architect":"#c79bff","Senior Engineer":"#ff9b5e",
  "Junior Engineer":"#ffc08a","QA Engineer":"#7dffb0","SDET":"#5ddc7a","Reviewer":"#5db8ff",
  "Technical Writer":"#e8e0a0","AppSec Engineer":"#ff6d6d",
};
const ROSTER = {
  product:["Stakeholder Proxy","Product Manager","Criteria Critic","Program Manager"],
  architecture:["Principal Architect"],
  engineering:["Senior Engineer","Junior Engineer"],
  qa:["QA Engineer","SDET","Reviewer","Technical Writer"],
};

function makeAgents(){
  const ags = new Map();
  for (const room in ROSTER){
    const names = ROSTER[room], R = ROOMS[room];
    names.forEach((name,i)=>{
      const span = (R.x1-R.x0)-50;
      const hx = R.x0+30 + (names.length>1? (span*i/(names.length-1)) : span/2);
      const hy = FLOOR_BOT-20 - (i%2)*26;
      ags.set(name,{name,room,hx,hy,x:hx,y:hy+200,color:COLORS[name]||"#ccc",
                    work:0,bubble:null,seen:false});
    });
  }
  return ags;
}

// ---- game state ------------------------------------------------------------
let S = freshState();
function freshState(){
  return {
    project:null, mode:"", floor:"—",
    agents:makeAgents(), pickups:[], gates:{}, chest:"closed", chestShine:0,
    gateFlash:0, cto:{x:40,y:FLOOR_BOT-18,tx:ROOMS.reception.cx}, activeRoom:"reception",
    log:[], t:0, ended:false,
  };
}

// ---- networking ------------------------------------------------------------
let es=null;
function startProject(req,mode,supervised){
  if (es){es.close();es=null;}
  S = freshState(); S.mode=mode;
  pushLog("hot","⏳ Breaking ground…");
  fetch("/api/projects",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({request:req,mode,supervised})})
   .then(r=>r.json()).then(({id,floor})=>{
      S.floor=floor; document.getElementById("floor-tag").textContent="FLOOR "+floor;
      es=new EventSource("/api/stream/"+id);
      es.onmessage=e=>{try{applyEvent(JSON.parse(e.data));}catch(_){}};
      es.addEventListener("end",()=>{es.close();es=null;});
   }).catch(err=>pushLog("bad","✗ "+err));
}
let CURRENT_ID=null;
function sendDecision(decision){
  if(!CURRENT_ID) return;
  fetch("/api/decision/"+CURRENT_ID,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decision})});
  hideCheckpoint();
}

// ---- event handling --------------------------------------------------------
function applyEvent(ev){
  switch(ev.type){
    case "project":
      CURRENT_ID = ev.id; S.project=ev.request; S.mode=ev.mode;
      document.getElementById("proj-tag").textContent=ev.request;
      document.getElementById("mode-tag").textContent="· "+ev.mode;
      break;
    case "narrate":
      pushLog(logClass(ev.text), ev.text); break;
    case "stage":
      S.activeRoom=ev.room; if(ROOMS[ev.room]) S.cto.tx=ROOMS[ev.room].cx;
      pushLog("gold","── "+ev.label+" ──"); break;
    case "agent":{
      const a=S.agents.get(ev.agent);
      if(a){a.seen=true;a.work=1.4;}
      break;}
    case "say":{
      const a=S.agents.get(ev.agent);
      if(a){a.seen=true;a.work=1.0;a.bubble={text:ev.text,until:S.t+5.2};
            if(ev.concern) pushLog("",`💭 ${ev.agent}: "${ev.concern}"`);}
      break;}
    case "artifact":{
      const R=ROOMS[ev.room]||ROOMS.product;
      S.pickups.push({label:ev.label,x:R.cx,y:FLOOR_BOT-60,vy:-14,until:S.t+2.6});
      pushLog("good","✚ "+ev.label+"  ·  "+ev.agent);
      break;}
    case "gate":{
      S.gates[ev.name]={status:ev.status,attested:ev.attested,advisory:ev.advisory};
      S.gateFlash=0.5; renderGatesMini();
      if(!ev.advisory) pushLog(ev.status==="pass"?"good":"bad",
        `🛡 GATE ${ev.name.toUpperCase()}: ${ev.status.toUpperCase()}${ev.attested?" (signed)":""}`);
      break;}
    case "await_decision":
      showCheckpoint(); break;
    case "checkpoint":
      S._cp={title:ev.title,summary:ev.summary,boundary:ev.boundary}; break;
    case "resolved":
      hideCheckpoint(); break;
    case "release":
      S.chest="open"; S.chestShine=1; S.cto.tx=ROOMS.vault.cx; S.activeRoom="vault";
      pushLog("gold","💰 RELEASED → "+(ev.uri||"artifact")); break;
    case "halt":
      S.gateFlash=1; pushLog("bad","⛔ HELD: "+ev.reason); break;
    case "done":
      S.ended=true; pushLog(ev.passed?"good":"bad",
        ev.passed?"✅ Build complete — shipped.":"❌ Build halted — nothing shipped."); break;
  }
}
function logClass(t){
  if(/✅|💰|green|Shipped/i.test(t)) return "good";
  if(/💥|🚫|⛔|error|halt/i.test(t)) return "bad";
  if(/📋|🏭|🧑/i.test(t)) return "hot";
  return "";
}
function pushLog(cls,text){
  S.log.push({cls,text});
  const ul=document.getElementById("log");
  const li=document.createElement("li"); li.className=cls; li.textContent=text;
  ul.appendChild(li); ul.scrollTop=ul.scrollHeight;
  while(ul.children.length>200) ul.removeChild(ul.firstChild);
}
function renderGatesMini(){
  const order=["spec","build","test","review","security","audit","e2e"];
  const el=document.getElementById("gates-mini"); el.innerHTML="";
  for(const n of order){ const g=S.gates[n]; if(!g) continue;
    const s=document.createElement("span"); s.className="g "+(g.status); s.textContent=n;
    el.appendChild(s);}
}

// ---- checkpoint modal ------------------------------------------------------
function showCheckpoint(){
  const cp=S._cp||{title:"CHECKPOINT",summary:""};
  document.getElementById("cp-title").textContent="CHECKPOINT · "+(cp.title||"");
  document.getElementById("cp-summary").textContent=cp.summary||"";
  document.getElementById("cp-modal").classList.remove("hidden");
}
function hideCheckpoint(){ document.getElementById("cp-modal").classList.add("hidden"); }

// ---- rendering -------------------------------------------------------------
function loop(dt){
  S.t+=dt;
  // agents settle into home; CTO walks; pickups rise; flashes decay
  for(const a of S.agents.values()){
    a.x+=(a.hx-a.x)*Math.min(1,dt*6); a.y+=(a.hy-a.y)*Math.min(1,dt*6);
    a.work=Math.max(0,a.work-dt);
    if(a.bubble&&S.t>a.bubble.until) a.bubble=null;
  }
  S.cto.x+=(S.cto.tx-S.cto.x)*Math.min(1,dt*2.2);
  S.pickups=S.pickups.filter(p=>S.t<p.until); for(const p of S.pickups) p.y+=p.vy*dt;
  S.gateFlash=Math.max(0,S.gateFlash-dt); S.chestShine=Math.max(0,S.chestShine-dt*0.4);
  render();
}
function render(){
  ctx.fillStyle="#0c1322"; ctx.fillRect(0,0,W,H);
  for(const key in ROOMS) drawRoom(ROOMS[key]);
  drawGate(); drawChest(); drawGuardian();
  // agents (only those revealed, plus always-on roster sprites faded)
  for(const a of S.agents.values()) drawAgent(a);
  for(const p of S.pickups) drawPickup(p);
  drawCTO();
  for(const a of S.agents.values()) if(a.bubble) drawBubble(a);
}
function drawRoom(R){
  // wall band + label
  ctx.fillStyle=R.color; ctx.fillRect(R.x0,FLOOR_TOP-26,R.x1-R.x0,26);
  ctx.fillStyle="rgba(0,0,0,.25)"; ctx.fillRect(R.x0,FLOOR_TOP-26,R.x1-R.x0,4);
  ctx.fillStyle="#dfe7ff"; ctx.font="bold 13px 'Courier New',monospace"; ctx.textAlign="center";
  ctx.fillText(R.label,R.cx,FLOOR_TOP-9);
  // floor tiles
  for(let y=FLOOR_TOP;y<FLOOR_BOT+20;y+=20)
    for(let x=R.x0;x<R.x1;x+=20){
      const c=((x/20|0)+(y/20|0))&1;
      ctx.fillStyle=shade(R.color,c?0.55:0.42); ctx.fillRect(x,y,20,20);
    }
  // active-room glow
  if(R.key===S.activeRoom){ ctx.fillStyle="rgba(255,210,90,.07)"; ctx.fillRect(R.x0,FLOOR_TOP,R.x1-R.x0,FLOOR_BOT-FLOOR_TOP+20);}
  ctx.fillStyle="rgba(255,255,255,.05)"; ctx.fillRect(R.x1-2,FLOOR_TOP-26,2,FLOOR_BOT-FLOOR_TOP+46);
}
function drawAgent(a){
  const px=Math.round(a.x), py=Math.round(a.y);
  const bob=a.work>0? Math.round(Math.sin(S.t*10)*2):0;
  const dim=a.seen?1:0.32; ctx.globalAlpha=dim;
  // shadow
  ctx.globalAlpha=dim*0.3; ctx.fillStyle="#000"; ctx.fillRect(px-7,py+13,16,4); ctx.globalAlpha=dim;
  // legs/body/head (chunky)
  ctx.fillStyle="#1a2236"; ctx.fillRect(px-6,py+6-bob,4,7); ctx.fillRect(px+2,py+6-bob,4,7);
  ctx.fillStyle=a.color; ctx.fillRect(px-7,py-6-bob,14,13);
  ctx.fillStyle="#f1c9a5"; ctx.fillRect(px-5,py-15-bob,10,9);   // head
  ctx.fillStyle="#2a2018"; ctx.fillRect(px-5,py-16-bob,10,3);   // hair/hat
  if(a.work>0){ ctx.fillStyle="#fff04a"; ctx.fillRect(px+7,py-12-bob,3,3); }  // work spark
  ctx.globalAlpha=1;
}
function drawGuardian(){
  const R=ROOMS.gate, x=Math.round(R.cx), y=FLOOR_BOT-22;
  ctx.fillStyle="#9aa6c0"; ctx.fillRect(x-8,y-8,16,16);
  ctx.fillStyle="#cfd8ee"; ctx.fillRect(x-6,y-16,12,10);
  ctx.fillStyle="#39406a"; ctx.fillRect(x-8,y-2,16,3);
  ctx.fillStyle="#c0c8e0"; ctx.fillRect(x+10,y-22,2,30);  // spear
}
function drawGate(){
  const R=ROOMS.gate;
  const failed=Object.values(S.gates).some(g=>!g.advisory&&g.status==="fail")||(S.gateFlash>0&&S.chest!=="open"&&anyFail());
  const open=S.chest==="open";
  const ax=R.x0+10, aw=R.x1-R.x0-20, atop=FLOOR_TOP+6, abot=FLOOR_BOT+16;
  ctx.fillStyle="#2b3147"; ctx.fillRect(ax,atop,aw,abot-atop);            // arch frame
  ctx.fillStyle = open? "#1f8a3f": failed? "#5a1820":"#0c1322";
  ctx.fillRect(ax+6,atop+6,aw-12,abot-atop-6);
  // bars when not open
  if(!open){ ctx.fillStyle=failed?"#ff5d6c":"#566089";
    for(let bx=ax+10;bx<ax+aw-8;bx+=12) ctx.fillRect(bx,atop+8,3,abot-atop-12);
    // latch
    ctx.fillStyle=failed?"#ff5d6c":"#aab4d6"; ctx.fillRect(R.cx-7,(atop+abot)/2-7,14,14);
    ctx.fillStyle="#0c1322"; ctx.fillRect(R.cx-3,(atop+abot)/2-2,6,8);
  } else {
    ctx.fillStyle="rgba(120,255,160,.18)"; ctx.fillRect(ax+6,atop+6,aw-12,abot-atop-6);
  }
  // shields above
  const order=["spec","build","test","review","security","audit","e2e"];
  let i=0;
  for(const n of order){ const g=S.gates[n]; if(!g) continue;
    const sx=ax+12+i*26, sy=atop-2; i++;
    ctx.fillStyle=g.status==="pass"?"#1f6b3a":"#6b1f28";
    ctx.fillRect(sx,sy-14,20,18);
    ctx.fillStyle=g.status==="pass"?"#5ddc7a":"#ff5d6c";
    ctx.font="bold 12px 'Courier New'"; ctx.textAlign="center";
    ctx.fillText(g.status==="pass"?"✓":"✗",sx+10,sy);
    if(g.attested){ctx.fillStyle="#ffcf52";ctx.fillRect(sx+14,sy-14,5,5);} // signed pip
  }
}
function anyFail(){return Object.values(S.gates).some(g=>g.status==="fail");}
function drawChest(){
  const R=ROOMS.vault, x=Math.round(R.cx), y=FLOOR_BOT-18;
  // glow
  if(S.chest==="open"){ const g=0.25+0.2*Math.sin(S.t*6);
    ctx.fillStyle=`rgba(255,210,90,${0.12+S.chestShine*0.3})`; ctx.fillRect(x-40,y-60,80,80);}
  ctx.fillStyle="#000"; ctx.globalAlpha=.3; ctx.fillRect(x-18,y+12,36,5); ctx.globalAlpha=1;
  // body
  ctx.fillStyle="#7a3b1a"; ctx.fillRect(x-18,y-6,36,18);
  ctx.fillStyle="#5a2c12"; ctx.fillRect(x-18,y+6,36,6);
  if(S.chest==="open"){
    ctx.fillStyle="#ffcf52"; ctx.fillRect(x-15,y-12,30,8);          // gold spilling
    ctx.fillStyle="#fff0a8"; for(let k=0;k<5;k++) ctx.fillRect(x-13+k*6,y-14,3,3);
    ctx.fillStyle="#7a3b1a"; ctx.fillRect(x-18,y-22,36,8);          // open lid back
  } else {
    ctx.fillStyle="#8a4720"; ctx.fillRect(x-18,y-14,36,10);         // closed lid
    ctx.fillStyle="#b9892f"; ctx.fillRect(x-3,y-8,6,10);            // lock
  }
  ctx.fillStyle="#caa24a"; ctx.fillRect(x-18,y-1,36,2);             // band
}
function drawCTO(){
  const x=Math.round(S.cto.x), y=Math.round(FLOOR_BOT-18), bob=Math.round(Math.sin(S.t*5)*1);
  ctx.fillStyle="#000"; ctx.globalAlpha=.3; ctx.fillRect(x-7,y+13,16,4); ctx.globalAlpha=1;
  ctx.fillStyle="#10131c"; ctx.fillRect(x-6,y+6-bob,4,7); ctx.fillRect(x+2,y+6-bob,4,7);
  ctx.fillStyle="#222a44"; ctx.fillRect(x-7,y-6-bob,14,13);         // suit
  ctx.fillStyle="#ffcf52"; ctx.fillRect(x-1,y-5-bob,2,8);          // tie
  ctx.fillStyle="#f1c9a5"; ctx.fillRect(x-5,y-15-bob,10,9);
  ctx.fillStyle="#15110c"; ctx.fillRect(x-5,y-16-bob,10,3);
  // CTO crown tag
  ctx.fillStyle="#ffcf52"; ctx.font="9px 'Courier New'"; ctx.textAlign="center";
  ctx.fillText("CTO",x,y-22-bob);
}
function drawPickup(p){
  const a=Math.max(0,(p.until-S.t)/2.6);
  ctx.globalAlpha=a; ctx.fillStyle="#161d31"; ctx.strokeStyle="#ffcf52"; ctx.lineWidth=2;
  const w=Math.max(64,p.label.length*7+18);
  ctx.fillRect(p.x-w/2,p.y-14,w,18); ctx.strokeRect(p.x-w/2,p.y-14,w,18);
  ctx.fillStyle="#ffe9a8"; ctx.font="11px 'Courier New'"; ctx.textAlign="center";
  ctx.fillText("✚ "+p.label,p.x,p.y-1); ctx.globalAlpha=1;
}
function drawBubble(a){
  const lines=wrap(a.bubble.text,26); const w=Math.max(...lines.map(l=>l.length))*6.4+14;
  const h=lines.length*13+10; const bx=Math.round(a.x-w/2), by=Math.round(a.y-30-h);
  ctx.fillStyle="#f4f7ff"; ctx.fillRect(bx,by,w,h);
  ctx.fillStyle="#f4f7ff"; ctx.fillRect(a.x-4,by+h,8,6); // tail
  ctx.fillStyle="#1a2236"; ctx.font="11px 'Courier New'"; ctx.textAlign="left";
  lines.forEach((l,i)=>ctx.fillText(l,bx+7,by+15+i*13));
}
function wrap(t,n){const out=[],w=(t||"").split(" ");let line="";
  for(const x of w){ if((line+" "+x).trim().length>n){out.push(line.trim());line=x;} else line+=" "+x;}
  if(line.trim())out.push(line.trim()); return out.slice(0,4);}
function shade(hex,f){const n=parseInt(hex.slice(1),16);
  let r=(n>>16)&255,g=(n>>8)&255,b=n&255;
  r=Math.round(r*f);g=Math.round(g*f);b=Math.round(b*f);
  return `rgb(${r},${g},${b})`;}

// ---- main loop -------------------------------------------------------------
let last=performance.now();
function frame(now){ const dt=Math.min(0.05,(now-last)/1000); last=now; loop(dt); requestAnimationFrame(frame);}
requestAnimationFrame(frame);

// ---- UI wiring -------------------------------------------------------------
const newModal=document.getElementById("new-modal");
document.getElementById("new-btn").onclick=()=>newModal.classList.remove("hidden");
document.getElementById("cancel-btn").onclick=()=>newModal.classList.add("hidden");
document.getElementById("start-btn").onclick=()=>{
  const req=document.getElementById("req-input").value;
  const mode=document.getElementById("mode-select").value;
  const sup=document.getElementById("supervised").checked;
  newModal.classList.add("hidden"); startProject(req,mode,sup);
};
document.getElementById("cp-approve").onclick=()=>sendDecision("approve");
document.getElementById("cp-reject").onclick=()=>sendDecision("reject");

// open the new-project dialog on first load
newModal.classList.remove("hidden");
