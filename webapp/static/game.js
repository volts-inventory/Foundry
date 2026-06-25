"use strict";
/* Foundry Workshop — Phaser 3 pixel factory, multi-floor (one floor per project).
 *
 * The engine owns the scene graph, tweens, particles, depth-sorting and timed
 * lifecycles. This file describes the workshop and reacts to the SAME Server-Sent
 * Events the backend already emits (server.py / events.py are untouched) — now
 * routed PER PROJECT: each project is its own floor (a Phaser scene); the elevator
 * switches the active floor. EVERY sprite, tile and prop is generated procedurally
 * in code (our own pixel art — no external image assets, no drop-in). Nothing here
 * can influence a build — the signed gates decide and this is only the view.
 */

// Canvas aspect (~1.5:1) is matched to the game panel so the world FILLS the frame
// (no letterbox). Pokémon-interior vertical split: WALL fills the top, FLOOR the
// rest. FLOOR_TOP is the wall/floor seam; characters stand on the floor near FLOOR_BOT.
const W = 1280, H = 840;
const FLOOR_TOP = 250, FLOOR_BOT = H - 64;

// ---- rooms (left -> right), weighted widths --------------------------------
const ROOM_DEFS = [
  ["lounge",       "LOUNGE",       1.5,  0x40354f],
  ["reception",    "RECEPTION",    0.9,  0x3a4a2e],
  ["product",      "PRODUCT",      1.6,  0x2e3b5a],
  ["architecture", "ARCHITECTURE", 1.4,  0x3a2e5a],
  ["engineering",  "ENGINEERING",  1.4,  0x5a3a2e],
  ["qa",           "QA · REVIEW",  1.7,  0x2e5a4a],
  ["gate",         "THE GATE",     0.9,  0x444444],
  ["vault",        "VAULT",        1.1,  0x5a4a18],
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
const AGENT_ROOM = {}; for (const rm in ROSTER) for (const nm of ROSTER[rm]) AGENT_ROOM[nm] = rm;

// ---- generated character sprites (our own pixel art) -----------------------
// 16-wide pixel map: big head, tiny torso, short legs. Details (eyes/ears/hair/
// clothes/accessories) are drawn as overlays so one base parameterises everyone.
const SP_BASE = [
"...KKKKKKKK.....","..KhhhhhhhhK....","..KhhhhhhhhK....","..KhhhhhhhhK....",
"..KssssssssK....","..KssssssssK....","..KssssssssK....","...KssssssK.....",
"...KccccccK.....","...KsccccsK.....","...KppppppK.....","...KppKppK......","...KbbKbbK......",
];
const SKINS = [0xf2c79c,0xe8b487,0xcf9a6e,0xa06a44,0x8a5a3a];
const HAIRS = [0x3a3f74,0x161109,0x4a3420,0xcaa84a,0x9a9aa2,0xe85a9a,0xb5542e,0x2a8b5a,0x3a6abf,0x6a4aa0,0xd86a2e];
const POOL_OUT = [0x3aa564,0xe07a2e,0x3a6abf,0xd24a86,0xcaa84a,0x2fae6a];
const POOL_STYLE = ['short','spiky','bun','ponytail','long'];
const GARMENTS = ['plain','collar','stripe','belt','vest','twotone','hoodie'];
const ROLE_KIT = {
  "Stakeholder Proxy":{body:0x3f6fb5,trim:0xcdd6e6,style:'swept',cloth:'collar',scarf:0xa83a2e},
  "Product Manager":{body:0xe0b53a,trim:0x7a5a18,style:'bun',cloth:'belt'},
  "Criteria Critic":{body:0xd24a86,trim:0xf4c2d6,style:'sidelock',cloth:'vest',glasses:1},
  "Program Manager":{body:0x7a5ad0,trim:0xd6c8f4,style:'ponytail',cloth:'stripe',headset:1},
  "Principal Architect":{body:0x5a6178,trim:0xcfd4e0,style:'short',cloth:'collar',coat:1,glasses:1,hair:0x9a9aa2},
  "Senior Engineer":{body:0xe07a2e,trim:0x3a2a18,style:'spiky',cloth:'hoodie'},
  "Junior Engineer":{body:0x3aa564,trim:0xe0e0c0,style:'short',cloth:'twotone',cap:0x2f6dbf},
  "QA Engineer":{body:0x2fae6a,trim:0xcfeede,style:'long',cloth:'collar',coat:1},
  "SDET":{body:0x1f9b9b,trim:0xcdeaea,style:'short',cloth:'stripe',headset:1},
  "Reviewer":{body:0x3a6abf,trim:0xcdd9f0,style:'swept',cloth:'vest',glasses:1},
  "Technical Writer":{body:0xcaa84a,trim:0x6a5520,style:'long',cloth:'belt'},
  "AppSec Engineer":{body:0xc0392b,trim:0xf0b0b0,style:'short',cloth:'hoodie',hood:0x7a1f24},
  "CTO":{body:0x2f3450,trim:0xc8a23a,style:'short',cloth:'collar',tie:0xc8a23a},
};
function hashStr(s){ let h=2166136261; for(let i=0;i<s.length;i++){ h^=s.charCodeAt(i); h=Math.imul(h,16777619); } return h>>>0; }
function kitFor(name, seed){
  const k = ROLE_KIT[name] || {body:0x4a78c0,trim:0xcdd6e6,style:'short',cloth:'collar'};
  const o = Object.assign({}, k);
  o.skin = SKINS[((seed*3+1)%SKINS.length+SKINS.length)%SKINS.length];
  o.hair = k.hair || HAIRS[((seed*7+2)%HAIRS.length+HAIRS.length)%HAIRS.length];
  return o;
}
function poolKit(i){ // the IC pool: same family, distinct people
  const o = {body:POOL_OUT[i%POOL_OUT.length], style:POOL_STYLE[i%POOL_STYLE.length],
    cloth:GARMENTS[(i+1)%GARMENTS.length], skin:SKINS[(i*3)%SKINS.length], hair:HAIRS[(i*7+1)%HAIRS.length]};
  o.trim = shade(o.body,1.4);
  if (i%2===0) o.cap = [0x2f9d54,0x3a6abf,0xb5542e][i%3];
  if (i%5===3) o.glasses = 1;
  return o;
}

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
  // Wall tile per room: a darker brick course (offset rows + mortar + highlight).
  for (const key in ROOMS){
    const wkey = "wall_"+key;
    if (scene.textures.exists(wkey)) continue;
    const R = ROOMS[key]; const g = scene.make.graphics({add:false});
    const brick = shade(R.color,0.30), mortar = shade(R.color,0.18), hi = shade(R.color,0.40);
    g.fillStyle(mortar,1); g.fillRect(0,0,40,32);
    g.fillStyle(brick,1);
    g.fillRect(1,1,18,14); g.fillRect(21,1,18,14);       // top course
    g.fillRect(-9,17,18,14); g.fillRect(11,17,18,14); g.fillRect(31,17,18,14);  // offset course
    g.fillStyle(hi,1);                                    // top-edge highlight per brick
    g.fillRect(1,1,18,2); g.fillRect(21,1,18,2); g.fillRect(11,17,18,2); g.fillRect(31,17,18,2);
    g.generateTexture(wkey,40,32); g.destroy();
  }
}

