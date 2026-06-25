"use strict";
/* Foundry Workshop — Phaser 3 pixel factory, multi-floor (one floor per project).
 *
 * The engine owns the scene graph, tweens, particles, depth-sorting and timed
 * lifecycles. This file describes the workshop and reacts to the SAME Server-Sent
 * Events the backend already emits (server.py / events.py are untouched) — now
 * routed PER PROJECT: each project is its own floor (a Phaser scene); the elevator
 * switches the active floor. Sprites are generated procedurally so the app stays
 * offline; a drop-in art slot (static/assets/manifest.json) can override the floor
 * tiles with real PNGs. Nothing here can influence a build — the signed gates
 * decide and this is only the view.
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
//  Shared procedural textures (generated ONCE; reused by every floor scene)   //
// =========================================================================== //
function buildSharedTextures(scene){
  if (!scene.textures.exists("spark")){
    const g = scene.make.graphics({add:false});
    g.fillStyle(0xffd95e,1); g.fillRect(0,0,4,4);
    g.fillStyle(0xfff0a8,1); g.fillRect(0,0,2,2);
    g.generateTexture("spark",4,4); g.destroy();
  }
  for (const key in ROOMS){
    const tkey = "floor_"+key;
    if (scene.textures.exists(tkey)) continue;
    const R = ROOMS[key]; const g = scene.make.graphics({add:false});
    g.fillStyle(shade(R.color,0.42),1); g.fillRect(0,0,40,40);              // base
    g.fillStyle(shade(R.color,0.55),1); g.fillRect(20,0,20,20); g.fillRect(0,20,20,20);
    g.fillStyle(shade(R.color,0.64),1); g.fillRect(6,6,2,2); g.fillRect(28,14,2,2); g.fillRect(14,30,2,2);
    g.fillStyle(shade(R.color,0.34),1); g.fillRect(32,33,2,2); g.fillRect(11,18,2,2);  // speckle
    g.generateTexture(tkey,40,40); g.destroy();
  }
}

// Optional drop-in art: static/assets/manifest.json may map room -> tile PNG to
// override the procedural floor texture. Best-effort; the procedural baseline
// always works, so missing/broken assets degrade silently.
function applyDropInArt(scene, done){
  const man = scene.cache.json.get("artManifest");
  const tiles = man && man.tiles;
  if (!tiles || typeof tiles !== "object"){ done(); return; }
  for (const [room, file] of Object.entries(tiles)){
    if (ROOMS[room] && typeof file === "string") scene.load.image("dropin_"+room, "assets/"+file);
  }
  scene.load.once("complete", ()=>{
    for (const room of Object.keys(tiles)){
      const dk = "dropin_"+room, key = "floor_"+room;
      if (scene.textures.exists(dk)){
        if (scene.textures.exists(key)) scene.textures.remove(key);
        scene.textures.addImage(key, scene.textures.get(dk).getSourceImage());
      }
    }
    done();
  });
  scene.load.on("loaderror", ()=>{});
  scene.load.start();
}

// =========================================================================== //
//  Boot: generate shared art (+ drop-in), then signal ready                   //
// =========================================================================== //
let bootResolve;
const bootReady = new Promise((r)=>{ bootResolve = r; });

class BootScene extends Phaser.Scene {
  constructor(){ super("boot"); }
  preload(){
    // A missing manifest is the normal (procedural-only) case — ignore its 404.
    this.load.json("artManifest", "assets/manifest.json");
    this.load.on("loaderror", ()=>{});
  }
  create(){
    buildSharedTextures(this);
    applyDropInArt(this, ()=>bootResolve());
  }
}

// =========================================================================== //
//  Floor scene — one workshop per project                                     //
// =========================================================================== //
class FloorScene extends Phaser.Scene {
  init(data){ this.proj = data || {}; }

  create(){
    this.cameras.main.setBackgroundColor(0x0a0f1c);
    this.agents = new Map();
    this.gates = {};
    this.gatePips = {};
    this.activeRoom = "reception";
    this.buildRooms();
    this.buildGate();
    this.buildChest();
    this.buildGuardian();
    this.buildAgents();
    this.buildCTO();
    const F = FLOORS.get(this.proj.floor);
    if (F) F.scene = this;
    this.sys.setVisible(this.proj.floor === ACTIVE);
  }

  buildRooms(){
    this.roomGlow = {};
    for (const key in ROOMS){
      const R = ROOMS[key];
      const fh = FLOOR_BOT - FLOOR_TOP + 20;
      this.add.tileSprite(R.x0, FLOOR_TOP, R.w, fh, "floor_"+key).setOrigin(0,0).setDepth(0);
      this.add.rectangle(R.x0, FLOOR_TOP-26, R.w, 26, R.color).setOrigin(0,0).setDepth(1);
      this.add.rectangle(R.x0, FLOOR_TOP-26, R.w, 4, 0x000000, 0.25).setOrigin(0,0).setDepth(1);
      this.add.rectangle(R.x1-2, FLOOR_TOP-26, 2, fh+26, 0xffffff, 0.05).setOrigin(0,0).setDepth(1);
      this.add.text(R.cx, FLOOR_TOP-22, R.label, {
        fontFamily:"'Courier New',monospace", fontSize:"13px",
        fontStyle:"bold", color:"#dfe7ff",
      }).setOrigin(0.5,0).setDepth(2);
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
    c.add(this.add.rectangle(11,-7,2,30,0xc0c8e0));
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
    this.gateBars = this.add.container(0,0);
    for (let bx=ax+10; bx<ax+aw-8; bx+=12)
      this.gateBars.add(this.add.rectangle(bx,atop+8,3,h-12,0x566089).setOrigin(0,0));
    this.gateLatch = this.add.rectangle(R.cx,(atop+abot)/2,14,14,0xaab4d6);
    this.gateBars.add(this.gateLatch);
    c.add(this.gateBars);
  }

  buildChest(){
    const R = ROOMS.vault, x = Math.round(R.cx), y = FLOOR_BOT-18;
    this.chestPos = {x,y};
    this.chestGlow = this.add.rectangle(x,y-20,90,90,0xffd25a,0).setDepth(4);
    const c = this.add.container(x,y).setDepth(5);
    c.add(this.add.ellipse(0,15,40,8,0x000000,0.3));
    c.add(this.add.rectangle(0,3,36,18,0x7a3b1a));
    c.add(this.add.rectangle(0,9,36,6,0x5a2c12));
    this.chestClosed = this.add.container(0,0);
    this.chestClosed.add(this.add.rectangle(0,-9,36,10,0x8a4720));
    this.chestClosed.add(this.add.rectangle(0,-3,6,10,0xb9892f));
    this.chestOpen = this.add.container(0,0).setVisible(false);
    this.chestOpen.add(this.add.rectangle(0,-18,36,8,0x7a3b1a));
    this.chestOpen.add(this.add.rectangle(0,-8,30,8,0xffcf52));
    for (let k=0;k<5;k++) this.chestOpen.add(this.add.rectangle(-12+k*6,-12,3,3,0xfff0a8));
    c.add([this.chestClosed,this.chestOpen]);
    c.add(this.add.rectangle(0,1,36,2,0xcaa24a));
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
        a.setPosition(hx, hy+200).setAlpha(0.32);
        a.seen = false; a.bubble = null;
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
    this.tweens.add({targets:c.bob, y:-1, duration:380, yoyo:true, repeat:-1, ease:"Sine.inOut"});
  }

  makePerson(x,y,bodyColor,isCTO){
    const c = this.add.container(x,y).setDepth(10);
    c.add(this.add.ellipse(1,14,16,4,0x000000,0.3));
    const bob = this.add.container(0,0);
    bob.add(this.add.rectangle(-4,9,4,7,0x1a2236));
    bob.add(this.add.rectangle(4,9,4,7,0x1a2236));
    bob.add(this.add.rectangle(0,0,14,13,bodyColor));
    if (isCTO) bob.add(this.add.rectangle(0,-1,2,8,0xffcf52));
    bob.add(this.add.rectangle(0,-10,10,9,0xf1c9a5));
    bob.add(this.add.rectangle(0,-15,10,3,isCTO?0x15110c:0x2a2018));
    const spark = this.add.rectangle(8,-8,3,3,0xfff04a).setVisible(false);
    bob.add(spark);
    c.add(bob);
    c.bob = bob; c.spark = spark;
    return c;
  }

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
        c.add(this.add.rectangle(10,4,20,18,0x000000));
        c.add(this.add.text(10,3,"",{fontFamily:"'Courier New'",fontSize:"12px",
          fontStyle:"bold",color:"#fff"}).setOrigin(0.5));
        c.add(this.add.rectangle(17,-3,5,5,0xffcf52).setVisible(false));
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
    this.gateDoor.setFillStyle(0x1f8a3f);
    this.tweens.add({targets:this.gateBars, alpha:0, y:-30, duration:500, ease:"Cubic.in"});
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

  renderOrg(ev){
    // The REAL org chart (Wave E telemetry): present areas -> Tech-Lead domains.
    if (this.orgPanel){ this.orgPanel.destroy(); }
    const areaColor = {
      frontend:0x7fd1ff, backend:0xff9b5e, data:0x7dffb0,
      infra:0xb59bff, security:0xff6d6d, default:0xcccccc,
    };
    const areas = ev.areas || [];
    const rowH = 18, padding = 10, headerH = 22;
    const w = 232, h = headerH + areas.length*rowH + padding;
    const c = this.add.container(12, FLOOR_TOP + 6).setDepth(28);
    c.add(this.add.rectangle(0,0,w,h,0x0c1322,0.88).setOrigin(0,0).setStrokeStyle(2,0xffcf52));
    c.add(this.add.text(8,5,"🏢 ORG · PM",{fontFamily:"'Courier New'",fontSize:"12px",
      fontStyle:"bold",color:"#ffcf52"}).setOrigin(0,0));
    areas.forEach((a,i)=>{
      const y = headerH + i*rowH;
      const col = areaColor[a.area] || areaColor.default;
      c.add(this.add.rectangle(8,y+3,10,10,col).setOrigin(0,0));
      const leads = (a.leads||[]).join(", ");
      c.add(this.add.text(24,y,`${a.area}: ${leads}`,
        {fontFamily:"'Courier New'",fontSize:"11px",color:"#e8eefb"}).setOrigin(0,0));
    });
    c.setAlpha(0);
    this.tweens.add({targets:c, alpha:1, duration:300});
    this.orgPanel = c;
  }
}

// =========================================================================== //
//  Phaser boot                                                                //
// =========================================================================== //
const GAME = new Phaser.Game({
  type: Phaser.AUTO,
  parent: "game",
  width: W, height: H,
  backgroundColor: "#0a0f1c",
  pixelArt: true,
  roundPixels: true,
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
  scene: [BootScene],
});

// =========================================================================== //
//  Floor manager + multi-project networking                                   //
// =========================================================================== //
const FLOORS = new Map();   // floorNum -> floor state (incl. its Phaser scene)
let ACTIVE = null;          // the floor currently shown

function floorKey(f){ return "floor-"+f; }

async function newProject(req, mode, supervised, backend, packs, extra){
  await bootReady;  // shared textures must exist before a FloorScene builds
  let resp;
  try {
    resp = await fetch("/api/projects", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({request:req, mode, supervised, backend, packs, ...(extra||{})}),
    }).then(r=>r.json());
  } catch(err){ appendLogDom("bad","✗ "+err); return; }

  const floor = resp.floor;
  const F = {
    id: resp.id, floor, request:req, mode, backend, scene:null,
    log:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
    seenUris:new Set(),
  };
  FLOORS.set(floor, F);
  GAME.scene.add(floorKey(floor), FloorScene, true, {floor, id:resp.id, request:req, mode});
  switchToFloor(floor);

  const es = new EventSource("/api/stream/"+resp.id);
  es.onmessage = e => { try { routeEvent(floor, JSON.parse(e.data)); } catch(_){} };
  es.addEventListener("end", ()=>es.close());
  F.es = es;
}

function switchToFloor(floor){
  ACTIVE = floor;
  for (const f of FLOORS.keys()){
    const sc = GAME.scene.getScene(floorKey(f));
    if (sc && sc.sys) sc.sys.setVisible(f === floor);
  }
  GAME.scene.bringToTop(floorKey(floor));
  const F = FLOORS.get(floor);
  rebuildLog(F);
  renderGatesMini(F.gates);
  updateHud(F);
  if (F.awaiting) showCheckpoint(F); else hideCheckpoint();
  renderElevator();
}

// ---- per-floor event routing ----------------------------------------------
function routeEvent(floor, ev){
  const F = FLOORS.get(floor); if (!F) return;
  const S = F.scene, isActive = floor === ACTIVE;
  switch(ev.type){
    case "project":
      F.request = ev.request; F.mode = ev.mode; F.backend = ev.backend || F.backend;
      if (isActive) updateHud(F);
      break;
    case "narrate": logTo(F, logClass(ev.text), ev.text); break;
    case "stage":
      if (S) S.setActiveRoom(ev.room);
      logTo(F, "gold", "── "+ev.label+" ──"); break;
    case "agent": if (S) S.agentWork(ev.agent); break;
    case "say":
      if (S) S.agentSay(ev.agent, ev.text);
      if (ev.concern) logTo(F, "", `💭 ${ev.agent}: "${ev.concern}"`); break;
    case "artifact":
      if (S) S.spawnPickup(ev.room, ev.label);
      logTo(F, "good", "✚ "+ev.label+"  ·  "+ev.agent); break;
    case "gate":
      F.gates[ev.name] = {status:ev.status, attested:ev.attested, advisory:ev.advisory};
      if (S) S.setGate(ev.name, F.gates[ev.name]);
      if (isActive) renderGatesMini(F.gates);
      if (!ev.advisory) logTo(F, ev.status==="pass"?"good":"bad",
        `🛡 GATE ${ev.name.toUpperCase()}: ${ev.status.toUpperCase()}${ev.attested?" (signed)":""}`);
      break;
    case "org":
      F.org = ev;
      if (S) S.renderOrg(ev);
      logTo(F, "gold", "🏢 ORG · "+(ev.areas||[]).map(a=>`${a.area}(${(a.leads||[]).length})`).join("  "));
      break;
    case "files":
      F.hasFiles = ev.count; break;
    case "checkpoint":
      F.cp = {title:ev.title, summary:ev.summary, boundary:ev.boundary}; break;
    case "await_decision":
      F.awaiting = true; renderElevator();
      if (isActive) showCheckpoint(F); break;
    case "resolved":
      F.awaiting = false; renderElevator();
      if (isActive) hideCheckpoint(); break;
    case "release":
      if (S) S.openGateAndChest();
      logTo(F, "gold", "💰 RELEASED → "+(ev.uri||"artifact")); break;
    case "halt":
      if (S) S.flashGate();
      logTo(F, "bad", "⛔ HELD: "+ev.reason); break;
    case "done":
      F.status = "done"; F.passed = !!ev.passed; renderElevator();
      logTo(F, ev.passed?"good":"bad",
        ev.passed?"✅ Build complete — shipped.":"❌ Build halted — nothing shipped."); break;
  }
}

// =========================================================================== //
//  DOM: quest log, gates-mini, HUD, elevator, checkpoint modal                //
// =========================================================================== //
function logClass(t){
  if(/✅|💰|green|Shipped/i.test(t)) return "good";
  if(/💥|🚫|⛔|error|halt/i.test(t)) return "bad";
  if(/📋|🏭|🧑/i.test(t)) return "hot";
  return "";
}
function appendLogDom(cls,text){
  const ul=document.getElementById("log");
  const li=document.createElement("li"); li.className=cls; li.textContent=text;
  ul.appendChild(li); ul.scrollTop=ul.scrollHeight;
  while(ul.children.length>200) ul.removeChild(ul.firstChild);
}
function logTo(F, cls, text){
  F.log.push({cls,text});
  if (F.log.length > 200) F.log.shift();
  if (F.floor === ACTIVE) appendLogDom(cls, text);
}
function rebuildLog(F){
  const ul=document.getElementById("log"); ul.innerHTML="";
  for (const {cls,text} of F.log){
    const li=document.createElement("li"); li.className=cls; li.textContent=text; ul.appendChild(li);
  }
  ul.scrollTop = ul.scrollHeight;
}
function renderGatesMini(gates){
  const el=document.getElementById("gates-mini"); el.innerHTML="";
  for(const n of GATE_ORDER){ const g=gates[n]; if(!g) continue;
    const s=document.createElement("span"); s.className="g "+(g.status); s.textContent=n;
    el.appendChild(s);}
}
function updateHud(F){
  document.getElementById("floor-tag").textContent = "FLOOR "+F.floor;
  document.getElementById("proj-tag").textContent = F.request || "—";
  const tail = (F.mode ? "· "+F.mode : "") + (F.backend ? " · "+F.backend : "");
  document.getElementById("mode-tag").textContent = tail.trim();
}
function renderElevator(){
  const el=document.getElementById("el-floors"); el.innerHTML="";
  for (const f of [...FLOORS.keys()].sort((a,b)=>a-b)){
    const F=FLOORS.get(f);
    const btn=document.createElement("button");
    let cls="el-btn";
    if (f===ACTIVE) cls+=" active";
    if (F.status==="done") cls += F.passed ? " done" : " failed";
    btn.className=cls;
    btn.innerHTML=`<span class="fl-no">${f}</span>FL`;
    if (F.awaiting){ const b=document.createElement("span"); b.className="badge"; b.textContent="!"; btn.appendChild(b); }
    btn.onclick=()=>switchToFloor(f);
    el.appendChild(btn);
  }
}

// ---- checkpoint modal (per active floor) -----------------------------------
function showCheckpoint(F){
  const cp = (F && F.cp) || {title:"CHECKPOINT", summary:""};
  document.getElementById("cp-title").textContent="CHECKPOINT · "+(cp.title||"");
  document.getElementById("cp-summary").textContent=cp.summary||"";
  document.getElementById("cp-modal").classList.remove("hidden");
}
function hideCheckpoint(){ document.getElementById("cp-modal").classList.add("hidden"); }

// ---- output (files) browser ------------------------------------------------
function openFiles(){
  const F = FLOORS.get(ACTIVE);
  const modal = document.getElementById("files-modal");
  const list = document.getElementById("files-list");
  const view = document.getElementById("files-view");
  list.innerHTML = ""; view.textContent = "";
  if(!F){ list.innerHTML = "<li class='muted'>Start a project first.</li>"; modal.classList.remove("hidden"); return; }
  document.getElementById("files-floor").textContent = "Floor "+F.floor;
  document.getElementById("files-zip").href = "/api/zip/"+F.id;
  view.textContent = "Loading…";
  fetch("/api/files/"+F.id)
    .then(r => r.ok ? r.json() : {files:[]})
    .then(({files}) => {
      if(!files || !files.length){
        list.innerHTML = "<li class='muted'>No output yet — finish a build (it writes code + docs).</li>";
        view.textContent = ""; return;
      }
      view.textContent = "Select a file…";
      for(const f of files){
        const li = document.createElement("li");
        li.textContent = `${f.path}  (${f.size}b)`;
        li.title = f.path;
        li.onclick = () => viewFile(F.id, f.path, li);
        list.appendChild(li);
      }
    })
    .catch(() => { list.innerHTML = "<li class='muted'>(could not list output)</li>"; view.textContent = ""; });
  modal.classList.remove("hidden");
}
function viewFile(id, path, li){
  for(const c of document.getElementById("files-list").children) c.classList.remove("sel");
  if(li) li.classList.add("sel");
  const view = document.getElementById("files-view");
  view.textContent = "Loading…";
  fetch("/api/file/"+id+"?path="+encodeURIComponent(path))
    .then(r => r.text())
    .then(t => { view.textContent = t || "(empty file)"; })
    .catch(() => { view.textContent = "(could not load file)"; });
}
function hideFiles(){ document.getElementById("files-modal").classList.add("hidden"); }
function sendDecision(decision){
  const F = FLOORS.get(ACTIVE);
  if (!F){ hideCheckpoint(); return; }
  fetch("/api/decision/"+F.id, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({decision})});
  F.awaiting = false; renderElevator(); hideCheckpoint();
}

// ---- UI wiring -------------------------------------------------------------
const newModal=document.getElementById("new-modal");
document.getElementById("new-btn").onclick=()=>newModal.classList.remove("hidden");
document.getElementById("cancel-btn").onclick=()=>newModal.classList.add("hidden");
// intake mode radios -> reveal the matching panel
function intakeMode(){ return (document.querySelector('input[name="intake"]:checked')||{}).value||"idea"; }
for(const r of document.querySelectorAll('input[name="intake"]')){
  r.addEventListener("change", ()=>{
    const v = intakeMode();
    for(const p of document.querySelectorAll(".intake-panel"))
      p.classList.toggle("hidden", p.dataset.mode !== v);
  });
}
// ---- spec chat with the leads (intake mode "chat") -------------------------
let chatSession = null, chatDraft = "";
function appendChatTurn(role, text){
  const log = document.getElementById("chat-log");
  const cls = role === "user" ? "user" : (role === "system" ? "system" : "lead");
  const div = document.createElement("div");
  div.className = "chat-turn "+cls;
  div.innerHTML = `<span class="who"></span> <span class="txt"></span>`;
  div.querySelector(".who").textContent = role === "user" ? "you" : role;
  div.querySelector(".txt").textContent = text;
  log.appendChild(div); log.scrollTop = log.scrollHeight;
}
function sendChat(){
  const input = document.getElementById("chat-msg");
  const msg = input.value.trim();
  if(!msg) return;
  input.value = "";
  appendChatTurn("user", msg);
  appendChatTurn("system", "…leads are thinking…");
  const backend = document.getElementById("backend-select").value;
  fetch("/api/ideate", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({session:chatSession, message:msg, backend})})
   .then(r=>r.json())
   .then(d=>{
     // drop the "thinking…" placeholder
     const log=document.getElementById("chat-log"); if(log.lastChild) log.removeChild(log.lastChild);
     chatSession = d.session;
     for(const m of (d.messages||[])) appendChatTurn(m.role||"lead", m.text||"");
     if(typeof d.spec === "string"){ chatDraft = d.spec; document.getElementById("chat-spec").textContent = chatDraft || "(nothing yet)"; }
   })
   .catch(()=>{ const log=document.getElementById("chat-log"); if(log.lastChild) log.removeChild(log.lastChild); appendChatTurn("system","(chat request failed)"); });
}

function fileToB64(file){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>res(String(r.result).split(",",2)[1]||"");  // drop the data: prefix
    r.onerror=()=>rej(r.error);
    r.readAsDataURL(file);
  });
}
document.getElementById("start-btn").onclick=async ()=>{
  const req=document.getElementById("req-input").value;
  const mode=document.getElementById("mode-select").value;
  const backend=document.getElementById("backend-select").value;
  const packs=document.getElementById("packs-input").value;
  const sup=document.getElementById("supervised").checked;
  const extra={};
  const intake=intakeMode();
  if(intake==="spec"){
    extra.spec_text=document.getElementById("spec-text").value;
  } else if(intake==="zip"){
    const f=document.getElementById("zip-file").files[0];
    if(!f){ alert("Choose a .zip of docs first."); return; }
    try { extra.spec_zip_b64=await fileToB64(f); }
    catch(_){ alert("Could not read that file."); return; }
  } else if(intake==="chat"){
    if(!chatDraft.trim()){ alert("Chat with the leads first to draft a spec (or pick another mode)."); return; }
    extra.spec_text=chatDraft;
  }
  newModal.classList.add("hidden");
  newProject(req, mode, sup, backend, packs, extra);
  // reset the chat for the next project
  chatSession=null; chatDraft="";
  document.getElementById("chat-log").innerHTML="";
  document.getElementById("chat-spec").textContent="(nothing yet)";
};
document.getElementById("chat-send").onclick=sendChat;
document.getElementById("chat-msg").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); sendChat(); }});
document.getElementById("cp-approve").onclick=()=>sendDecision("approve");
document.getElementById("cp-reject").onclick=()=>sendDecision("reject");
document.getElementById("output-btn").onclick=openFiles;
document.getElementById("files-close").onclick=hideFiles;

// preselect the configured default backend (FOUNDRY_MODEL_BACKEND on the server;
// mock if unset). A deployment sets the env to default to claude-cli.
fetch("/api/config").then(r=>r.json()).then(c=>{
  const sel=document.getElementById("backend-select");
  if(c && c.default_backend && [...sel.options].some(o=>o.value===c.default_backend))
    sel.value=c.default_backend;
}).catch(()=>{});

// open the new-project dialog on first load
newModal.classList.remove("hidden");
