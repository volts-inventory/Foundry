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
const FLOOR_TOP = 100, FLOOR_BOT = H - 40;   // general playfield bounds (rooms are placed boxes)

// ---- floorplan: a tidy 3x3 GRID of section zones with walkway aisles (the "#").
// One open level; the elevator switches PROJECTS. Aisles between cells are corridors
// people walk along; sections are coloured floor rugs in the cells.
const WALL_TOP = 118, FLOOR_BOTTOM = H - 26;
const CELL_W = 368, CELL_H = 196, AISLE = 48, GX0 = 40, GY0 = 132, GX1 = W - 40;
function cellBox(c,r){ return {x: GX0 + c*(CELL_W+AISLE), y: GY0 + r*(CELL_H+AISLE), w:CELL_W, h:CELL_H}; }
const GRID = {  // rooms mirror the pipeline, read left->right, top->bottom:
  // product -> architecture -> engineering -> qa -> security -> gate -> vault.
  product:[0,0], architecture:[1,0], engineering:[2,0],
  qa:[0,1], security:[1,1], gate:[2,1],
  vault:[2,2],                       // gate(2,1) sits directly above vault(2,2)
};
// (0,2) = the LOUNGE (idle agents relax here); (1,2) = a decorative ENTRANCE /
// lobby (the org chart on the wall + where the CTO greets). No work happens in either.
const LOUNGE_CELL = [0,2], ENTRANCE_CELL = [1,2];
const RBASE = {
  product:{label:"PRODUCT",color:0x2e3b5a}, architecture:{label:"ARCHITECTURE",color:0x3a2e5a},
  engineering:{label:"ENGINEERING",color:0x5a3a2e}, qa:{label:"QA · REVIEW",color:0x2e5a4a},
  security:{label:"SECURITY",color:0x5a2e36},
  gate:{label:"GATE",color:0x2c4a55}, vault:{label:"VAULT",color:0x5a4a18},
};
const ROOMS = {};
for (const k in RBASE) ROOMS[k] = Object.assign({}, RBASE[k], cellBox(GRID[k][0], GRID[k][1]));
const ROOM_ORDER = ["product","architecture","engineering","qa","security","gate","vault"];
const TINT = {security:[0xff6d6d,0.05], gate:[0x6aa0ff,0.05], vault:[0xffd25a,0.06]};
// the "#": centre-lines of the walkway aisles between columns / rows
const AISLE_VX = [GX0+CELL_W+AISLE/2, GX0+2*CELL_W+AISLE+AISLE/2];
const AISLE_HY = [GY0+CELL_H+AISLE/2, GY0+2*CELL_H+AISLE+AISLE/2];
(function layout(){ for (const k in ROOMS){ const R = ROOMS[k];
  R.key=k; R.x0=R.x; R.x1=R.x+R.w; R.y1=R.y+R.h; R.cx=R.x+R.w/2; R.cy=R.y+R.h/2;
  R.wallH=0; R.floorY=R.y+18; R.standY=R.y1-14;
} })();
function roomAt(x,y){ for (const k in ROOMS){ const R=ROOMS[k];
  if (x>=R.x && x<=R.x1 && y>=R.y && y<=R.y1) return k; } return null; }

const COLORS = {
  "Stakeholder Proxy":0x7fd1ff,"Product Manager":0xffd95e,"Criteria Critic":0xff8fae,
  "Program Manager":0xb59bff,"Principal Architect":0xc79bff,"Senior Engineer":0xff9b5e,
  "Junior Engineer":0xffc08a,"QA Engineer":0x7dffb0,"SDET":0x5ddc7a,"Reviewer":0x5db8ff,
  "Technical Writer":0xe8e0a0,"AppSec Engineer":0xff6d6d,
  "Audit & Compliance":0xffb38a,"Supply-chain Reviewer":0x9bb0ff,
};
const ROSTER = {
  product:["Stakeholder Proxy","Product Manager","Criteria Critic","Program Manager"],
  architecture:["Principal Architect"],
  engineering:["Senior Engineer","Junior Engineer"],
  qa:["QA Engineer","SDET","Reviewer","Technical Writer"],
  security:["AppSec Engineer","Audit & Compliance","Supply-chain Reviewer"],
};
const GATE_ORDER = ["spec","build","test","review","security","audit","supply_chain","e2e"];
const AGENT_ROOM = {}; for (const rm in ROSTER) for (const nm of ROSTER[rm]) AGENT_ROOM[nm] = rm;

// ---- generated character sprites (our own pixel art) -----------------------
// route1rodent-style 16x22 GBA RPG sprite: a BIG rounded hair mass (the hero) over
// a small face, a narrow slate body with one accent colour, and two split leg stubs.
// Character sprites come from the route1rodent sheet (see CHARACTERS / makePerson
// below) — the old procedural pixel body + per-role hair/skin/accent kit is gone.
function hashStr(s){ let h=2166136261; for(let i=0;i<s.length;i++){ h^=s.charCodeAt(i); h=Math.imul(h,16777619); } return h>>>0; }
// route1rodent CC BY-SA 4.0 sheet -> 13x6 grid of 64px frames (assets/sprites/CREDITS.md).
// frame index = row*13 + col. 10 characters; `front` = walk frames (cycle), `back` =
// the away/back-of-head pose used while seated at a desk facing the monitor.
const CHARACTERS = [
  {front:[0,1],      back:26},   // 0 navy
  {front:[2,3,4],    back:28},   // 1 brown
  {front:[5,6,7],    back:31},   // 2 pink
  {front:[8,9,10],   back:34},   // 3 white
  {front:[11,12],    back:37},   // 4 blue
  {front:[39,40],    back:39},   // 5 red
  {front:[41,42,43], back:41},   // 6 purple
  {front:[44,45,46], back:44},   // 7 gold
  {front:[47,48,49], back:47},   // 8 green
  {front:[50,51],    back:50},   // 9 orange
];
// Stable, room-distinct character per role (no two leads in a room share a sprite).
const ROLE_CHAR = {
  "Stakeholder Proxy":4,"Product Manager":7,"Criteria Critic":2,"Program Manager":6,
  "Principal Architect":0,"Senior Engineer":5,"Junior Engineer":9,
  "QA Engineer":8,"SDET":1,"Reviewer":4,"Technical Writer":3,
  "AppSec Engineer":5,"Audit & Compliance":7,"Supply-chain Reviewer":0,"CTO":3,
};
function charFor(name, seed){
  if (ROLE_CHAR[name] != null) return ROLE_CHAR[name];
  return ((seed||hashStr(name||""))>>>0) % CHARACTERS.length;   // pool/tech-leads: hashed
}
function kitFor(name, seed){ return {ci: charFor(name, seed)}; }
function poolKit(i){ return {ci: (i*5+3) % CHARACTERS.length}; }

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
  if (!scene.textures.exists("vignette")){      // radial dark-edge overlay for depth
    const cv = document.createElement("canvas"); cv.width = cv.height = 256;
    const g = cv.getContext("2d"); const rg = g.createRadialGradient(128,128,46,128,128,172);
    rg.addColorStop(0,"rgba(0,0,0,0)"); rg.addColorStop(1,"rgba(0,0,0,1)");
    g.fillStyle = rg; g.fillRect(0,0,256,256); scene.textures.addCanvas("vignette", cv);
  }
  for (const key in ROOMS){
    const tkey = "floor_"+key;
    if (scene.textures.exists(tkey)) continue;
    const R = ROOMS[key]; const g = scene.make.graphics({add:false});
    g.fillStyle(shade(R.color,0.60),1); g.fillRect(0,0,40,40);              // base (lifted)
    g.fillStyle(shade(R.color,0.76),1); g.fillRect(20,0,20,20); g.fillRect(0,20,20,20);
    g.fillStyle(shade(R.color,0.95),1); g.fillRect(6,6,2,2); g.fillRect(28,14,2,2); g.fillRect(14,30,2,2); // bright speckle
    g.fillStyle(shade(R.color,0.45),1); g.fillRect(32,33,2,2); g.fillRect(11,18,2,2);  // dark speckle
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
  // neutral shared-floor + perimeter-wall tiles (open-plan look)
  if (!scene.textures.exists("floor_base")){
    const base=0x232a3c, g=scene.make.graphics({add:false});
    g.fillStyle(shade(base,1.0),1); g.fillRect(0,0,40,40);
    g.fillStyle(shade(base,1.14),1); g.fillRect(20,0,20,20); g.fillRect(0,20,20,20);
    g.fillStyle(shade(base,0.82),1); g.fillRect(0,0,40,1); g.fillRect(0,0,1,40);   // tile seams
    g.generateTexture("floor_base",40,40); g.destroy();
  }
  if (!scene.textures.exists("wall_base")){
    const base=0x1b2236, g=scene.make.graphics({add:false});
    g.fillStyle(shade(base,0.7),1); g.fillRect(0,0,40,32);
    g.fillStyle(shade(base,1.0),1);
    g.fillRect(1,1,18,14); g.fillRect(21,1,18,14); g.fillRect(-9,17,18,14); g.fillRect(11,17,18,14); g.fillRect(31,17,18,14);
    g.fillStyle(shade(base,1.3),1);
    g.fillRect(1,1,18,2); g.fillRect(21,1,18,2); g.fillRect(11,17,18,2); g.fillRect(31,17,18,2);
    g.generateTexture("wall_base",40,32); g.destroy();
  }
}

