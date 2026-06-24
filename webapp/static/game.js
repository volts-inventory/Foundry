"use strict";
/* Foundry Workshop — a Phaser 3 pixel factory floor driven by REAL build telemetry.
 *
 * The engine (Phaser) owns the scene graph, tweens, particles, depth-sorting and
 * timed lifecycles. This file only describes the workshop and reacts to the SAME
 * Server-Sent-Events the vanilla renderer consumed — so server.py / events.py are
 * untouched. Sprites are generated procedurally (no binary art) so the whole app
 * stays offline, like the rest of Foundry. Nothing here can influence a build; the
 * signed gates decide and this is only the view.
 */

const W = 1280, H = 620;
const FLOOR_TOP = 96, FLOOR_BOT = H - 70;

// ---- rooms (left -> right), weighted widths --------------------------------
const ROOM_DEFS = [
  ["reception",    "RECEPTION",    1.0,  0x3a4a2e],
  ["product",      "PRODUCT",      1.7,  0x2e3b5a],
  ["architecture", "ARCHITECTURE", 1.5,  0x3a2e5a],
  ["engineering",  "ENGINEERING",  1.5,  0x5a3a2e],
  ["qa",           "QA · REVIEW",  1.8,  0x2e5a4a],
  ["gate",         "THE GATE",     0.95, 0x444444],
  ["vault",        "VAULT",        1.15, 0x5a4a18],
];
const ROOMS = {};
(function layout(){
  const total = ROOM_DEFS.reduce((s,r)=>s+r[2],0);
  let x = 0;
  for (const [key,label,w,color] of ROOM_DEFS){
    const px = Math.round((w/total)*W);
    ROOMS[key] = {key,label,color,x0:x,x1:x+px,cx:x+px/2,w:px};
    x += px;
  }
})();

// ---- roster: who idles in which room ---------------------------------------
const COLORS = {
  "Stakeholder Proxy":0x7fd1ff,"Product Manager":0xffd95e,"Criteria Critic":0xff8fae,
  "Program Manager":0xb59bff,"Principal Architect":0xc79bff,"Senior Engineer":0xff9b5e,
  "Junior Engineer":0xffc08a,"QA Engineer":0x7dffb0,"SDET":0x5ddc7a,"Reviewer":0x5db8ff,
  "Technical Writer":0xe8e0a0,"AppSec Engineer":0xff6d6d,
};
const ROSTER = {
  product:["Stakeholder Proxy","Product Manager","Criteria Critic","Program Manager"],
  architecture:["Principal Architect"],
  engineering:["Senior Engineer","Junior Engineer"],
  qa:["QA Engineer","SDET","Reviewer","Technical Writer"],
};
const GATE_ORDER = ["spec","build","test","review","security","audit","e2e"];

function shade(color,f){
  const r=Math.min(255,Math.round(((color>>16)&255)*f));
  const g=Math.min(255,Math.round(((color>>8)&255)*f));
  const b=Math.min(255,Math.round((color&255)*f));
  return (r<<16)|(g<<8)|b;
}

// =========================================================================== //
//  Scene                                                                      //
// =========================================================================== //
class WorkshopScene extends Phaser.Scene {
  constructor(){ super("workshop"); }

  preload(){
    // a 4x4 gold "spark" texture for particle emitters (release confetti / work)
    const g = this.make.graphics({x:0,y:0,add:false});
    g.fillStyle(0xffd95e,1); g.fillRect(0,0,4,4);
    g.fillStyle(0xfff0a8,1); g.fillRect(0,0,2,2);
    g.generateTexture("spark",4,4);
    g.destroy();
  }

  create(){
    SCENE = this;
    this.cameras.main.setBackgroundColor(0x0a0f1c);
    this.agents = new Map();
    this.gates = {};            // name -> {status, attested, advisory}
    this.gatePips = {};         // name -> container shown above the gate
    this.activeRoom = "reception";

    this.buildRooms();
    this.buildGate();
    this.buildChest();
    this.buildGuardian();
    this.buildAgents();
    this.buildCTO();
  }