// =========================================================================== //
//  Boot: generate ALL our own pixel art, then signal ready                    //
// =========================================================================== //
let bootResolve;
const bootReady = new Promise((r)=>{ bootResolve = r; });

class BootScene extends Phaser.Scene {
  constructor(){ super("boot"); }
  create(){
    buildSharedTextures(this);   // every texture is generated here — no external assets
    bootResolve();
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
    if (this.proj.slots) this.setPool(this.proj.slots);  // rehydrate / initial pool
    this.startAmbient();                                 // blink + monitor flicker
    this.sys.setVisible(this.proj.floor === ACTIVE);
  }

  // Visualize the parallel IC pool (Wave C): N junior engineers building at once,
  // bobbing in the engineering room, with a ⚙ count badge. n<=1 => serial (none).
  setPool(n){
    if (this.poolGroup) this.poolGroup.forEach(s=>s.destroy());
    if (this.poolLabel){ this.poolLabel.destroy(); this.poolLabel = null; }
    this.poolGroup = [];
    if (!n || n <= 1) return;
    const R = ROOMS.engineering, count = Math.min(n, 8);
    for (let i=0;i<count;i++){
      const x = Math.round(R.x0+24 + (R.w-48)*(count>1 ? i/(count-1) : 0.5));
      const y = FLOOR_BOT - 96 + (i%2)*42;                    // two rows of desks
      const k = poolKit(i);
      const ws = this.makeWorkstation(x, y+22, k.body);
      const s = this.makePerson(x, y, k);
      s.setScale(2.3).setDepth(y).setAlpha(0.96);
      s.home = {x,y}; s.loungeSlot = 12 + i; s.seen = true;    // can walk to the lounge too
      this.startWalk(s);                                       // ICs are busy building
      this.poolGroup.push(s, ws);
    }
    this.poolLabel = this.add.text(R.cx, FLOOR_TOP+8, "⚙ "+n+" ICs",
      {fontFamily:"'PixelFont','Courier New'",fontSize:"11px",fontStyle:"bold",color:"#ffc08a"})
      .setOrigin(0.5,0).setDepth(28);
  }

  buildRooms(){
    this.roomGlow = {};
    const floorH = H - FLOOR_TOP;
    for (const key in ROOMS){
      const R = ROOMS[key];
      // wall (top) -> baseboard -> floor (bottom): a side-on interior per room.
      this.add.tileSprite(R.x0, 0, R.w, FLOOR_TOP, "wall_"+key).setOrigin(0,0).setDepth(0);
      this.add.tileSprite(R.x0, FLOOR_TOP, R.w, floorH, "floor_"+key).setOrigin(0,0).setDepth(0);
      // baseboard skirting where wall meets floor (light top edge + dark shadow).
      this.add.rectangle(R.x0, FLOOR_TOP-6, R.w, 6, shade(R.color,0.5)).setOrigin(0,0).setDepth(1);
      this.add.rectangle(R.x0, FLOOR_TOP, R.w, 3, 0x000000, 0.3).setOrigin(0,0).setDepth(1);
      // room divider pilasters
      this.add.rectangle(R.x1-2, 0, 2, H, 0x000000, 0.22).setOrigin(0,0).setDepth(1);
      this.add.rectangle(R.x1, 0, 2, H, 0xffffff, 0.04).setOrigin(0,0).setDepth(1);
      // hanging room sign at the very top of the wall
      const sw = Math.min(R.w-14, R.label.length*7+18);
      this.add.rectangle(R.cx, 12, sw, 20, shade(R.color,0.24)).setOrigin(0.5,0).setDepth(2)
        .setStrokeStyle(2, shade(R.color,0.62));
      this.add.text(R.cx, 15, R.label, {
        fontFamily:"'PixelFont','Courier New',monospace", fontSize:"12px",
        fontStyle:"bold", color:"#eef3ff",
      }).setOrigin(0.5,0).setDepth(3);
      // a big lit window (animated sky glow) centred on the wall
      const wy = 70, ww = Math.min(72, R.w-66), wh = 70;
      this.add.rectangle(R.cx, wy, ww+8, wh+8, shade(R.color,0.16)).setOrigin(0.5,0).setDepth(2);
      const glow = this.add.rectangle(R.cx, wy+4, ww, wh, 0x1c4a86).setOrigin(0.5,0).setDepth(2);
      this.add.rectangle(R.cx, wy+4, ww, wh*0.5, 0x357bbe).setOrigin(0.5,0).setDepth(2); // sky
      this.add.circle(R.cx+ww*0.28, wy+18, 6, 0xffe9a8, 0.9).setDepth(2);                // sun
      this.add.rectangle(R.cx, wy+4, 3, wh, shade(R.color,0.16)).setOrigin(0.5,0).setDepth(2);
      this.add.rectangle(R.cx, wy+4+wh*0.5, ww, 3, shade(R.color,0.16)).setOrigin(0.5,0).setDepth(2);
      this.tweens.add({targets:glow, fillAlpha:{from:0.7,to:1}, duration:2600, yoyo:true, repeat:-1});
      // a framed poster + a wall clock flanking the window, then the dado rail
      if (R.w > 150){
        this.add.rectangle(R.x0+22, wy+22, 26, 34, shade(R.color,0.5)).setDepth(2).setStrokeStyle(2, shade(R.color,0.7));
        this.add.rectangle(R.x0+22, wy+22, 18, 26, shade(R.color,1.25)).setDepth(2);
      }
      const clk = this.add.circle(R.x1-22, wy+18, 9, 0xf2efe0).setDepth(2).setStrokeStyle(2, 0x2a2a33);
      this.clocks = this.clocks || []; this.clocks.push({x:R.x1-22, y:wy+18});
      this.add.rectangle(R.x1-22, wy+18, 2, 6, 0x2a2a33).setDepth(3);
      this.add.rectangle(R.x0, FLOOR_TOP-34, R.w, 4, shade(R.color,0.5)).setOrigin(0,0).setDepth(1); // dado rail
      this.add.rectangle(R.x0, FLOOR_TOP-34, R.w, 30, shade(R.color,0.34), 0.5).setOrigin(0,0).setDepth(0); // wainscot
      this.roomGlow[key] = this.add.rectangle(R.x0, 0, R.w, H, 0xffd25a, 0)
        .setOrigin(0,0).setDepth(1);
    }
    this.furnish();
    this.setActiveRoom("reception");
  }