// =========================================================================== //
//  Boot: generate ALL our own pixel art, then signal ready                    //
// =========================================================================== //
let bootResolve;
const bootReady = new Promise((r)=>{ bootResolve = r; });

class BootScene extends Phaser.Scene {
  constructor(){ super("boot"); }
  preload(){
    // route1rodent 16x16 RPG sprites (CC BY-SA 4.0) — see assets/sprites/CREDITS.md.
    // Re-tiled to a clean 13x6 grid of 64px frames.
    this.load.spritesheet("chars", "/static/assets/sprites/characters.png",
      {frameWidth:64, frameHeight:64});
  }
  create(){
    buildSharedTextures(this);   // procedural textures (floors/walls/props) — sprites load above
    // wait for the pixel font so the FIRST canvas labels render in it (no monospace flash)
    const ready = (document.fonts && document.fonts.load)
      ? document.fonts.load('16px "PixelFont"').then(()=>document.fonts.ready).catch(()=>{})
      : Promise.resolve();
    ready.then(()=>bootResolve());
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
    this.activeRoom = "lobby";
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
  // bobbing in the engineering room, with an "ICs xN" count badge. n<=1 => serial.
  setPool(n){
    if (this.poolGroup) this.poolGroup.forEach(s=>s.destroy());
    if (this.poolLabel){ this.poolLabel.destroy(); this.poolLabel = null; }
    this.poolGroup = [];
    if (!n || n <= 1) return;
    const R = ROOMS.engineering, count = Math.min(n, 8), cols = Math.min(count, 4);
    for (let i=0;i<count;i++){
      const col = i%cols, row = Math.floor(i/cols);
      const x = Math.round(R.x+30 + (R.w-60)*(cols>1 ? col/(cols-1) : 0.5));
      const y = R.floorY + 74 + row*52;                        // pool desks below the leads
      const k = poolKit(i);
      const ws = this.makeWorkstation(x, y+16, k.accent);
      const s = this.makePerson(x, y, k);
      s.setScale(1.4).setDepth(y).setAlpha(0.96);   // match the leads so IC faces stay legible
      s.home = {x,y}; s.loungeSlot = 12 + i; s.seen = true;    // can walk to the lounge too
      this.startWalk(s);                                       // ICs are busy building
      this.poolGroup.push(s, ws);
    }
    this.poolLabel = this.add.text(R.cx, R.y+24, "ICs ×"+n,
      {fontFamily:"'PixelFont','Courier New'",fontSize:"10px",fontStyle:"bold",color:"#ffc08a"})
      .setOrigin(0.5,0).setDepth(28);
  }

  buildRooms(){
    this.buildSky();                              // roof + night sky strip (no sun)
    this.roomGlow = {};
    // ONE shared open floor across the whole interior
    this.add.tileSprite(24, WALL_TOP, W-48, FLOOR_BOTTOM-WALL_TOP, "floor_base").setOrigin(0,0).setDepth(0);
    // walkable corridor aisles forming the "#" between the section cells
    const lane=(x,y,w,h,horiz)=>{
      this.add.rectangle(x,y,w,h,0x303a55).setOrigin(0,0).setDepth(0);
      this.add.rectangle(x,y,w,1,0x3e4a68).setOrigin(0,0).setDepth(0);
      this.add.rectangle(x,y+h-1,w,1,0x1d2438).setOrigin(0,0).setDepth(0);
      // dashed centre line
      const g=this.add.graphics().setDepth(0); g.fillStyle(0x4a587a,0.5);
      if (horiz){ for(let dx=x+6; dx<x+w-6; dx+=18) g.fillRect(dx,y+h/2-1,9,2); }
      else { for(let dy=y+6; dy<y+h-6; dy+=18) g.fillRect(x+w/2-1,dy,2,9); }
    };
    for (const vx of AISLE_VX) lane(vx-AISLE/2, WALL_TOP, AISLE, FLOOR_BOTTOM-WALL_TOP, false);
    for (const hy of AISLE_HY) lane(GX0, hy-AISLE/2, GX1-GX0, AISLE, true);
    // perimeter walls only (top + left + right + bottom edges)
    this.add.tileSprite(0, 50, W, WALL_TOP-50, "wall_base").setOrigin(0,0).setDepth(1);
    this.add.rectangle(0, 46, W, 6, 0x2a3550).setOrigin(0,0).setDepth(1);                 // roof eave
    this.add.rectangle(0, WALL_TOP-4, W, 4, 0x0a0d16).setOrigin(0,0).setDepth(1);          // baseboard
    this.add.rectangle(0, 50, 24, FLOOR_BOTTOM-50, 0x141a2c).setOrigin(0,0).setDepth(1);
    this.add.rectangle(W-24, 50, 24, FLOOR_BOTTOM-50, 0x141a2c).setOrigin(0,0).setDepth(1);
    this.add.rectangle(0, FLOOR_BOTTOM, W, H-FLOOR_BOTTOM, 0x0e1322).setOrigin(0,0).setDepth(1);
    // windows in the top wall (NO sun) + god-rays into the floor
    for (let wx=80; wx<W-110; wx+=210){
      const ww=78, wh=44, wy=60;
      this.add.rectangle(wx, wy, ww+6, wh+6, 0x10182c).setOrigin(0,0).setDepth(2);
      const glow=this.add.rectangle(wx+3, wy+3, ww, wh, 0x284a70).setOrigin(0,0).setDepth(2);
      this.add.rectangle(wx+3, wy+3, ww, wh*0.5, 0x3a6ea0).setOrigin(0,0).setDepth(2);     // sky band, no sun
      this.add.rectangle(wx+2+ww/2, wy+3, 2, wh, 0x10182c).setOrigin(0,0).setDepth(2);
      this.add.rectangle(wx+3, wy+3+wh/2, ww, 2, 0x10182c).setOrigin(0,0).setDepth(2);
      this.tweens.add({targets:glow, fillAlpha:{from:0.8,to:1}, duration:3000, yoyo:true, repeat:-1});
      const ray=this.add.triangle(wx+ww/2+3, wy+wh+4, -ww*0.5,170, ww*0.5,170, 0,0, 0xbfd2ff, 0.08).setDepth(0);
      this.tweens.add({targets:ray, alpha:{from:0.05,to:0.12}, duration:3600, yoyo:true, repeat:-1});
    }
    // a few hanging lamps over the open floor — each casts a warm pool on the floor
    [240,560,1140].forEach(lx=>{ this.add.rectangle(lx, WALL_TOP, 1, 16, 0x2a2f40).setOrigin(0,0).setDepth(2);
      this.add.ellipse(lx, WALL_TOP+92, 96, 34, 0xffe6a0, 0.08).setDepth(0);   // warm floor pool
      this.add.circle(lx, WALL_TOP+20, 4, 0xffe6a0, 0.95).setDepth(2);
      this.add.circle(lx, WALL_TOP+20, 10, 0xffe6a0, 0.10).setDepth(2); });
    this.drawFlowPath();
    // each section is a soft RUG on the shared floor (no walls)
    const cc = 0x232a3c;   // base-floor colour for chamfered corners
    for (const key in ROOMS){
      const R = ROOMS[key];
      this.add.rectangle(R.x+5, R.y+6, R.w, R.h, 0x000000, 0.22).setOrigin(0,0).setDepth(0);    // soft shadow
      this.add.tileSprite(R.x, R.y, R.w, R.h, "floor_"+key).setOrigin(0,0).setDepth(0).setAlpha(0.95);
      this.add.rectangle(R.x, R.y, R.w, R.h, shade(R.color,1.7), 0.16).setOrigin(0,0).setDepth(1); // brighten/pop
      const t = TINT[key]; if (t) this.add.rectangle(R.x, R.y, R.w, R.h, t[0], t[1]).setOrigin(0,0).setDepth(1);
      // rug trim: light outer band + dark inner line
      this.add.rectangle(R.x, R.y, R.w, R.h).setOrigin(0,0).setStrokeStyle(3, shade(R.color,1.25)).setDepth(1);
      this.add.rectangle(R.x+4, R.y+4, R.w-8, R.h-8).setOrigin(0,0).setStrokeStyle(1, shade(R.color,0.55)).setDepth(1);
      // chamfer the 4 corners so rugs read as rounded
      [[R.x,R.y],[R.x1-3,R.y],[R.x,R.y1-3],[R.x1-3,R.y1-3]].forEach(([px,py])=>{
        this.add.rectangle(px,py,3,1,cc).setOrigin(0,0).setDepth(1); this.add.rectangle(px,py,1,3,cc).setOrigin(0,0).setDepth(1); });
      // floating label plaque (rounded look: dark body + light top edge + dept dot)
      const sw = Math.min(R.w-12, R.label.length*7+22);
      this.add.rectangle(R.cx, R.y+6, sw, 16, 0x0e1422, 0.92).setOrigin(0.5,0).setDepth(3).setStrokeStyle(1, shade(R.color,1.1));
      this.add.rectangle(R.cx, R.y+6, sw, 2, shade(R.color,1.3)).setOrigin(0.5,0).setDepth(4);
      this.roomGlyph(key, R.cx-sw/2+5, R.y+8);   // dept identity glyph (white, shape — not just hue)
      this.add.text(R.cx+4, R.y+8, R.label, {fontFamily:"'PixelFont','Courier New',monospace",
        fontSize:"10px", fontStyle:"bold", color:"#eef3ff"}).setOrigin(0.5,0).setDepth(4);
      this.roomGlow[key] = this.add.rectangle(R.x, R.y, R.w, R.h, 0xffd25a, 0).setOrigin(0,0).setDepth(1);
    }
    this.decorateFloor();
    this.furnish();
    this.buildAtmosphere();                       // vignette + dust motes on top
    this.setActiveRoom("lobby");
  }

  // A ~10px WHITE department glyph on the plaque so rooms are identifiable by SHAPE
  // (colour-blind / grayscale safe), not hue. (x,y)=top-left. High contrast on dark.
  roomGlyph(key, x, y){
    const W = 0xffffff, D = 0x0e1422;     // white glyph, plaque-dark cut-outs
    const p = (dx,dy,w,h,c)=> this.add.rectangle(x+dx, y+dy, w, h, c).setOrigin(0,0).setDepth(4);
    const M = (rects)=> rects.forEach(([dx,dy,w,h,k])=> p(dx,dy,w,h, k?W:D));
    switch(key){
      case "engineering":  // gear: body + 4 teeth + hole
        M([[2,2,6,6,1],[4,0,2,2,1],[4,8,2,2,1],[0,4,2,2,1],[8,4,2,2,1],[4,4,2,2,0]]); break;
      case "qa":           // magnifier: square lens ring + diagonal handle
        M([[1,0,6,1,1],[1,5,6,1,1],[0,1,1,4,1],[6,1,1,4,1],[6,6,2,2,1],[8,8,2,2,1]]); break;
      case "gate":         // shield: flat top, tapered point
        M([[2,0,6,2,1],[1,2,8,3,1],[2,5,6,1,1],[3,6,4,1,1],[4,7,2,2,1]]); break;
      case "security":     // padlock: shackle + body + keyhole
        M([[3,0,4,1,1],[2,1,1,3,1],[7,1,1,3,1],[1,4,8,6,1],[4,6,2,3,0]]); break;
      case "product":      // clipboard: board + clip + two text lines
        M([[3,0,4,1,1],[1,1,8,9,1],[3,3,4,1,0],[3,5,4,1,0],[3,7,3,1,0]]); break;
      case "architecture": // solid triangle (set-square)
        M([[4,0,2,1,1],[3,1,4,1,1],[3,2,4,1,1],[2,3,6,1,1],[2,4,6,1,1],[1,5,8,1,1],[1,6,8,1,1],[0,7,10,1,1],[0,8,10,1,1]]); break;
      case "vault":        // key: round bow (hole) + clean straight stem + 2 teeth
        M([[1,1,4,4,1],[2,2,2,2,0],[5,2,5,1,1],[7,3,1,2,1],[9,3,1,2,1]]); break;
      case "reception":    // door: frame + dark panel + knob
        M([[2,0,6,10,1],[3,1,4,8,0],[6,4,1,2,1]]); break;
      case "lounge":       // mug: cup + handle
        M([[1,1,5,8,1],[6,2,1,1,1],[7,3,1,2,1],[6,5,1,1,1],[2,0,3,1,1]]); break;
      default: p(2,2,6,6,W);
    }
  }

  buildSky(){   // roof + a thin night-sky strip above the building — NO sun
    this.add.rectangle(0,0,W,H,0x0c1020).setOrigin(0,0).setDepth(-2);
    [[0,0x10182e],[22,0x1c2742]].forEach(([y,c])=>this.add.rectangle(0,y,W,52-y,c).setOrigin(0,0).setDepth(-1));
    const cloud=(cx,cy,cw,a)=>{ const c=this.add.rectangle(cx,cy,cw,7,0x2a3a58,a).setOrigin(0,0).setDepth(-1);
      this.tweens.add({targets:c, x:cx+60, duration:22000, yoyo:true, repeat:-1, ease:"Sine.inOut"}); };
    cloud(160,18,90,0.5); cloud(520,30,130,0.4); cloud(900,14,80,0.5);
  }

  drawFlowPath(){   // faint dotted line winding through the build order
    const g = this.add.graphics().setDepth(0); g.lineStyle(2, 0xffcf52, 0.16);
    let started = false;
    for (const k of ROOM_ORDER){ const R = ROOMS[k]; if(!R) continue;
      if (!started){ g.beginPath(); g.moveTo(R.cx, R.cy); started = true; } else g.lineTo(R.cx, R.cy);
    }
    g.strokePath();
  }

  decorateFloor(){   // ORDERLY, grid-aligned decor along the corridors
    const P=(k,x,y)=>this.makeProp(k, x, y);
    // a potted plant at each of the 4 corridor intersections of the "#"
    for (const x of AISLE_VX) for (const y of AISLE_HY) P("plant", x, y+6);
    // a water cooler tucked at each corridor mouth (symmetric)
    P("cooler", AISLE_VX[0], WALL_TOP+24); P("cooler", AISLE_VX[1], WALL_TOP+24);
    // the LOUNGE (bottom-left): couch, TV, coffee table, water cooler — where idle
    // leads sit, nap and mill about between stages (see loungeArrive / mill).
    const L = cellBox(LOUNGE_CELL[0], LOUNGE_CELL[1]);
    this.add.text(L.x+L.w/2, L.y+8, "LOUNGE", {fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"10px", color:"#8aa0c8"}).setOrigin(0.5,0).setDepth(4);
    P("couch", L.x+44, L.y+L.h-30); P("ctable", L.x+44, L.y+L.h-6);
    P("tv", L.x+L.w-44, L.y+50); P("cooler", L.x+L.w-28, L.y+L.h-14);
    P("plant", L.x+22, L.y+48);
    // the ENTRANCE (bottom-centre): a purely decorative lobby — welcome mat, plants,
    // and the company ORG chart on the wall (renderOrg pins here). No work happens here.
    const E = cellBox(ENTRANCE_CELL[0], ENTRANCE_CELL[1]);
    this.add.text(E.x+E.w/2, E.y+8, "ENTRANCE", {fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"10px", color:"#8aa0c8"}).setOrigin(0.5,0).setDepth(4);
    this.add.rectangle(E.x+E.w/2, E.y+E.h-16, 74, 22, 0x2a3550, 0.55).setDepth(0)
      .setStrokeStyle(2, 0x3e4a68);
    this.add.text(E.x+E.w/2, E.y+E.h-16, "WELCOME",
      {fontFamily:"'PixelFont','Courier New'",fontSize:"7px",color:"#8aa0c8"}).setOrigin(0.5).setDepth(1);
    P("plant", E.x+24, E.y+E.h-20); P("plant", E.x+E.w-24, E.y+E.h-20);
  }

  buildAtmosphere(){
    if (this.textures.exists("vignette"))
      this.add.image(W/2,H/2,"vignette").setDisplaySize(W+20,H+20).setDepth(60).setAlpha(0.5);
    for (let i=0;i<24;i++){
      const rx=()=>Math.random()*W, ry=()=>FLOOR_TOP+Math.random()*(H-FLOOR_TOP);
      const m=this.add.rectangle(rx(),ry(),2,2,0xfff0c0,0.16).setDepth(33);
      this.tweens.add({targets:m, y:m.y-24-Math.random()*36, x:m.x+(Math.random()*24-12), alpha:0,
        duration:5000+Math.random()*4000, repeat:-1, delay:Math.random()*5000,
        onRepeat:()=>{ m.x=rx(); m.y=ry(); m.alpha=0.16; }});
    }
  }

  // Procedural pixel furniture so each room reads like a furnished interior.
  furnish(){
    // ambient props at room EDGES (the working desks are per-lead, in the floor)
    const props = {
      product:     [["board",0.10],["plant",0.92]],
      architecture:[["board",0.10],["board",0.52],["shelf",0.90]],
      engineering: [["rack",0.07],["plant",0.94]],
      qa:          [["shelf",0.07],["plant",0.95]],
      security:    [["rack",0.08],["shelf",0.92]],
    };
    for (const key in props){
      const R = ROOMS[key]; if(!R) continue;
      for (const [kind,fx] of props[key]) this.makeProp(kind, R.x + R.w*fx, R.floorY+6);
    }
    // the lounge: a couch + coffee table + plants — where idle folks hang out
    const L = ROOMS.lounge;
    if (L){
      this.makeProp("plant", L.x + 14, L.floorY+6);
      this.makeProp("couch", L.cx, L.floorY + 34);
      this.makeProp("ctable", L.cx, L.floorY + 76);
      this.makeProp("plant", L.x1 - 14, L.floorY+6);
    }
  }

  makeProp(kind, x, floorY){
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(3);  // props (proportional to the 32px sprites)
    const out = (rx,ry,w,h,col)=>{ // outlined pixel rect (1px black border) + 1px top-light
      c.add(this.add.rectangle(rx,ry,w+2,h+2,0x0a0d16).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry,w,h,col).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry-h+1,w,1,shade(col,1.3)).setOrigin(0.5,1));   // top highlight
      c.add(this.add.rectangle(rx,ry-1,w,1,shade(col,0.6)).setOrigin(0.5,1));     // base shadow
    };
    if (kind==="plant"){
      const v = Math.abs(Math.round(x))%3;
      out(0,0,8,12,0x6a4a2a);                                   // pot (now top-lit by `out`)
      if (v===0){ out(0,-10,18,16,0x2f8f4e); out(-4,-18,10,12,0x37a85c); }     // bushy
      else if (v===1){ out(0,-12,12,22,0x2f8f4e); out(0,-24,8,10,0x37a85c); }  // tall
      else { out(0,-9,22,12,0x2f8f4e); out(2,-15,10,8,0x37a85c); }             // round
      c.add(this.add.rectangle(-4,-13,2,7,shade(0x37a85c,1.35)).setOrigin(0.5,1)); // leaf highlight
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
    } else if (kind==="cooler"){
      out(0,0,12,9,0x6a7a8a);                                   // base
      c.add(this.add.rectangle(0,-9,9,13,0x9fd8ff,0.92).setOrigin(0.5,1)); // water jug
      c.add(this.add.rectangle(0,-9,9,5,0xcfeaff,0.95).setOrigin(0.5,1));
      c.add(this.add.rectangle(0,-3,4,2,0x3a4150).setOrigin(0.5,1));       // spout
    } else if (kind==="bench"){
      out(0,0,32,5,0x6b4a2c);
      c.add(this.add.rectangle(-13,5,3,8,0x4a3320).setOrigin(0.5,1));
      c.add(this.add.rectangle(13,5,3,8,0x4a3320).setOrigin(0.5,1));
      c.add(this.add.rectangle(0,-2,30,2,0x7a5836).setOrigin(0.5,1));      // backrest slat
    } else if (kind==="tv"){
      out(0,0,10,5,0x2a3550);                                   // stand
      out(0,-7,34,22,0x10151f);                                 // body/bezel
      const scr=this.add.rectangle(0,-9,26,15,0x2bd3ff,0.7).setOrigin(0.5,1); // screen
      c.add(scr);
      this.tweens.add({targets:scr, fillAlpha:{from:0.45,to:0.8}, duration:700+Math.abs(Math.round(x))%500,
        yoyo:true, repeat:-1, ease:"Sine.inOut"});                // flicker
    }
    return c;
  }

  // A desk + monitor + keyboard each lead works at, placed IN the room floor.
  makeWorkstation(x, floorY, accent){
    if (accent == null) accent = 0x7fd1ff;   // monitor glow default (sprites carry their own colour)
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(floorY+5).setScale(1.05);  // desk (proportional to the 32px sprites)
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
      fontSize:(size||13)+"px",fontStyle:"bold",color:color||"#ffe9a8"}).setOrigin(0.5).setDepth(2000);
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
    const up=()=>{
      if (a.bob) this.tweens.add({targets:a.bob, scaleY:0.86, duration:60, yoyo:true, ease:"Quad.out"}); // anticipation squash
      this.tweens.add({targets:a, y:y0-15, duration:130, delay:55, ease:"Quad.out", yoyo:true,
        onComplete:()=>{ a.y=y0; if (++i<times) up(); }}); };
    up();
  }
  deskIdle(a){ if (!a || !a.bob) return; this.stopDeskIdle(a);
    a.breathe = this.tweens.add({targets:a.bob, y:{from:0,to:-1}, duration:1500, yoyo:true, repeat:-1, ease:"Sine.inOut"}); }
  stopDeskIdle(a){ if (a && a.breathe){ a.breathe.stop(); a.breathe=null; if (a.bob) a.bob.y=0; } }
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
      fontSize:"24px",fontStyle:"bold",color:"#ffd95e"}).setOrigin(0.5).setDepth(2000);
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
    const b=this.add.container(W/2, FLOOR_TOP+40).setDepth(2200);
    b.add(this.add.rectangle(0,0,380,48,0x0c1322,0.95).setStrokeStyle(3,color));
    b.add(this.add.text(0,0,txt,{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"22px",fontStyle:"bold",color:"#ffffff"}).setOrigin(0.5));
    b.setScale(0.2).setAlpha(0);
    this.tweens.add({targets:b, scale:1, alpha:1, duration:300, ease:"Back.out"});
    this.time.delayedCall(2400,()=>this.tweens.add({targets:b, alpha:0, y:b.y-20, duration:400,
      onComplete:()=>b.destroy()}));
  }

  // a security guard posted at the checkpoint desk (dark uniform, cap, badge)
  buildGuardian(){
    const R = ROOMS.gate, x = Math.round(R.x + R.w*0.30), y = R.y1-30;
    const c = this.add.container(x,y).setDepth(6);
    c.add(this.add.ellipse(0,9,18,5,0x000000,0.28));            // shadow
    c.add(this.add.rectangle(0,0,14,16,0x2b3350));              // torso (navy uniform)
    c.add(this.add.rectangle(0,2,14,2,0x1a2030));               // belt line
    c.add(this.add.rectangle(-4,-4,3,3,0xffd95e));              // brass badge
    c.add(this.add.rectangle(0,-12,12,11,0xe8b487));            // head
    c.add(this.add.rectangle(0,-18,13,5,0x1a2030));             // cap brim
    c.add(this.add.rectangle(0,-21,11,4,0x2b3350));             // cap crown
    c.add(this.add.rectangle(-3,-12,2,2,0x141822));             // eyes
    c.add(this.add.rectangle(3,-12,2,2,0x141822));
    this.guardSpark = this.add.rectangle(7,-6,3,3,0x5ddc7a,0.9);// radio LED
    c.add(this.guardSpark);
    this.tweens.add({targets:this.guardSpark, alpha:0.2, duration:900, yoyo:true, repeat:-1});
  }

  // The Gate is the RELEASE CHECKPOINT: a CCTV bank + checkpoint desk, plus a sealed
  // vault door (vertical) in the gap to the VAULT below. The horizontal corridor
  // between the two cells is walled off — the only link is that short door,
  // which unlocks only when every release gate passes.
  buildGate(){
    const G = ROOMS.gate, V = ROOMS.vault, cx = Math.round(G.cx);
    // ---- CCTV monitor bank on the office wall (camera feeds) ----
    const bx = G.x+46, by = G.floorY+18, mon=[0x2bd3ff,0x7dffb0,0xffd95e,0x5db8ff];
    const bank = this.add.container(bx,by).setDepth(3);
    bank.add(this.add.rectangle(0,0,76,52,0x10151f).setStrokeStyle(2,0x2a3550));
    for (let i=0;i<4;i++){ const sx=-18+(i%2)*36, sy=-12+Math.floor(i/2)*24;
      bank.add(this.add.rectangle(sx,sy,30,18,mon[i],0.85));
      bank.add(this.add.rectangle(sx,sy,30,18).setStrokeStyle(1,0x05080f)); }
    const led=this.add.rectangle(bx+30,by-22,4,4,0xff5d6c,0.9).setDepth(3);  // REC light
    this.tweens.add({targets:led, alpha:0.2, duration:700, yoyo:true, repeat:-1});
    // ---- checkpoint desk / counter ----
    const dx = cx+30, dy = G.y1-34;
    const desk = this.add.container(dx,dy).setDepth(3);
    desk.add(this.add.rectangle(0,0,70,16,0x394566).setOrigin(0.5,1));
    desk.add(this.add.rectangle(0,-15,70,4,0x4a587a).setOrigin(0.5,1));      // counter top
    desk.add(this.add.rectangle(-20,-26,18,12,0x10151f).setOrigin(0.5,1));   // terminal
    desk.add(this.add.rectangle(-20,-28,14,8,0x2bd3ff,0.85).setOrigin(0.5,1));
    // ---- seal the corridor gap between the GATE and the VAULT below ----
    const gapTop = G.y1, gapBot = V.y, dw = 60;          // short vertical door
    const wallC = 0x1a2030, capC = 0x2a3550;
    const seal=(sx,w)=>{ if (w<=0) return;
      this.add.rectangle(sx, gapTop, w, gapBot-gapTop, wallC).setOrigin(0,0).setDepth(7);
      this.add.rectangle(sx, gapTop, w, 3, capC).setOrigin(0,0).setDepth(7);
      this.add.rectangle(sx, gapBot-3, w, 3, 0x0a0d16).setOrigin(0,0).setDepth(7); };
    seal(G.x0, (cx-dw/2)-G.x0);                          // left wall segment
    seal(cx+dw/2, G.x1-(cx+dw/2));                       // right wall segment
    // ---- the sealed VAULT DOOR (vertical, fills the gap) ----
    const atop = gapTop, abot = gapBot, h = abot-atop, ax = cx-dw/2;
    this.gateGeom = {ax, aw:dw, atop, abot, h, cx};
    const c = this.add.container(0,0).setDepth(8);
    this.gateFrame = this.add.rectangle(ax-4, atop, dw+8, h, 0x566089).setOrigin(0,0); // steel frame
    this.gateDoor  = this.add.rectangle(ax, atop+3, dw, h-6, 0x0c1322).setOrigin(0,0);  // door panel
    c.add([this.gateFrame, this.gateDoor]);
    this.gateBars = this.add.container(0,0);
    for (let by2=atop+9; by2<abot-6; by2+=10)            // horizontal bolts
      this.gateBars.add(this.add.rectangle(ax+5, by2, dw-10, 3, 0x6a7aa0).setOrigin(0,0));
    this.gateLatch = this.add.circle(cx,(atop+abot)/2,8,0xaab4d6).setStrokeStyle(2,0x2a3550); // vault wheel
    this.gateBars.add(this.gateLatch);
    c.add(this.gateBars);
  }

  buildChest(){
    const R = ROOMS.vault, x = Math.round(R.cx), y = Math.round(R.cy+12);
    this.chestPos = {x,y};
    // enshrined: concentric floor inlay rings + a pedestal so the Vault reads as
    // "the prize lives here" even before it's earned.
    this.add.ellipse(x, y+10, 132, 44).setStrokeStyle(2, shade(R.color,1.5)).setDepth(0).setAlpha(0.5);
    this.add.ellipse(x, y+10, 92, 30).setStrokeStyle(1, shade(R.color,1.7)).setDepth(0).setAlpha(0.4);
    this.add.ellipse(x, y+14, 56, 16, shade(R.color,1.3), 0.5).setDepth(0);   // pedestal top
    this.chestGlow = this.add.rectangle(x,y-20,120,120,0xffd25a,0.10).setDepth(4);
    this.tweens.add({targets:this.chestGlow, fillAlpha:{from:0.06,to:0.14}, duration:2200, yoyo:true, repeat:-1});
    const c = this.add.container(x,y).setDepth(5).setScale(1.3);
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
      const names = ROSTER[room], R = ROOMS[room]; if (!R) continue;
      const cols = Math.min(names.length, 3);
      names.forEach((name,i)=>{
        const col = i%cols, row = Math.floor(i/cols);
        // tight pods: fixed pitch from a left margin, centred as a block in the room
        const blockW = (cols-1)*64, hx = R.cx - blockW/2 + col*64;
        const hy = R.floorY + 34 + row*48;          // stand in the room's floor
        const seed = hashStr((this.proj.id||'f')+name), kit = kitFor(name, seed);
        this.makeWorkstation(hx, hy+16, kit.accent);              // desk just in front of them
        const a = this.makePerson(hx, hy, kit);
        a.setScale(1.4).setDepth(hy);                             // y-sort (desk occludes legs)
        a.home = {x:hx,y:hy}; a.room = room; a.loungeSlot = li++;
        // Start visible in the lounge (idle); stages pull each team to its desks.
        const ls = this.loungeSpot(a.loungeSlot);
        a.setPosition(ls.x, ls.y).setAlpha(1).setDepth(ls.y);
        a.seen = true; a.inLounge = true; a.bubble = null;
        // Clickable -> interview this lead (their journal + a question), advisory.
        a.setInteractive(new Phaser.Geom.Rectangle(-8,-15,16,22), Phaser.Geom.Rectangle.Contains);
        a.on("pointerdown", ()=> openInterview(name));
        a.on("pointerover", ()=>{ this.input.manager.canvas.style.cursor = "pointer"; });
        a.on("pointerout", ()=>{ this.input.manager.canvas.style.cursor = "default"; });
        a.setScale(0); this.tweens.add({targets:a, scale:1.4, duration:400, delay:60*li, ease:"Back.out"});
        this.agents.set(name, a);
      });
    }
  }

  buildCTO(){
    const L = cellBox(ENTRANCE_CELL[0], ENTRANCE_CELL[1]);
    this.lobby = {cx: Math.round(L.x+L.w/2), standY: Math.round(L.y+L.h-24)};
    const c = this.makePerson(this.lobby.cx, this.lobby.standY, kitFor("CTO", hashStr((this.proj.id||'f')+"CTO")));
    c.setScale(1.4).setDepth(900); c.fixedDepth = 900;   // CTO walks the floor -> always on top
    c.add(this.add.text(0,-22,"CTO",{fontFamily:"'PixelFont','Courier New'",fontSize:"8px",fontStyle:"bold",color:"#ffcf52"})
      .setOrigin(0.5));
    this.cto = c;
    this.tweens.add({targets:c.bob, y:-1, duration:380, yoyo:true, repeat:-1, ease:"Sine.inOut"});
  }

  // Our own generated GBA-style sprite: big head, tiny torso, pointed ears, vertical
  // eyes, colourful clothes. `opt` = a role/pool kit (body/trim/style/cloth/hw/acc).
  // A character from the route1rodent sprite sheet. Preserves the old API
  // (c.bob / c.faceFront / c.faceBack / c.spark / c.eyes) so walk / desk-idle /
  // hop / work animations keep working — the body is now a single sprite frame.
  makePerson(x,y,opt){
    opt = opt || {};
    const ci = opt.ci != null ? opt.ci : 0;
    const ch = CHARACTERS[((ci % CHARACTERS.length) + CHARACTERS.length) % CHARACTERS.length];
    const c = this.add.container(x,y).setDepth(10);
    c.add(this.add.ellipse(0,3,11,4,0x000000,0.30));        // shadow under the feet
    const bob = this.add.container(0,0);
    // 0.36 x the caller's 1.4 container = ~0.5 net of the 64px frame => crisp 32px
    // (integer-friendly half-scale; non-integer scales soften pixel art).
    const sprite = this.add.sprite(0,4,"chars",ch.front[0]).setOrigin(0.5,1).setScale(0.36);
    bob.add(sprite);
    const spark = this.add.rectangle(6,-24,2,2,0xfff04a).setOrigin(0,0).setVisible(false);
    bob.add(spark);
    c.add(bob);
    c.bob=bob; c.sprite=sprite; c.spark=spark; c.eyes=[]; c.char=ch; c._wf=0;
    c.faceFront = ()=>{ sprite.setFrame(ch.front[c._wf % ch.front.length]); };
    c.faceBack  = ()=>{ sprite.setFrame(ch.back); };
    return c;
  }

  // Stepped walk: a snappy 2-frame bob + waddle on the whole body (legs are baked).
  startWalk(a){
    if (!a || !a.bob) return;
    if (a.faceFront) a.faceFront();              // show the face while moving
    this.stopDeskIdle(a);                        // stop breathing while walking
    if (a.walkEv) return;
    let f = 0;
    a.walkEv = this.time.addEvent({delay:150, loop:true, callback:()=>{
      f ^= 1;
      a.bob.y = f ? -1 : 0;
      a.bob.x = f ? 0.6 : -0.6;
      if (a.sprite && a.char){ a._wf = (a._wf+1) % a.char.front.length;
        a.sprite.setFrame(a.char.front[a._wf]); }   // step the walk cycle
    }});
  }
  stopWalk(a){
    if (!a || !a.bob) return;
    if (a.walkEv){ a.walkEv.remove(); a.walkEv = null; }
    a.bob.y = 0; a.bob.x = 0;
    if (a.sprite && a.char){ a._wf = 0; a.sprite.setFrame(a.char.front[0]); }
  }

  setActiveRoom(key){
    this.activeRoom = key;
    for (const k in this.roomGlow)
      this.tweens.add({targets:this.roomGlow[k], fillAlpha:(k===key?0.10:0), duration:420});
    const R = ROOMS[key];
    if (R) this.walkCTO(R.cx, R.standY);
    else if (this.lobby) this.walkCTO(this.lobby.cx, this.lobby.standY);
    this.staffRoom(key);                 // everyone stays at their own desk; active room glows
  }

  // ---- movement: desks <-> lounge -----------------------------------------
  // What each idle lead does in the lounge — a stable mix by slot: sit, nap, mill.
  loungeMode(i){ return ["sit","wander","sleep","wander","sit","wander"][i % 6]; }
  // Idle agents relax in the LOUNGE (bottom-left). Their spot depends on what they're
  // doing: sitters by the couch (lower-left), sleepers in the nap corner (lower-right),
  // wanderers loosely spread up top (mill() nudges them around).
  loungeSpot(i){
    const B = cellBox(LOUNGE_CELL[0], LOUNGE_CELL[1]);
    const mode = this.loungeMode(i);
    if (mode === "sit"){
      const k = Math.floor(i/6);
      return { x: B.x + 32 + (i%3)*26, y: B.y + B.h - 36 - k*16, mode };
    }
    if (mode === "sleep"){
      return { x: B.x + B.w - 40 - (Math.floor(i/6)%2)*22, y: B.y + B.h - 28, mode };
    }
    const per = 4, col = i%per, row = Math.floor(i/per) % 2;
    return { x: B.x + 30 + col*((B.w-60)/(per-1)), y: B.y + 60 + row*30, mode };
  }
  // shortest path along the "#" corridors: exit to the nearest aisle, run the aisles,
  // then step into the destination. (Within-room moves stay direct — handled in walkTo.)
  corridorRoute(sx,sy,tx,ty){
    const near=(arr,v)=>arr.reduce((b,n)=>Math.abs(n-v)<Math.abs(b-v)?n:b, arr[0]);
    const exitY=near(AISLE_HY,sy), entryY=near(AISLE_HY,ty), vx=near(AISLE_VX,(sx+tx)/2);
    const pts=[[sx,exitY]];                              // step out to the nearest horizontal aisle
    if (exitY!==entryY){ pts.push([vx,exitY],[vx,entryY],[tx,entryY]); }  // switch rows via a vertical aisle
    else { pts.push([tx,exitY]); }                       // same aisle -> straight along it
    pts.push([tx,ty]);                                   // step into the destination
    return pts;
  }
  walkTo(a, tx, ty, then){
    if (!a) return;
    if (a.moveTween) a.moveTween.stop();
    if (Math.hypot(tx-a.x, ty-a.y) < 3){ a.x=tx; a.y=ty; if (then) then(); return; }
    // within the same room (or a tiny hop): walk freely/direct. Between rooms: corridors.
    const ra = roomAt(a.x, a.y), rb = roomAt(tx, ty);
    const pts = ((ra && ra===rb) || Math.hypot(tx-a.x, ty-a.y) < 50)
      ? [[tx, ty]] : this.corridorRoute(a.x, a.y, tx, ty);
    this.startWalk(a);
    let i = 0;
    const step = ()=>{
      if (i >= pts.length){ this.stopWalk(a); a.scaleX = 1; if (a.fixedDepth==null) a.setDepth(ty); if (then) then(); return; }
      const [px,py] = pts[i++], d = Math.hypot(px-a.x, py-a.y);
      if (Math.abs(px-a.x) > 3) a.scaleX = px < a.x ? -1 : 1;   // face travel direction
      if (a.fixedDepth == null) a.setDepth(Math.max(py, a.y) + 1);
      a.moveTween = this.tweens.add({targets:a, x:px, y:py, duration:Math.max(200, d*3.2), ease:"Sine.inOut", onComplete:step});
    };
    step();
  }
  // Off duty -> walk to the lounge (so a room only ever shows people actually
  // working that stage; idle leads physically wait it out in the break area).
  gotoLounge(a){ if (!a || !a.seen) return; a.inLounge = true;
    if (a.faceFront) a.faceFront(); this.stopDeskIdle(a);
    const s = this.loungeSpot(a.loungeSlot||0);
    this.walkTo(a, s.x + (Math.random()*10-5), s.y, ()=> this.loungeArrive(a, s.mode)); }
  // Once parked in the lounge, do something: nap (with Zzz), sit and breathe, or mill.
  loungeArrive(a, mode){
    if (!a || !a.inLounge) return;
    this.clearLounge(a);
    a.loungeBusy = mode;
    if (mode === "sleep"){ if (a.faceFront) a.faceFront(); this.sleepZzz(a);
      a.napTween = this.tweens.add({targets:a.bob, y:{from:0,to:-1.5}, duration:2200,
        yoyo:true, repeat:-1, ease:"Sine.inOut"}); }
    else if (mode === "sit"){ if (a.faceFront) a.faceFront(); this.deskIdle(a); }
  }
  sleepZzz(a){
    if (a.zzzEv) return;
    a.zzzEv = this.time.addEvent({delay:1500, loop:true, callback:()=>{
      if (!a.inLounge || a.loungeBusy !== "sleep") return;
      const t = this.add.text(a.x+8, a.y-22, "z",
        {fontFamily:"'PixelFont','Courier New'",fontSize:"10px",color:"#9fb4d8"}).setDepth(2000);
      this.tweens.add({targets:t, y:t.y-18, x:t.x+8, alpha:{from:0.9,to:0}, duration:1600,
        ease:"Sine.out", onComplete:()=>t.destroy()});
    }});
  }
  clearLounge(a){
    if (!a) return;
    a.loungeBusy = null;
    if (a.napTween){ a.napTween.stop(); a.napTween = null; if (a.bob) a.bob.y = 0; }
    if (a.zzzEv){ a.zzzEv.remove(); a.zzzEv = null; }
    this.stopDeskIdle(a);
  }
  gotoDesk(a){ if (!a) return; a.inLounge = false; this.clearLounge(a);
    if (!a.seen){ a.seen = true; a.setAlpha(1); }
    if (a.home) this.walkTo(a, a.home.x, a.home.y, ()=>{ if (a.faceBack) a.faceBack(); this.deskIdle(a); }); }
  staffRoom(key){
    this.activeRoomKey = key;
    const hasTeam = !!ROSTER[key];
    for (const [name,a] of this.agents){
      if (hasTeam && AGENT_ROOM[name] === key) this.gotoDesk(a);
      else if (a.seen) this.gotoLounge(a);
    }
    // IC pool + per-area Tech-Leads belong to engineering: at their desks while
    // engineering is active, otherwise off to the lounge with everyone else.
    for (const s of [...(this.poolGroup||[]), ...(this.techLeadGroup||[])]){
      if (!s || !s.bob) continue;
      if (key === 'engineering' && s.home) this.walkTo(s, s.home.x, s.home.y, ()=>{ if (s.faceBack) s.faceBack(); });
      else this.gotoLounge(s); }
  }
  toLounge(){   // everyone hangs out (checkpoints / done)
    for (const [,a] of this.agents) if (a.seen) this.gotoLounge(a);
    for (const s of (this.poolGroup||[])) if (s && s.bob) this.gotoLounge(s);
  }
  mill(){       // gentle wander for the lounge "wander" folks (sitters/sleepers stay put)
    const B = cellBox(LOUNGE_CELL[0], LOUNGE_CELL[1]);
    const move = (a)=>{ if (a && a.seen && a.inLounge && a.loungeBusy === "wander"
        && !a.moveTween?.isPlaying() && Math.random()<0.5){
      this.walkTo(a, B.x+26+Math.random()*(B.w-52), B.y+58+Math.random()*(B.h-86)); } };
    for (const [,a] of this.agents) move(a);
    for (const s of [...(this.poolGroup||[]), ...(this.techLeadGroup||[])]) move(s);
  }

  walkCTO(tx, ty){   // routes along the corridors like everyone else
    if (!this.cto) return;
    this.walkTo(this.cto, tx, (ty==null ? this.cto.y : ty));
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
    const c = this.add.container(a.x, a.y-26).setDepth(2000);
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
    const c = this.add.container(R.cx, R.floorY+24).setDepth(2000);
    c.add(this.add.rectangle(0,0,w,18,0x161d31).setStrokeStyle(2,0xffcf52));
    c.add(this.add.text(0,0,"+ "+label,{fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#ffe9a8"})
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
      label.setText(pass?"P":"F").setColor(pass?"#5ddc7a":"#ff5d6c");
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
    // 1) the vault door unlocks first…
    this.gateDoor.setFillStyle(0x1f8a3f);
    this.tweens.add({targets:this.gateBars, alpha:0, y:-30, duration:500, ease:"Cubic.in"});
    if (this.chestOpened) return;
    this.chestOpened = true;
    this.setActiveRoom("vault");
    // 2) …then, a beat later, the chest pops open (sequential, not simultaneous).
    this.time.delayedCall(320, ()=>{
      this.chestClosed.setVisible(false);
      this.chestOpen.setVisible(true).setScale(1,0);
      this.tweens.add({targets:this.chestOpen, scaleY:1, duration:300, ease:"Back.out"});
      this.tweens.add({targets:this.chestGlow, fillAlpha:0.34, duration:400, yoyo:true,
        repeat:-1, ease:"Sine.inOut"});
      this.ring(this.chestPos.x, this.chestPos.y-12, 0xffd95e);
      this.add.particles(this.chestPos.x, this.chestPos.y-12, "spark", {
        speed:{min:60,max:160}, angle:{min:230,max:310}, gravityY:240,
        lifespan:1100, quantity:3, frequency:40, scale:{start:1.4,end:0},
        duration:1400, depth:24,
      });
    });
  }

  renderOrg(ev){
    // The REAL org chart (Wave E telemetry): present areas -> Tech-Lead domains.
    if (this.orgPanel){ this.orgPanel.destroy(); }
    const areaColor = {
      frontend:0x7fd1ff, backend:0xff9b5e, data:0x7dffb0,
      infra:0xb59bff, security:0xff6d6d, default:0xcccccc,
    };
    const areas = ev.areas || [];
    const rowH = 16, padding = 8, headerH = 20;
    // pin the org board to the ENTRANCE wall (the company org chart in the lobby)
    const B = cellBox(ENTRANCE_CELL[0], ENTRANCE_CELL[1]);
    const w = Math.min(232, B.w-24), h = headerH + areas.length*rowH + padding;
    const c = this.add.container(Math.round(B.x+(B.w-w)/2), B.y+22).setDepth(28);
    c.add(this.add.rectangle(0,0,w,h,0x0c1322,0.92).setOrigin(0,0).setStrokeStyle(2,0xffcf52));
    c.add(this.add.rectangle(0,0,w,2,0xffcf52,0.5).setOrigin(0,0));   // board top rail
    c.add(this.add.rectangle(w/2,-3,5,5,0xffcf52).setOrigin(0.5,0));  // pin
    c.add(this.add.text(8,4,"ORG · PM",{fontFamily:"'PixelFont','Courier New'",fontSize:"12px",
      fontStyle:"bold",color:"#ffcf52"}).setOrigin(0,0));
    areas.forEach((a,i)=>{
      const y = headerH + i*rowH;
      const col = areaColor[a.area] || areaColor.default;
      c.add(this.add.rectangle(8,y+2,9,9,col).setOrigin(0,0));
      const leads = (a.leads||[]).join(", ");
      c.add(this.add.text(22,y,`${a.area}: ${leads}`,
        {fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#e8eefb"}).setOrigin(0,0));
    });
    c.setAlpha(0);
    this.tweens.add({targets:c, alpha:1, duration:300});
    this.orgPanel = c;
  }

  // Fan-out (Wave D/E): the senior engineer re-instantiates as one "Tech-Lead ·
  // {area}" per org area. Draw one sprite per area in the engineering room, tinted
  // by area, so the floor headcount reflects the REAL per-area leads (not just one).
  renderTechLeads(areas){
    if (this.techLeadGroup) this.techLeadGroup.forEach(s=>s.destroy());
    if (this.tlLabel){ this.tlLabel.destroy(); this.tlLabel = null; }
    this.techLeadGroup = [];
    const list = (areas||[]).filter(a=>a && a.area);
    if (!list.length) return;
    const areaColor = {frontend:0x7fd1ff, backend:0xff9b5e, data:0x7dffb0,
      infra:0xb59bff, security:0xff6d6d};
    const R = ROOMS.engineering, n = Math.min(list.length, 5);
    for (let i=0;i<n;i++){
      const a = list[i], accent = areaColor[a.area] || 0xffd25a;
      const x = Math.round(R.x+34 + (R.w-68)*(n>1 ? i/(n-1) : 0.5)), y = R.y1 - 30;
      const k = kitFor("Tech-Lead "+a.area, hashStr((this.proj.id||'f')+"TL"+a.area));
      k.accent = accent;
      const ws = this.makeWorkstation(x, y+16, accent);
      const s = this.makePerson(x, y, k);
      s.setScale(1.3).setDepth(y);
      s.home = {x,y}; s.loungeSlot = 24 + i; s.seen = true; s.inLounge = false;
      s.add(this.add.text(0,-19, a.area.slice(0,2).toUpperCase(),
        {fontFamily:"'PixelFont','Courier New'",fontSize:"8px",fontStyle:"bold",
         color:"#"+accent.toString(16).padStart(6,"0")}).setOrigin(0.5));
      this.startWalk(s);
      this.techLeadGroup.push(s, ws);
    }
    this.tlLabel = this.add.text(R.cx, R.y1-48, "Tech-Leads ×"+n,
      {fontFamily:"'PixelFont','Courier New'",fontSize:"9px",fontStyle:"bold",color:"#9bd0ff"})
      .setOrigin(0.5,0).setDepth(28);
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
  autoRound: true,                       // integer canvas sizing -> crisper pixels
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
  scene: [BootScene],
});

// =========================================================================== //
//  Floor manager + multi-project networking                                   //
// =========================================================================== //
// Workshop token (for a deployment that sets FOUNDRY_WORKSHOP_TOKEN): pass it as
// ?token=… in the URL and it rides every state-changing POST. No token → omitted.
const WORKSHOP_TOKEN = new URLSearchParams(location.search).get("token");
function postHeaders(){
  const h = {"Content-Type":"application/json"};
  if (WORKSHOP_TOKEN) h["X-Workshop-Token"] = WORKSHOP_TOKEN;
  return h;
}

const FLOORS = new Map();   // floorNum -> floor state (incl. its Phaser scene)
let ACTIVE = null;          // the floor currently shown

function floorKey(f){ return "floor-"+f; }

async function newProject(req, mode, supervised, backend, packs, extra){
  await bootReady;  // shared textures must exist before a FloorScene builds
  let resp;
  try {
    resp = await fetch("/api/projects", {
      method:"POST", headers:postHeaders(),
      body: JSON.stringify({request:req, mode, supervised, backend, packs, ...(extra||{})}),
    }).then(r=>r.json());
  } catch(err){ appendLogDom("bad","ERR "+err); return; }

  const floor = resp.floor;
  const release = resp.release || 1;
  const slots = (extra && extra.max_slots) || 1;
  const openStream = (F) => {
    const es = new EventSource("/api/stream/"+resp.id);
    es.onmessage = e => { try { routeEvent(floor, JSON.parse(e.data)); } catch(_){} };
    es.addEventListener("end", ()=>es.close());
    F.es = es;
  };
  const existing = FLOORS.get(floor);
  if (existing){
    // NEW RELEASE on an existing project: reset per-release state + replay the scene.
    if (existing.es){ try{ existing.es.close(); }catch(_){} }
    Object.assign(existing, {
      id:resp.id, release, releases:(existing.releases||1)+1, request:req, mode, backend,
      log:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
      slots, journal:[], seenUris:new Set(),
    });
    const sc = GAME.scene.getScene(floorKey(floor));
    if (sc) sc.scene.restart({floor, id:resp.id, request:req, mode, slots});
    else GAME.scene.add(floorKey(floor), FloorScene, true, {floor, id:resp.id, request:req, mode, slots});
    switchToFloor(floor);
    openStream(existing);
    return;
  }
  // NEW PROJECT: a fresh floor.
  const F = {
    id: resp.id, floor, release, releases:1, request:req, mode, backend, scene:null,
    log:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
    slots, journal:[], seenUris:new Set(),
  };
  FLOORS.set(floor, F);
  GAME.scene.add(floorKey(floor), FloorScene, true, {floor, id:resp.id, request:req, mode, slots});
  switchToFloor(floor);
  openStream(F);
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
      if (ev.concern) logTo(F, "", `${ev.agent}: "${ev.concern}"`); break;
    case "artifact":
      if (S){ S.spawnPickup(ev.room, ev.label); const pa=S.agents.get(ev.agent);
        if (pa){ S.hop(pa,1); const R=ROOMS[ev.room]; if(R) S.burst(R.cx, R.floorY+20, [0xffd95e,0xffffff], 8); } }
      logTo(F, "good", "+ "+ev.label+"  ·  "+ev.agent); break;
    case "gate":
      F.gates[ev.name] = {status:ev.status, attested:ev.attested, advisory:ev.advisory};
      if (S){ S.setGate(ev.name, F.gates[ev.name]);
        const g=S.gateGeom; if(g){ const col=ev.status==="pass"?0x5ddc7a:0xff5d6c;
          S.ring(g.cx,(g.atop+g.abot)/2,col); S.burst(g.cx,(g.atop+g.abot)/2,[col,0xffffff],8); } }
      if (isActive) renderGatesMini(F.gates);
      if (!ev.advisory) logTo(F, ev.status==="pass"?"good":"bad",
        `GATE ${ev.name.toUpperCase()}: ${ev.status.toUpperCase()}${ev.attested?" (signed)":""}`);
      break;
    case "org":
      F.org = ev;
      if (S){ S.renderOrg(ev); S.renderTechLeads(ev.areas);
        if (S.activeRoomKey) S.staffRoom(S.activeRoomKey); }   // place new TLs by active stage
      logTo(F, "gold", "ORG · "+(ev.areas||[]).map(a=>`${a.area}(${(a.leads||[]).length})`).join("  "));
      break;
    case "files":
      F.hasFiles = ev.count; break;
    case "checkpoint":
      F.cp = {title:ev.title, summary:ev.summary, boundary:ev.boundary}; break;
    case "await_decision":
      F.awaiting = true; F.cpIndex = ev.checkpoint;
      if (Array.isArray(ev.journal)) F.journal = ev.journal;
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
      logTo(F, "gold", "RELEASED -> "+(ev.uri||"artifact")); break;
    case "halt":
      if (S){ S.flashGate(); S.stampGate(false); }
      logTo(F, "bad", "HELD: "+ev.reason); break;
    case "done":
      F.status = "done"; F.passed = !!ev.passed; renderElevator();
      if (S){ S.toLounge(); if (!ev.passed){ S.gloom(); S.banner("HELD AT THE GATE", 0xff5d6c); } }
      logTo(F, ev.passed?"good":"bad",
        ev.passed?"Build complete — shipped.":"Build halted — nothing shipped."); break;
  }
}

// =========================================================================== //
//  DOM: quest log, gates-mini, HUD, elevator, checkpoint modal                //
// =========================================================================== //
function logClass(t){   // classify BEFORE stripping, so server emoji still tint the row
  if(/✅|💰|green|shipped|released|complete/i.test(t)) return "good";
  if(/💥|🚫|⛔|error|halt|held|reject|fail/i.test(t)) return "bad";
  if(/📋|🏭|🧑|mode|stage|crew/i.test(t)) return "hot";
  return "";
}
// strip any emoji so the QUEST LOG is pure bitmap-font text (matches the pixel UI)
const EMOJI_RE = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2300}-\u{23FF}\u{2190}-\u{21FF}️]/gu;
function deEmoji(t){ return (t==null?"":(""+t)).replace(EMOJI_RE,"").replace(/^\s+/,""); }
function appendLogDom(cls,text){
  const ul=document.getElementById("log");
  const li=document.createElement("li"); li.className=cls; li.textContent=deEmoji(text);
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
    const li=document.createElement("li"); li.className=cls; li.textContent=deEmoji(text); ul.appendChild(li);
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
  document.getElementById("floor-tag").textContent =
    "FLOOR "+F.floor + (F.release>1 ? " · r"+F.release : "");
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
    if (F.releases>1){ const r=document.createElement("span"); r.className="rel-badge"; r.textContent="r"+F.release; btn.appendChild(r); }
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
  // ITERATE: start a new floor from this finished build (enabled once it's done).
  const itBtn = document.getElementById("files-iterate");
  itBtn.style.display = (F.status === "done") ? "" : "none";
  itBtn.onclick = () => { hideFiles(); showNewModal(F.id); };
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
  fetch("/api/decision/"+F.id, {method:"POST", headers:postHeaders(),
    body: JSON.stringify({decision, checkpoint: F.cpIndex})});
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
  fetch("/api/interview/"+IV_TARGET.id,{method:"POST",headers:postHeaders(),
    body:JSON.stringify({role:IV_TARGET.role, question:q})})
   .then(r=>r.json()).then(d=>{ ans.textContent = d.answer || "(no answer)"; })
   .catch(()=>{ ans.textContent = "(interview request failed)"; });
}

// ---- UI wiring -------------------------------------------------------------
const newModal=document.getElementById("new-modal");
document.getElementById("new-btn").onclick=()=>showNewModal();
document.getElementById("cancel-btn").onclick=()=>newModal.classList.add("hidden");

// ---- "Start from": fresh build, or iterate on a finished build -------------
const startFromSel=document.getElementById("startfrom-select");
const REQ_FRESH='What should the factory build? <span class="hint">— a sentence, or a full brief</span>';
const REQ_ITER='What to change or add? <span class="hint">— the agents build on the selected floor</span>';
const SEED_REQ='build a tiny URL-shortener service';
async function loadStartFrom(){
  let builds=[];
  try{ const r=await fetch("/api/builds"); builds=((await r.json())||{}).builds||[]; }catch(_){}
  startFromSel.innerHTML='<option value="">✦ Fresh build</option>';
  for(const b of builds){
    if(b.has_repo===false) continue;
    const o=document.createElement("option");
    o.value=b.id;
    o.textContent=`⎘ Floor ${b.floor} ${b.passed?"✓":"✗"} · ${(b.request||"").slice(0,40)}`;
    startFromSel.appendChild(o);
  }
}
function syncStartFrom(){
  const iterating=!!startFromSel.value;
  document.getElementById("req-label").innerHTML=iterating?REQ_ITER:REQ_FRESH;
  const req=document.getElementById("req-input");
  if(iterating && (req.value.trim()===""||req.value.trim()===SEED_REQ)){
    req.value=""; req.placeholder="e.g. add link expiry + custom slugs, and rate-limit by IP";
  }
}
startFromSel.addEventListener("change", syncStartFrom);
function showNewModal(parentId){
  newModal.classList.remove("hidden");
  loadStartFrom().then(()=>{
    startFromSel.value=(parentId && [...startFromSel.options].some(o=>o.value===parentId))?parentId:"";
    syncStartFrom();
  });
}
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
  fetch("/api/ideate", {method:"POST", headers:postHeaders(),
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
  const fix=parseInt(document.getElementById("fix-input").value,10);
  if(Number.isFinite(fix) && fix>=0) extra.max_fix_attempts=fix;
  const cap=parseFloat(document.getElementById("cap-input").value);
  if(Number.isFinite(cap) && cap>0) extra.cost_cap_usd=cap;
  if(startFromSel.value) extra.parent_id=startFromSel.value;   // iterate on a finished build
  const intake=intakeMode();
  if(startFromSel.value && !req.trim()){ alert("Describe what to change or add."); return; }
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
  fetch("/api/suggest-slots",{method:"POST",headers:postHeaders(),
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
  syncCostCap();
}).catch(()=>{});

// The USD cost cap only binds *metered* API backends (real per-token charges).
// mock is offline, ollama is local, and claude-cli bills a flat subscription —
// so for those a per-build USD cap is meaningless: disable + annotate the field.
const METERED_BACKENDS = ["anthropic","codex"];
function syncCostCap(){
  const backend = document.getElementById("backend-select").value;
  const cap = document.getElementById("cap-input");
  if(!cap) return;
  const metered = METERED_BACKENDS.includes(backend);
  cap.disabled = !metered;
  cap.placeholder = metered ? "no cap" : `${backend} isn't metered — no USD cost`;
  if(!metered) cap.value = "";
}
document.getElementById("backend-select").addEventListener("change", syncCostCap);
syncCostCap();

// Rehydrate on (re)load: floors run server-side independently of this tab, so a
// refresh / reconnect from another device rebuilds every floor + reconnects the
// live stream of any still-running build. No floors yet => open the new dialog.
function adoptFloor(row){
  if (FLOORS.has(row.floor)) return;
  const F = {
    id:row.id, floor:row.floor, release:row.release||1, releases:row.releases||1,
    request:row.request, mode:row.mode, backend:row.backend,
    scene:null, log:[], gates:row.gates||{}, org:null, cp:null, awaiting:!!row.awaiting,
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