  // -- static scenery --------------------------------------------------------
  buildRooms(){
    this.roomGlow = {};
    for (const key in ROOMS){
      const R = ROOMS[key];
      // checker floor as a TileSprite over a generated 40x40 tile
      const tkey = "floor_"+key;
      if (!this.textures.exists(tkey)){
        const g = this.make.graphics({add:false});
        g.fillStyle(shade(R.color,0.42),1); g.fillRect(0,0,40,40);
        g.fillStyle(shade(R.color,0.55),1); g.fillRect(20,0,20,20); g.fillRect(0,20,20,20);
        g.generateTexture(tkey,40,40); g.destroy();
      }
      const fh = FLOOR_BOT - FLOOR_TOP + 20;
      this.add.tileSprite(R.x0, FLOOR_TOP, R.w, fh, tkey).setOrigin(0,0).setDepth(0);
      // wall band + top shadow line
      this.add.rectangle(R.x0, FLOOR_TOP-26, R.w, 26, R.color).setOrigin(0,0).setDepth(1);
      this.add.rectangle(R.x0, FLOOR_TOP-26, R.w, 4, 0x000000, 0.25).setOrigin(0,0).setDepth(1);
      // divider
      this.add.rectangle(R.x1-2, FLOOR_TOP-26, 2, fh+26, 0xffffff, 0.05).setOrigin(0,0).setDepth(1);
      this.add.text(R.cx, FLOOR_TOP-22, R.label, {
        fontFamily:"'Courier New',monospace", fontSize:"13px",
        fontStyle:"bold", color:"#dfe7ff",
      }).setOrigin(0.5,0).setDepth(2);
      // active-room glow overlay (alpha tweened on stage change)
      this.roomGlow[key] = this.add.rectangle(R.x0, FLOOR_TOP, R.w, fh, 0xffd25a, 0)
        .setOrigin(0,0).setDepth(1);
    }
    this.setActiveRoom("reception");
  }

  buildGuardian(){
    const R = ROOMS.gate, x = Math.round(R.cx), y = FLOOR_BOT-22;
    const c = this.add.container(x,y).setDepth(6);
    c.add(this.add.rectangle(0,0,16,16,0x9aa6c0));
    c.add(this.add.rectangle(0,-12,12,10,0xcfd8ee));
    c.add(this.add.rectangle(0,7,16,3,0x39406a));
    c.add(this.add.rectangle(11,-7,2,30,0xc0c8e0));   // spear
  }

  buildGate(){
    const R = ROOMS.gate;
    const ax = R.x0+10, aw = R.w-20, atop = FLOOR_TOP+6, abot = FLOOR_BOT+16;
    const h = abot-atop;
    this.gateGeom = {ax,aw,atop,abot,h,cx:R.cx};
    const c = this.add.container(0,0).setDepth(5);
    this.gateFrame = this.add.rectangle(ax,atop,aw,h,0x2b3147).setOrigin(0,0);
    this.gateDoor  = this.add.rectangle(ax+6,atop+6,aw-12,h-6,0x0c1322).setOrigin(0,0);
    c.add([this.gateFrame,this.gateDoor]);
    // bars + latch (hidden when the gate opens)
    this.gateBars = this.add.container(0,0);
    for (let bx=ax+10; bx<ax+aw-8; bx+=12)
      this.gateBars.add(this.add.rectangle(bx,atop+8,3,h-12,0x566089).setOrigin(0,0));
    this.gateLatch = this.add.rectangle(R.cx,(atop+abot)/2,14,14,0xaab4d6);
    this.gateBars.add(this.gateLatch);
    c.add(this.gateBars);
    this.gateContainer = c;
  }