  // Procedural pixel furniture so each room reads like a furnished interior.
  furnish(){
    // ambient props at room EDGES (the working desks are per-lead, in the floor)
    const props = {
      reception:   [["plant",0.12],["plant",0.88]],
      product:     [["board",0.10],["plant",0.92]],
      architecture:[["board",0.10],["plant",0.90]],
      engineering: [["rack",0.07],["plant",0.94]],
      qa:          [["shelf",0.07],["plant",0.95]],
    };
    for (const key in props){
      const R = ROOMS[key]; if(!R) continue;
      for (const [kind,fx] of props[key]) this.makeProp(kind, R.x0 + R.w*fx, FLOOR_TOP);
    }
    // the lounge: a couch + coffee table + plants — where idle folks hang out
    const L = ROOMS.lounge;
    if (L){
      this.makeProp("plant", L.x0 + 16, FLOOR_TOP);
      this.makeProp("couch", L.cx, FLOOR_TOP + 118);
      this.makeProp("ctable", L.cx, FLOOR_TOP + 168);
      this.makeProp("plant", L.x1 - 16, FLOOR_TOP);
    }
  }

  makeProp(kind, x, floorY){
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(3);
    const out = (rx,ry,w,h,col)=>{ // outlined pixel rect (1px black border)
      c.add(this.add.rectangle(rx,ry,w+2,h+2,0x0a0d16).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry,w,h,col).setOrigin(0.5,1));
    };
    if (kind==="plant"){
      out(0,0,8,12,0x6a4a2a); out(0,-10,18,16,0x2f8f4e); out(-4,-18,10,12,0x37a85c);
    } else if (kind==="monitor"){
      out(0,0,6,10,0x2a3550); out(0,-9,26,18,0x10151f);
      c.add(this.add.rectangle(0,-11,20,12,0x2bd3ff,0.85).setOrigin(0.5,1)); // glowing screen
    } else if (kind==="desk"){
      out(0,0,46,14,0x6b4a2c); out(-16,-12,10,12,0x10151f); // desk + small monitor
      c.add(this.add.rectangle(-16,-14,7,7,0x7dffb0,0.8).setOrigin(0.5,1));
    } else if (kind==="rack"){
      out(0,0,26,40,0x1b2233);
      for(let i=0;i<5;i++) c.add(this.add.rectangle(8,-6-i*7,6,3,i%2?0x5ddc7a:0xffd95e).setOrigin(0.5,1));
    } else if (kind==="shelf"){
      out(0,0,34,30,0x5a4326);
      for(let i=0;i<4;i++) c.add(this.add.rectangle(-12+i*8,-16,5,12,[0xff8fae,0x7fd1ff,0xffd95e,0x7dffb0][i]).setOrigin(0.5,1));
    } else if (kind==="board"){
      out(0,-6,40,26,0x223047);
      c.add(this.add.rectangle(-12,-22,12,3,0xffd95e).setOrigin(0.5,1));
      c.add(this.add.rectangle(2,-16,16,3,0x7fd1ff).setOrigin(0.5,1));
      c.add(this.add.rectangle(-6,-10,10,3,0x7dffb0).setOrigin(0.5,1));
    } else if (kind==="couch"){
      out(0,0,52,10,0x6a4a7a);                                  // seat
      out(0,-8,52,8,0x7a5a8a);                                  // backrest
      c.add(this.add.rectangle(-25,-4,5,14,0x5a3a6a).setOrigin(0.5,1)); // arm L
      c.add(this.add.rectangle(25,-4,5,14,0x5a3a6a).setOrigin(0.5,1));  // arm R
      c.add(this.add.rectangle(-11,-3,16,4,0x8a6aa0).setOrigin(0.5,1)); // cushions
      c.add(this.add.rectangle(11,-3,16,4,0x8a6aa0).setOrigin(0.5,1));
    } else if (kind==="ctable"){
      out(0,0,24,7,0x6b4a2c);                                   // coffee table
      c.add(this.add.rectangle(-5,-6,6,4,0x9adfff).setOrigin(0.5,1)); // mug
      c.add(this.add.rectangle(7,-6,8,2,0xe8e0c0).setOrigin(0.5,1));  // a magazine
    }
    return c;
  }

  // A desk + monitor + keyboard each lead works at, placed IN the room floor.
  makeWorkstation(x, floorY, accent){
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(floorY+5);  // occludes its lead's legs
    const O = 0x0a0d16;
    const out = (rx,ry,w,h,col)=>{
      c.add(this.add.rectangle(rx,ry,w+2,h+2,O).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry,w,h,col).setOrigin(0.5,1));
    };
    out(0,0,40,7,0x6b4a2c);                                       // desktop (smaller)
    c.add(this.add.rectangle(-17,7,3,10,0x4a3320).setOrigin(0.5,1));
    c.add(this.add.rectangle(17,7,3,10,0x4a3320).setOrigin(0.5,1)); // legs
    out(-4,-7,17,12,0x10151f);                                    // monitor bezel
    const screen = this.add.rectangle(-4,-9,12,8,shade(accent,1.25),0.92).setOrigin(0.5,1);
    c.add(screen);                                                // dept-tinted screen
    c.add(this.add.rectangle(-7,-12,6,1.5,shade(accent,1.5))); c.add(this.add.rectangle(-8,-9,9,1.5,shade(accent,1.4))); // code lines
    out(12,-1,11,3,0x2a3550);                                     // keyboard
    this.screens = this.screens || []; this.screens.push(screen);
    return c;
  }

  // ========================= animation library ============================ //
  // Ambient life: blink + monitor flicker, always running on a floor.
  startAmbient(){
    this.time.addEvent({delay:2600, loop:true, callback:()=>{
      for (const a of this.agents.values()) if (a.seen) this.blink(a);
      this.blink(this.cto);
    }});
    this.time.addEvent({delay:360, loop:true, callback:()=>{
      const s = this.screens; if (!s || !s.length) return;
      const sc = s[(this._sf = (this._sf||0)+1) % s.length];
      if (sc) this.tweens.add({targets:sc, alpha:{from:0.6,to:0.95}, duration:150, yoyo:true});
    }});
    this.time.addEvent({delay:2600, loop:true, callback:()=>this.mill()});  // lounge wander
  }
  blink(a){
    if (!a || !a.eyes) return;
    a.eyes.forEach(e=>{ e.scaleY = 0.15; });
    this.time.delayedCall(110, ()=>{ if (a.eyes) a.eyes.forEach(e=>{ e.scaleY = 1; }); });
  }
  // generic effects
  floatText(x,y,txt,color,size){
    const t = this.add.text(x,y,txt,{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:(size||13)+"px",fontStyle:"bold",color:color||"#ffe9a8"}).setOrigin(0.5).setDepth(40);
    this.tweens.add({targets:t, y:y-34, alpha:{from:1,to:0}, duration:1100, ease:"Cubic.out",
      onComplete:()=>t.destroy()});
  }
  burst(x,y,colors,n){
    n = n||12;
    for (let i=0;i<n;i++){
      const ang=(Math.PI*2*i)/n + (x%6)/6, sp=44+(i%4)*22;
      const p=this.add.rectangle(x,y,4,4,colors[i%colors.length]).setDepth(41);
      this.tweens.add({targets:p, x:x+Math.cos(ang)*sp, y:y+Math.sin(ang)*sp,
        alpha:{from:1,to:0}, scale:{from:1.4,to:0.2}, duration:560+(i%5)*40, ease:"Cubic.out",
        onComplete:()=>p.destroy()});
    }
  }
  ring(x,y,color){
    const r=this.add.circle(x,y,8,color,0).setStrokeStyle(3,color).setDepth(41);
    this.tweens.add({targets:r, scale:{from:0.4,to:4}, alpha:{from:0.9,to:0}, duration:520,
      onComplete:()=>r.destroy()});
  }
  shake(ms,amt){ this.cameras.main.shake(ms||300, amt||0.012); }
  hop(a, times){
    if (!a) return; const y0=a.y; let i=0; times=times||1;
    const up=()=>{ this.tweens.add({targets:a, y:y0-15, duration:130, ease:"Quad.out", yoyo:true,
      onComplete:()=>{ a.y=y0; if (++i<times) up(); }}); };
    up();
  }
  codeGlyph(a){
    if (!a) return;
    const g=["</>","{ }","( )",";","==","fn","[]","#"][Math.abs(Math.round(a.x))%8];
    this.floatText(a.x+12, a.y-30, g, "#9fe6ff", 12);
  }
  // gate stamp (big PASS/FAIL slam) + ring + burst
  stampGate(pass){
    const g=this.gateGeom; if (!g) return;
    const cx=g.cx, cy=(g.atop+g.abot)/2, col=pass?0x5ddc7a:0xff5d6c;
    const s=this.add.container(cx,cy).setDepth(42);
    s.add(this.add.rectangle(0,0,58,32,0x101622,0.96).setStrokeStyle(3,col));
    s.add(this.add.text(0,0,pass?"PASS":"FAIL",{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"16px",fontStyle:"bold",color:pass?"#9dffbf":"#ffb3ba"}).setOrigin(0.5));
    s.setScale(3.4).setAlpha(0).setAngle(-12);
    this.tweens.add({targets:s, scale:1, alpha:1, angle:-6, duration:170, ease:"Back.out",
      onComplete:()=>{ this.ring(cx,cy,col); this.burst(cx,cy,[col,0xffffff],16);
        this.time.delayedCall(950,()=>this.tweens.add({targets:s,alpha:0,duration:300,onComplete:()=>s.destroy()})); }});
    if (!pass) this.shake(380,0.015);
  }
  // checkpoint spotlight + bouncing "?" over the CTO
  spotlightCTO(on){
    if (this.spot){ this.spot.destroy(); this.spot=null; }
    if (this.qmark){ this.qmark.destroy(); this.qmark=null; }
    if (!on || !this.cto) return;
    const x=this.cto.x;
    const dim=this.add.rectangle(0,0,W,H,0x05080f,0.5).setOrigin(0,0);
    const beam=this.add.triangle(x,FLOOR_TOP-70,-66,180,66,180,0,0,0xfff1c0,0.10);
    this.spot=this.add.container(0,0).setDepth(34); this.spot.add([dim,beam]);
    const q=this.add.text(x,this.cto.y-48,"?",{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"24px",fontStyle:"bold",color:"#ffd95e"}).setOrigin(0.5).setDepth(43);
    this.qmark=q;
    this.tweens.add({targets:q, y:q.y-7, duration:480, yoyo:true, repeat:-1, ease:"Sine.inOut"});
  }
  celebrate(){
    for (const a of this.agents.values()) if (a.seen) this.hop(a,3);
    this.hop(this.cto,3);
    for (const s of (this.poolGroup||[])) if (s && s.bob) this.hop(s,2);
    this.confetti();
  }
  confetti(){
    const cols=[0xff5d6c,0x5ddc7a,0x5db8ff,0xffcf52,0xff8fae,0xb59bff];
    for (let i=0;i<72;i++){
      const x=20+Math.abs((i*97)%(W-40)), c=cols[i%cols.length];
      const p=this.add.rectangle(x,-12,5,8,c).setDepth(45).setAngle((i*53)%360);
      this.tweens.add({targets:p, y:H+20, angle:p.angle+360, duration:1900+(i%6)*260,
        delay:(i%10)*70, ease:"Cubic.in", onComplete:()=>p.destroy()});
      this.tweens.add({targets:p, x:x+((i%2)?46:-46), duration:1900, yoyo:true});
    }
  }
  gloom(){
    const o=this.add.rectangle(0,0,W,H,0x0a0f1c,0).setOrigin(0,0).setDepth(33);
    this.tweens.add({targets:o, fillAlpha:0.45, duration:700});
    for (const a of this.agents.values()) if (a.seen)
      this.tweens.add({targets:a.bob, angle:7, y:3, duration:500});
  }
  banner(txt,color){
    const b=this.add.container(W/2, FLOOR_TOP+40).setDepth(46);
    b.add(this.add.rectangle(0,0,380,48,0x0c1322,0.95).setStrokeStyle(3,color));
    b.add(this.add.text(0,0,txt,{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"22px",fontStyle:"bold",color:"#ffffff"}).setOrigin(0.5));
    b.setScale(0.2).setAlpha(0);
    this.tweens.add({targets:b, scale:1, alpha:1, duration:300, ease:"Back.out"});
    this.time.delayedCall(2400,()=>this.tweens.add({targets:b, alpha:0, y:b.y-20, duration:400,
      onComplete:()=>b.destroy()}));
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
    let li = 0;                                    // global lounge-slot index
    for (const room in ROSTER){
      const names = ROSTER[room], R = ROOMS[room];
      names.forEach((name,i)=>{
        const span = R.w-64;
        const hx = R.x0+36 + (names.length>1 ? span*i/(names.length-1) : span/2);
        // each lead works at their OWN desk inside the room, staggered front/back
        const hy = FLOOR_TOP + 78 + (i%2)*94;
        const seed = hashStr((this.proj.id||'f')+name), kit = kitFor(name, seed);
        this.makeWorkstation(hx, hy+22, kit.body);                // desk just in front of them
        const a = this.makePerson(hx, hy, kit);
        a.setScale(2.9).setDepth(hy);                             // y-sort (desk occludes legs)
        a.home = {x:hx,y:hy}; a.room = room; a.loungeSlot = li++;
        a.setPosition(hx, hy+200).setAlpha(0.32);
        a.seen = false; a.bubble = null;
        // Clickable -> interview this lead (their journal + a question), advisory.
        a.setInteractive(new Phaser.Geom.Rectangle(-8,-8,16,15), Phaser.Geom.Rectangle.Contains);
        a.on("pointerdown", ()=> openInterview(name));
        a.on("pointerover", ()=>{ this.input.manager.canvas.style.cursor = "pointer"; });
        a.on("pointerout", ()=>{ this.input.manager.canvas.style.cursor = "default"; });
        this.tweens.add({targets:a, y:hy, duration:700, delay:120*i, ease:"Back.out"});
        this.agents.set(name, a);
      });
    }
  }

  buildCTO(){
    const c = this.makePerson(ROOMS.reception.cx, FLOOR_BOT-18, kitFor("CTO", hashStr((this.proj.id||'f')+"CTO")));
    c.setScale(2.6).setDepth(15);
    c.add(this.add.text(0,-22,"CTO",{fontFamily:"'PixelFont','Courier New'",fontSize:"5px",color:"#ffcf52"})
      .setOrigin(0.5));
    this.cto = c;
    this.tweens.add({targets:c.bob, y:-1, duration:380, yoyo:true, repeat:-1, ease:"Sine.inOut"});
  }

  // Our own generated GBA-style sprite: big head, tiny torso, pointed ears, vertical
  // eyes, colourful clothes. `opt` = a role/pool kit (body/trim/style/cloth/hw/acc).
  makePerson(x,y,opt){
    opt = opt || {};
    const c = this.add.container(x,y).setDepth(10);
    const O=0x0a0a0c, e=0x141822, g=0x464d60, m=0x5a627a, lens=0xbfe6ff, pant=0x2b3146, shoe=0x0a0a0c;
    const body=opt.body!=null?opt.body:0x4a78c0, a=opt.trim!=null?opt.trim:shade(body,1.4);
    const skin=opt.skin!=null?opt.skin:0xf2c79c, hair=opt.hair!=null?opt.hair:0x3a2616;
    c.add(this.add.ellipse(0.5,6.5,11,3,0x000000,0.3));    // shadow under the feet
    const bob = this.add.container(0,0);
    const P=(gx,gy,w,h,col)=>{ const r=this.add.rectangle(gx-8,gy-7,w,h,col).setOrigin(0,0); bob.add(r); return r; };
    const pal={K:O,h:hair,s:skin,c:body,p:pant,b:shoe};
    SP_BASE.forEach((row,gy)=>{ for(let gx=0;gx<16;gx++){ const col=pal[row[gx]]; if(col!=null) P(gx,gy,1,1,col); }});
    // pointed ears
    P(2,5,1,1,skin);P(1,4,1,1,skin);P(0,4,1,1,O);P(1,3,1,1,O);P(1,5,1,1,O);
    P(11,5,1,1,skin);P(12,4,1,1,skin);P(13,4,1,1,O);P(12,3,1,1,O);P(12,5,1,1,O);
    // colourful clothing detail
    const ct=opt.cloth;
    if(ct==='collar') P(4,8,6,1,a);
    else if(ct==='stripe'){P(6,8,1,2,a);P(7,8,1,2,a);}
    else if(ct==='belt') P(4,9,6,1,a);
    else if(ct==='vest'){P(4,8,1,2,a);P(9,8,1,2,a);P(5,8,4,1,a);}
    else if(ct==='twotone') P(4,9,6,1,a);
    else if(ct==='hoodie'){P(4,7,6,1,a);P(5,8,1,1,a);P(8,8,1,1,a);}
    // vertical eyes (tracked for blink)
    const eL=P(4,5,1,2,e), eR=P(9,5,1,2,e);
    // hair styles
    const st=opt.style||'short';
    if(st==='short'){P(2,4,1,1,hair);P(11,4,1,1,hair);}
    else if(st==='swept'){P(2,4,1,2,hair);P(3,4,1,1,hair);P(9,4,2,1,hair);P(11,4,1,2,hair);}
    else if(st==='sidelock'){P(11,4,1,5,hair);P(12,4,1,6,O);P(11,9,1,1,O);P(2,4,1,1,hair);}
    else if(st==='bun'){P(6,-1,4,2,hair);P(5,-1,1,2,O);P(10,-1,1,2,O);P(5,-2,5,1,O);P(2,4,1,1,hair);P(11,4,1,1,hair);}
    else if(st==='spiky'){[4,6,8,10].forEach(gx=>P(gx,0,1,1,hair));P(5,-1,1,1,hair);P(9,-1,1,1,hair);}
    else if(st==='afro'){for(let gx=3;gx<11;gx++)P(gx,0,1,1,hair);P(2,1,1,3,hair);P(11,1,1,3,hair);P(2,0,8,1,O);P(1,1,1,4,O);P(12,1,1,4,O);}
    else if(st==='long'){P(2,4,1,6,hair);P(11,4,1,6,hair);P(1,4,1,7,O);P(12,4,1,7,O);P(2,10,2,1,O);P(10,10,2,1,O);}
    else if(st==='ponytail'){P(11,3,1,5,hair);P(12,3,1,6,O);P(11,8,1,1,O);P(2,4,1,1,hair);}
    // headwear (coloured, never pure black)
    if(opt.cap!=null){P(3,1,8,2,opt.cap);P(2,2,10,1,opt.cap);P(2,0,8,1,O);P(0,2,3,1,O);P(12,2,1,1,O);}
    if(opt.hood!=null){P(2,-1,10,4,opt.hood);P(2,3,2,7,opt.hood);P(12,3,2,7,opt.hood);P(1,-1,1,11,O);P(14,3,1,7,O);P(2,-2,10,1,O);}
    // accessories (slate glasses/headset, not black)
    if(opt.glasses){P(3,5,3,2,g);P(8,5,3,2,g);P(6,5,2,1,g);P(4,5,1,1,lens);P(9,5,1,1,lens);}
    if(opt.headset){P(2,1,10,1,m);P(2,2,1,3,m);P(11,4,1,2,m);P(11,6,1,1,0xcc5555);}
    if(opt.scarf!=null){P(4,7,6,1,opt.scarf);P(8,8,1,1,opt.scarf);}
    if(opt.tie!=null){P(8,8,1,2,opt.tie);}
    if(opt.coat){P(4,8,1,2,0xe6e9ef);P(9,8,1,2,0xd2d7e0);}
    const spark = this.add.rectangle(4,-10,2,2,0xfff04a).setOrigin(0,0).setVisible(false); bob.add(spark);
    c.add(bob);
    c.bob = bob; c.spark = spark; c.eyes = [eL, eR];
    return c;
  }

  // Stepped walk: a snappy 2-frame bob + waddle on the whole body (legs are baked).
  startWalk(a){
    if (!a || !a.bob || a.walkEv) return;
    let f = 0;
    a.walkEv = this.time.addEvent({delay:130, loop:true, callback:()=>{
      f ^= 1;
      a.bob.y = f ? -1 : 0;
      a.bob.x = f ? 0.6 : -0.6;
    }});
  }
  stopWalk(a){
    if (!a || !a.bob) return;
    if (a.walkEv){ a.walkEv.remove(); a.walkEv = null; }
    a.bob.y = 0; a.bob.x = 0;
  }

  setActiveRoom(key){
    this.activeRoom = key;
    for (const k in this.roomGlow)
      this.tweens.add({targets:this.roomGlow[k], fillAlpha:(k===key?0.10:0), duration:420});
    if (ROOMS[key]) this.walkCTO(ROOMS[key].cx);
    this.staffRoom(key);                 // active team to desks, everyone else to the lounge
  }

  // ---- movement: desks <-> lounge -----------------------------------------
  loungeSpot(i){
    const R = ROOMS.lounge; if (!R) return {x: 80, y: FLOOR_BOT-40};
    const per = 4, col = i%per, row = Math.floor(i/per);
    return { x: R.x0 + 22 + col*((R.w-44)/(per-1)), y: FLOOR_TOP + 150 + row*64 };
  }
  walkTo(a, tx, ty, then){
    if (!a) return;
    if (a.moveTween) a.moveTween.stop();
    const dx = tx - a.x;
    if (Math.abs(dx) > 3) a.scaleX = dx < 0 ? -1 : 1;   // face travel direction
    const dist = Math.hypot(tx-a.x, ty-a.y);
    a.setDepth(Math.max(ty, a.y) + 1);
    if (dist < 3){ a.x = tx; a.y = ty; if (then) then(); return; }
    this.startWalk(a);
    a.moveTween = this.tweens.add({targets:a, x:tx, y:ty, duration:Math.max(450, dist*4), ease:"Sine.inOut",
      onComplete:()=>{ this.stopWalk(a); a.scaleX = 1; a.setDepth(ty); if (then) then(); }});
  }
  gotoLounge(a){ if (!a || !a.seen) return; a.inLounge = true;
    const s = this.loungeSpot(a.loungeSlot||0); this.walkTo(a, s.x + (Math.random()*12-6), s.y); }
  gotoDesk(a){ if (!a) return; a.inLounge = false;
    if (!a.seen){ a.seen = true; a.setAlpha(1); }
    if (a.home) this.walkTo(a, a.home.x, a.home.y); }
  staffRoom(key){
    this.activeRoomKey = key;
    const hasTeam = !!ROSTER[key];
    for (const [name,a] of this.agents){
      if (hasTeam && AGENT_ROOM[name] === key) this.gotoDesk(a);
      else if (a.seen) this.gotoLounge(a);
    }
    for (const s of (this.poolGroup||[])){ if (!s || !s.bob) continue;
      if (key === 'engineering' && s.home) this.walkTo(s, s.home.x, s.home.y); else this.gotoLounge(s); }
  }
  toLounge(){   // everyone hangs out (checkpoints / done)
    for (const [,a] of this.agents) if (a.seen) this.gotoLounge(a);
    for (const s of (this.poolGroup||[])) if (s && s.bob) this.gotoLounge(s);
  }
  mill(){       // gentle wander for whoever is in the lounge
    const move = (a)=>{ if (a && a.seen && a.inLounge && !a.moveTween?.isPlaying() && Math.random()<0.5){
      const s = this.loungeSpot(a.loungeSlot||0); this.walkTo(a, s.x+(Math.random()*20-10), s.y+(Math.random()*16-8)); } };
    for (const [,a] of this.agents) move(a);
    for (const s of (this.poolGroup||[])) move(s);
  }

  walkCTO(tx){
    if (!this.cto) return;
    const dist = Math.abs(tx - this.cto.x);
    if (dist < 2) return;
    this.cto.scaleX = (tx < this.cto.x) ? -1 : 1;   // face the way they walk
    this.startWalk(this.cto);
    if (this.ctoWalk) this.ctoWalk.stop();
    this.ctoWalk = this.tweens.add({
      targets:this.cto, x:tx, duration:Math.max(350,dist*4), ease:"Sine.inOut",
      onComplete:()=>{ this.stopWalk(this.cto); this.cto.scaleX = 1; },
    });
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
    // typing: quick stepped bob + a couple of floating code glyphs rising off the desk
    a.workTween = this.tweens.add({
      targets:a.bob, y:-2, duration:90, yoyo:true, repeat:13, ease:"Sine.inOut",
      onComplete:()=>{ a.spark.setVisible(false); a.bob.y=0; },
    });
    this.codeGlyph(a);
    this.time.delayedCall(420, ()=>this.codeGlyph(a));
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
      {fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#1a2236"}).setOrigin(0,0)));
    c.setScale(0.6); c.setAlpha(0);
    this.tweens.add({targets:c, scale:1, alpha:1, duration:200, ease:"Back.out"});
    return c;
  }

  spawnPickup(room, label){
    const R = ROOMS[room] || ROOMS.product;
    const w = Math.max(64, label.length*7+18);
    const c = this.add.container(R.cx, FLOOR_BOT-60).setDepth(20);
    c.add(this.add.rectangle(0,0,w,18,0x161d31).setStrokeStyle(2,0xffcf52));
    c.add(this.add.text(0,0,"✚ "+label,{fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#ffe9a8"})
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
        c.add(this.add.text(10,3,"",{fontFamily:"'PixelFont','Courier New'",fontSize:"12px",
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
    c.add(this.add.text(8,5,"🏢 ORG · PM",{fontFamily:"'PixelFont','Courier New'",fontSize:"12px",
      fontStyle:"bold",color:"#ffcf52"}).setOrigin(0,0));
    areas.forEach((a,i)=>{
      const y = headerH + i*rowH;
      const col = areaColor[a.area] || areaColor.default;
      c.add(this.add.rectangle(8,y+3,10,10,col).setOrigin(0,0));
      const leads = (a.leads||[]).join(", ");
      c.add(this.add.text(24,y,`${a.area}: ${leads}`,
        {fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#e8eefb"}).setOrigin(0,0));
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
  const slots = (extra && extra.max_slots) || 1;
  const F = {
    id: resp.id, floor, request:req, mode, backend, scene:null,
    log:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
    slots, journal:[], seenUris:new Set(),
  };
  FLOORS.set(floor, F);
  GAME.scene.add(floorKey(floor), FloorScene, true, {floor, id:resp.id, request:req, mode, slots});
  switchToFloor(floor);

  const es = new EventSource("/api/stream/"+resp.id);
  es.onmessage = e => { try { routeEvent(floor, JSON.parse(e.data)); } catch(_){} };
  es.addEventListener("end", ()=>es.close());
  F.es = es;
}

function switchToFloor(floor){
  const w = document.getElementById("wipe");      // chunky Zelda-style screen wipe
  if (w){ w.classList.remove("go"); void w.offsetWidth; w.classList.add("go"); }
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
      F.slots = ev.slots || 1;
      if (S) S.setPool(F.slots);
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
      if (S){ S.spawnPickup(ev.room, ev.label); const pa=S.agents.get(ev.agent);
        if (pa){ S.hop(pa,1); const R=ROOMS[ev.room]; if(R) S.burst(R.cx, FLOOR_TOP+70, [0xffd95e,0xffffff], 8); } }
      logTo(F, "good", "✚ "+ev.label+"  ·  "+ev.agent); break;
    case "gate":
      F.gates[ev.name] = {status:ev.status, attested:ev.attested, advisory:ev.advisory};
      if (S){ S.setGate(ev.name, F.gates[ev.name]);
        const g=S.gateGeom; if(g){ const col=ev.status==="pass"?0x5ddc7a:0xff5d6c;
          S.ring(g.cx,(g.atop+g.abot)/2,col); S.burst(g.cx,(g.atop+g.abot)/2,[col,0xffffff],8); } }
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
      F.awaiting = true; if (Array.isArray(ev.journal)) F.journal = ev.journal;
      renderElevator();
      if (S){ S.spotlightCTO(true); S.toLounge(); }   // team hangs out while CTO decides
      if (isActive) showCheckpoint(F); break;
    case "resolved":
      F.awaiting = false; renderElevator();
      if (S){ S.spotlightCTO(false);
        if (S.activeRoomKey) S.staffRoom(S.activeRoomKey);   // back to work
        const ap = ev.decision === "approve";
        S.hop(S.cto, ap?2:1); S.ring(S.cto.x, S.cto.y-10, ap?0x5ddc7a:0xff5d6c);
        S.burst(S.cto.x, S.cto.y-10, ap?[0x5ddc7a,0xffffff]:[0xff5d6c,0xffffff], 12);
        S.floatText(S.cto.x, S.cto.y-30, ap?"APPROVED":"REJECTED", ap?"#9dffbf":"#ffb3ba", 13);
        if (!ap) S.shake(280,0.01); }
      if (isActive) hideCheckpoint(); break;
    case "release":
      if (S){ S.openGateAndChest(); S.stampGate(true); S.celebrate(); S.banner("SHIPPED!", 0x5ddc7a); }
      logTo(F, "gold", "💰 RELEASED → "+(ev.uri||"artifact")); break;
    case "halt":
      if (S){ S.flashGate(); S.stampGate(false); }
      logTo(F, "bad", "⛔ HELD: "+ev.reason); break;
    case "done":
      F.status = "done"; F.passed = !!ev.passed; renderElevator();
      if (S){ S.toLounge(); if (!ev.passed){ S.gloom(); S.banner("HELD AT THE GATE", 0xff5d6c); } }
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

// ---- interview a lead (click an agent) -------------------------------------
let IV_TARGET = null;
function openInterview(name){
  const F = FLOORS.get(ACTIVE); if(!F) return;
  const journal = F.journal || [];
  const mine = journal.filter(e=>e.role===name);
  const entries = mine.length ? mine : journal;
  document.getElementById("iv-role").textContent = name;
  const jc = document.getElementById("iv-journal"); jc.innerHTML = "";
  if(!entries.length){
    jc.innerHTML = "<div class='cp-note'>No journal yet — reach a checkpoint first (or run supervised).</div>";
  }
  for(const e of entries.slice(0,6)){
    const d = document.createElement("div"); d.className = "iv-entry";
    const conf = (e.confidence!=null) ? ` · conf ${Math.round(e.confidence*100)}%` : "";
    d.innerHTML = `<div class="role"></div><div class="sum"></div><div class="meta"></div>`;
    d.querySelector(".role").textContent = (e.role||"agent")+conf;
    d.querySelector(".sum").textContent = e.summary||"";
    const meta = d.querySelector(".meta");
    const assumptions = e.assumptions||[], concerns = e.concerns||[];
    if(assumptions.length){ const a=document.createElement("div"); a.textContent="assumes: "+assumptions.join("; "); meta.appendChild(a); }
    if(concerns.length){ const c=document.createElement("div"); c.className="concern"; c.textContent="concern: "+concerns.join("; "); meta.appendChild(c); }
    jc.appendChild(d);
  }
  const ans=document.getElementById("iv-answer"); ans.classList.add("hidden"); ans.textContent="";
  document.getElementById("iv-question").value = "";
  IV_TARGET = {id:F.id, role:name};
  document.getElementById("iv-modal").classList.remove("hidden");
}
function askInterview(){
  if(!IV_TARGET) return;
  const q = document.getElementById("iv-question").value.trim();
  const ans = document.getElementById("iv-answer");
  ans.classList.remove("hidden"); ans.textContent = "…asking "+IV_TARGET.role+"…";
  fetch("/api/interview/"+IV_TARGET.id,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({role:IV_TARGET.role, question:q})})
   .then(r=>r.json()).then(d=>{ ans.textContent = d.answer || "(no answer)"; })
   .catch(()=>{ ans.textContent = "(interview request failed)"; });
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
  const extra={max_slots: parseInt(document.getElementById("slots-input").value,10)||1};
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
// interview modal
document.getElementById("iv-ask").onclick=askInterview;
document.getElementById("iv-close").onclick=()=>document.getElementById("iv-modal").classList.add("hidden");
document.getElementById("iv-question").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); askInterview(); }});
// suggest a parallel-IC count from the request + packs
document.getElementById("suggest-btn").onclick=()=>{
  const request=document.getElementById("req-input").value;
  const packs=document.getElementById("packs-input").value.split(",").map(s=>s.trim()).filter(Boolean);
  const hint=document.getElementById("slots-hint"); hint.textContent="…";
  fetch("/api/suggest-slots",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({request,packs})})
   .then(r=>r.json()).then(d=>{
     document.getElementById("slots-input").value=d.slots;
     hint.textContent="≈"+d.slots+" areas: "+(d.areas||[]).join(", ");
   }).catch(()=>{ hint.textContent=""; });
};

// preselect the configured default backend (FOUNDRY_MODEL_BACKEND on the server;
// mock if unset). A deployment sets the env to default to claude-cli.
fetch("/api/config").then(r=>r.json()).then(c=>{
  const sel=document.getElementById("backend-select");
  if(c && c.default_backend && [...sel.options].some(o=>o.value===c.default_backend))
    sel.value=c.default_backend;
  if(c && c.default_max_slots) document.getElementById("slots-input").value=c.default_max_slots;
}).catch(()=>{});

// Rehydrate on (re)load: floors run server-side independently of this tab, so a
// refresh / reconnect from another device rebuilds every floor + reconnects the
// live stream of any still-running build. No floors yet => open the new dialog.
function adoptFloor(row){
  if (FLOORS.has(row.floor)) return;
  const F = {
    id:row.id, floor:row.floor, request:row.request, mode:row.mode, backend:row.backend,
    scene:null, log:[], gates:{}, org:null, cp:null, awaiting:!!row.awaiting,
    status:row.status, passed:!!row.passed, slots:row.slots||1, journal:[], seenUris:new Set(),
  };
  FLOORS.set(row.floor, F);
  GAME.scene.add(floorKey(row.floor), FloorScene, true,
    {floor:row.floor, id:row.id, request:row.request, mode:row.mode, slots:F.slots});
  if (row.status !== "done"){          // running build: reconnect its live stream
    const es = new EventSource("/api/stream/"+row.id);
    es.onmessage = e => { try { routeEvent(row.floor, JSON.parse(e.data)); } catch(_){} };
    es.addEventListener("end", ()=>es.close());
    F.es = es;
  }
}
async function rehydrate(){
  await bootReady;  // shared textures must exist before a FloorScene builds
  try {
    const {projects} = await fetch("/api/projects").then(r=>r.json());
    if(!projects || !projects.length){ newModal.classList.remove("hidden"); return; }
    for(const row of projects) adoptFloor(row);
    switchToFloor(projects[projects.length-1].floor);
    renderElevator();
  } catch(_){ newModal.classList.remove("hidden"); }
}
rehydrate();