  buildChest(){
    const R = ROOMS.vault, x = Math.round(R.cx), y = FLOOR_BOT-18;
    this.chestPos = {x,y};
    this.chestGlow = this.add.rectangle(x,y-20,90,90,0xffd25a,0).setDepth(4);
    const c = this.add.container(x,y).setDepth(5);
    c.add(this.add.ellipse(0,15,40,8,0x000000,0.3));
    c.add(this.add.rectangle(0,3,36,18,0x7a3b1a));
    c.add(this.add.rectangle(0,9,36,6,0x5a2c12));
    // closed lid + lock (toggled off on release)
    this.chestClosed = this.add.container(0,0);
    this.chestClosed.add(this.add.rectangle(0,-9,36,10,0x8a4720));
    this.chestClosed.add(this.add.rectangle(0,-3,6,10,0xb9892f));
    // open lid + gold (hidden until release)
    this.chestOpen = this.add.container(0,0).setVisible(false);
    this.chestOpen.add(this.add.rectangle(0,-18,36,8,0x7a3b1a));
    this.chestOpen.add(this.add.rectangle(0,-8,30,8,0xffcf52));
    for (let k=0;k<5;k++) this.chestOpen.add(this.add.rectangle(-12+k*6,-12,3,3,0xfff0a8));
    c.add([this.chestClosed,this.chestOpen]);
    c.add(this.add.rectangle(0,1,36,2,0xcaa24a));
    this.chestContainer = c;
    this.chestOpened = false;
  }

  buildAgents(){
    for (const room in ROSTER){
      const names = ROSTER[room], R = ROOMS[room];
      names.forEach((name,i)=>{
        const span = R.w-50;
        const hx = R.x0+30 + (names.length>1 ? span*i/(names.length-1) : span/2);
        const hy = FLOOR_BOT-20 - (i%2)*26;
        const a = this.makePerson(hx, hy, COLORS[name]||0xcccccc, false);
        a.home = {x:hx,y:hy};
        a.setPosition(hx, hy+200).setAlpha(0.32);    // off-floor, faded until revealed
        a.seen = false; a.bubble = null;
        // walk into place
        this.tweens.add({targets:a, y:hy, duration:700, delay:120*i, ease:"Back.out"});
        this.agents.set(name, a);
      });
    }
  }

  buildCTO(){
    const c = this.makePerson(ROOMS.reception.cx, FLOOR_BOT-18, 0x222a44, true);
    c.setDepth(15);
    c.add(this.add.text(0,-30,"CTO",{fontFamily:"'Courier New'",fontSize:"9px",color:"#ffcf52"})
      .setOrigin(0.5));
    this.cto = c;
    // idle bob
    this.tweens.add({targets:c.bob, y:-1, duration:380, yoyo:true, repeat:-1, ease:"Sine.inOut"});
  }

  // a chunky person as a Container; `bob` is the inner group tweened for motion
  makePerson(x,y,bodyColor,isCTO){
    const c = this.add.container(x,y).setDepth(10);
    c.add(this.add.ellipse(1,14,16,4,0x000000,0.3));     // shadow (static)
    const bob = this.add.container(0,0);
    bob.add(this.add.rectangle(-4,9,4,7,0x1a2236));       // legs
    bob.add(this.add.rectangle(4,9,4,7,0x1a2236));
    bob.add(this.add.rectangle(0,0,14,13,bodyColor));     // body
    if (isCTO) bob.add(this.add.rectangle(0,-1,2,8,0xffcf52)); // tie
    bob.add(this.add.rectangle(0,-10,10,9,0xf1c9a5));     // head
    bob.add(this.add.rectangle(0,-15,10,3,isCTO?0x15110c:0x2a2018)); // hair
    const spark = this.add.rectangle(8,-8,3,3,0xfff04a).setVisible(false);
    bob.add(spark);
    c.add(bob);
    c.bob = bob; c.spark = spark;
    return c;
  }

  // -- live reactions to telemetry ------------------------------------------
  setActiveRoom(key){
    this.activeRoom = key;
    for (const k in this.roomGlow)
      this.tweens.add({targets:this.roomGlow[k], fillAlpha:(k===key?0.10:0), duration:420});
    if (ROOMS[key]) this.walkCTO(ROOMS[key].cx);
  }

  walkCTO(tx){
    if (!this.cto) return;
    const dist = Math.abs(tx - this.cto.x);
    this.tweens.add({targets:this.cto, x:tx, duration:Math.max(350,dist*4), ease:"Sine.inOut"});
  }

  reveal(name){
    const a = this.agents.get(name); if (!a) return a;
    if (!a.seen){ a.seen = true; this.tweens.add({targets:a, alpha:1, duration:300}); }
    return a;
  }

  agentWork(name){
    const a = this.reveal(name); if (!a) return;
    a.spark.setVisible(true);
    if (a.workTween) a.workTween.stop();
    a.bob.y = 0;
    a.workTween = this.tweens.add({
      targets:a.bob, y:-2, duration:90, yoyo:true, repeat:13, ease:"Sine.inOut",
      onComplete:()=>{ a.spark.setVisible(false); a.bob.y=0; },
    });
  }

  agentSay(name, text){
    const a = this.reveal(name); if (!a) return;
    this.agentWork(name);
    if (a.bubble){ a.bubble.destroy(); a.bubble = null; }
    if (a.bubbleTimer){ a.bubbleTimer.remove(); a.bubbleTimer = null; }
    a.bubble = this.makeBubble(a, text);
    a.bubbleTimer = this.time.delayedCall(5200, ()=>{
      if (a.bubble){ this.tweens.add({targets:a.bubble, alpha:0, duration:250,
        onComplete:()=>{ if(a.bubble){a.bubble.destroy(); a.bubble=null;} }}); }
    });
  }

  makeBubble(a, text){
    const lines = wrap(text, 26);
    const w = Math.max(...lines.map(l=>l.length))*6.4 + 16;
    const h = lines.length*13 + 12;
    const c = this.add.container(a.x, a.y-26).setDepth(30);
    c.add(this.add.rectangle(0,-h/2,w,h,0xf4f7ff).setStrokeStyle(2,0xc7d2ec));
    c.add(this.add.triangle(0,0,-4,-1,4,-1,0,6,0xf4f7ff));
    lines.forEach((l,i)=>c.add(this.add.text(-w/2+8,-h+8+i*13,l,
      {fontFamily:"'Courier New'",fontSize:"11px",color:"#1a2236"}).setOrigin(0,0)));
    c.setScale(0.6); c.setAlpha(0);
    this.tweens.add({targets:c, scale:1, alpha:1, duration:200, ease:"Back.out"});
    return c;
  }

  spawnPickup(room, label){
    const R = ROOMS[room] || ROOMS.product;
    const w = Math.max(64, label.length*7+18);
    const c = this.add.container(R.cx, FLOOR_BOT-60).setDepth(20);
    c.add(this.add.rectangle(0,0,w,18,0x161d31).setStrokeStyle(2,0xffcf52));
    c.add(this.add.text(0,0,"✚ "+label,{fontFamily:"'Courier New'",fontSize:"11px",color:"#ffe9a8"})
      .setOrigin(0.5));
    this.tweens.add({targets:c, y:c.y-50, alpha:0, duration:2600, ease:"Cubic.out",
      onComplete:()=>c.destroy()});
  }

  setGate(name, info){
    this.gates[name] = info;
    this.drawGatePips();
    if (info.status === "fail") this.flashGate();
  }

  drawGatePips(){
    const {ax,atop} = this.gateGeom;
    let i = 0;
    for (const n of GATE_ORDER){
      const g = this.gates[n]; if (!g) continue;
      const sx = ax+12+i*26, sy = atop-12;
      if (!this.gatePips[n]){
        const c = this.add.container(sx, sy).setDepth(6);
        c.add(this.add.rectangle(10,4,20,18,0x000000));        // plate (recolored below)
        c.add(this.add.text(10,3,"",{fontFamily:"'Courier New'",fontSize:"12px",
          fontStyle:"bold",color:"#fff"}).setOrigin(0.5));
        c.add(this.add.rectangle(17,-3,5,5,0xffcf52).setVisible(false)); // signed pip
        this.gatePips[n] = c;
        c.setScale(0); this.tweens.add({targets:c, scale:1, duration:240, ease:"Back.out"});
      }
      const [plate,label,pip] = this.gatePips[n].list;
      const pass = g.status === "pass";
      plate.setFillStyle(pass?0x1f6b3a:0x6b1f28);
      label.setText(pass?"✓":"✗").setColor(pass?"#5ddc7a":"#ff5d6c");
      pip.setVisible(!!g.attested);
      i++;
    }
  }

  flashGate(){
    this.tweens.add({targets:this.gateDoor, fillColor:{from:0x5a1820,to:0x0c1322},
      duration:520, onUpdate:(_t,o)=>this.gateDoor.setFillStyle(o.fillColor)});
    this.cameras.main.shake(160, 0.004);
  }

  openGateAndChest(){
    // door swings to green, bars retract
    this.gateDoor.setFillStyle(0x1f8a3f);
    this.tweens.add({targets:this.gateBars, alpha:0, y:-30, duration:500, ease:"Cubic.in"});
    // chest opens with a gold burst + glow
    if (this.chestOpened) return;
    this.chestOpened = true;
    this.chestClosed.setVisible(false);
    this.chestOpen.setVisible(true).setScale(1,0);
    this.tweens.add({targets:this.chestOpen, scaleY:1, duration:300, ease:"Back.out"});
    this.tweens.add({targets:this.chestGlow, fillAlpha:0.28, duration:400, yoyo:true,
      repeat:-1, ease:"Sine.inOut"});
    this.add.particles(this.chestPos.x, this.chestPos.y-12, "spark", {
      speed:{min:60,max:160}, angle:{min:230,max:310}, gravityY:240,
      lifespan:1100, quantity:3, frequency:40, scale:{start:1.4,end:0},
      duration:1400, depth:24,
    });
    this.setActiveRoom("vault");
  }

  reset(){
    // tear down a finished build's dynamic objects, re-seat the crew
    for (const a of this.agents.values()){
      if (a.bubble){ a.bubble.destroy(); a.bubble=null; }
      if (a.workTween) a.workTween.stop();
      a.spark.setVisible(false); a.bob.y=0;
      a.seen=false; a.setAlpha(0.32).setPosition(a.home.x, a.home.y);
    }
    this.gates={};
    for (const n in this.gatePips){ this.gatePips[n].destroy(); }
    this.gatePips={};
    this.gateBars.setAlpha(1).setY(0); this.gateDoor.setFillStyle(0x0c1322);
    this.chestOpened=false; this.chestClosed.setVisible(true);
    this.chestOpen.setVisible(false); this.chestGlow.setAlpha(0);
    this.tweens.killTweensOf(this.chestGlow);
    this.setActiveRoom("reception");
    renderGatesMini(this.gates);
  }
}

// ---- text wrapping (shared) -----------------------------------------------
function wrap(t,n){
  const out=[], words=(t||"").split(" "); let line="";
  for (const x of words){
    if ((line+" "+x).trim().length>n){ out.push(line.trim()); line=x; }
    else line += " "+x;
  }
  if (line.trim()) out.push(line.trim());
  return out.slice(0,4);
}

// =========================================================================== //
//  Phaser boot                                                                //
// =========================================================================== //
let SCENE = null;
const GAME = new Phaser.Game({
  type: Phaser.AUTO,
  parent: "game",
  width: W, height: H,
  backgroundColor: "#0a0f1c",
  pixelArt: true,
  roundPixels: true,
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
  scene: WorkshopScene,
});

// =========================================================================== //
//  DOM HUD + networking (unchanged event protocol)                            //
// =========================================================================== //
function pushLog(cls,text){
  const ul=document.getElementById("log");
  const li=document.createElement("li"); li.className=cls; li.textContent=text;
  ul.appendChild(li); ul.scrollTop=ul.scrollHeight;
  while(ul.children.length>200) ul.removeChild(ul.firstChild);
}
function logClass(t){
  if(/✅|💰|green|Shipped/i.test(t)) return "good";
  if(/💥|🚫|⛔|error|halt/i.test(t)) return "bad";
  if(/📋|🏭|🧑/i.test(t)) return "hot";
  return "";
}
function renderGatesMini(gates){
  const el=document.getElementById("gates-mini"); el.innerHTML="";
  for(const n of GATE_ORDER){ const g=gates[n]; if(!g) continue;
    const s=document.createElement("span"); s.className="g "+(g.status); s.textContent=n;
    el.appendChild(s);}
}

let es=null, CURRENT_ID=null;
function startProject(req,mode,supervised){
  if(es){es.close();es=null;}
  if(SCENE) SCENE.reset();
  document.getElementById("log").innerHTML="";
  pushLog("hot","⏳ Breaking ground…");
  fetch("/api/projects",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({request:req,mode,supervised})})
   .then(r=>r.json()).then(({id,floor})=>{
      document.getElementById("floor-tag").textContent="FLOOR "+floor;
      es=new EventSource("/api/stream/"+id);
      es.onmessage=e=>{try{applyEvent(JSON.parse(e.data));}catch(_){}};
      es.addEventListener("end",()=>{es.close();es=null;});
   }).catch(err=>pushLog("bad","✗ "+err));
}
function sendDecision(decision){
  if(!CURRENT_ID) return;
  fetch("/api/decision/"+CURRENT_ID,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decision})});
  hideCheckpoint();
}

let PENDING_CP=null;
function applyEvent(ev){
  const S=SCENE;
  switch(ev.type){
    case "project":
      CURRENT_ID=ev.id;
      document.getElementById("proj-tag").textContent=ev.request;
      document.getElementById("mode-tag").textContent="· "+ev.mode;
      break;
    case "narrate": pushLog(logClass(ev.text),ev.text); break;
    case "stage":
      if(S) S.setActiveRoom(ev.room);
      pushLog("gold","── "+ev.label+" ──"); break;
    case "agent": if(S) S.agentWork(ev.agent); break;
    case "say":
      if(S) S.agentSay(ev.agent,ev.text);
      if(ev.concern) pushLog("",`💭 ${ev.agent}: "${ev.concern}"`); break;
    case "artifact":
      if(S) S.spawnPickup(ev.room,ev.label);
      pushLog("good","✚ "+ev.label+"  ·  "+ev.agent); break;
    case "gate":
      if(S){ S.setGate(ev.name,{status:ev.status,attested:ev.attested,advisory:ev.advisory});
             renderGatesMini(S.gates); }
      if(!ev.advisory) pushLog(ev.status==="pass"?"good":"bad",
        `🛡 GATE ${ev.name.toUpperCase()}: ${ev.status.toUpperCase()}${ev.attested?" (signed)":""}`);
      break;
    case "checkpoint":
      PENDING_CP={title:ev.title,summary:ev.summary,boundary:ev.boundary}; break;
    case "await_decision": showCheckpoint(); break;
    case "resolved": hideCheckpoint(); break;
    case "release":
      if(S) S.openGateAndChest();
      pushLog("gold","💰 RELEASED → "+(ev.uri||"artifact")); break;
    case "halt":
      if(S) S.flashGate();
      pushLog("bad","⛔ HELD: "+ev.reason); break;
    case "done":
      pushLog(ev.passed?"good":"bad",
        ev.passed?"✅ Build complete — shipped.":"❌ Build halted — nothing shipped."); break;
  }
}

// ---- checkpoint modal ------------------------------------------------------
function showCheckpoint(){
  const cp=PENDING_CP||{title:"CHECKPOINT",summary:""};
  document.getElementById("cp-title").textContent="CHECKPOINT · "+(cp.title||"");
  document.getElementById("cp-summary").textContent=cp.summary||"";
  document.getElementById("cp-modal").classList.remove("hidden");
}
function hideCheckpoint(){ document.getElementById("cp-modal").classList.add("hidden"); }

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
