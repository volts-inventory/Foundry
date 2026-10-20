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

// Canvas aspect is matched to the game panel so the world FILLS the frame (no
// letterbox). Pokémon-interior vertical split: WALL fills the top, FLOOR the rest.
// FLOOR_TOP is the wall/floor seam; characters stand on the floor near FLOOR_BOT.
//
// TWO floorplans, one per orientation. A room's desk grid sits at fixed offsets
// (buildAgents puts the first row at R.y+94 with a 50px pitch), so cells cannot shrink —
// the only thing free to change is the SHAPE of the grid. Four cells across makes a
// 1280px world, which a 390px phone can only draw at ~30%; three across is 970px and
// draws at ~40%, and the block is nearly square, so on a phone it fills the screen
// instead of sitting in it as a letterbox strip.
const PORTRAIT_MQ = "(max-width: 820px) and (orientation: portrait)";
let W, H, FLOOR_TOP, FLOOR_BOT;

// ---- floorplan: a GRID of section zones with walkway aisles (the "#").
// One open level; the elevator switches PROJECTS. Aisles between cells are corridors
// people walk along; sections are coloured floor rugs in the cells.
let WALL_TOP, FLOOR_BOTTOM;
// Cell size is FIXED in both plans: the desk grid inside a room is placed at absolute
// offsets (PX/SY/RP in buildAgents + layoutEngineering), so a shorter or narrower cell
// would push desks through the walls. Only the grid's shape changes.
const CELL_W = 274, CELL_H = 196, AISLE = 34, GX0 = 40, GY0 = 132;
let GX1;
function cellBox(c,r){ const x = GX0 + c*(CELL_W+AISLE), y = GY0 + r*(CELL_H+AISLE);
  // include derived coords so non-ROOM callers (the lounge cell) get cx/cy/x1/y1 too.
  return {x, y, w:CELL_W, h:CELL_H, x0:x, y0:y, x1:x+CELL_W, y1:y+CELL_H, cx:x+CELL_W/2, cy:y+CELL_H/2}; }
// Both plans read pipeline-order left-to-right, top-to-bottom, keep the GATE directly
// above the VAULT (the vault's only opening is a top gap punched at the gate's centre,
// and buildGate seals the one-aisle corridor between them), and give the vault two
// cells so the prize room spans a full row-end.
const PLANS = {
  // 4 across × 3 down — the desktop floor, unchanged.
  landscape: {
    W:1280, H:840, cols:4, rows:3,
    grid:{ plan:[0,0], frontend:[1,0], backend:[2,0], data:[3,0],
           review:[0,1], security:[1,1], gate:[2,1],
           vault:[2,2],                   // gate(2,1) sits directly above vault(2..3,2)
           office:[0,2] },                // the CTO's own office (row 2, far left)
    vaultSpan:[[2,2],[3,2]],
    lounge:[1,2],
  },
  // 3 across × 4 down — the portrait phone floor. Same cells, taller stack, so a
  // 390px screen draws it at ~40% instead of ~30% and the block is nearly square.
  portrait: {
    W:970, H:1070, cols:3, rows:4,
    grid:{ plan:[0,0], office:[2,0],
           frontend:[0,1], backend:[1,1], data:[2,1],
           review:[0,2], security:[1,2], gate:[2,2],
           vault:[2,3] },                 // gate(2,2) sits directly above vault(1..2,3)
    vaultSpan:[[1,3],[2,3]],
    lounge:[0,3],
  },
};
let PLAN = null;
let GRID, LOUNGE_CELL, ENTRANCE_CELL;
// Pipeline ORDER (the build sequence) — drives the order numerals + the flow path.
const PIPELINE_ORDER = ["plan","frontend","backend","data","review","security","gate","vault"];
// The CTO OFFICE is a real room (see each plan's grid); the LOUNGE cell is the
// RECEPTION. No pipeline work happens in either — and nobody is ever SENT to the
// lounge: every agent lives at their own desk (see "presence" below), so it's the
// entrance + a furnished break room, not a holding pen for idle staff.
const ENG_ROOMS = ["frontend","backend","data"];               // engineering discipline rooms
const ENG_AREA_ROOM = {frontend:"frontend", backend:"backend", data:"data",
                       infra:"backend", security:"backend"};   // org AREA → its discipline room
const RBASE = {
  plan:{label:"PLAN",color:0x2e3b5a},
  frontend:{label:"FRONTEND",color:0x243f5a}, backend:{label:"BACKEND",color:0x5a4524},
  data:{label:"DATA",color:0x245a42}, review:{label:"REVIEW",color:0x2e5a4a},
  security:{label:"SECURITY",color:0x5a2e36},
  gate:{label:"GATE",color:0x2c4a55}, vault:{label:"VAULT",color:0x5a4a18},
  office:{label:"CTO OFFICE",color:0x4a4030},   // the CTO's room (opens on the RIGHT wall)
};
// Build-order numeral per room (engineering's 3 rooms share stage 3 — they run in
// parallel; support rooms have none).
const ORDER_NO = {plan:1, frontend:2, backend:2, data:2,
                  review:3, security:4, gate:5, vault:6};
const ROOMS = {};
const ROOM_ORDER = ["plan","frontend","backend","data","review","security","gate","vault"];
const TINT = {security:[0xff6d6d,0.05], gate:[0x6aa0ff,0.05], vault:[0xffd25a,0.06]};
// the "#": centre-lines of the walkway aisles between columns / rows (generated).
// corridorRoute() walks these, so the counts must match the plan's cols/rows.
const AISLE_VX = [], AISLE_HY = [];

// Lay the world out for the current orientation. ROOMS and the aisle arrays are
// rebuilt IN PLACE so every module-level reference to them stays live. Returns false
// when the plan is already the right one, so a resize can call it cheaply.
function applyFloorplan(force){
  const want = window.matchMedia(PORTRAIT_MQ).matches ? "portrait" : "landscape";
  if (!force && PLAN === PLANS[want]) return false;
  PLAN = PLANS[want];
  W = PLAN.W; H = PLAN.H;
  FLOOR_TOP = 100; FLOOR_BOT = H - 40;   // general playfield bounds (rooms are placed boxes)
  WALL_TOP = 118; FLOOR_BOTTOM = H - 26;
  GX1 = W - 40;
  GRID = PLAN.grid;
  LOUNGE_CELL = PLAN.lounge; ENTRANCE_CELL = PLAN.lounge;
  for (const k of Object.keys(ROOMS)) delete ROOMS[k];
  for (const k in RBASE) ROOMS[k] = Object.assign({}, RBASE[k], cellBox(GRID[k][0], GRID[k][1]));
  // Virtual "engineering" region = bbox over the discipline rooms. NOT drawn (virtual),
  // but present so server events keyed to room "engineering" (stage glow / pickups /
  // bursts) still resolve to a sensible position.
  { const rs = ENG_ROOMS.map(k=>ROOMS[k]);
    const x0=Math.min(...rs.map(r=>r.x)), y0=Math.min(...rs.map(r=>r.y));
    const x1=Math.max(...rs.map(r=>r.x+r.w)), y1=Math.max(...rs.map(r=>r.y+r.h));
    ROOMS.engineering = {label:"ENGINEERING", color:0x5a3a2e, virtual:true, x:x0, y:y0, w:x1-x0, h:y1-y0}; }
  // The VAULT is the shipped-artifact destination — give it prominence by spanning two
  // cells (fills the row end with REAL content; it's the prize room, not filler).
  { const [ca, cb] = PLAN.vaultSpan, a = cellBox(ca[0],ca[1]), b = cellBox(cb[0],cb[1]);
    Object.assign(ROOMS.vault, {x:a.x, y:a.y, w:(b.x + b.w) - a.x, h:a.h}); }
  AISLE_VX.length = 0; AISLE_HY.length = 0;
  for (let c = 0; c < PLAN.cols-1; c++) AISLE_VX.push(GX0 + (c+1)*CELL_W + c*AISLE + AISLE/2);
  for (let r = 0; r < PLAN.rows-1; r++) AISLE_HY.push(GY0 + (r+1)*CELL_H + r*AISLE + AISLE/2);
  for (const k in ROOMS){ const R = ROOMS[k];
    R.key=k; R.x0=R.x; R.x1=R.x+R.w; R.y1=R.y+R.h; R.cx=R.x+R.w/2; R.cy=R.y+R.h/2;
    R.wallH=0; R.floorY=R.y+18; R.standY=R.y1-14;
  }
  return true;
}
applyFloorplan(true);
function roomAt(x,y){ for (const k in ROOMS){ const R=ROOMS[k];
  if (x>=R.x && x<=R.x1 && y>=R.y && y<=R.y1) return k; } return null; }

const COLORS = {
  "Planner":0xffd95e,"Senior Engineer":0xff9b5e,
  "Junior Engineer":0xffc08a,"Reviewer":0x5db8ff,"AppSec Engineer":0xff6d6d,
  "Audit & Compliance":0xffb38a,"Supply-chain Reviewer":0x9bb0ff,
};
const ROSTER = {
  plan:["Planner"],
  // engineering is NOT a static room: its engineers are placed per-discipline
  // (frontend/backend/data) from the live org telemetry (layoutEngineering).
  review:["Reviewer"],
  security:["AppSec Engineer","Audit & Compliance","Supply-chain Reviewer"],
};
const GATE_ORDER = ["spec","build","test","smoke","review","security","audit","supply_chain","e2e"];
const AGENT_ROOM = {}; for (const rm in ROSTER) for (const nm of ROSTER[rm]) AGENT_ROOM[nm] = rm;
// Per-area accent (shared by the org board, the Tech-Leads, and the area-expert
// IC pool) so frontend/backend/data/… read the same colour everywhere.
const AREA_COLOR = {frontend:0x7fd1ff, backend:0xff9b5e, data:0x7dffb0,
                    infra:0xb59bff, security:0xff6d6d};
const AREA_TAG = {frontend:"FE", backend:"BE", data:"DA", infra:"IN", security:"SE"};
// short role tags for the named static-room agents (the senior roles), shown on a badge.
const ROLE_TAG = {
  "Planner":"PLANNER", "Reviewer":"REVIEWER", "AppSec Engineer":"APPSEC",
  "Audit & Compliance":"AUDIT", "Supply-chain Reviewer":"SUPPLY",
};

// ---- presence: everybody lives at their own desk ---------------------------
// Nobody is ever herded off to a break room. An agent is ALWAYS at their own
// workstation; the only thing that changes is their POSTURE:
//   working  -> turned to the monitor (back of head), typing, code glyphs
//   off duty -> turned around, settled into an idle MOOD + the odd chat bubble
// Which group is actually live is read off the CTO — he goes and stands with
// them (see sendCTO), so "where the boss is" == "where the work is".
//
// A mood is stable for the whole idle spell (and per person), so the floor has
// a recognisable cast: the same one naps, the same one is always on a coffee
// run. It re-rolls only when they come off the clock again.
const IDLE_MOODS = ["nap","happy","chat","bored","coffee","tunes","stretch"];
const IDLE_CHATTER = {
  nap:    ["zzz...", "five more minutes", "...", "just resting my eyes"],
  happy:  ["that one shipped clean", "green across the board", "good build :)", "nailed it"],
  chat:   ["seen the latest diff?", "who owns that module?", "standup after this?",
           "nice catch earlier", "coffee machine's fixed!"],
  bored:  ["waiting on the gate...", "nothing queued for me", "*drums on desk*", "sigh",
           "anyone need a hand?"],
  coffee: ["coffee run?", "third refill today", "need caffeine", "kettle's on"],
  tunes:  ["~ headphones on ~", "new playlist, no notes", "~ ~ ~", "this track though"],
  stretch:["*stretches*", "my back...", "twelve steps today", "standing desk next sprint"],
};
// What the CTO says as he arrives to oversee whichever group just went live.
const CTO_LINES = ["how's it looking?", "keep going, this is good", "what's blocking you?",
                   "I'll be right here", "show me when it's ready"];
// The CTO walks a BEAT rather than being leashed to the newest event: he stands with one
// live team for a spell, then moves on to the next room that has work. Rooms he can stand
// in, in floor order (the gate/vault/his office aren't teams).
const PATROL_ROOMS = ["plan","frontend","backend","data","review","security"];
const CTO_DWELL_MS = [7000, 13000];   // how long he oversees one room before moving on
const pick = (arr)=> arr[Math.floor(Math.random()*arr.length)];

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
  "Planner":7,"Senior Engineer":5,"Junior Engineer":9,
  "Reviewer":4,
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
// linear blend between two packed colours (t=0 → a, t=1 → b)
function mix(a,b,t){
  const ar=(a>>16)&255, ag=(a>>8)&255, ab=a&255;
  const br=(b>>16)&255, bg=(b>>8)&255, bb=b&255;
  const r=Math.round(ar+(br-ar)*t), g=Math.round(ag+(bg-ag)*t), bl=Math.round(ab+(bb-ab)*t);
  return (r<<16)|(g<<8)|bl;
}

// ── GBA Pokémon-Center bright palette ──────────────────────────────────────
// One source of truth for the whole-floor reskin: a teal tiled floor, cream
// walls with a pink stripe, navy ink. Room identity is a soft cream-tint of the
// area accent so the backend-driven rooms still read distinct.
const PC = {
  // WARM scheme: a tan tiled FLOOR with terracotta/BRICK walls and a cream stripe.
  // (GBA art-direction pass: quieter floor checker, cooler shadows, one scene ink.)
  floor:0xe8d2a8, floorAlt:0xe2c99e, floorSeam:0xcdb99a,   // tan floor — checker ~6% off, seam whispers
  room:0xf3ead0, roomAlt:0xe9dcbe, roomSeam:0xcdbd98,       // (room tiles, unused now)
  wall:0xc2614a, wallHi:0xcf7158, wallShade:0x8a4248,       // brick face — softer cap, rose-cooled shade
  stripe:0xf2e8cc, stripeHi:0xfffaf0,                       // cream wall trim (the anchor light)
  ink:0x33405e, line:0x2c4a8c,                              // navy TEXT ink
  sceneInk:0x241d2e, shadow:0x1a1726,                       // one outline ink + one cool cast-shadow
  bg:0x140d0a,                                              // dark warm letterbox around the building
};

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
  // Per-room floor: cream tile checker, lightly tinted by the room's accent so each
  // discipline still reads distinct (a pale teal/amber/green wash on the cream).
  for (const key in ROOMS){
    const tkey = "floor_"+key;
    if (scene.textures.exists(tkey)) continue;
    const R = ROOMS[key]; const g = scene.make.graphics({add:false});
    const base = mix(PC.room, R.color, 0.07), alt = mix(PC.roomAlt, R.color, 0.10);
    g.fillStyle(base,1); g.fillRect(0,0,40,40);
    g.fillStyle(alt,1);  g.fillRect(20,0,20,20); g.fillRect(0,20,20,20);            // checker
    g.fillStyle(PC.roomSeam,1); g.fillRect(0,0,40,1); g.fillRect(0,0,1,40);          // tile seams
    g.fillStyle(PC.roomSeam,1); g.fillRect(20,0,1,40); g.fillRect(0,20,40,1);
    g.fillStyle(shade(base,1.05),1); g.fillRect(7,6,2,2); g.fillRect(27,25,2,2);     // light fleck
    g.fillStyle(PC.roomSeam,1);      g.fillRect(31,9,2,2); g.fillRect(11,31,2,2);     // dark fleck
    g.generateTexture(tkey,40,40); g.destroy();
  }
  // Per-room interior wall tile: cream face with a shaded lower course + bright top.
  for (const key in ROOMS){
    const wkey = "wall_"+key;
    if (scene.textures.exists(wkey)) continue;
    const g = scene.make.graphics({add:false});
    g.fillStyle(PC.wallShade,1); g.fillRect(0,0,40,32);
    g.fillStyle(PC.wall,1);
    g.fillRect(1,1,18,14); g.fillRect(21,1,18,14);
    g.fillRect(-9,17,18,14); g.fillRect(11,17,18,14); g.fillRect(31,17,18,14);
    g.fillStyle(PC.wallHi,1);
    g.fillRect(1,1,18,2); g.fillRect(21,1,18,2); g.fillRect(11,17,18,2); g.fillRect(31,17,18,2);
    g.generateTexture(wkey,40,32); g.destroy();
  }
  // Shared teal floor — a QUILTED grid of small beveled tiles (the Pokémon-Center
  // floor): each 16px tile is lit on its top-left and dark-seamed on its bottom-right,
  // with a gentle 2-tone checker. Used EVERYWHERE (rooms + corridors are one floor).
  if (!scene.textures.exists("floor_base")){
    const g=scene.make.graphics({add:false}), S=16;
    // QUILTED tile: each square is a slightly RAISED ceramic tile — a lit top-left bevel,
    // a soft inner shade curving to the bottom-right, and a darker groove seam between
    // tiles. Subtle enough to read as one tan floor, detailed enough to look drawn.
    const tile=(tx,ty,base)=>{
      g.fillStyle(base,1);                  g.fillRect(tx,ty,S,S);
      g.fillStyle(shade(base,1.06),1);      g.fillRect(tx,ty,S-1,1); g.fillRect(tx,ty,1,S-1);     // top-left bevel
      g.fillStyle(shade(base,1.10),1);      g.fillRect(tx,ty,2,2);                                 // bright corner
      g.fillStyle(shade(base,0.95),1);      g.fillRect(tx+2,ty+S-2,S-3,1); g.fillRect(tx+S-2,ty+2,1,S-3); // inner soft shade
      g.fillStyle(PC.floorSeam,1);          g.fillRect(tx,ty+S-1,S,1); g.fillRect(tx+S-1,ty,1,S);  // groove seam
    };
    for(let r=0;r<4;r++) for(let c=0;c<4;c++) tile(c*S, r*S, (r+c)%2 ? PC.floorAlt : PC.floor);
    g.generateTexture("floor_base", S*4, S*4); g.destroy();
  }
  // Perimeter wall — CLEAN brick: even courses, quiet cool mortar, a 1px lit top edge
  // and a 1px shaded bottom edge per brick (top-left light). Reads as drawn masonry.
  if (!scene.textures.exists("wall_base")){
    const g=scene.make.graphics({add:false});
    g.fillStyle(PC.wallShade,1); g.fillRect(0,0,40,32);                       // mortar bed
    const brick=(bx,by,bw)=>{
      g.fillStyle(PC.wall,1);            g.fillRect(bx,by,bw,13);
      g.fillStyle(PC.wallHi,1);          g.fillRect(bx,by,bw,1);              // lit top edge
      g.fillStyle(shade(PC.wall,0.88),1);g.fillRect(bx,by+12,bw,1);          // shaded bottom edge
    };
    brick(1,1,17); brick(20,1,17);                                           // course 1
    brick(-8,17,17); brick(11,17,17); brick(30,17,17);                       // course 2 (half-offset)
    g.generateTexture("wall_base",40,32); g.destroy();
  }
}

// Bake the pixel TTF into a BITMAP FONT once (a glyph atlas + RetroFont data). Floor
// labels then render as BitmapText: sampled 1:1 from the texture with NEAREST, so they
// stay crisp at any scale instead of re-rasterising the TTF (which blurs when scaled).
const PXFONT_CHARS =
  " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`" +
  "abcdefghijklmnopqrstuvwxyz{|}~·×";
function buildPixelFont(scene){
  if (scene.cache.bitmapFont.exists("px")) return;
  // RetroFont (fixed cell) renders reliably here. Keep the cell TIGHT (≈ the glyph
  // width) and centre each glyph so the mostly-uppercase labels read evenly, not spaced.
  const CW=11, CH=18, F=14, FONT = F+'px "PixelFont","Courier New",monospace';
  const cv = document.createElement("canvas");
  cv.width = PXFONT_CHARS.length*CW; cv.height = CH;
  const ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false; ctx.font = FONT;
  ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = "#ffffff";
  for (let i=0;i<PXFONT_CHARS.length;i++) ctx.fillText(PXFONT_CHARS[i], i*CW + CW/2, Math.round(CH/2)+1);
  scene.textures.addCanvas("pxfont", cv);
  scene.cache.bitmapFont.add("px", Phaser.GameObjects.RetroFont.Parse(scene, {
    image:"pxfont", width:CW, height:CH, chars:PXFONT_CHARS, charsPerRow:PXFONT_CHARS.length,
    offset:{x:0,y:0}, spacing:{x:0,y:0}, lineSpacing:0,
  }));
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
    ready.then(()=>{ try { buildPixelFont(this); } catch(e){ console.warn("pxfont", e); } bootResolve(); });
  }
}

// =========================================================================== //
//  Floor scene — one workshop per project                                     //
// =========================================================================== //
class FloorScene extends Phaser.Scene {
  init(data){ this.proj = data || {}; }

  create(){
    this.cameras.main.setBackgroundColor(PC.bg);
    this.agents = new Map();
    this.gates = {};
    this.gatePips = {};
    this.activeRoom = "lobby";
    this.liveRooms = new Set();      // rooms with a node in-flight (concurrent stages)
    // agent name -> how many of its nodes are in flight. A COUNT, not a set: a platform
    // build runs several services at once, so the same role ("Frontend Lead") can be
    // mid-call twice — one service finishing must not unseat the other.
    this.workingAgents = new Map();
    this.roomInflight = {};          // room -> count of in-flight nodes (ref-count)
    this.ctoRoom = null;             // the room the CTO is overseeing = the active group
    // per-floor offset into IDLE_MOODS so the napper isn't the same person on every floor
    this._moodSeed = hashStr(this.proj.id || "f") % IDLE_MOODS.length;
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

  // The IC pool size (Wave C) — the number of engineers building at once. Stored,
  // then the engineering room is (re)laid out: per-area lanes once the org is known,
  // a flat pool until then. n<=1 => serial (no pool).
  setPool(n){
    this.poolN = n;
    this.layoutEngineering();
  }

  buildRooms(){
    this.buildSky();                              // roof + night sky strip (no sun)
    this.roomGlow = {};
    // ONE shared open floor across the whole interior
    this.add.tileSprite(24, WALL_TOP, W-48, FLOOR_BOTTOM-WALL_TOP, "floor_base").setOrigin(0,0).setDepth(0);
    // Corridors are NOT a separate surface — the whole interior is the one teal floor
    // (floor_base, laid above); the thick cream walls + their open gaps form the rooms
    // and passages, exactly like the reference. (AISLE_VX/HY still position props.)
    // perimeter walls only (top + left + right + bottom edges)
    this.add.tileSprite(0, 50, W, WALL_TOP-50, "wall_base").setOrigin(0,0).setDepth(1);
    this.add.rectangle(0, 46, W, 6, PC.wallShade).setOrigin(0,0).setDepth(1);              // roof eave
    // the signature pink stripe runs along the top wall (with a lighter highlight line)
    this.add.rectangle(0, WALL_TOP-16, W, 7, PC.stripe).setOrigin(0,0).setDepth(1);
    this.add.rectangle(0, WALL_TOP-16, W, 2, PC.stripeHi).setOrigin(0,0).setDepth(1);
    this.add.rectangle(0, WALL_TOP-4, W, 4, PC.wallShade).setOrigin(0,0).setDepth(1);       // baseboard
    this.add.tileSprite(0, 50, 24, FLOOR_BOTTOM-50, "wall_base").setOrigin(0,0).setDepth(1); // left wall
    this.add.tileSprite(W-24, 50, 24, FLOOR_BOTTOM-50, "wall_base").setOrigin(0,0).setDepth(1); // right wall
    this.add.rectangle(0, FLOOR_BOTTOM, W, H-FLOOR_BOTTOM, PC.bg).setOrigin(0,0).setDepth(1);
    // framed wall pictures along the top wall — each a DIFFERENT scene (the sun is on
    // only one, not repeated in every window).
    // Same count and pitch as ever; only a narrower world re-centres the run, so it
    // doesn't leave a dead gap against the right wall.
    let pi = 0;
    const picPitch = 232, picW = 80;
    const picN = Math.max(3, Math.floor((W - 130 - 86) / picPitch) + 1);
    const picX0 = PLAN.cols >= 4 ? 86
      : Math.round((W - (picN-1)*picPitch - picW) / 2);
    for (let wx=picX0; pi<picN; wx+=picPitch){
      // the SUN belongs to exactly one window (pi 0); the rest cycle the other
      // three scenes (clouds / mountain / birds) so no scene — sun included — repeats back-to-back.
      const ww=80, wh=42, wy=60, v = pi===0 ? 0 : 1 + ((pi-1) % 3);
      // WOODEN frame: outer wood + a lit top edge + a dark inner liner (a real picture frame)
      this.add.rectangle(wx-4, wy-4, ww+8, wh+8, 0x6b4527).setOrigin(0,0).setDepth(2);
      this.add.rectangle(wx-4, wy-4, ww+8, 2, 0x8a5e36).setOrigin(0,0).setDepth(2);            // frame top light
      this.add.rectangle(wx-4, wy+wh+2, ww+8, 2, 0x4a2f18).setOrigin(0,0).setDepth(2);         // frame bottom shade
      this.add.rectangle(wx-2, wy-2, ww+4, wh+4, PC.sceneInk).setOrigin(0,0).setDepth(2);      // inner liner
      // LANDSCAPE: banded sky + two layered hills
      this.add.rectangle(wx, wy, ww, wh, 0xbfe6f2).setOrigin(0,0).setDepth(2);                 // sky
      this.add.rectangle(wx, wy, ww, Math.round(wh*0.42), 0xa6d6ee).setOrigin(0,0).setDepth(2);// upper sky
      this.add.rectangle(wx, wy+Math.round(wh*0.55), ww, Math.round(wh*0.2), 0x9ec98a).setOrigin(0,0).setDepth(2); // far hill
      this.add.rectangle(wx, wy+Math.round(wh*0.66), ww, Math.round(wh*0.34), 0x6fae74).setOrigin(0,0).setDepth(2);// near hill
      if (v===0){ this.add.circle(wx+ww-18, wy+13, 9, 0xfff0a8, 0.3).setDepth(2);
        this.add.circle(wx+ww-18, wy+13, 6, 0xfff4be).setDepth(2); }                            // sun + glow
      else if (v===1){ this.add.ellipse(wx+22, wy+13, 22, 8, 0xffffff,0.9).setDepth(2);
        this.add.ellipse(wx+54, wy+19, 26, 8, 0xffffff,0.8).setDepth(2); }                       // clouds
      else if (v===2){ this.add.triangle(0,0, wx+22,wy+28, wx+46,wy+28, wx+34,wy+11, 0x93a4ba).setDepth(2);
        this.add.triangle(0,0, wx+30,wy+13, wx+38,wy+13, wx+34,wy+11, 0xffffff).setDepth(2); }   // snowy mountain
      else { this.add.rectangle(wx+18,wy+13,6,1.5,0x33405e).setOrigin(0,0).setDepth(2);
        this.add.rectangle(wx+28,wy+16,6,1.5,0x33405e).setOrigin(0,0).setDepth(2); }             // distant birds
      this.add.rectangle(wx+7, wy, 3, wh, 0xffffff, 0.10).setOrigin(0,0).setDepth(2);           // glass glint
      pi++;
    }
    // soft ceiling-light pools on the floor (kept subtle, bright theme). Fractions of
    // W, not the three coordinates picked for 1280 (1140 falls outside a 970 world);
    // these reproduce 240/560/1140 exactly at the landscape width.
    [0.1875, 0.4375, 0.890625].map(f => Math.round(f*W)).forEach(lx=>{
      this.add.ellipse(lx, WALL_TOP+92, 110, 38, 0xffffff, 0.06).setDepth(0);
      this.add.circle(lx, WALL_TOP+18, 5, 0xfff6d8, 0.95).setDepth(2).setStrokeStyle(2, PC.wallShade);
    });
    this.drawFlowPath();
    // The WHOLE floor is one teal tiled surface (the shared floor_base laid down above
    // already covers it). Rooms are defined only by the thick cream walls + a label —
    // no cream rugs, no borders — exactly like the reference interior.
    for (const key in ROOMS){
      const R = ROOMS[key];
      if (R.virtual) continue;
      const accent = shade(R.color, 1.7);
      const t = TINT[key]; if (t) this.add.rectangle(R.x, R.y, R.w, R.h, t[0], t[1]).setOrigin(0,0).setDepth(1);
      // label placard — a navy sign hanging just BELOW the banded top wall (bigger sign)
      const sw = Math.min(R.w-10, R.label.length*9+28);
      this.add.rectangle(R.cx, R.y+22, sw, 22, PC.ink, 0.96).setOrigin(0.5,0).setDepth(3).setStrokeStyle(2, accent);
      this.add.rectangle(R.cx, R.y+22, sw, 2, mix(accent,0xffffff,0.4)).setOrigin(0.5,0).setDepth(4);
      this.roomGlyph(key, R.cx-sw/2+7, R.y+27);
      this.bm(R.cx+6, R.y+28, R.label, 13, 0xfbf3df, 0.5, 0).setDepth(4);
      const no = ORDER_NO[key];
      if (no != null){
        const bx = R.x+15, by = R.y+34;
        this.add.circle(bx, by, 10, PC.stripe, 0.98).setStrokeStyle(2, PC.wallHi).setDepth(4);
        this.bm(bx, by, String(no), 13, 0xfff4e0, 0.5, 0.5).setDepth(5);
      }
      this.roomGlow[key] = this.add.rectangle(R.x, R.y, R.w, R.h, 0xffe27a, 0).setOrigin(0,0).setDepth(1);
    }
    this.buildWalls();                            // thick cream walls with open passages
    this.decorateFloor();
    this.furnish();
    this.buildAtmosphere();                       // vignette + dust motes on top
    this.setActiveRoom("lobby");
  }

  // Interior WALLS around each room (with doorways) so the floor reads as a real
  // building's plan, not rugs on open space. Walls sit below the agents (depth 4)
  // so people always render in front; doorways open onto the corridors.
  buildWalls(){
    const T = 12, DOOR = 48, BAND = 20;   // THICK brick walls, wide open passage, tall top band
    const FACE = PC.wall, CAP = PC.wallHi, BASE = PC.wallShade, EDGE = 0x4a2018;
    const WHITE = 0x7a3526;   // dark-brick line framing the cream stripe
    const R0 = (x,y,w,h,c,a)=> this.add.rectangle(x,y,w,h,c,a==null?1:a).setOrigin(0,0).setDepth(4);
    // Plain THICK teal wall: dark outline, teal face, lit cap, shadowed base.
    const seg = (x, y, w, h) => {
      if (w <= 0 || h <= 0) return;
      R0(x-1, y-1, w+2, h+2, EDGE); R0(x, y, w, h, FACE);
      R0(x, y, w, Math.min(2,h), CAP);
      if (h > 5) R0(x, y+h-2, w, 2, BASE);
      if (w > 5) R0(x+w-2, y, 2, h, BASE);
    };
    // Top wall: a TALL banded teal wall with a white-bordered PINK STRIPE + baseboard.
    const band = (x, w, y) => {
      if (w <= 0) return;
      R0(x-1, y-1, w+2, BAND+2, EDGE);
      R0(x, y,    w, 8, FACE);  R0(x, y, w, 1, CAP);                // teal upper + lit cap
      R0(x, y+8,  w, 1, WHITE);                                     // white pinline
      R0(x, y+9,  w, 5, PC.stripe); R0(x, y+9, w, 1, PC.stripeHi);  // pink stripe + highlight
      R0(x, y+14, w, 1, WHITE);                                     // white pinline
      R0(x, y+15, w, 4, FACE);                                      // teal lower
      R0(x, y+19, w, 1, BASE);                                      // baseboard shadow
    };
    for (const key in ROOMS){
      const R = ROOMS[key]; if (R.virtual) continue;
      const cx = Math.round(R.cx);
      // Openings (no doors — plain gaps): normal rooms open at the BOTTOM centre. The
      // GATE also has a TOP entrance (from security above) so work flows security →
      // gate → vault. The VAULT is sealed except a single TOP opening under the gate.
      const topGap = key==="gate" ? cx : (key==="vault" ? Math.round(ROOMS.gate.cx) : null);
      // The CTO OFFICE opens on its RIGHT wall (onto the corridor), not the bottom —
      // everyone else opens at the bottom centre. Vault is sealed bottom (top-only).
      // In a plan where the office is the last column there is no corridor to its
      // right, only the perimeter wall, so it falls back to a bottom door.
      const rightGap = (key==="office" && R.x1 < GX1) ? Math.round(R.cy) : null;
      const botGap = (key==="vault" || rightGap != null) ? null : cx;
      seg(R.x, R.y, T, R.h);                                   // left  (solid)
      if (rightGap != null){ seg(R.x1-T, R.y, T, (rightGap-DOOR/2)-R.y); seg(R.x1-T, rightGap+DOOR/2, T, R.y1-(rightGap+DOOR/2)); }
      else seg(R.x1 - T, R.y, T, R.h);                         // right (solid or split)
      if (topGap != null){ band(R.x, (topGap-DOOR/2)-R.x, R.y); band(topGap+DOOR/2, R.x1-(topGap+DOOR/2), R.y); }
      else band(R.x, R.w, R.y);                                // banded top (solid or split)
      if (botGap != null){ seg(R.x, R.y1-T, (botGap-DOOR/2)-R.x, T); seg(botGap+DOOR/2, R.y1-T, R.x1-(botGap+DOOR/2), T); }
      else seg(R.x, R.y1 - T, R.w, T);                         // bottom (solid or split)
    }
  }

  // A ~10px WHITE department glyph on the plaque so rooms are identifiable by SHAPE
  // (colour-blind / grayscale safe), not hue. (x,y)=top-left. High contrast on dark.
  roomGlyph(key, x, y){
    const W = 0xffffff, D = PC.ink;     // white glyph, navy plaque cut-outs
    const p = (dx,dy,w,h,c)=> this.add.rectangle(x+dx, y+dy, w, h, c).setOrigin(0,0).setDepth(4);
    const M = (rects)=> rects.forEach(([dx,dy,w,h,k])=> p(dx,dy,w,h, k?W:D));
    switch(key){
      case "engineering":  // gear: body + 4 teeth + hole
        M([[2,2,6,6,1],[4,0,2,2,1],[4,8,2,2,1],[0,4,2,2,1],[8,4,2,2,1],[4,4,2,2,0]]); break;
      case "review":       // magnifier: square lens ring + diagonal handle
        M([[1,0,6,1,1],[1,5,6,1,1],[0,1,1,4,1],[6,1,1,4,1],[6,6,2,2,1],[8,8,2,2,1]]); break;
      case "gate":         // shield: flat top, tapered point
        M([[2,0,6,2,1],[1,2,8,3,1],[2,5,6,1,1],[3,6,4,1,1],[4,7,2,2,1]]); break;
      case "security":     // padlock: shackle + body + keyhole
        M([[3,0,4,1,1],[2,1,1,3,1],[7,1,1,3,1],[1,4,8,6,1],[4,6,2,3,0]]); break;
      case "plan":         // clipboard: board + clip + two text lines
        M([[3,0,4,1,1],[1,1,8,9,1],[3,3,4,1,0],[3,5,4,1,0],[3,7,3,1,0]]); break;
      case "vault":        // key: round bow (hole) + clean straight stem + 2 teeth
        M([[1,1,4,4,1],[2,2,2,2,0],[5,2,5,1,1],[7,3,1,2,1],[9,3,1,2,1]]); break;
      case "reception":    // door: frame + dark panel + knob
        M([[2,0,6,10,1],[3,1,4,8,0],[6,4,1,2,1]]); break;
      case "lounge":       // mug: cup + handle
        M([[1,1,5,8,1],[6,2,1,1,1],[7,3,1,2,1],[6,5,1,1,1],[2,0,3,1,1]]); break;
      default: p(2,2,6,6,W);
    }
  }

  buildSky(){   // dark letterbox + a slim cream roof-cap above the building
    this.add.rectangle(0,0,W,H,PC.bg).setOrigin(0,0).setDepth(-2);
    this.add.rectangle(0,44,W,8,PC.wallShade).setOrigin(0,0).setDepth(-1);     // roof cap
    this.add.rectangle(0,44,W,2,PC.wallHi).setOrigin(0,0).setDepth(-1);
    const cloud=(cx,cy,cw,a)=>{ const c=this.add.rectangle(cx,cy,cw,6,0x1a2e30,a).setOrigin(0,0).setDepth(-1);
      this.tweens.add({targets:c, x:cx+60, duration:22000, yoyo:true, repeat:-1, ease:"Sine.inOut"}); };
    // fractions of W: each cloud drifts +60px, so fixed x's bunch against the right
    // wall on a narrow world. These are 160/520/900 exactly at the landscape width.
    cloud(Math.round(W*0.125),18,90,0.5); cloud(Math.round(W*0.40625),30,130,0.4);
    cloud(Math.round(W*0.703125),14,80,0.5);
  }

  drawFlowPath(){   // faint guide line through the build STAGES (engineering as one waypoint)
    const stages = ["plan","engineering","review","security","gate","vault"];
    const pts = stages.map(k=>ROOMS[k]).filter(Boolean).map(R=>({x:R.cx, y:R.cy}));
    const g = this.add.graphics().setDepth(0); g.lineStyle(2, 0xffcf52, 0.18);
    g.beginPath(); g.moveTo(pts[0].x, pts[0].y);
    for (let i=1;i<pts.length;i++) g.lineTo(pts[i].x, pts[i].y);
    g.strokePath();
    // a small arrowhead dot at each stage so the DIRECTION of the flow reads
    for (const pt of pts) this.add.circle(pt.x, pt.y, 2.5, 0xffcf52, 0.3).setDepth(0);
  }

  decorateFloor(){   // DELIBERATE decor — everything tucked against a wall, symmetric.
    const P=(k,x,y)=>this.makeProp(k, x, y);
    const TI = 16;            // inset from a wall face (walls are 12px thick)
    const top = (R)=> R.y + 26;        // just below the 20px banded top wall
    const bot = (R)=> R.y1 - TI;       // just inside the bottom wall

    // RECEPTION / LOUNGE: a square room-sized rug with a seating set that all FACES RIGHT
    // (toward the entrance) — a side-profile TV, armchairs, and coffee tables on the rug,
    // a vending machine and plants tucked into the corners.
    const L = cellBox(LOUNGE_CELL[0], LOUNGE_CELL[1]);
    const cs = L.h - 56, cxn = Math.round(L.cx), cyn = Math.round(L.y + L.h/2 + 8);
    this.add.rectangle(cxn, cyn, cs+10, cs+10, 0x8a4a40).setOrigin(0.5).setDepth(1);             // rug border (warm)
    this.add.rectangle(cxn, cyn, cs, cs, 0xb9685a).setOrigin(0.5).setDepth(1);                   // rug field
    this.add.rectangle(cxn, cyn-cs/2, cs, 2, 0xcf7a68).setOrigin(0.5,0).setDepth(1);             // top sheen
    this.add.rectangle(cxn, cyn, cs-18, cs-18).setOrigin(0.5).setDepth(1).setStrokeStyle(2, 0xd98a78); // inner ring
    this.bm(L.cx, top(L)-2, "LOUNGE", 12, 0x8aa0c8, 0.5, 0).setDepth(4);
    P("tv_r",    L.x+58, cyn+8);                                        // flat-screen, faces RIGHT
    P("chair_r", L.x+150, cyn-20); P("chair_r", L.x+150, cyn+34);       // armchairs facing right
    P("ctable",  L.x+198, cyn-14); P("ctable",  L.x+198, cyn+38);       // coffee tables
    P("vending", L.x1-TI-12, L.y+74);
    P("plant",   L.x+TI+6, bot(L)); P("plant", L.x1-TI-8, bot(L));

    // CTO OFFICE (walled room, row 2): his executive desk + chair at the bottom and
    // plants in the corners (no org board — the room stays uncluttered). Opens RIGHT.
    const E = ROOMS.office;
    P("counter", E.cx, E.y1-34);                                       // executive desk (CTO stands behind)
    P("plant", E.x+TI+8, E.y1-TI); P("plant", E.x1-TI-10, E.y1-TI);    // corner plants (clear of the door)
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
    // ONE themed prop per room, tucked into the same top-right corner against the wall
    // (consistent, never random). The working desks are placed per-lead by the floor.
    const props = {
      plan:"board", frontend:"rack", backend:"rack",
      data:"shelf", review:"shelf", security:"rack",
    };
    for (const key in props){
      const R = ROOMS[key]; if(!R) continue;
      this.makeProp(props[key], R.x1 - 22, R.y + 34);
    }
  }

  makeProp(kind, x, floorY){
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(3);  // props (proportional to the 32px sprites)
    // CRAFTED block: one scene-ink outline + top-LEFT light (top+left edges) + bottom-RIGHT
    // shade (bottom+right edges). One light direction for every prop (the "drawn" look).
    const out = (rx,ry,w,h,col)=>{
      c.add(this.add.rectangle(rx,ry,w+2,h+2,PC.sceneInk).setOrigin(0.5,1));            // ink outline
      c.add(this.add.rectangle(rx,ry,w,h,col).setOrigin(0.5,1));                        // face
      c.add(this.add.rectangle(rx,ry-h+1,w,1,shade(col,1.22)).setOrigin(0.5,1));        // top light
      c.add(this.add.rectangle(rx-w/2,ry,1,h,shade(col,1.12)).setOrigin(0,1));          // left light
      c.add(this.add.rectangle(rx,ry-1,w,1,shade(col,0.80)).setOrigin(0.5,1));          // bottom shade
      c.add(this.add.rectangle(rx+w/2-1,ry,1,h,shade(col,0.84)).setOrigin(0,1));        // right shade
    };
    c.add(this.add.ellipse(0,3,18,5,PC.shadow,0.22));                                   // contact shadow on the floor
    if (kind==="plant"){
      // potted plant — terracotta pot (rim + soil) and a fan of SHAPED tapering leaf
      // blades (each a 2-tone triangle), splaying out and up. Drawn back-to-front.
      const leaf=(bx,tipx,tipy,col)=>{
        c.add(this.add.triangle(0,0, bx-3,-9, bx+3,-9, bx+tipx, tipy, shade(col,0.7)));   // dark edge
        c.add(this.add.triangle(0,0, bx-2,-9, bx+1,-9, bx+tipx-1, tipy+1, col));          // lit inner
      };
      leaf(-4,-9,-19,0x2f8f4e); leaf(4,9,-19,0x2f8f4e);         // outer (splay down-out)
      leaf(-2,-3,-30,0x37a85c); leaf(2,3,-30,0x37a85c);         // mid
      leaf(0,0,-37,0x4cc06f);                                    // centre (tall)
      out(0,0,15,8,0xcf8a46);                                   // pot body (over blade bases)
      c.add(this.add.rectangle(0,-7,17,3,0xe0a05a).setOrigin(0.5,1));  // pot rim (overhang)
      c.add(this.add.rectangle(0,-9,12,2,0x5e3b1e).setOrigin(0.5,1));  // soil
    } else if (kind==="monitor"){
      out(0,0,6,10,0x2a3550); out(0,-9,26,18,0x10151f);
      c.add(this.add.rectangle(0,-11,20,12,0x2bd3ff,0.85).setOrigin(0.5,1)); // glowing screen
    } else if (kind==="desk"){
      out(0,0,46,14,0x6b4a2c); out(-16,-12,10,12,0x10151f); // desk + small monitor
      c.add(this.add.rectangle(-16,-14,7,7,0x7dffb0,0.8).setOrigin(0.5,1));
    } else if (kind==="rack"){
      out(0,0,28,42,0x2a3040);                                  // server cabinet (dark steel)
      c.add(this.add.rectangle(0,-2,22,1,0x4a5266).setOrigin(0.5,1));            // door seam
      for(let r=0;r<5;r++){                                     // 5 rack units, each w/ LEDs
        const sy=-5-r*7;
        c.add(this.add.rectangle(0,sy,22,5,0x1a1f2b).setOrigin(0.5,1));          // unit face
        c.add(this.add.rectangle(0,sy,22,1,0x3a4250).setOrigin(0.5,1));          // unit top edge
        for(let i=0;i<3;i++) c.add(this.add.rectangle(-7+i*3,sy-2,1.6,1.6,[0x5ddc7a,0xffd95e,0xff6d6d][(r+i)%3]).setOrigin(0.5,1)); // LEDs
        c.add(this.add.rectangle(8,sy-2,5,1.6,0x6a7488).setOrigin(0.5,1));       // drive slot
      }
    } else if (kind==="shelf"){
      out(0,0,32,34,0x6b4a2c);                                  // wooden bookcase
      const books=[0xd96a82,0x6fb8e0,0xe0b84a,0x6fcf86,0x9b86d0,0xd86a5a];
      for(let r=0;r<3;r++){
        const sy=-5-r*10;
        c.add(this.add.rectangle(0,sy,28,1,0x4a3320).setOrigin(0.5,1));          // shelf board
        for(let i=0;i<5;i++){ const bh=7+((i+r)%3); const bc=books[(r*5+i)%books.length];
          c.add(this.add.rectangle(-11+i*5,sy-1,4,bh,bc).setOrigin(0.5,1).setStrokeStyle(1,shade(bc,0.6))); } // books (varied height)
      }
    } else if (kind==="board"){
      out(0,-4,42,4,0x7a5028);                                   // wooden frame base
      out(0,-6,38,24,0xd8b87a);                                  // tan corkboard
      c.add(this.add.rectangle(-12,-22,12,3,0xe06a86).setOrigin(0.5,1));   // pinned notes
      c.add(this.add.rectangle(2,-16,16,3,0x5db8ff).setOrigin(0.5,1));
      c.add(this.add.rectangle(-6,-10,10,3,0x6fcf86).setOrigin(0.5,1));
    } else if (kind==="couch"){
      out(0,-6,52,12,0x6a4a7a);                                 // backrest (behind)
      out(-23,-2,8,16,0x5a3a6a); out(23,-2,8,16,0x5a3a6a);      // padded arms
      out(0,0,40,9,0x7a5a8a);                                   // seat base (front)
      for(let i=0;i<3;i++){                                     // back + seat cushions
        c.add(this.add.rectangle(-13+i*13,-7,11,6,0x82628f).setOrigin(0.5,1).setStrokeStyle(1,0x5a3a6a));
        c.add(this.add.rectangle(-13+i*13,-2,11,5,0x8f6fa6).setOrigin(0.5,1).setStrokeStyle(1,0x5a3a6a)); }
      c.add(this.add.rectangle(-22,9,3,4,0x3a2a4a).setOrigin(0.5,1)); c.add(this.add.rectangle(22,9,3,4,0x3a2a4a).setOrigin(0.5,1)); // legs
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
      c.add(this.add.rectangle(0,0,18,3,0x3a3f4a).setOrigin(0.5,1));    // base
      c.add(this.add.rectangle(0,-2,4,4,0x2a2f38).setOrigin(0.5,1));    // neck
      out(0,-5,38,24,0x1a1e26);                                        // flat-screen bezel
      const scr=this.add.rectangle(0,-8,32,18,0x2bd3ff,0.7).setOrigin(0.5,1); // screen
      c.add(scr);
      c.add(this.add.rectangle(-12,-24,12,1,0x9fe8ff,0.5).setOrigin(0.5,1)); // screen glint
      this.tweens.add({targets:scr, fillAlpha:{from:0.45,to:0.8}, duration:700+Math.abs(Math.round(x))%500,
        yoyo:true, repeat:-1, ease:"Sine.inOut"});                // flicker
    } else if (kind==="vending"){
      out(0,0,28,48,0xe6b733);                                  // yellow cabinet
      c.add(this.add.rectangle(0,-25,24,34,PC.sceneInk).setOrigin(0.5,1));       // display frame
      c.add(this.add.rectangle(0,-26,20,30,0x1f5fc0).setOrigin(0.5,1));          // blue interior
      const cans=[0xff5d6c,0x5ddc7a,0xffd95e,0xff8fae,0x7fd1ff,0xffa54a,0xff6d6d,0x6fcf86,0xffd95e];
      for(let r=0;r<3;r++){                                     // three shelves of cans
        c.add(this.add.rectangle(0,-27-r*9,20,1,0x14406f).setOrigin(0.5,1));     // shelf line
        for(let i=0;i<3;i++) c.add(this.add.rectangle(-6+i*6,-29-r*9,4,6,cans[r*3+i]).setOrigin(0.5,1).setStrokeStyle(1,0x14406f));
      }
      c.add(this.add.rectangle(-3,-26,8,30,0xbfe0ff,0.16).setOrigin(0.5,1));     // glass sheen
      c.add(this.add.rectangle(9,-32,4,6,0xb0851e).setOrigin(0.5,1));            // coin/button panel
      c.add(this.add.rectangle(0,-5,18,6,PC.sceneInk).setOrigin(0.5,1));         // dispenser tray
      c.add(this.add.rectangle(0,-6,14,3,0x14406f).setOrigin(0.5,1));            // tray mouth
    } else if (kind==="counter"){
      out(0,0,58,12,0xe6dcc0);                                  // cream counter front
      c.add(this.add.rectangle(0,-12,62,5,0xf2ead2).setOrigin(0.5,1));           // top surface (overhang)
      c.add(this.add.rectangle(0,-16,62,1,0xfff8e8).setOrigin(0.5,1));           // top edge highlight
      c.add(this.add.rectangle(0,-12,62,1,PC.sceneInk).setOrigin(0.5,1));        // seam under top
      c.add(this.add.rectangle(0,-7,58,2,0xd6473f).setOrigin(0.5,1));            // red trim band
      c.add(this.add.rectangle(-19,-1,1,9,0xcdbf9c).setOrigin(0.5,1)); c.add(this.add.rectangle(19,-1,1,9,0xcdbf9c).setOrigin(0.5,1)); // panel seams
      c.add(this.add.rectangle(-16,-15,3,3,0x5a5650).setOrigin(0.5,1));          // terminal stand
      out(-16,-17,15,11,0x3a4660);                              // terminal bezel
      c.add(this.add.rectangle(-16,-19,11,7,0x5ddc7a,0.95).setOrigin(0.5,1));    // green screen
      c.add(this.add.rectangle(16,-15,9,4,0xff8fae).setOrigin(0.5,1).setStrokeStyle(1,0xc05070)); // tray
    } else if (kind==="stool"){
      c.add(this.add.rectangle(0,-2,5,6,0xb05f24).setOrigin(0.5,1));            // post/leg
      c.add(this.add.ellipse(0,-6,15,7,0x9c4a1c).setOrigin(0.5,1));             // seat rim (dark)
      c.add(this.add.ellipse(0,-7,13,6,0xe0863a).setOrigin(0.5,1));             // cushion top
      c.add(this.add.ellipse(-2,-8,6,2,0xf4a45a).setOrigin(0.5,1));             // top-left highlight
    } else if (kind==="chair_r"){
      // an armchair in SIDE PROFILE facing RIGHT — backrest on the left, seat to the right.
      c.add(this.add.rectangle(-7,2,4,7,0x5a3a6a).setOrigin(0.5,1));            // back leg
      c.add(this.add.rectangle(7,1,4,6,0x5a3a6a).setOrigin(0.5,1));             // front leg
      out(-6,-2,5,20,0x6a4a7a);                                                 // tall backrest (left)
      out(2,-1,18,8,0x7a5a8a);                                                  // seat block (extends right)
      c.add(this.add.rectangle(2,-8,16,4,0x8f6fa6).setOrigin(0.5,1).setStrokeStyle(1,0x5a3a6a)); // seat cushion
      c.add(this.add.rectangle(-3,-6,4,12,0x9a7ab0).setOrigin(0.5,1));          // backrest cushion (lit, faces right)
    } else if (kind==="tv_r"){
      // a flat-screen in SIDE PROFILE, the bright screen on its RIGHT face (facing right).
      c.add(this.add.rectangle(0,0,16,3,0x3a3f4a).setOrigin(0.5,1));            // base
      c.add(this.add.rectangle(0,-2,4,4,0x2a2f38).setOrigin(0.5,1));            // neck
      out(-2,-5,10,24,0x1a1e26);                                               // narrow side bezel
      const scr=this.add.rectangle(4,-7,4,18,0x2bd3ff,0.7).setOrigin(0.5,1);    // screen on the RIGHT face
      c.add(scr);
      c.add(this.add.rectangle(6,-22,1,16,0x9fe8ff,0.5).setOrigin(0.5,1));      // right-edge glint
      this.tweens.add({targets:scr, fillAlpha:{from:0.45,to:0.8}, duration:700+Math.abs(Math.round(x))%500,
        yoyo:true, repeat:-1, ease:"Sine.inOut"});                            // flicker
    }
    return c;
  }

  // A desk + monitor + keyboard each lead works at, placed IN the room floor.
  makeWorkstation(x, floorY, accent){
    if (accent == null) accent = 0x7fd1ff;   // monitor glow default (sprites carry their own colour)
    x = Math.round(x);
    const c = this.add.container(x, floorY).setDepth(floorY+5).setScale(1.3);
    const O = PC.sceneInk;                                        // one scene ink outline
    const out = (rx,ry,w,h,col)=>{   // crafted block: ink outline + top-left light + br-shade
      c.add(this.add.rectangle(rx,ry,w+2,h+2,O).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry,w,h,col).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx,ry-h+1,w,1,shade(col,1.18)).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx-w/2,ry,1,h,shade(col,1.1)).setOrigin(0,1));
      c.add(this.add.rectangle(rx,ry-1,w,1,shade(col,0.82)).setOrigin(0.5,1));
      c.add(this.add.rectangle(rx+w/2-1,ry,1,h,shade(col,0.86)).setOrigin(0,1));
    };
    // the orange round STOOL the worker sits on — a SEPARATE object drawn BEHIND/under
    // the person (lower depth than the person sprite) and just in front of the table,
    // so it reads as the person seated on it. (floorY-16 is where the person stands.)
    const chair = this.add.container(x, floorY-16).setDepth(floorY-20).setScale(1.25);
    chair.add(this.add.ellipse(0,5,16,6,PC.shadow,0.30).setOrigin(0.5,1));           // contact shadow
    chair.add(this.add.rectangle(0,3,5,5,0xb05f24).setOrigin(0.5,1));                // post/leg
    chair.add(this.add.ellipse(0,-1,16,8,0x9c4a1c).setOrigin(0.5,1));                // seat rim (dark)
    chair.add(this.add.ellipse(0,-2,14,7,0xe0863a).setOrigin(0.5,1));                // cushion top
    chair.add(this.add.ellipse(-2,-3,7,3,0xf4a45a).setOrigin(0.5,1));                // top-left highlight
    c.chair = chair;     // tracked for teardown by the workstation it belongs to
    // a GRAY PC COUNTER: a front face + a lighter top surface (reads 3D), legs, a seam.
    out(0,0,46,8,0xb0aaa2);                                       // front face (warm gray)
    c.add(this.add.rectangle(0,-8,48,5,0xc8c3bb).setOrigin(0.5,1));   // top surface (lighter, overhang)
    c.add(this.add.rectangle(0,-12,48,1,0xe4e0d8).setOrigin(0.5,1));  // top edge highlight
    c.add(this.add.rectangle(0,-8,48,1,PC.sceneInk).setOrigin(0.5,1));// seam under the top surface
    c.add(this.add.rectangle(-21,8,3,7,0x837d75).setOrigin(0.5,1));
    c.add(this.add.rectangle(21,8,3,7,0x837d75).setOrigin(0.5,1));    // legs
    // a MONITOR on the counter: stand + dark bezel + dept-tinted screen (live activity)
    c.add(this.add.rectangle(-13,-12,4,3,0x5a5650).setOrigin(0.5,1));// stand
    out(-13,-14,15,11,0x44485a);                                 // bezel (slate)
    const screen = this.add.rectangle(-13,-16,11,7,shade(accent,1.22),0.97).setOrigin(0.5,1);
    c.add(screen);
    c.add(this.add.rectangle(-16,-18,5,1,shade(accent,1.5))); c.add(this.add.rectangle(-17,-15,8,1,shade(accent,1.4)));
    // a small KEYBOARD on the counter top
    out(13,-9,13,3,0x9a958d); c.add(this.add.rectangle(13,-11,13,1,0xc4bfb6).setOrigin(0.5,1));
    this.screens = this.screens || []; this.screens.push(screen);
    return c;
  }

  // Crisp pixel label via the baked BITMAP font (NEAREST-sampled — never blurs when
  // scaled). px = display height; tint colours the white glyphs. Returns the BitmapText.
  bm(x, y, txt, px, tint, ox, oy){
    const t = this.add.bitmapText(0, 0, "px", txt, px);
    if (tint!=null) t.setTint(tint);
    ox = ox==null?0:ox; oy = oy==null?0:oy;
    t.setPosition(Math.round(x - t.width*ox), Math.round(y - t.height*oy));  // manual origin (version-safe)
    return t;
  }

  // A name/role badge pinned over a person `s`. `filled` → solid accent pill with dark
  // text (leads); else a dark pill with accent text (ICs / generic). BitmapText = crisp.
  pinBadge(s, lbl, color, filled, yy){
    const PX=8, y = yy==null ? -26 : yy;   // sits ABOVE the head (clear of the face)
    const bg = filled ? color : 0x2a2230, fg = filled ? 0x241d2e : color;  // OPAQUE solid placard
    const t = this.bm(0,y,lbl,PX,fg,0.5,0.5);            // proportional text → size the pill to it
    const pw = Math.ceil(t.width)+8;
    // a 2px pin tail so the badge reads as belonging to THIS character (not a floating tooltip)
    s.add(this.add.rectangle(0,y+5,2,4,PC.sceneInk).setOrigin(0.5,0));
    // opaque placard: cool-ink shadow base + the solid face (1px ink outline) + the text on top
    s.add(this.add.rectangle(0,y+1,pw,11,PC.sceneInk).setOrigin(0.5));
    s.add(this.add.rectangle(0,y,pw-2,9,bg).setOrigin(0.5).setStrokeStyle(1,PC.sceneInk));
    s.add(t);
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
    this.time.addEvent({delay:1500, loop:true, callback:()=>this.ctoPatrol()});  // the CTO's rounds
    this.time.addEvent({delay:2600, loop:true, callback:()=>this.deskSweep()});  // stay-at-your-desk sweep
    this.time.addEvent({delay:1600, loop:true, callback:()=>this.chatter()});    // idle chat bubbles
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
    s.add(this.add.rectangle(0,0,58,32,0xfffdf3,0.98).setStrokeStyle(3,col));
    s.add(this.add.text(0,0,pass?"PASS":"FAIL",{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"16px",fontStyle:"bold",color:pass?"#1d7a3f":"#b53129"}).setOrigin(0.5));
    s.setScale(3.4).setAlpha(0).setAngle(-12);
    this.tweens.add({targets:s, scale:1, alpha:1, angle:-6, duration:170, ease:"Back.out",
      onComplete:()=>{ this.ring(cx,cy,col); this.burst(cx,cy,[col,0xffffff],16);
        this.time.delayedCall(950,()=>this.tweens.add({targets:s,alpha:0,duration:300,onComplete:()=>s.destroy()})); }});
    if (!pass) this.shake(380,0.015);
  }
  // checkpoint spotlight + bouncing "?" over the CTO
  spotlightCTO(on){
    this.ctoHold = !!on;        // he's deciding, not doing his rounds — pause the patrol
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
    b.add(this.add.rectangle(0,0,380,48,0xfffdf3,0.98).setStrokeStyle(3,color));
    b.add(this.add.text(0,0,txt,{fontFamily:"'PixelFont','Courier New',monospace",
      fontSize:"22px",fontStyle:"bold",color:"#33405e"}).setOrigin(0.5));
    b.setScale(0.2).setAlpha(0);
    this.tweens.add({targets:b, scale:1, alpha:1, duration:300, ease:"Back.out"});
    this.time.delayedCall(2400,()=>this.tweens.add({targets:b, alpha:0, y:b.y-20, duration:400,
      onComplete:()=>b.destroy()}));
  }

  // a security guard posted at the checkpoint desk (dark uniform, cap, badge)
  buildGuardian(){
    const R = ROOMS.gate, x = Math.round(R.x + R.w*0.30), y = R.y1-30;
    const c = this.add.container(x,y).setDepth(6);
    c.add(this.add.ellipse(0,9,18,5,PC.shadow,0.30));           // cool cast shadow
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
    this.pinBadge(c, "GUARD", 0xbcd0ff, true, -30);            // label the gate security guard
  }

  // The Gate is the RELEASE CHECKPOINT: a CCTV bank + checkpoint desk, plus a sealed
  // vault door (vertical) in the gap to the VAULT below. The horizontal corridor
  // between the two cells is walled off — the only link is that short door,
  // which unlocks only when every release gate passes.
  buildGate(){
    const G = ROOMS.gate, V = ROOMS.vault, cx = Math.round(G.cx);
    // ---- CCTV monitors MOUNTED on the top wall (right of the GATE sign), so they read
    // as wall-hung camera feeds — not loose blocks on the floor. ----
    const bx = G.x1-58, by = G.y+52, mon=[0x2bd3ff,0x7dffb0,0xffd95e,0x5db8ff];
    const bank = this.add.container(bx,by).setDepth(3);
    bank.add(this.add.rectangle(0,-30,6,18,0x2a3550).setOrigin(0.5,1));        // wall mount bracket
    bank.add(this.add.rectangle(0,0,54,34,0x10151f).setOrigin(0.5,1).setStrokeStyle(2,0x2a3550));
    for (let i=0;i<4;i++){ const sx=-12+(i%2)*24, sy=-19+Math.floor(i/2)*16;
      bank.add(this.add.rectangle(sx,sy,20,12,mon[i],0.85).setOrigin(0.5,1));
      bank.add(this.add.rectangle(sx,sy,20,12).setOrigin(0.5,1).setStrokeStyle(1,0x05080f)); }
    const led=this.add.rectangle(bx+22,by-30,4,4,0xff5d6c,0.9).setDepth(3);    // REC light
    this.tweens.add({targets:led, alpha:0.2, duration:700, yoyo:true, repeat:-1});
    // ---- checkpoint desk: a clear, legged counter sitting IN FRONT OF the guard, so
    // the two read together as a manned security checkpoint. ----
    const dx = Math.round(G.x + G.w*0.30), dy = G.y1-18;
    const desk = this.add.container(dx,dy).setDepth(7);
    desk.add(this.add.ellipse(0,2,72,7,PC.shadow,0.28).setOrigin(0.5,1));    // contact shadow
    desk.add(this.add.rectangle(-28,1,5,9,0x2a3550).setOrigin(0.5,1));       // left leg
    desk.add(this.add.rectangle(28,1,5,9,0x2a3550).setOrigin(0.5,1));        // right leg
    desk.add(this.add.rectangle(0,-7,64,12,0x394566).setOrigin(0.5,1).setStrokeStyle(1,PC.sceneInk)); // counter front
    desk.add(this.add.rectangle(0,-19,68,5,0x4a587a).setOrigin(0.5,1).setStrokeStyle(1,PC.sceneInk)); // counter top
    desk.add(this.add.rectangle(0,-19,68,1,0x6a7aa0).setOrigin(0.5,1));      // top edge highlight
    desk.add(this.add.rectangle(-18,-26,16,11,0x10151f).setOrigin(0.5,1).setStrokeStyle(1,0x05080f)); // terminal
    desk.add(this.add.rectangle(-18,-28,12,7,0x2bd3ff,0.85).setOrigin(0.5,1)); // terminal screen
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
    c.add(this.add.ellipse(0,15,40,8,PC.shadow,0.32));
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
    // The chest is the door to this floor's repo: once the build is DONE you can
    // click it to jump straight to the repo (the Floor 0 repos, preselected to
    // this one). While agents are still working — first build OR a later revision
    // — it's locked and does nothing; same when there's no repo yet.
    this.chest = c;
    c.setInteractive(new Phaser.Geom.Rectangle(-22,-22,44,42), Phaser.Geom.Rectangle.Contains);
    c.on("pointerover", ()=>{ if(this.chestActionable()){ this.game.canvas.style.cursor="pointer"; c.setScale(1.42); } });
    c.on("pointerout",  ()=>{ this.game.canvas.style.cursor="default"; c.setScale(1.3); });
    c.on("pointerdown", ()=> this.onChestClick());
  }
  // Actionable only when this floor's build has finished (a repo exists). Locked
  // while running/iterating, so agents-at-work never opens a half-baked tree.
  chestActionable(){
    const F = FLOORS.get(this.proj.floor);
    return !!(F && F.status === "done");
  }
  onChestClick(){
    const F = FLOORS.get(this.proj.floor);
    if(!F || F.status !== "done") return;   // locked / no repo yet -> do nothing
    openFilesFor({id:F.id, floor:F.floor, release:F.release, request:F.request, status:"done"});
  }

  buildAgents(){
    let li = 0;                                    // global idle-mood slot index
    for (const room in ROSTER){
      const names = ROSTER[room], R = ROOMS[room]; if (!R) continue;
      if (room === "security" && this.proj.mode === "assistant") continue;   // gated-mode crew only
      const cols = Math.min(names.length, 3);
      const single = names.length === 1;                         // a one-role room → centre it
      const PX = 84, SY = R.y + 94, RP = 50;                     // SAME grid as the eng rooms
      names.forEach((name,i)=>{
        const col = i%cols, row = Math.floor(i/cols);
        // centred columns, first row well below the room label (matches every other room)
        const blockW = (cols-1)*PX, hx = single ? R.cx : R.cx - blockW/2 + col*PX;
        const hy = single ? Math.round(R.y + R.h*0.52)            // single role → centred desk
                          : SY + row*RP;                          // else the consistent grid
        const seed = hashStr((this.proj.id||'f')+name), kit = kitFor(name, seed);
        this.makeWorkstation(hx, hy+16, kit.accent);              // desk just in front of them
        const a = this.makePerson(hx, hy, kit);
        a.setScale(1.5).setDepth(hy);                             // y-sort (desk occludes legs)
        // role badge over the agent so every senior role is labelled (like the eng leads)
        const rtag = ROLE_TAG[name] || name.slice(0,9).toUpperCase();
        const acc = (kit.accent!=null ? kit.accent : 0x9fb4d8);
        this.pinBadge(a, rtag, acc, true);
        a.home = {x:hx,y:hy}; a.room = room; a.idleSlot = li++;
        // Everyone starts AT their own desk, off the clock: stages just turn them
        // round to the monitor. Nobody is ever staged in the lounge.
        a.setAlpha(1); a.seen = true; a.bubble = null;
        this.standDown(a);
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
    const O = ROOMS.office;
    this.lobby = {cx: Math.round(O.cx), standY: Math.round(O.y1 - 48)};   // behind his desk
    const c = this.makePerson(this.lobby.cx, this.lobby.standY, kitFor("CTO", hashStr((this.proj.id||'f')+"CTO")));
    c.setScale(1.5).setDepth(900); c.fixedDepth = 900;   // CTO walks the floor -> always on top
    this.pinBadge(c, "CTO", 0xffd95e, true, -26);         // gold badge, above the head
    // A pulsing gold pool of light under his feet. Everyone else is permanently at a
    // desk, so HE is the active-group indicator — this makes him findable at a glance.
    const pool = this.add.ellipse(0, 4, 40, 16, 0xffd95e, 0.34);
    c.addAt(pool, 0);                                    // under the body (and its shadow)
    this.tweens.add({targets:pool, scaleX:{from:0.80,to:1.16}, scaleY:{from:0.80,to:1.16},
      alpha:{from:0.42,to:0.18}, duration:1200, yoyo:true, repeat:-1, ease:"Sine.inOut"});
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
    // ON-THE-CLOCK marker: a green disc BEHIND the whole person while their node is
    // running, so "who is working right now" reads from across the floor. First child
    // of the container = backmost, so it never covers the body, the badge or the desk;
    // it lives on the container rather than `bob` so the walk bob doesn't jitter it.
    const glow = this.add.circle(0,-8,19,0x2fe07a,0.35).setStrokeStyle(2,0x6cffae,0.95)
      .setAlpha(0).setVisible(false);
    c.add(glow);
    c.add(this.add.ellipse(0,3,11,4,PC.shadow,0.32));       // cool shadow under the feet
    const bob = this.add.container(0,0);
    // 0.36 x the caller's 1.4 container = ~0.5 net of the 64px frame => crisp 32px
    // (integer-friendly half-scale; non-integer scales soften pixel art).
    // selective dark OUTLINE: a slightly-larger silhouette behind the body, tinted scene
    // ink and kept frame-synced — gives every character a clean 1px edge so they pop.
    const ol = this.add.sprite(0,4,"chars",ch.front[0]).setOrigin(0.5,1).setScale(0.378).setTint(PC.sceneInk);
    bob.add(ol);
    const sprite = this.add.sprite(0,4,"chars",ch.front[0]).setOrigin(0.5,1).setScale(0.36);
    const _sf = sprite.setFrame.bind(sprite);
    sprite.setFrame = (f)=>{ _sf(f); ol.setFrame(f); return sprite; };   // outline mirrors the body frame
    bob.add(sprite);
    const spark = this.add.rectangle(6,-24,2,2,0xfff04a).setOrigin(0,0).setVisible(false);
    bob.add(spark);
    c.add(bob);
    c.bob=bob; c.sprite=sprite; c.glow=glow; c.spark=spark; c.eyes=[]; c.char=ch; c._wf=0;
    c.faceFront = ()=>{ sprite.setFrame(ch.front[c._wf % ch.front.length]); };
    c.faceBack  = ()=>{ sprite.setFrame(ch.back); };
    return c;
  }

  // Light (or drop) the working disc. Idempotent: restaff() re-seats the whole floor
  // on every telemetry event, so re-lighting an already-lit agent must not restart the
  // pulse (it would flicker in step with the event stream instead of breathing).
  setGlow(a, on){
    if (!a || !a.glow || !!a.glowOn === !!on) return;
    a.glowOn = !!on;
    if (a.glowTween){ a.glowTween.stop(); a.glowTween = null; }
    if (!on){ a.glow.setAlpha(0).setVisible(false); return; }
    a.glow.setVisible(true).setAlpha(1);
    a.glowTween = this.tweens.add({targets:a.glow, alpha:{from:0.62,to:1},
      duration:760, yoyo:true, repeat:-1, ease:"Sine.inOut"});
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

  // A discipline room is REBUILDING (a service deepening): pulse its glow + a
  // floating "↻ rebuilding" tag so it reads at a glance, not just in the log.
  pulseRoom(key){
    const R = ROOMS[key]; if(!R || R.virtual) return;
    const g = this.roomGlow[key];
    if(g) this.tweens.add({targets:g, fillAlpha:{from:0.0,to:0.22}, duration:260,
                           yoyo:true, repeat:2, ease:"Sine.inOut"});
    const t = this.add.text(R.cx, R.y+20, "↻ rebuilding",
      {fontFamily:"'PixelFont','Courier New'",fontSize:"9px",fontStyle:"bold",color:"#ffb05a"})
      .setOrigin(0.5,0).setDepth(2000);
    this.tweens.add({targets:t, y:t.y-14, alpha:{from:1,to:0}, duration:1400,
                     ease:"Sine.out", onComplete:()=>t.destroy()});
  }
  // A room "seats its crew" when it's in the LIVE set — the rooms currently producing
  // node telemetry. The real build runs stages concurrently (engineering can still be
  // heads-down while QA reviews), so this is a SET, not one active room: a room joins
  // on its `active` event and every live room stays staffed at once. Engineering is a
  // virtual stage — when it's live, all three discipline rooms count.
  roomLive(room){
    const L = this.liveRooms;
    return !!room && !!L && (L.has(room) || (L.has("engineering") && ENG_ROOMS.includes(room)));
  }
  // key = the PRIMARY (most recent) room: drives the glow, the CTO visit and the
  // forward-phase handoff. reset=true (a new phase boundary) clears the live set first,
  // so a fresh phase doesn't inherit the prior phase's crew.
  setActiveRoom(key, reset=false){
    const prev = this.activeRoom;
    this.activeRoom = key;
    this.liveRooms = this.liveRooms || new Set();
    if (reset){                            // new phase → start the crew fresh
      this.liveRooms.clear(); this.workingAgents.clear(); this.roomInflight = {};
    }
    if (key) this.liveRooms.add(key);      // this room is now producing work
    // When the build advances to the NEXT phase, a worker from the finished room carries
    // the "work" over to the new room (the handoff). Only on a forward phase boundary.
    const SEQ = ["plan","engineering","review","security","gate","vault"];
    const ph = (k)=> (ENG_ROOMS.includes(k)||k==="engineering") ? "engineering" : k;
    const oi = SEQ.indexOf(ph(prev)), ni = SEQ.indexOf(ph(key));
    if (oi >= 0 && ni > oi) this.deliverWork(prev, key);
    // The CTO GOES AND STANDS WITH a group that's live — since everyone else is
    // permanently at their own desk, he's the thing that tells you at a glance WHERE the
    // work is. He does NOT jump to every newest event (with several rooms live that just
    // teleports him around): the patrol picks the room, and only moves him when his dwell
    // is up or the room he's in has gone dark.
    this.ctoPatrol();
    this.staffRoom(key);                 // the live crew heads-down; everyone else off duty
  }

  // The rooms with work in them right now, in floor order — the CTO's patrol route.
  // `roomLive` expands the virtual engineering stage, so its three lanes each count.
  patrolRooms(){ return PATROL_ROOMS.filter(k => ROOMS[k] && this.roomLive(k)); }
  // One beat of the patrol: stay put while the dwell runs, then walk to the next live
  // room. Called on a timer AND whenever the live set changes, so he reacts at once when
  // the first work lands or the room he's overseeing goes quiet.
  ctoPatrol(){
    if (!this.cto || this._dead || this.ctoHold) return;         // held during a checkpoint
    if (this.cto.moveTween && this.cto.moveTween.isPlaying()) return;   // already walking
    const rooms = this.patrolRooms();
    if (!rooms.length){ if (this.ctoRoom !== null) this.sendCTO(null); return; }
    const here = rooms.indexOf(this.ctoRoom);
    if (here >= 0 && (rooms.length === 1 || Date.now() < (this.ctoDwellUntil||0))) return;
    this.sendCTO(rooms[(here + 1) % rooms.length]);   // here<0 (office/dark room) -> rooms[0]
  }

  // Walk the CTO to the room he's overseeing (null → back to his office) and mark it.
  // `ctoRoom` also drives the brightest room glow, so his room reads as THE active one.
  sendCTO(key){
    const R = key ? ROOMS[key] : null;
    // A VIRTUAL stage (engineering) has no room of its own — stand in a live lane, and
    // let ctoRoom name that REAL room so the glow marks exactly where he is.
    const T = (R && R.virtual) ? ROOMS[ENG_ROOMS.find(r=>this.roomLive(r)) || "backend"] : R;
    const prev = this.ctoRoom;
    this.ctoRoom = T ? T.key : null;
    this.paintGlow();
    if (!this.cto || !this.lobby) return;
    // Fresh dwell for wherever he's headed, so the patrol leaves him alone while he
    // oversees this room (jittered, so he isn't metronomic).
    const dwell = ()=>{ this.ctoDwellUntil = Date.now() + CTO_DWELL_MS[0]
      + Math.random()*(CTO_DWELL_MS[1]-CTO_DWELL_MS[0]); };
    const tx = Math.round(T ? T.cx : this.lobby.cx);
    const ty = Math.round(T ? T.standY : this.lobby.standY);
    if (Math.abs(this.cto.x-tx) < 4 && Math.abs(this.cto.y-ty) < 4){ dwell(); return; }
    this.walkTo(this.cto, tx, ty, ()=>{
      if (this._dead) return;
      dwell();
      // overseeing a team → faces up into the room, watching them work; in his own
      // office he faces the door instead.
      if (T){ if (this.cto.faceBack) this.cto.faceBack(); }
      else if (this.cto.faceFront) this.cto.faceFront();
      if (!T || T.key === prev) return;
      this.floatText(tx, ty-34, "◆ "+(T.label || T.key), "#ffe6a0", 11);
      this.showBubble(this.cto, pick(CTO_LINES), 3200, true);
    });
  }
  // Room glow: every LIVE room is lit, and the room the CTO is standing in is lit
  // brightest — so "where the boss is" is unmistakably "where the work is".
  paintGlow(){
    for (const k in this.roomGlow){
      const g = this.roomGlow[k];
      const on = this.roomLive(k) ? (k === this.ctoRoom ? 0.20 : 0.10)
                                  : (k === this.ctoRoom ? 0.12 : 0);
      this.tweens.killTweensOf(g);          // one fade at a time (restaff can re-paint often)
      this.tweens.add({targets:g, fillAlpha:on, duration:420});
    }
  }

  // A worker from the just-finished phase carries the "work" package to the next
  // phase's room, drops it (a pickup pops there), then heads back and leaves.
  deliverWork(fromKey, toKey){
    const rk = (k)=> k==="engineering" ? "backend" : k;     // virtual eng → a real room
    const A = ROOMS[rk(fromKey)], B = ROOMS[rk(toKey)];
    if (!A || !B || this._dead) return;
    const ax = Math.round(A.cx), ay = Math.round(A.standY);
    const runner = this.makePerson(ax, ay, poolKit(4));
    runner.setScale(1.4).setDepth(945); runner.fixedDepth = 945;   // stays on top while crossing
    this.pinBadge(runner, "RUNNER", 0xffcf52, true, -17);          // label the delivery courier
    const pkg = this.add.container(0,-17);                          // the "work" carried in hand
    pkg.add(this.add.rectangle(0,0,12,9,0xffcf52).setStrokeStyle(1,0x7a5028));
    pkg.add(this.add.rectangle(0,-1,12,2,0xfff0a8));
    pkg.add(this.add.rectangle(0,0,2,9,0xc89b2e));
    runner.add(pkg);
    this.floatText(ax, ay-26, "→ "+(B.label||"next"), "#ffe6a0", 10);   // sets off with the work
    this.walkTo(runner, Math.round(B.cx), Math.round(B.standY), ()=>{
      if (this._dead){ runner.destroy(); return; }
      this.burst(B.cx, B.standY-10, [0xffcf52,0xffffff], 10);
      this.floatText(B.cx, B.standY-26, "✦ work delivered", "#ffe6a0", 10);
      if (pkg) pkg.destroy();
      // brief beat at the destination, then head back and leave
      this.time.delayedCall(600, ()=>{ if (this._dead){ runner.destroy(); return; }
        this.walkTo(runner, ax, ay, ()=>{ runner.destroy(); }); });
    });
  }

  // ---- presence: nobody ever leaves their desk ----------------------------
  // The idle MOOD for a desk slot — stable per person, offset per floor. This is
  // the whole cast of off-duty behaviour: one naps, one's happy, one's bored…
  idleMood(i){ return IDLE_MOODS[(i + (this._moodSeed||0)) % IDLE_MOODS.length]; }
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
  // Flip ONLY the sprite (and its synced outline) for facing — never the whole
  // container, so pinned name badges stay un-mirrored/readable while walking.
  faceX(a, dir){
    const t = (a && a.bob) ? a.bob : a;
    if (t) t.scaleX = dir;
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
      if (i >= pts.length){ this.stopWalk(a); this.faceX(a, 1); if (a.fixedDepth==null) a.setDepth(ty); if (then) then(); return; }
      const [px,py] = pts[i++], d = Math.hypot(px-a.x, py-a.y);
      if (Math.abs(px-a.x) > 3) this.faceX(a, px < a.x ? -1 : 1);   // face travel direction (sprite only)
      if (a.fixedDepth == null) a.setDepth(Math.max(py, a.y) + 1);
      a.moveTween = this.tweens.add({targets:a, x:px, y:py, duration:Math.max(200, d*3.2), ease:"Sine.inOut", onComplete:step});
    };
    step();
  }
  // Off duty — and they STAY at their own desk. They just swivel round (away from
  // the monitor) and settle into their mood. Only re-rolls the mood on a genuine
  // busy->idle transition, so restaff() churn can't make anyone twitch.
  standDown(a){
    if (!a || !a.seen || !a.home) return;
    this.setGlow(a, false);                            // off the clock -> no halo
    if (a.idle && !a.working && a.atDesk) return;      // already settled — leave them be
    a.working = false; a.atDesk = true;
    this.clearBubble(a, true);
    a.idle = this.idleMood((a.idleSlot||0) + (a.moodBump = (a.moodBump||0)+1));
    const settle = ()=> this.idleSettle(a);
    const here = Math.abs(a.x-a.home.x) < 2 && Math.abs(a.y-a.home.y) < 2;
    if (here) settle(); else this.walkTo(a, a.home.x, a.home.y, settle);
  }
  // Settle IN PLACE at the desk: nappers slump with a slow Zzz, everyone else turns
  // to face the room and breathes. Nobody walks again until their stage calls them.
  idleSettle(a){
    if (!a || a.working || !a.idle) return;
    this.stopWalk(a);
    if (a.napTween){ a.napTween.stop(); a.napTween = null; }
    if (a.faceFront) a.faceFront();                    // swivelled away from the screen
    this.zzzLoop(a);                                   // gated by restingNow()
    if (a.idle === "nap"){
      if (a.bob) a.bob.y = 0;
      this.stopDeskIdle(a);
      a.napTween = this.tweens.add({targets:a.bob, y:{from:0,to:-1.5}, duration:2200,
        yoyo:true, repeat:-1, ease:"Sine.inOut"});
    } else this.deskIdle(a);
  }
  // Idle RIGHT NOW = parked at their desk, off the clock, not mid-walk.
  idleNow(a){
    return !!a && !!a.seen && !a.working && !!a.atDesk && !!a.idle
      && !(a.moveTween && a.moveTween.isPlaying());
  }
  // Is this person at rest (so a Zzz should float up)? — only the desk nappers.
  restingNow(a){ return this.idleNow(a) && a.idle === "nap"; }
  // One persistent Zzz emitter per person; the gate decides when it actually shows.
  zzzLoop(a){
    if (!a || a.zzzEv) return;
    // Stagger each sleeper's first puff so the Zzz don't all blink in unison.
    a.zzzEv = this.time.addEvent({delay:1700, loop:true, startAt:Math.floor(Math.random()*1700), callback:()=>{
      if (a._dead || !this.restingNow(a)) return;
      const t = this.add.text(a.x+8, a.y-20, "Zzz",
        {fontFamily:"'PixelFont','Courier New'",fontSize:"11px",fontStyle:"bold",color:"#bcd6ff"})
        .setOrigin(0,1).setDepth(2000);
      this.tweens.add({targets:t, y:t.y-16, x:t.x+6, alpha:{from:0.95,to:0}, duration:1600,
        ease:"Sine.out", onComplete:()=>t.destroy()});
    }});
  }
  // Drop the idle mood + its animation (called when someone goes ON the clock).
  clearIdle(a){
    if (!a) return;
    a.idle = null;
    if (a.napTween){ a.napTween.stop(); a.napTween = null; if (a.bob) a.bob.y = 0; }
    this.stopDeskIdle(a);   // the Zzz emitter (a.zzzEv) is PERSISTENT — gated by restingNow
  }
  gotoDesk(a){ this.deskRest(a, true); }
  // Put a person to WORK at their desk: heads-down at the monitor, no mood, no Zzz.
  // Everyone is at their desk either way (standDown is the off-duty half of this) —
  // ``working`` only decides which way they're facing and whether they're typing.
  deskRest(a, working, busy){
    if (!a || !a.home) return;
    a.working = working; a.atDesk = true; this.clearIdle(a); this.clearBubble(a, true);
    this.setGlow(a, working);
    if (!a.seen){ a.seen = true; a.setAlpha(1); }
    this.zzzLoop(a);                       // idempotent; the gate shows it only when idle
    const settle = ()=>{
      if (a.faceBack) a.faceBack();
      if (working && busy) this.startWalk(a);   // engineers actively building = busy bob
      else { this.stopWalk(a); this.deskIdle(a); }
    };
    const here = Math.abs(a.x-a.home.x) < 2 && Math.abs(a.y-a.home.y) < 2;
    if (here) settle(); else this.walkTo(a, a.home.x, a.home.y, settle);
  }
  // Live per-node telemetry: a node just STARTED (seat its agent for the whole model
  // call) or FINISHED (clear them). Ref-counts in-flight nodes per room so a room's crew
  // only sits while it genuinely has work running. Legacy "pulse" (no phase) just marks
  // the room live — a pre-seat before real start/finish telemetry takes over.
  nodeTelemetry(room, agent, phase){
    if (!room) return;
    if (phase === "finish"){
      if (agent){
        const n = (this.workingAgents.get(agent)||1) - 1;
        if (n > 0) this.workingAgents.set(agent, n); else this.workingAgents.delete(agent);
      }
      this.roomInflight[room] = Math.max(0, (this.roomInflight[room]||0) - 1);
      if (this.roomInflight[room] === 0) this.liveRooms.delete(room);  // no work left here
      this.restaff();
      return;
    }
    this.liveRooms.add(room);
    if (agent) this.workingAgents.set(agent, (this.workingAgents.get(agent)||0) + 1);
    if (phase === "start") this.roomInflight[room] = (this.roomInflight[room]||0) + 1;
    // The primary room (glow + CTO visit) follows the most recent start; re-seat either way.
    if (this.activeRoom !== room) this.setActiveRoom(room); else this.restaff();
    if (agent) this.agentWork(agent);   // the seated agent visibly types
  }
  staffRoom(key){
    this.activeRoomKey = key;
    if (key === null){        // build over / no stage → nobody working
      this.liveRooms.clear(); this.workingAgents.clear(); this.roomInflight = {};
    } else {
      this.liveRooms.add(key);
    }
    this.restaff();
  }
  // Seat everyone by the CURRENT live/working sets — pure, no mutation (so a `finish`
  // can re-seat without re-marking its room live). A named-agent room seats only the
  // agents actually in-flight; if the room is live but none are named yet (a room-only
  // pulse), its whole crew sits as a fallback.
  //
  // ENGINEERING has a real lead/IC split that the graph reports: the Senior Engineer
  // node (the LEAD — decomposes the work) and the Junior Engineer node (the ICs — build
  // the code). So the area Tech-LEADS sit while the Senior is in-flight; the IC POOL sits
  // while the Junior is. (Per-AREA — frontend vs backend — isn't in the telemetry: both
  // nodes run once across every area, so all disciplines light together.)
  restaff(){
    const namedIn = (rm)=>{ for (const a of this.workingAgents.keys()) if (AGENT_ROOM[a]===rm) return true; return false; };
    for (const [name,a] of this.agents){
      if (!a.seen) continue;
      const rm = AGENT_ROOM[name];
      const onDesk = this.roomLive(rm) && (this.workingAgents.has(name) || !namedIn(rm));
      if (onDesk) this.deskRest(a, true); else this.standDown(a);
    }
    // Per-lane lead/IC seating from the distinct telemetry: each discipline room's LEAD
    // sprite sits while its lane's lead node runs ("Frontend Lead" …), its IC sprites
    // while its lane's engineer node runs ("Frontend Engineer" …). Legacy single-node
    // builds (room "engineering" + "Senior/Junior Engineer") still work: the Senior lights
    // every lane's leads, the Junior every lane's ICs. When a room is live but nothing is
    // named (a room-only pulse), its whole crew sits as a fallback.
    const wa = this.workingAgents, CAP = {frontend:"Frontend", backend:"Backend", data:"Data"};
    const anyEngNamed = wa.has("Senior Engineer") || wa.has("Junior Engineer")
      || ENG_ROOMS.some(r => wa.has(CAP[r]+" Lead") || wa.has(CAP[r]+" Engineer"));
    const leadUp = (room)=> wa.has((CAP[room]||"")+" Lead") || wa.has((CAP[room]||"")+" Engineer")
      || wa.has("Senior Engineer") || !anyEngNamed;   // leads fold into their lane's Engineer
    const engUp  = (room)=> wa.has((CAP[room]||"")+" Engineer") || wa.has("Junior Engineer") || !anyEngNamed;
    const seat = (group, wants)=>{
      for (const s of (group||[])){
        if (!s || !s.bob) continue;        // group arrays also hold workstations (no .bob)
        if (this.roomLive(s.room) && wants(s.room)) this.deskRest(s, true, true);
        else this.standDown(s);
      }
    };
    seat(this.techLeadGroup, leadUp);      // each lane's LEAD works while its lead node runs
    seat(this.poolGroup, engUp);           // each lane's ICs work while its engineer node runs
    this.paintGlow();                      // and re-light the rooms for the new live set
  }
  // Every person on the floor (named leads + the engineering lanes), one iterator.
  *everyone(){
    for (const [,a] of this.agents) if (a && a.seen) yield a;
    for (const s of [...(this.poolGroup||[]), ...(this.techLeadGroup||[])])
      if (s && s.bob && s.seen) yield s;    // the group arrays also hold workstations
  }
  standDownAll(){   // whole floor off the clock (checkpoints / done) — still at their desks
    for (const a of this.everyone()) this.standDown(a);
  }
  deskSweep(){   // NOBODY roams: the sweep just walks anyone off-duty back to their own
    // desk if some animation left them adrift, and re-settles anyone with no mood.
    for (const a of this.everyone()){
      if (a.working || (a.moveTween && a.moveTween.isPlaying())) continue;
      if (!a.home) continue;
      const away = Math.abs(a.x-a.home.x) > 2 || Math.abs(a.y-a.home.y) > 2;
      if (away || !a.idle){ a.idle = null; this.standDown(a); }
    }
  }
  // Ambient chat: every tick, ONE off-duty person may float a bubble that matches
  // their mood — the nappers mumble, the bored sigh, the happy ones gloat. Capped
  // and randomly skipped so the floor murmurs instead of shouting in unison, and it
  // never speaks over a real telemetry line (agentSay owns those bubbles).
  chatter(){
    if (this._dead || !this.sys.isVisible()) return;    // only the floor you're looking at
    const pool = [];
    let live = 0;
    for (const a of this.everyone()){
      if (a.bubble) live++;
      else if (this.idleNow(a)) pool.push(a);
    }
    if (!pool.length || live >= 4) return;              // a murmur, never a shouting match
    if (Math.random() < 0.25) return;                   // uneven cadence, not a metronome
    const a = pick(pool);
    this.showBubble(a, pick(IDLE_CHATTER[a.idle] || IDLE_CHATTER.chat), 2800, true);
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
    this.showBubble(a, text, 5200, false);   // real telemetry — outranks idle chatter
  }

  // Live activity from inside a step ("editing src/store.py"): a short bubble + a
  // typing beat, so the sprite visibly does the thing. Ambient-priority — a real
  // journal line (agentSay) always wins the bubble.
  agentAct(name, text, room){
    const a = this.actorFor(name, room);
    if (!a) return;
    this.codeGlyph(a);
    this.showBubble(a, text, 2600, true);
  }
  // Name (or failing that, room) -> a sprite. The static rooms have a name->sprite
  // map; the engineering lanes don't — "Backend Engineer" is an IC in the backend
  // room and "Backend Lead" its lead — so those resolve by lane, and anything else
  // falls back to whoever is at a desk in the reported room.
  actorFor(name, room){
    const direct = this.agents.get(name);
    if (direct) return direct;
    const m = /^(Frontend|Backend|Data) (Lead|Engineer)$/.exec(name || "");
    const pick = (group, rm) => (group||[]).find(s => s && s.bob && s.room === rm) || null;
    if (m){
      const hit = pick(m[2] === "Lead" ? this.techLeadGroup : this.poolGroup,
                       m[1].toLowerCase());
      if (hit) return hit;
    }
    if (!room) return null;
    return pick(this.poolGroup, room) || pick(this.techLeadGroup, room);
  }

  // Show a speech bubble over anyone (agents, engineers, the CTO). `ambient` marks it
  // as idle flavour: ambient chatter never overwrites a real line, and is dropped the
  // moment its owner is called back to work.
  showBubble(a, text, ms, ambient){
    if (!a || this._dead) return;
    if (ambient && a.bubble) return;                        // don't talk over what's up
    this.clearBubble(a);
    a.bubble = this.makeBubble(a, text);
    a.bubbleAmbient = !!ambient;
    a.bubbleTimer = this.time.delayedCall(ms || 5200, ()=>{
      a.bubbleTimer = null;
      if (!a.bubble) return;
      this.tweens.add({targets:a.bubble, alpha:0, duration:250,
        onComplete:()=>{ if (a.bubble){ a.bubble.destroy(); a.bubble = null; } }});
    });
  }
  clearBubble(a, ambientOnly){
    if (!a || !a.bubble) return;
    if (ambientOnly && !a.bubbleAmbient) return;            // leave a real line alone
    if (a.bubbleTimer){ a.bubbleTimer.remove(); a.bubbleTimer = null; }
    a.bubble.destroy(); a.bubble = null; a.bubbleAmbient = false;
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
    const R = ROOMS[room] || ROOMS.plan;
    const w = Math.max(64, label.length*7+18);
    const c = this.add.container(R.cx, R.floorY+24).setDepth(2000);
    c.add(this.add.rectangle(0,0,w,18,0xfffdf3).setStrokeStyle(2,PC.ink));
    c.add(this.add.text(0,0,"+ "+label,{fontFamily:"'PixelFont','Courier New'",fontSize:"11px",color:"#1d7a3f"})
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
    const {atop, cx} = this.gateGeom;
    // Centred on the gate and clamped inside the world. All nine gates make a ~230px
    // strip, which ran off the right edge whenever the gate sits in the last column —
    // so the run is re-centred on every call and existing pips are moved with it.
    const present = GATE_ORDER.filter(n => this.gates[n]);
    const PITCH = 26, span = (present.length - 1) * PITCH;
    const x0 = Math.round(Math.min(Math.max(cx - span/2 - 10, 16), W - span - 46));
    let i = 0;
    for (const n of present){
      const g = this.gates[n];
      const sx = x0 + i*PITCH, sy = atop-12;
      if (!this.gatePips[n]){
        const c = this.add.container(sx, sy).setDepth(6);
        c.add(this.add.rectangle(10,4,20,18,PC.sceneInk));
        c.add(this.add.text(10,3,"",{fontFamily:"'PixelFont','Courier New'",fontSize:"12px",
          fontStyle:"bold",color:"#fff"}).setOrigin(0.5));
        c.add(this.add.rectangle(17,-3,5,5,0xffcf52).setVisible(false));
        this.gatePips[n] = c;
        c.setScale(0); this.tweens.add({targets:c, scale:1, duration:240, ease:"Back.out"});
      } else this.gatePips[n].setPosition(sx, sy);
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
    // The org chart used to hang on the CTO office back wall, but it cluttered the
    // room — the live org is reflected by who actually staffs each discipline room,
    // so no board is drawn here. (Kept as a no-op; setOrg still drives the rooms.)
    if (this.orgPanel){ this.orgPanel.destroy(); this.orgPanel = null; }
  }

  // Store the REAL org (areas + per-domain units), then populate the per-discipline
  // engineering ROOMS: all of an area's engineers — its Tech-Lead + expert ICs — live
  // in that area's room (frontend/backend/data), each on a colour carpet.
  setOrg(areas, domains){
    this.orgAreas = (areas||[]).filter(a=>a && a.area);
    this.orgDomains = domains||[];
    this.layoutEngineering();
  }

  // Per-area engineer counts: the IC pool distributed across the present areas ∝
  // each area's unit weight (largest-remainder, ≥1 where the pool allows). null when
  // there's no org telemetry yet.
  engineeringLanes(){
    const list = (this.orgAreas||[]).map(a=>a&&a.area).filter(Boolean);
    if(!list.length) return null;
    // Real per-area workload from the ownership map (units owned) = the IC demand.
    const units={}; list.forEach(a=>units[a]=0);
    for(const d of (this.orgDomains||[]))
      if(d && (d.area in units)) units[d.area] += (d.units&&d.units.length)?d.units.length:0;
    // Each role gets ONE required worker (its lead persona) — the always-on baseline.
    // On TOP of that sits the EXTRA pool (poolN): the additional workers we can afford,
    // which management hands out across roles by demand, deploying only as many as the
    // work needs. So total crew = (1 per role) + min(demand, extra budget): the baseline
    // grows on its own as the architect adds teams, and the extra number is what you set.
    const icSlots=Math.max(0, this.poolN||0);              // extra worker budget (on top of 1/role)
    const dem=list.map(a=>Math.max(0, units[a]));
    const totalDem=dem.reduce((s,n)=>s+n,0);
    const ics={}; list.forEach(a=>ics[a]=0);
    if(icSlots>0 && totalDem>0){
      if(totalDem<=icSlots){
        list.forEach((a,i)=>{ ics[a]=dem[i]; });           // room to spare: one IC per owned unit
      } else {                                              // oversubscribed: split slots by demand
        const rem=[]; let used=0;
        list.forEach((a,i)=>{ const raw=icSlots*dem[i]/totalDem; ics[a]=Math.floor(raw); used+=ics[a]; rem.push([a,raw-ics[a]]); });
        rem.sort((p,q)=>q[1]-p[1]);
        for(let k=0; used<icSlots && rem.length; k++,used++) ics[rem[k%rem.length][0]]++;
      }
    }
    return list.map(a=>({area:a, ics:ics[a]||0, color:AREA_COLOR[a]||0xffc08a}));
  }

  // Seat the engineers in their discipline ROOMS from current org + pool state.
  layoutEngineering(){
    // setGlow first: destroy() does NOT kill tweens targeting the object, so a lit
    // sprite would leave a repeat:-1 alpha tween writing to a dead target forever —
    // and this runs on every org event, so it accumulates.
    const kill=(s)=>{ if(!s) return; this.setGlow(s, false);
      s._dead=true; if(s.zzzEv){ s.zzzEv.remove(); s.zzzEv=null; }
      if(s.chair){ s.chair.destroy(); s.chair=null; } s.destroy(); };
    if(this.techLeadGroup) this.techLeadGroup.forEach(kill);
    if(this.poolGroup) this.poolGroup.forEach(kill);
    if(this.poolLabel){ this.poolLabel.destroy(); this.poolLabel=null; }
    if(this.engLabels) this.engLabels.forEach(o=>o.destroy());
    if(this.laneDecor) this.laneDecor.forEach(o=>o.destroy());
    this.techLeadGroup=[]; this.poolGroup=[]; this.laneDecor=[]; this.engLabels=[];

    const dist=this.engineeringLanes()||[];
    // Bucket each area's people (1 Tech-Lead + its ICs) into its discipline room.
    const perRoom={};
    for(const d of dist){
      const rk=ENG_AREA_ROOM[d.area]||"backend";
      (perRoom[rk]=perRoom[rk]||[]).push({area:d.area, color:d.color, lead:true});
      for(let i=0;i<d.ics;i++) perRoom[rk].push({area:d.area, color:d.color, lead:false});
    }
    // ALWAYS staff the three discipline lanes (frontend/backend/data) — the build graph
    // now has a lead + engineer node per lane, so each room shows its pair even when the
    // architect assigned that lane no ownership units (an empty lane just never lights).
    for(const rk of ENG_ROOMS){
      if(perRoom[rk] && perRoom[rk].length) continue;
      const col=AREA_COLOR[rk]||0xffc08a;
      perRoom[rk]=[{area:rk, color:col, lead:true}, {area:rk, color:col, lead:false}];
    }
    let slot=12;
    for(const rk in perRoom){
      const R=ROOMS[rk]; if(!R) continue;
      const people=perRoom[rk];
      const lead=people.find(p=>p.lead), ics=people.filter(p=>!p.lead);
      // create one seated worker (+desk) at (x,y); leads get the star + "<AREA> ENG" badge.
      const place=(p, x, y, faceUp)=>{
        x=Math.round(x); y=Math.round(y);
        const col6=p.color.toString(16).padStart(6,"0");
        const tag=AREA_TAG[p.area]||p.area.slice(0,2).toUpperCase();
        const k = p.lead ? kitFor("Tech-Lead "+p.area, hashStr((this.proj.id||'f')+"TL"+p.area)) : poolKit(slot);
        k.accent=p.color;
        const ws=this.makeWorkstation(x, y+16, p.color);
        const s=this.makePerson(x, y, k); s.setScale(p.lead?1.45:1.3).setDepth(y).setAlpha(p.lead?1:0.94);
        if (p.lead){
          s.add(this.add.text(0,-39,"★",{fontFamily:"'PixelFont','Courier New'",fontSize:"10px",
            color:"#ffd95e"}).setOrigin(0.5).setResolution(3));
          this.pinBadge(s, tag+" ENG", p.color, true);        // filled accent badge
          if (faceUp && s.faceBack) s.faceBack();              // overseeing — faces up into the room
        } else {
          this.pinBadge(s, tag, p.color, false);               // every IC labelled too
        }
        s.home={x,y}; s.room=rk; s.idleSlot=slot++; s.seen=true; s.atDesk=true;
        this.zzzLoop(s);
        // Spawn AT the desk either way — heads-down if this lane is live, otherwise
        // turned round with an idle mood.
        if (this.roomLive(rk)) this.deskRest(s, true, true); else this.standDown(s);
        (p.lead?this.techLeadGroup:this.poolGroup).push(s, ws);
      };
      // ONE consistent desk grid for every room: centred columns, first row well BELOW
      // the room label so it's never covered. (PX=column pitch, SY=first-row Y, RP=row pitch.)
      const PX=84, SY=R.y+94, RP=50;
      if (!ics.length && lead){
        place(lead, R.cx, Math.round(R.y + R.h*0.52), true);   // ONLY the lead → centre the desk
      } else {
        const c2=Math.min(3, Math.max(1, ics.length));
        ics.forEach((p,i)=>{ const c=i%c2, r=Math.floor(i/c2), bw=(c2-1)*PX;
          place(p, R.cx - bw/2 + c*PX, SY + r*RP, false); });   // centred IC grid (upper room)
        if (lead) place(lead, R.x1 - 50, R.y1 - 36, true);     // LEAD oversees from the bottom-right
      }
      this.engLabels.push(this.bm(R.x1-8, R.y+8, "×"+people.length, 11, 0xffc08a, 1, 0).setDepth(28));
    }
  }

  // No org telemetry yet — a flat pool in the BACKEND room (default engineering room).
  _flatPool(){
    const n=this.poolN; if(!n || n<=1) return;
    const R=ROOMS.backend; if(!R) return;
    const count=Math.min(n,8), cols=Math.min(count,3);
    for(let i=0;i<count;i++){
      const col=i%cols, row=Math.floor(i/cols);
      const x=Math.round(R.x + R.w*((col+1)/(cols+1)));
      const y=R.floorY+34+row*44;
      const k=poolKit(i);
      const ws=this.makeWorkstation(x, y+16, k.accent);
      const s=this.makePerson(x, y, k); s.setScale(1.4).setDepth(y).setAlpha(0.96);
      s.home={x,y}; s.room="backend"; s.idleSlot=12+i; s.seen=true; s.atDesk=true;
      this.zzzLoop(s);
      // At the desk either way: heads-down while backend is live, else an idle mood.
      if (this.roomLive("backend")) this.deskRest(s, true, true); else this.standDown(s);
      this.poolGroup.push(s, ws);
    }
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

// Rotating the phone swaps which floorplan fits, so re-lay the world and replay every
// floor scene onto it. A scene restart is already how a new release redraws a floor,
// so the floors rebuild from the state the client is holding.
window.matchMedia(PORTRAIT_MQ).addEventListener("change", ()=>{
  if (!applyFloorplan()) return;
  // setGameSize, not resize: resize() leaves displaySize's aspect ratio on the old
  // world, so FIT keeps letterboxing the new one to the previous shape.
  GAME.scale.setGameSize(W, H);
  for (const F of FLOORS.values()){
    const sc = GAME.scene.getScene(floorKey(F.floor));
    if (sc) sc.scene.restart({floor:F.floor, id:F.id, request:F.request, mode:F.mode, slots:F.slots});
  }
  // resize() keeps the FIT computed for the OLD panel box, and the box changes on this
  // same media query — so re-fit once the new one has been laid out.
  requestAnimationFrame(refitGame);
});
// A ResizeObserver fires once the moment it is attached, before Phaser has made its
// canvas, and refresh() dereferences that canvas — hence the guard.
function refitGame(){ if (GAME.scale && GAME.scale.canvas) GAME.scale.refresh(); }
new ResizeObserver(refitGame).observe(document.getElementById("game"));

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
  const openStream = (F) => openFloorStream(F);
  const existing = FLOORS.get(floor);
  if (existing){
    // NEW RELEASE on an existing project: reset per-release state + replay the scene.
    if (existing.es){ try{ existing.es.close(); }catch(_){} }
    Object.assign(existing, {
      id:resp.id, release, releases:(existing.releases||1)+1, request:req, mode, backend,
      log:[], progress:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
      slots, journal:[], seenUris:new Set(), _agrp:null, _svcOpen:null,
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
    log:[], progress:[], gates:{}, org:null, cp:null, awaiting:false, status:"running", passed:false,
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
  updateLiveDot();
  for (const f of FLOORS.keys()){
    const sc = GAME.scene.getScene(floorKey(f));
    if (sc && sc.sys) sc.sys.setVisible(f === floor);
  }
  GAME.scene.bringToTop(floorKey(floor));
  const F = FLOORS.get(floor);
  rebuildLog(F);
  rebuildProgress(F);
  renderGatesMini(F.gates);
  if (F.platprog) renderPlatProg(F.platprog);   // platform floors show service chips
  updateHud(F);
  if (F.awaiting) showCheckpoint(F); else hideCheckpoint();
  renderElevator();
}

// ---- per-floor event routing ----------------------------------------------
// Rooms and cast labels from OLD builds' stored/replayed telemetry -> the current floor
// (product/architecture -> plan, qa -> review; the per-role agents -> Planner/Reviewer,
// each lane's lead -> that lane's Engineer).
const LEGACY_ROOM = {product:"plan", architecture:"plan", qa:"review"};
const LEGACY_AGENT = {
  "Stakeholder Proxy":"Planner","Product Manager":"Planner","Criteria Critic":"Planner",
  "Program Manager":"Planner","Principal Architect":"Planner",
  "QA Engineer":"Reviewer","SDET":"Reviewer","Technical Writer":"Reviewer",
  "Frontend Lead":"Frontend Engineer","Backend Lead":"Backend Engineer","Data Lead":"Data Engineer",
};
function modernEvent(ev){
  const room = LEGACY_ROOM[ev.room], agent = LEGACY_AGENT[ev.agent];
  if (!room && !agent) return ev;
  return Object.assign({}, ev, room ? {room} : {}, agent ? {agent} : {});
}
function routeEvent(floor, ev){
  const F = FLOORS.get(floor); if (!F) return;
  ev = modernEvent(ev);
  const S = F.scene, isActive = floor === ACTIVE;
  switch(ev.type){
    case "project":
      F.request = ev.request; F.mode = ev.mode; F.backend = ev.backend || F.backend;
      F.slots = ev.slots || 1;
      if (S) S.setPool(F.slots);
      if (isActive) updateHud(F);
      break;
    case "narrate": logNarrate(F, ev.text); break;
    case "svc": logService(F, ev); break;   // platform: one box per service, updated in place
    case "limitwait":              // provider usage limit: global pause banner
      showLimitToast(ev.until);
      break;
    case "platprog":               // platform: per-service progress for the HUD
      F.platprog = ev;
      if (isActive) renderPlatProg(ev);
      break;
    case "rebuild":                // a service is deepening — pulse its room
      if (S && ev.room) S.pulseRoom(ev.room);
      break;
    case "stage":
      if (S) S.setActiveRoom(ev.room, true);   // new phase boundary → reset the live crew
      resetStage(F);                            // new stage → fresh artifact/journal buckets
      logTo(F, "gold", "── "+ev.label+" ──"); break;   // Progress gets the stage's ✓ cap at its checkpoint
    case "agent": if (S) S.agentWork(ev.agent); break;
    case "active": {
      // LIVE per-node telemetry: a real graph node STARTED (seat its agent for the whole
      // model call) or FINISHED (clear them, with a summary). Only in-flight agents sit,
      // so the office shows who is genuinely working RIGHT NOW — not the whole room.
      const phase = ev.phase || "pulse";     // legacy no-phase "active" = a room pre-seat
      if (S) S.nodeTelemetry(ev.room, ev.agent, phase);
      // `quiet` = floor-only telemetry: a platform build already narrates every node as a
      // per-service `svc` box, so logging it again here would double every line.
      if (ev.quiet) break;
      if (phase === "finish"){
        if (ev.agent) agentActivity(F, ev.room, ev.agent, true, ev.summary);   // box → done
        else if (ev.step) logTo(F, "good", "✓ "+ev.step);
        else if (ev.summary) logTo(F, "good", "✓ "+ev.summary);
      } else if (phase !== "pulse" && ev.agent){
        agentActivity(F, ev.room, ev.agent, false);   // START opens the agent's box (its header)
        F.liveRoom = ev.room; F.liveAgent = ev.agent;
      } else if (phase !== "pulse" && ev.step){
        // A desk-less STEP (a gate run, a fix-forward pass, the doc pass). These used
        // to move the floor and log nothing, which is what made the log look asleep
        // while the crew visibly re-staffed around it.
        logTo(F, "hot", "⋯ "+ev.step);
        F.liveRoom = ev.room;
      }
      break;
    }
    case "act":
      // LIVE ACTIVITY from inside a step: one short phrase per tool call the coding
      // substrate runs ("editing src/store.py", "running pytest -q"). A step is one
      // blocking call, so this is the only thing that reports while it's in flight.
      logAct(F, ev);
      if (S) S.agentAct(ev.agent, ev.text, ev.room);
      break;
    case "say":
      if (S) S.agentSay(ev.agent, ev.text);
      logSay(F, ev);   // fold the journal note onto the agent's artifact group + stage summary
      if (ev.concern) logTo(F, "", `${ev.agent}: "${ev.concern}"`); break;
    case "artifact":
      if (S){ S.spawnPickup(ev.room, ev.label); const pa=S.agents.get(ev.agent);
        if (pa){ S.hop(pa,1); const R=ROOMS[ev.room]; if(R) S.burst(R.cx, R.floorY+20, [0xffd95e,0xffffff], 8); } }
      logArtifact(F, ev); break;   // same-kind artifacts collapse into one expandable row
    case "gate":
      F.gates[ev.name] = {status:ev.status, attested:ev.attested, advisory:ev.advisory,
                          run_against:ev.run_against||""};
      if (S){ S.setGate(ev.name, F.gates[ev.name]);
        const g=S.gateGeom; if(g){ const col=ev.status==="pass"?0x5ddc7a:0xff5d6c;
          S.ring(g.cx,(g.atop+g.abot)/2,col); S.burst(g.cx,(g.atop+g.abot)/2,[col,0xffffff],8); } }
      if (isActive) renderGatesMini(F.gates);
      if (!ev.advisory) logTo(F, ev.status==="pass"?"good":"bad",
        `GATE ${ev.name.toUpperCase()}: ${ev.status.toUpperCase()}${ev.attested?" (signed)":""}`);
      break;
    case "org":
      if (S){ S.renderOrg(ev); S.setOrg(ev.areas, ev.domains);
        if (S.activeRoomKey) S.staffRoom(S.activeRoomKey); }   // lane-lay engineering by area
      logTo(F, "gold", "ORG · "+(ev.areas||[]).map(a=>`${a.area}(${(a.leads||[]).length})`).join("  "));
      break;
    case "files":
      break;   // file count is read live from the repo view, not cached on F
    case "checkpoint": {
      // `phase`/`route`/`state_sha` are the pause's provenance (WHERE in the pipeline,
      // WHICH state); `actions` is the per-button consequence copy from the server.
      F.cp = {title:ev.title, summary:ev.summary, boundary:ev.boundary,
              phase:ev.phase||"", route:ev.route||"", state_sha:ev.state_sha||"",
              actions:ev.actions||null};
      // Stage outcome -> the Progress tab as a COLLAPSIBLE row: the summary states what
      // was completed (the artifact breakdown), and expanding lists it + the agents'
      // journal notes (what specifically each lead produced) + the raw count.
      const bd = kindBreakdown(F.stageArts);
      const desc = (ev.summary||"").trim();   // T3 descriptive caption of the stage
      const head = "✓ "+(ev.title||ev.boundary||"stage")+" finished"
        + (desc ? " — "+desc : (bd.length ? " — "+bd.join(" · ") : ""));
      // Body: the itemised breakdown + each agent's descriptive caption (not a repeat of
      // the header — the header is the T3 prose, the body is the specifics).
      const lines=[];
      if(bd.length) lines.push("Produced: "+bd.join(", "));
      const says=F.stageSays||{};
      for(const ag in says) lines.push(ag+" — "+says[ag]);
      logProgress(F, "good", head, lines); break;
    }
    case "await_decision":
      F.awaiting = true; F.cpIndex = ev.checkpoint;
      // AI expert advisors (gated release checkpoint): ride into the modal. The
      // NOTE says why the panel is absent when it is — an empty panel must not read
      // as "everyone approved". `spend` is the advisory cost/elapsed context.
      F.cp = Object.assign({}, F.cp, {advisors: ev.advisors || [],
                                      advisorsNote: ev.advisors_note || "",
                                      spend: ev.spend || null,
                                      // the stage's artifacts are frozen at the pause
                                      arts: (F.stageArts || []).slice()});
      // Autopilot PROPOSAL: carry the panel's request so the modal offers an edit box.
      if (ev.boundary === "autopilot_proposal")
        F.cp = Object.assign({}, F.cp, {boundary: ev.boundary, request: ev.request || ""});
      if (Array.isArray(ev.journal)) F.journal = ev.journal;
      renderElevator();
      if (S){ S.spotlightCTO(true); S.standDownAll(); }   // whole floor off duty while the CTO decides
      if (isActive) showCheckpoint(F); break;
    case "resolved": {
      F.awaiting = false; renderElevator();
      // three-way verdict: approve (green) · needs_changes (gold) · reject (red)
      const ap = ev.decision === "approve", ch = ev.decision === "needs_changes";
      const txt = ap?"APPROVED":(ch?"CHANGES":"REJECTED");
      logProgress(F, ap?"good":(ch?"hot":"bad"), "CTO "+txt);
      if (S){ S.spotlightCTO(false);
        if (S.activeRoomKey) S.staffRoom(S.activeRoomKey);   // back to work
        const col = ap?0x5ddc7a:(ch?0xffcf52:0xff5d6c);
        const hex = ap?"#9dffbf":(ch?"#ffe6a3":"#ffb3ba");
        S.hop(S.cto, ap?2:1); S.ring(S.cto.x, S.cto.y-10, col);
        S.burst(S.cto.x, S.cto.y-10, [col,0xffffff], 12);
        S.floatText(S.cto.x, S.cto.y-30, txt, hex, 13);
        if (ev.decision === "reject") S.shake(280,0.01); }
      if (isActive) hideCheckpoint(); break;
    }
    case "release":
      if (S){ S.openGateAndChest(); S.stampGate(true); S.celebrate(); S.banner("SHIPPED!", 0x5ddc7a); }
      logTo(F, "gold", "RELEASED -> "+(ev.uri||"artifact"));
      logProgress(F, "good", "★ SHIPPED — "+(ev.uri||"artifact")); break;
    case "halt":
      if (S){ S.flashGate(); S.stampGate(false); }
      logTo(F, "bad", "HELD: "+ev.reason);
      logProgress(F, "bad", "✕ HELD — "+ev.reason); break;
    case "done":
      F.status = "done"; F.passed = !!ev.passed; renderElevator();
      // Build's over: nobody is working, so everyone heads to the LOUNGE
      // (staffRoom(null) marks no room active and routes idle folks there).
      if (S){ S.staffRoom(null); if (!ev.passed){ S.gloom(); S.banner("HELD AT THE GATE", 0xff5d6c); } }
      logTo(F, ev.passed?"good":"bad",
        ev.passed?"Build complete — shipped.":"Build halted — nothing shipped.");
      logProgress(F, ev.passed?"good":"bad",
        ev.passed?"■ Build complete — shipped.":"■ Build halted — nothing shipped."); break;
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
// Provider-limit pause banner: the account's usage window ran out and the build
// is sleeping until it resets. One global toast (the limit throttles every
// floor), live countdown, dismissible, auto-clears at the resume time. Replayed
// stale events are ignored (until <= now).
function showLimitToast(until){
  const now = Date.now()/1000;
  if (!until || until <= now) return;
  let t = document.getElementById("limit-toast");
  if (!t){
    t = document.createElement("div"); t.id = "limit-toast";
    const msg = document.createElement("span"); msg.id = "limit-toast-msg";
    const x = document.createElement("button"); x.textContent = "✕";
    x.onclick = ()=>{ if (t._iv) clearInterval(t._iv); t.remove(); };
    t.appendChild(msg); t.appendChild(x);
    document.body.appendChild(t);
  }
  t._until = Math.max(t._until || 0, until);
  const upd = ()=>{
    const left = t._until - Date.now()/1000;
    if (left <= 0){ if (t._iv) clearInterval(t._iv); t.remove(); return; }
    const hhmm = new Date(t._until*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
    const el = document.getElementById("limit-toast-msg");
    if (el) el.textContent = "⏸ USAGE LIMIT — BUILD PAUSED · resumes ~"+hhmm
      +" ("+Math.ceil(left/60)+" min)";
  };
  upd();
  if (!t._iv) t._iv = setInterval(upd, 30000);
}

// Stream-health dot in the QUEST LOG title. The server sends a named `ka`
// heartbeat every ~15s of idle (real events count too), so "no traffic for 40s"
// means the stream is actually down — not just a long, quiet model call.
function updateLiveDot(){
  const el = document.getElementById("log-live"); if (!el) return;
  const F = FLOORS.get(ACTIVE);
  if (!F || F.status !== "running"){ el.className="log-live"; el.textContent=""; return; }
  const fresh = F.lastBeat && (Date.now() - F.lastBeat) < 40000;
  el.className = "log-live " + (fresh ? "on" : "stale");
  // Healthy = just the pulsing dot (the ● LIVE tab right below already says the
  // word, and two "LIVE"s side by side read as one broken label). Trouble is loud.
  el.textContent = fresh ? "" : "RECONNECTING…";
  el.title = fresh ? "Stream connected — quiet stretches are the model thinking."
                   : "No data for 40s — check the connection / server.";
}
setInterval(updateLiveDot, 5000);
// A log/progress row. With no `lines` it's a plain <li>; with `lines` it's a
// collapsible <details> whose SUMMARY (always visible, even collapsed) carries the
// short "what was completed" statement, and whose body lists the specifics.
function rowNode(cls, headline, lines){
  const li=document.createElement("li"); li.className=cls;
  if(!lines || !lines.length){ li.textContent=deEmoji(headline); return li; }
  const d=document.createElement("details");
  const s=document.createElement("summary"); s.textContent=deEmoji(headline); d.appendChild(s);
  const ul=document.createElement("ul"); ul.className="row-detail";
  for(const ln of lines){ const l=document.createElement("li"); l.textContent=deEmoji(ln); ul.appendChild(l); }
  d.appendChild(ul); li.appendChild(d); return li;
}
function artBase(label){ return (String(label||"").replace(/\s*#\d+\s*$/,"").trim()) || "Artifact"; }
// ---- the log FOLLOWS new activity ------------------------------------------
// Every mutation of the live log goes through this: it measures whether you were
// already at the bottom BEFORE the DOM grows, then scrolls to the new bottom if you
// were. Boxes grow in place as their agent works (a new "· editing x.py" line inside
// an existing box), so scrolling only on append left the newest lines under the fold —
// which is what made the log look stuck. If you HAVE scrolled up, we leave the view
// where you put it and raise the jump-to-latest pill instead.
const LOG_PIN_SLACK = 48;   // px from the bottom that still counts as "at the bottom"
// Following = you're parked at the bottom. Measured from the live scroll position on
// every mutation, so a growing box never silently unpins the view and scrolling up to
// read something never gets yanked back down. Only kept as state to survive the log
// being hidden (the PROGRESS tab), where nothing is measurable.
let logFollow = true;
function logAtBottom(ul){
  return ul.scrollHeight - ul.scrollTop - ul.clientHeight <= LOG_PIN_SLACK;
}
function showLogJump(on){
  const b=document.getElementById("log-jump"); if(!b) return;
  b.classList.toggle("hidden", !on);
}
// Run a mutation of the live log and, if we're following, reveal the new bottom.
// Hidden (the PROGRESS tab is showing) => every measurement is 0, so touching
// scrollTop would silently rewind the live list to the top: leave it alone and let
// selectLogTab re-pin when the tab comes back.
function inLog(mutate){
  const ul=document.getElementById("log");
  if(!ul){ mutate(null); return; }
  // Re-read the position rather than trusting the scroll listener: `scroll` fires on
  // the NEXT frame, so a mutation landing in the same tick as your scroll would still
  // see the stale value and yank you back down.
  if(ul.clientHeight) logFollow=logAtBottom(ul);
  mutate(ul);
  if(!ul.clientHeight) return;
  if(logFollow) ul.scrollTop=ul.scrollHeight;
  showLogJump(!logFollow);
}
function scrollLogToEnd(){
  const ul=document.getElementById("log");
  logFollow=true;
  if(ul && ul.clientHeight) ul.scrollTop=ul.scrollHeight;
  showLogJump(false);
}
{ const ul=document.getElementById("log"), jump=document.getElementById("log-jump");
  if(ul) ul.addEventListener("scroll", ()=>{
    if(!ul.clientHeight) return;
    logFollow=logAtBottom(ul);
    showLogJump(!logFollow);
  });
  if(jump) jump.onclick=scrollLogToEnd; }
function appendLogDom(cls, text, lines){
  inLog((ul)=>{
    if(!ul) return;
    ul.appendChild(rowNode(cls, text, lines));
    // Trimming the head removes content ABOVE the viewport, so a reader who isn't
    // following would watch the text slide up under their eyes on every event once
    // the log is at cap. Give back exactly the height that was cut.
    const before=ul.scrollHeight;
    while(ul.children.length>240) ul.removeChild(ul.firstChild);
    if(!logFollow) ul.scrollTop=Math.max(0, ul.scrollTop-(before-ul.scrollHeight));
  });
}
function logTo(F, cls, text, lines){
  F.log.push({cls, text, lines:(lines&&lines.length)?lines:null});
  if (F.log.length > 240) F.log.shift();
  if (F.floor === ACTIVE) appendLogDom(cls, text, lines);
}
// A narrate line. If an agent is CURRENTLY working (and this isn't a system/status
// message), it's that agent's output — fold it into their box body. Otherwise it's a
// standalone system line (collapsible if long).
const _SYS_NARRATE=/no step|usage limit|sleeping|paused|reconnect|drafting has begun|reports for duty|new project on floor|mode:/i;
function logNarrate(F, text){
  const t=String(text||"").trim();
  if(F.curAgent && F._agrp && F._agrp[F.curAgent] && !_SYS_NARRATE.test(t)){
    const g=F._agrp[F.curAgent]; g.narrates.push(deEmoji(t));
    if(F.floor===ACTIVE) refreshAgentBox(g);
    return;
  }
  const cls=logClass(t);
  let head=t, detail="";
  const d=t.indexOf(" — ");
  if(d>0 && (t.length-d)>32){ head=t.slice(0,d); detail=t.slice(d+3).trim(); }
  else if(t.length>110){ const p=t.slice(0,110).lastIndexOf(". ");
    const cut=p>40?p+1:110; head=t.slice(0,cut).trim(); detail=t.slice(cut).trim(); }
  logTo(F, cls, head, detail ? [detail] : null);
}
// Group a set of {base} items into "PRD · Acceptance Criterion ×18 · Sprint Plan".
function kindBreakdown(items){
  const order=[], c={};
  for(const it of (items||[])){ const b=it.base; if(!(b in c)){ order.push(b); c[b]=0; } c[b]++; }
  return order.map(b=> b+(c[b]>1?" ×"+c[b]:""));
}
// EVENT LOG = one bordered BOX per agent. The box HEADER is the activity line
// ("▸ PLAN · Planner working" → "✓ …" when done); the BODY holds that agent's
// response — a descriptive caption (their T3/journal note) plus what they produced (their
// narrate output and each artifact with its content one-liner). One border around the
// whole thing; the response is shown, not hidden behind a disclosure.
function agentHeadText(g){
  return (g.done?"✓ ":"▸ ")+(g.room?g.room.toUpperCase()+" · ":"")+g.agent+(g.done?"":" working");
}
function renderAgentBox(g){
  const li=document.createElement("li"); li.className="agent-box"+(g.done?" done":"");
  const head=document.createElement("div"); head.className="agent-head";
  head.textContent=deEmoji(agentHeadText(g)); li.appendChild(head);
  const cap=g.cap||g.fallback;
  const acts=(g.done?[]:(g.acts||[]));      // live activity only while the step runs
  const body=(cap?1:0)+g.narrates.length+g.items.length+acts.length;
  if(body){
    const bd=document.createElement("div"); bd.className="agent-body";
    if(cap){ const c=document.createElement("div"); c.className="agent-cap"; c.textContent=deEmoji(cap); bd.appendChild(c); }
    for(const nr of g.narrates){ const d=document.createElement("div"); d.className="agent-item"; d.textContent=deEmoji(nr); bd.appendChild(d); }
    // what it's doing RIGHT NOW, newest last (dropped once the step reports its result)
    for(const a of acts){ const d=document.createElement("div"); d.className="agent-item act";
      d.textContent="· "+deEmoji(a); bd.appendChild(d); }
    for(const it of g.items){ const d=document.createElement("div"); d.className="agent-item";
      d.textContent=deEmoji(it.label+(it.detail?" — "+it.detail:"")); bd.appendChild(d); }
    li.appendChild(bd);
  }
  return li;
}
function refreshAgentBox(g){
  if(!g.el) return;
  inLog(()=>{ const n=renderAgentBox(g); g.el.replaceWith(n); g.el=n; });
}
// Find-or-create the agent's box (keyed by agent for the whole build), appended in the
// order the agent first became active — so the box header IS the "… working" line and
// its body fills in beneath as the agent narrates / finishes / its artifacts land.
function agentBox(F, agent){
  F._agrp=F._agrp||{};
  let g=F._agrp[agent];
  if(!g){
    g={box:true, agent, room:"", done:false, cap:"", fallback:"", narrates:[], items:[]};
    F._agrp[agent]=g; F.log.push(g); if(F.log.length>240) F.log.shift();
    if(F.floor===ACTIVE) inLog((ul)=>{ if(!ul) return; g.el=renderAgentBox(g); ul.appendChild(g.el); });
  }
  return g;
}
// active telemetry: START opens the agent's box (header shows "… working"); FINISH marks
// it done and seeds a fallback caption (the T3 caption overrides it later, via `say`).
function agentActivity(F, room, agent, done, summary){
  if(!agent) return;
  const g=agentBox(F, agent);
  if(room) g.room=room;
  if(done){ g.done=true; if(summary && !g.fallback) g.fallback=summary; }
  else F.curAgent=agent;
  if(F.floor===ACTIVE) refreshAgentBox(g);
}
// LIVE activity inside a step: fold it into the working agent's OPEN box, so the box
// grows as the agent works ("· editing src/store.py") instead of the log sitting still
// for the whole call. No open box (an infra step, or a finished agent) -> a dim line.
// Only the last few are kept per box: the point is "it's moving", not a full transcript.
function logAct(F, ev){
  const t = String(ev.text || "").trim(); if(!t) return;
  // Only the agent the SERVER attributed it to — an empty agent means the running step
  // has no desk (a gate, a fix pass), so it must not be filed under whoever went last.
  const g = ev.agent && F._agrp && F._agrp[ev.agent];
  if (g && !g.done){
    g.acts = g.acts || [];
    g.acts.push(t);
    if (g.acts.length > 6) g.acts.shift();
    if (F.floor === ACTIVE) refreshAgentBox(g);
    return;
  }
  logTo(F, "", "· "+t);
}
// An artifact: fold its CONTENT into its producing agent's box body.
function logArtifact(F, ev){
  const base=artBase(ev.label), agent=ev.agent||"(system)", detail=ev.detail||"";
  (F.stageArts=F.stageArts||[]).push({kind:ev.kind||base, base, label:ev.label, agent, detail});
  if((ev.kind||"")==="feature_diff") F.hasCode=true;   // code exists -> the checkpoint can offer to show it
  const g=agentBox(F, agent);
  g.items.push({base, label:ev.label, detail});
  if(F.floor===ACTIVE) refreshAgentBox(g);
}
// A journal note (or its T3 caption): the descriptive response line for the agent's box.
function logSay(F, ev){
  F.stageSays=F.stageSays||{};
  if(ev.text) F.stageSays[ev.agent]=ev.text;
  const g=(F._agrp||{})[ev.agent];
  if(g && ev.text && g.cap!==ev.text){ g.cap=ev.text; if(F.floor===ACTIVE) refreshAgentBox(g); }
}
// New STAGE: reset only the per-stage buckets that feed the PROGRESS summary; the agent
// boxes are build-global (each agent appears once, filling in as its work lands).
function resetStage(F){ F.stageArts=[]; F.stageSays={}; }
// ---- Platform EVENT boxes: one bordered box PER pipeline event -----------------
// A platform build streams each service's pipeline as `svc` events. Each event is its
// OWN box (not one box per service): a stage OPENS a box when it starts (header
// "▸ WEB · designing the system", empty body) and the box is FINALISED with what it
// did (green "✓ …" header + a one-line body) when that stage ends. Service kickoff,
// each pass result, rebuilds, critiques and the service's completion each land as
// their own box too — so the log reads as a running list of started/finished events.
// The "more details" that live behind a box's caret — the specifics the agent recorded.
function _evExtras(g){
  const x=[];
  for(const a of (g.arts||[])){                 // WHAT the agent produced, content first
    const label=String(a.kind||"").replace(/_/g," ");
    if(a.detail) x.push("⊞ "+label+": "+a.detail);
    else x.push("⊞ "+label+(a.n>1?" ×"+a.n:""));
  }
  for(const c of (g.concerns||[])) x.push("⚠ "+c);
  for(const a of (g.assumptions||[])) x.push("• "+a);
  for(const b of (g.body||[])) x.push(b);
  return x;
}
function renderEvBox(g){
  const li=document.createElement("li");
  li.className="agent-box ev-box"+(g.tone==="good"?" done":g.tone==="bad"?" held":"");
  const live = (g.kind==="agent"||g.kind==="stage");   // ▸ while working, ✓ once done
  const headText = live ? ((g.done?"✓ ":"▸ ")+g.title) : g.title;
  const main = g.summary || g.did || "";     // the SPECIFIC, always-visible context line
  const extras = _evExtras(g);               // the MORE details, revealed by the caret
  if(extras.length){                          // collapsible: a chevron opens the specifics
    const d=document.createElement("details"); d.className="ev-det";
    const s=document.createElement("summary"); s.className="ev-head";
    s.textContent=deEmoji(headText+(main?" — "+main:""));
    d.appendChild(s);
    const bd=document.createElement("div"); bd.className="agent-body";
    for(const ln of extras){ const e=document.createElement("div"); e.className="agent-item";
      e.textContent=deEmoji(ln); bd.appendChild(e); }
    d.appendChild(bd); li.appendChild(d);
  } else {                                     // no extra specifics → header (+ the main line)
    const head=document.createElement("div"); head.className="agent-head";
    head.textContent=deEmoji(headText); li.appendChild(head);
    if(main){ const bd=document.createElement("div"); bd.className="agent-body";
      const e=document.createElement("div"); e.className="agent-item"; e.textContent=deEmoji(main);
      bd.appendChild(e); li.appendChild(bd); }
  }
  return li;
}
function refreshEvBox(g){
  if(!g.el) return;
  inLog(()=>{ const n=renderEvBox(g); g.el.replaceWith(n); g.el=n; });
}
// Append one event box (every event is a fresh box).
function pushEvBox(F, g){
  g.evbox=true; g.body=g.body||[]; g.assumptions=g.assumptions||[]; g.concerns=g.concerns||[]; g.arts=g.arts||[];
  F.log.push(g); if(F.log.length>240) F.log.shift();
  if(F.floor===ACTIVE) inLog((ul)=>{ if(!ul) return; g.el=renderEvBox(g); ul.appendChild(g.el); });
  return g;
}
// Fold an agent's reported specifics (journal summary + assumptions/concerns/artifacts)
// into its box.
function _mergeNote(g, ev){
  if(ev.summary) g.summary=ev.summary;
  for(const c of (ev.concerns||[])) if(g.concerns.indexOf(c)<0) g.concerns.push(c);
  for(const a of (ev.assumptions||[])) if(g.assumptions.indexOf(a)<0) g.assumptions.push(a);
  // Each agent box gets ONE note carrying its produced artifacts (kind + content
  // one-liner) — set them directly (the last note wins if a node re-runs).
  if(ev.artifacts && ev.artifacts.length) g.arts=ev.artifacts.slice(0, 12);
}
// Finalise any of a service's still-open agent boxes (e.g. when a pass or the build ends).
function closeOpenAgents(F, svc){
  const o=F._svcOpen||{}, pfx=svc+"|";
  for(const k in o){ const g=o[k];
    if(k.indexOf(pfx)===0 && g && !g.done){ g.done=true; g.tone="good"; if(F.floor===ACTIVE) refreshEvBox(g); o[k]=null; } }
}
// Apply one `svc` event → a box. EACH AGENT ACTION is its own box: it opens ("▸ working")
// when the agent's node starts and fills ("✓ …") with what it did when the node finishes —
// its journal summary (else a role verb), with assumptions/concerns/artifacts behind the
// caret. Service kickoff, passes, rebuilds, the critic and completion are their own boxes.
function logService(F, ev){
  const svc=ev.service||"service", SVC=svc.toUpperCase();
  F._svcOpen=F._svcOpen||{};
  switch(ev.phase){
    case "start":
      pushEvBox(F, {kind:"marker", tone:"", done:true,
        title:"▶ "+SVC+" — building"+(ev.desc?" "+ev.desc:"")});
      break;
    case "agent_start": {                 // an agent BEGAN its step → open its box
      const key=svc+"|"+(ev.node||ev.agent);
      F._svcOpen[key]=pushEvBox(F, {kind:"agent", tone:"", done:false,
        title:SVC+" · "+(ev.agent||"agent")});
      break;
    }
    case "agent_done": {                  // the agent FINISHED → fill its box with what it did
      const key=svc+"|"+(ev.node||ev.agent);
      let g=(F._svcOpen||{})[key];
      if(!g) g=pushEvBox(F, {kind:"agent", tone:"", done:false, title:SVC+" · "+(ev.agent||"agent")});
      _mergeNote(g, ev);
      if(!g.summary && ev.did) g.summary=ev.did;   // fall back to the role verb
      g.done=true; g.tone="good";
      if(F._svcOpen) F._svcOpen[key]=null;
      if(F.floor===ACTIVE) refreshEvBox(g);
      break;
    }
    case "rebuild":
      closeOpenAgents(F, svc);
      pushEvBox(F, {kind:"marker", tone:"", done:true,
        title:"↻ "+SVC+" — rebuilding · pass "+(ev.passno||"?")});
      break;
    case "pass_result":
      closeOpenAgents(F, svc);
      pushEvBox(F, {kind:"marker", tone:ev.passed?"good":"bad", done:true,
        title:(ev.passed?"✓ ":"✗ ")+SVC+" — pass "+(ev.passno||"?")
          +(ev.passed?" · gates green":" · held at the gate")});
      break;
    case "critic":
      pushEvBox(F, {kind:"marker", tone:ev.complete?"good":"", done:true,
        title:ev.complete? "✦ "+SVC+" — approved: technically complete"
          : "• "+SVC+" — needs more work",
        did: ev.complete? "" : (ev.detail||"")});
      break;
    case "done":
      closeOpenAgents(F, svc);
      pushEvBox(F, {kind:"marker", tone:ev.passed?"good":"bad", done:true,
        title:(ev.passed?"✓ ":"✗ ")+SVC+" — "+(ev.passed?"built":"held at the gate")
          +" · "+(ev.passes||1)+" pass"+((ev.passes||1)>1?"es":""),
        body:(ev.files||[]).map(f=>"⊞ "+svc+"/"+f)});
      break;
  }
}
function rebuildLog(F){
  const ul=document.getElementById("log"); ul.innerHTML="";
  for (const e of F.log){
    if(e.box){ e.el=renderAgentBox(e); ul.appendChild(e.el); }
    else if(e.evbox){ e.el=renderEvBox(e); ul.appendChild(e.el); }
    else ul.appendChild(rowNode(e.cls, e.text, e.lines||null));
  }
  scrollLogToEnd();          // a floor switch always lands you on the newest activity
}
// The high-level PROGRESS tab: one collapsible row per stage / milestone. The summary
// carries the short "what was completed" statement; expanding shows the specifics.
function logProgress(F, cls, text, lines){
  (F.progress = F.progress || []).push({cls,text,lines:(lines&&lines.length)?lines:null});
  if (F.progress.length > 160) F.progress.shift();
  if (F.floor === ACTIVE){
    const ul=document.getElementById("progress"); if (!ul) return;
    ul.appendChild(rowNode(cls, text, lines)); ul.scrollTop=ul.scrollHeight;
    while(ul.children.length>160) ul.removeChild(ul.firstChild);
  }
}
function rebuildProgress(F){
  const ul=document.getElementById("progress"); if (!ul) return; ul.innerHTML="";
  for (const e of (F.progress||[])) ul.appendChild(rowNode(e.cls, e.text, e.lines));
  ul.scrollTop = ul.scrollHeight;
}
// Switch the quest-log panel between the LIVE feed and the PROGRESS skim.
function selectLogTab(tab){
  const F = FLOORS.get(ACTIVE);
  document.querySelectorAll(".log-tab").forEach(b=>b.classList.toggle("sel", b.dataset.tab===tab));
  const live=document.getElementById("log"), prog=document.getElementById("progress");
  if (live) live.style.display = tab==="progress" ? "none" : "";
  if (prog) prog.style.display = tab==="progress" ? "" : "none";
  if (F && tab==="progress") rebuildProgress(F);
  // Nothing was measurable while the live list was hidden, so restore the state the
  // tab was left in: re-pin if following, otherwise put the pill back.
  if (tab==="progress") showLogJump(false);
  else if (logFollow) scrollLogToEnd();
  else showLogJump(true);
}
function renderGatesMini(gates){
  const el=document.getElementById("gates-mini"); el.innerHTML="";
  for(const n of GATE_ORDER){ const g=gates[n]; if(!g) continue;
    const s=document.createElement("span"); s.className="g "+(g.status); s.textContent=n;
    el.appendChild(s);}
}
// Platform builds: one chip per service in the HUD, coloured by lifecycle state,
// with a "done/total" tally. Overrides the (empty) gate chips for a platform floor.
function renderPlatProg(pp){
  const el=document.getElementById("gates-mini"); if(!el) return;
  if(!pp || !pp.services){ return; }
  el.innerHTML="";
  const names=Object.keys(pp.services); let done=0;
  for(const name of names){
    const st=pp.services[name];
    if(st==="done"||st==="approved") done++;
    const s=document.createElement("span");
    s.className="g plat "+st; s.textContent=name; s.title=name+" — "+st;
    el.appendChild(s);
  }
  const tot=document.createElement("span");
  tot.className="g plat-count"; tot.textContent=done+"/"+names.length+" ✓";
  el.appendChild(tot);
}
function updateHud(F){
  document.getElementById("floor-tag").textContent =
    "FLOOR "+F.floor + (F.release>1 ? " · r"+F.release : "");
  // A milestone build's request is the model's prompt — the root request with
  // " — MILESTONE i/n: <title>" appended — so it runs to hundreds of characters.
  // Split it back into identity + current milestone; portrait gives each its own
  // line, desktop rejoins them with the em dash from .proj-mile::before.
  const proj = document.getElementById("proj-tag");
  const req = F.request || "—";
  const m = req.match(/^([\s\S]*?) — MILESTONE (\d+\/\d+): ([\s\S]*)$/);
  proj.textContent = "";
  proj.title = req;
  if (m){
    const root = document.createElement("span");
    root.className = "proj-req"; root.textContent = m[1];
    const mile = document.createElement("span");
    mile.className = "proj-mile"; mile.textContent = "M"+m[2]+" · "+m[3];
    proj.append(root, mile);
  } else {
    const root = document.createElement("span");
    root.className = "proj-req"; root.textContent = req;
    proj.append(root);
  }
  // Portrait clamps each line, so tapping the row unclamps it. The chevron is only
  // drawn when something really is cut off — a request short enough to fit gets no
  // affordance and no hint that there is more to see.
  proj.onclick = () => proj.classList.toggle("open");
  requestAnimationFrame(() => {
    const cut = Array.from(proj.children).some(c => c.scrollWidth > c.clientWidth + 1);
    if (!cut) proj.classList.remove("open");
    proj.classList.toggle("clamped", cut);
  });
  const tail = (F.mode ? "· "+F.mode : "") + (F.backend ? " · "+F.backend : "");
  document.getElementById("mode-tag").textContent = tail.trim();
}
function renderElevator(){
  const el=document.getElementById("el-floors"); el.innerHTML="";
  // Floor 0 — the GitHub home: a hub of every build's repo. Always present, even
  // before any project exists, so the repositories are always one click away.
  const hub=document.createElement("button");
  hub.className="el-btn hub"+(REPOS_OPEN?" active":"");
  hub.title="Floor 0 — repositories (every build's code)";
  hub.innerHTML=`<span class="fl-no">0</span>⌥`;
  hub.onclick=()=>openRepoHub();
  el.appendChild(hub);
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
// The dialog's job is not to announce a pause — it is to hand over enough evidence
// to answer "continue or not?" without leaving it. Everything below is already in
// hand when the pause fires (gate verdicts, the agents' own concerns, each
// artifact's content one-liner); the dialog used to drop all of it and show one
// sentence. Order is by decisiveness: gates, then flags, then what landed.

// "4m 12s" / "1h 06m" — an elapsed count a human reads at a glance.
function fmtDur(s){
  s = Math.max(0, Math.round(Number(s)||0));
  if (s < 60) return s+"s";
  const m = Math.floor(s/60), r = s%60;
  if (m < 60) return m+"m "+String(r).padStart(2,"0")+"s";
  return Math.floor(m/60)+"h "+String(m%60).padStart(2,"0")+"m";
}
// "12.4k" — token counts, short.
function fmtNum(n){
  n = Number(n)||0;
  return n >= 1000 ? (n/1000).toFixed(n >= 10000 ? 0 : 1)+"k" : String(n);
}
// Does an artifact's content one-liner describe a NON-ZERO problem? The details are
// generated strings ("3 failed", "severity high — 2 finding(s)", "0 policy
// violation(s)"), so a zero count must not be flagged as trouble.
function artIsRisk(detail){
  const d = String(detail||"").toLowerCase();
  if (/\b0 (failed|finding|policy violation|residual)/.test(d)) return false;
  if (/\b([1-9]\d*) (failed|finding|policy violation)/.test(d)) return true;
  return /severity (high|critical)/.test(d);
}
function chip(parent, text, cls){
  const s = document.createElement("span");
  s.className = "cp-chip" + (cls ? " "+cls : "");
  s.textContent = text;
  parent.appendChild(s);
  return s;
}
// Cost + elapsed for the work behind THIS pause (advisory; the server meters it).
// `shared` means other builds ran concurrently against the one global egress, so the
// token figure is an upper bound — say so rather than quietly overstating.
function renderCpMeta(F, cp){
  const el = document.getElementById("cp-meta"); if(!el) return;
  el.innerHTML = "";
  const sp = cp.spend;
  if (!sp){ el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  chip(el, "this stage " + fmtDur(sp.stage_s));
  chip(el, "build " + fmtDur(sp.build_s));
  if (sp.stage_calls) chip(el, sp.stage_calls + " model call" + (sp.stage_calls===1?"":"s"));
  if (sp.stage_tokens) chip(el, fmtNum(sp.stage_tokens) + " tokens" + (sp.shared ? "*" : ""));
  if (sp.cap_usd) chip(el, "cap $" + sp.cap_usd);
  if (sp.shared) chip(el, "*shared with " + sp.builds + " running builds", "dim");
}

// The gate verdicts SO FAR — the single most decision-relevant thing at a pause, and
// the one the dialog never showed. Failing gates are named in full underneath; a
// green strip alone can hide WHICH gate is red behind a colour.
function renderCpGates(F){
  const el = document.getElementById("cp-gates"); if(!el) return;
  el.innerHTML = "";
  const gates = (F && F.gates) || {};
  const names = GATE_ORDER.filter(n => gates[n]);
  if (!names.length){ el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const strip = document.createElement("div"); strip.className = "cp-gate-strip";
  const bad = [];
  for (const n of names){
    const g = gates[n];
    const s = document.createElement("span");
    s.className = "g " + (g.status || "");
    s.textContent = n;
    if (g.run_against) s.title = "run against " + String(g.run_against).slice(0,12);
    strip.appendChild(s);
    if (g.status === "fail" || g.status === "error") bad.push(n);
  }
  el.appendChild(strip);
  const line = document.createElement("div");
  line.className = "cp-gate-line " + (bad.length ? "bad" : "ok");
  line.textContent = bad.length
    ? "✕ not green: " + bad.join(", ").toUpperCase()
      + " — approving cannot pass these; they must go green before anything ships"
    : "✓ " + names.length + " gate" + (names.length===1?"":"s") + " green so far";
  el.appendChild(line);
}

// What the agents themselves flagged: `concerns` from the advisory journal, plus
// anyone reporting low confidence. Zero extra model calls — the payload already
// carries it, and it was previously reachable only by clicking each lead.
function renderCpFlags(F){
  const el = document.getElementById("cp-flags"); if(!el) return;
  el.innerHTML = "";
  const rows = [];
  for (const e of ((F && F.journal) || [])){
    const role = e.role || e.node_id || "agent";
    for (const c of (e.concerns || [])) if (String(c).trim()) rows.push({role, text:String(c), kind:"concern"});
    if (typeof e.confidence === "number" && e.confidence < 0.6)
      rows.push({role, text:"low confidence ("+e.confidence.toFixed(2)+") in this step", kind:"conf"});
  }
  if (!rows.length){ el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const head = document.createElement("div");
  head.className = "cp-flags-head";
  head.textContent = "⚠ " + rows.length + " thing" + (rows.length===1?"":"s") + " the crew flagged";
  el.appendChild(head);
  for (const r of rows.slice(0, 8)){
    const d = document.createElement("div"); d.className = "cp-flag "+r.kind;
    d.textContent = "· " + r.role + " — " + r.text;
    el.appendChild(d);
  }
  if (rows.length > 8){
    const more = document.createElement("div"); more.className = "cp-flag dim";
    more.textContent = "· +" + (rows.length-8) + " more (click a lead to interview them)";
    el.appendChild(more);
  }
}

// What actually landed this stage, with each artifact's CONTENT one-liner ("12
// passed / 3 failed", "severity high — 2 finding(s)") rather than a bare count.
// Anything describing a non-zero failure sorts to the top and is coloured.
function renderCpArts(F, cp){
  const wrap = document.getElementById("cp-arts-wrap");
  const el = document.getElementById("cp-arts");
  const count = document.getElementById("cp-arts-count");
  if (!wrap || !el) return;
  el.innerHTML = "";
  const arts = (cp.arts && cp.arts.length) ? cp.arts : ((F && F.stageArts) || []);
  if (!arts.length){ wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  const risky = arts.filter(a => artIsRisk(a.detail));
  if (count) count.textContent = "— " + arts.length + " artifact" + (arts.length===1?"":"s")
    + (risky.length ? " · " + risky.length + " needing a look" : "");
  // Risky first, original order preserved within each group.
  wrap.open = risky.length > 0;      // auto-expand only when there is something to see
  for (const a of risky.concat(arts.filter(x => !artIsRisk(x.detail)))){
    const row = document.createElement("div");
    row.className = "cp-art" + (artIsRisk(a.detail) ? " risk" : "");
    const lab = document.createElement("span"); lab.className = "cp-art-label";
    lab.textContent = a.label || a.base || a.kind || "artifact";
    row.appendChild(lab);
    if (a.detail){
      const det = document.createElement("span"); det.className = "cp-art-detail";
      det.textContent = " — " + a.detail;
      row.appendChild(det);
    }
    el.appendChild(row);
  }
}

// WHERE this pause is and WHICH state it decides on — provenance the payload always
// carried and the dialog dropped.
function renderCpProvenance(cp){
  const el = document.getElementById("cp-prov"); if(!el) return;
  const bits = [];
  if (cp.phase) bits.push("phase " + cp.phase);
  if (cp.route) bits.push("route " + cp.route);
  if (cp.state_sha) bits.push("state " + String(cp.state_sha).slice(0, 12));
  el.textContent = bits.join("  ·  ");
  el.classList.toggle("hidden", !bits.length);
}

// Per-button consequence copy + the "go read the code" escape hatch.
function renderCpActions(F, cp){
  const acts = cp.actions || null;
  const set = (id, text) => {
    const el = document.getElementById(id); if(!el) return;
    el.textContent = text || "";
    el.classList.toggle("hidden", !text);
  };
  set("cp-approve-sub", acts && acts.approve);
  set("cp-changes-sub", acts && acts.changes);
  set("cp-reject-sub",  acts && acts.reject);
  // Reviewing the actual diff is the strongest evidence there is — but only offer it
  // once code exists (nothing is written at the product/design pauses).
  const code = document.getElementById("cp-code");
  if (code){
    // A reloaded floor has no per-stage artifact history, so the release boundary
    // (where code always exists by definition) counts too.
    const has = !!(F && (F.hasCode || cp.boundary === "release"));
    code.classList.toggle("hidden", !has);
    code.onclick = () => { if (F) openFilesFor(F); };   // same shape openFiles() passes
  }
}

function showCheckpoint(F){
  const cp = (F && F.cp) || {title:"CHECKPOINT", summary:""};
  document.getElementById("cp-title").textContent="CHECKPOINT · "+(cp.title||"");
  document.getElementById("cp-summary").textContent=cp.summary||"";
  renderCpMeta(F, cp);
  renderCpGates(F);
  renderCpFlags(F);
  renderCpArts(F, cp);
  renderCpProvenance(cp);
  renderCpActions(F, cp);
  // AI expert advisors (the same panel that judges autonomous releases, here
  // ADVISORY): show each domain's verdict + findings above the buttons.
  const adv = document.getElementById("cp-advisors");
  if (adv){
    const list = cp.advisors || [];
    adv.classList.toggle("hidden", !list.length);
    adv.innerHTML = "";
    for (const a of list){
      const row = document.createElement("div");
      row.className = "cp-advisor " + (a.verdict==="approve" ? "ok" : (a.verdict==="request-changes" ? "warn" : "na"));
      const head = document.createElement("div"); head.className = "adv-head";
      head.textContent = (a.verdict==="approve" ? "✅ " : (a.verdict==="request-changes" ? "⚠ " : "❔ "))
        + a.domain.toUpperCase() + " · " + (a.verdict||"?").toUpperCase();
      row.appendChild(head);
      for (const f of (a.findings||[])){
        const li = document.createElement("div"); li.className = "adv-finding"; li.textContent = "· " + f;
        row.appendChild(li);
      }
      adv.appendChild(row);
    }
  }
  // No panel is NOT a clean bill of health — say which it is.
  const advNote = document.getElementById("cp-adv-note");
  if (advNote){
    const note = (cp.advisors && cp.advisors.length) ? "" : (cp.advisorsNote || "");
    advNote.textContent = note;
    advNote.classList.toggle("hidden", !note);
  }
  // Autopilot proposal: surface the panel's request as an EDITABLE next-iteration
  // request (approve = as-is; request-changes = build the edited text; reject = stop).
  const wrap = document.getElementById("cp-edit-wrap");
  if (wrap){
    const isProposal = cp.boundary === "autopilot_proposal";
    wrap.classList.toggle("hidden", !isProposal);
    if (isProposal) document.getElementById("cp-edit").value = cp.request || "";
  }
  document.getElementById("cp-modal").classList.remove("hidden");
}
function hideCheckpoint(){ document.getElementById("cp-modal").classList.add("hidden"); }

// ---- repo browser (self-hosted GitHub over the store) ----------------------
let FILES_ID = null;   // which release's repo the browser is currently showing
let FILES_TREE = [];   // the open repo's flat file list ({path,size}); folded into folders below
let FILES_CWD = "";    // the folder the Code tab is currently showing (""=repo root)
function selectTab(name){
  for(const t of document.querySelectorAll(".repo-tab"))
    t.classList.toggle("sel", t.dataset.tab === name);
  for(const p of document.querySelectorAll(".repo-pane"))
    p.classList.toggle("hidden", p.dataset.pane !== name);
}
function openFiles(){
  const F = FLOORS.get(ACTIVE);
  if(!F){ openRepoHub(); return; }   // no active build — go to the Floor 0 repo hub
  openFilesFor(F);
}
// Open the repo browser for any build (`meta` = {id, floor, release, request,
// status}) — used by the active floor AND by the Floor 0 repo hub.
function openFilesFor(meta){
  const modal = document.getElementById("files-modal");
  const list = document.getElementById("files-list");
  const view = document.getElementById("files-view");
  const rels = document.getElementById("files-releases");
  list.innerHTML = ""; view.textContent = ""; rels.innerHTML = "";
  document.getElementById("files-floor").textContent = "Floor "+meta.floor;
  for(const t of document.querySelectorAll(".repo-tab")) t.onclick = () => selectTab(t.dataset.tab);
  selectTab("code");
  // ITERATE: start a new floor from this finished build (enabled once it's done).
  const itBtn = document.getElementById("files-iterate");
  itBtn.style.display = (meta.status === "done") ? "" : "none";
  itBtn.onclick = () => { hideFiles(); showNewModal(meta.id); };
  const gz = document.getElementById("files-gitzip");
  if (gz) gz.href = "/api/gitzip/" + meta.id;   // whole-floor lineage as ONE git repo
  renderReleases(meta);              // release history for this floor (newest first)
  loadRepo(meta.id);                 // start on the latest release's repo
  modal.classList.remove("hidden");
}
// ---- Floor 0: the repo hub (a self-hosted GitHub home) ---------------------
let REPOS_OPEN = false;
// A short GitHub-ish repo name slugged from the build's request line.
function repoSlug(req){
  const s = String(req||"project").toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");
  return s.split("-").filter(Boolean).slice(0,5).join("-") || "project";
}
// Open the hub: list every build that produced code (newest first). The catalog
// grows as more floors finish, so each open reflects the latest set of repos.
function openRepoHub(){
  const modal = document.getElementById("repos-modal");
  const list = document.getElementById("repos-list");
  const cnt = document.getElementById("repos-count");
  list.innerHTML = "<li class='muted'>Loading…</li>"; cnt.textContent = "";
  modal.classList.remove("hidden");
  REPOS_OPEN = true; renderElevator();
  fetch("/api/builds")
    .then(r => r.ok ? r.json() : {builds:[]})
    .then(({builds}) => {
      const repos = (builds||[]).filter(b => b.has_repo)
        .sort((a,b) => String(b.created_at||"").localeCompare(String(a.created_at||"")));
      cnt.textContent = repos.length ? `${repos.length} ${repos.length===1?"repository":"repositories"}` : "";
      list.innerHTML = "";
      if(!repos.length){
        list.innerHTML = "<li class='muted'>No repositories yet — finish a build and its code lands here.</li>";
        return;
      }
      for(const b of repos) list.appendChild(repoRow(b));
    })
    .catch(() => { list.innerHTML = "<li class='muted'>(could not load repositories)</li>"; });
}
function repoRow(b){
  const li = document.createElement("li"); li.className = "repo-item";
  const ok = b.passed ? "ok" : "bad";
  const pill = b.passed ? "✓ passing" : "✗ failing";
  const when = b.created_at ? String(b.created_at).slice(0,10) : "";
  li.innerHTML =
    `<div class="repo-row1">`+
      `<span class="repo-name">${escapeHtml(repoSlug(b.request))}</span>`+
      `<span class="repo-vis">store</span>`+
      (b.release>1 ? `<span class="repo-rel" title="${b.release} releases — this project has been iterated">↻ r${b.release}</span>` : ``)+
      `<span class="repo-pill ${ok}">${pill}</span>`+
    `</div>`+
    `<div class="repo-desc">${escapeHtml(b.request||"")}</div>`+
    `<div class="repo-meta">`+
      `<span>⌥ floor ${b.floor}</span>`+
      (when ? `<span>updated ${when}</span>` : ``)+
    `</div>`;
  li.onclick = () => { closeRepoHub();
    openFilesFor({id:b.id, floor:b.floor, release:b.release, request:b.request, status:"done"}); };
  return li;
}
function closeRepoHub(){
  document.getElementById("repos-modal").classList.add("hidden");
  REPOS_OPEN = false; renderElevator();
}
// Load the whole repo (tree + checks + pull-request diffs) for one release id.
function loadRepo(id){
  FILES_ID = id;
  const list = document.getElementById("files-list");
  const view = document.getElementById("files-view");
  document.getElementById("files-zip").href = "/api/zip/"+id;
  { const gz = document.getElementById("files-gitzip");
    if (gz) gz.href = "/api/gitzip/"+id; }   // lineage repo up to THIS release
  list.innerHTML = ""; view.textContent = "Loading…";
  document.getElementById("repo-sha").textContent = "";
  document.getElementById("repo-src").textContent = "";
  fetch("/api/repo/"+id)
    .then(r => r.ok ? r.json() : {files:[],checks:[],diffs:[],sign_offs:[]})
    .then(repo => { renderRepo(id, repo); })
    .catch(() => { list.innerHTML = "<li class='muted'>(could not load repo)</li>"; view.textContent = ""; });
}
function renderRepo(id, repo){
  const sha = document.getElementById("repo-sha");
  if(repo.tree_sha){ sha.textContent = "sha "+repo.tree_sha.slice(0,10); }
  const src = document.getElementById("repo-src");
  src.textContent = repo.source === "store" ? "● from store" : "○ from disk";
  src.className = "repo-src " + (repo.source === "store" ? "ok" : "");
  renderTree(id, repo.files || []);
  renderChecks(repo.checks || []);
  renderPulls(repo.diffs || []);
  renderSignOffs(repo.sign_offs || []);
}
function renderTree(id, files){
  FILES_TREE = files || [];
  FILES_CWD = "";
  const view = document.getElementById("files-view");
  const list = document.getElementById("files-list");
  const crumb = document.getElementById("files-crumb");
  if(!FILES_TREE.length){
    list.innerHTML = "<li class='muted'>No files yet — finish a build (its code lands in the store).</li>";
    crumb.innerHTML = ""; view.textContent = ""; return;
  }
  view.textContent = "Select a file…";
  renderDir("");                     // start at the repo root, GitHub-style
}
// Immediate children of `dir` folded out of the flat path list: subfolders
// (with a descendant count) first, then files — each sorted alphabetically.
function dirChildren(dir){
  const prefix = dir ? dir + "/" : "";
  const folders = new Map();
  const files = [];
  for(const f of FILES_TREE){
    if(prefix && !f.path.startsWith(prefix)) continue;
    const rest = f.path.slice(prefix.length);
    const slash = rest.indexOf("/");
    if(slash === -1){ files.push(f); continue; }
    const name = rest.slice(0, slash);
    const fld = folders.get(name) || {name, count:0};
    fld.count++; folders.set(name, fld);
  }
  return {
    folders: [...folders.values()].sort((a,b) => a.name.localeCompare(b.name)),
    files: files.sort((a,b) => a.path.localeCompare(b.path)),
  };
}
// Render one folder level: a breadcrumb trail + this directory's folders/files.
function renderDir(dir){
  FILES_CWD = dir;
  const list = document.getElementById("files-list");
  const crumb = document.getElementById("files-crumb");
  list.innerHTML = ""; crumb.innerHTML = "";
  // breadcrumb: ⌂ root / a / b — every ancestor is a jump-up link.
  const crumbLink = (label, target) => {
    const s = document.createElement("span");
    s.className = "crumb" + (target === dir ? " here" : "");
    s.textContent = label;
    if(target !== dir) s.onclick = () => renderDir(target);
    return s;
  };
  crumb.appendChild(crumbLink("⌂ root", ""));
  let acc = "";
  for(const part of (dir ? dir.split("/") : [])){
    acc = acc ? acc + "/" + part : part;
    const sep = document.createElement("span"); sep.className = "crumb-sep"; sep.textContent = "/";
    crumb.appendChild(sep);
    crumb.appendChild(crumbLink(part, acc));
  }
  const {folders, files} = dirChildren(dir);
  // ".." row to step back up one level (absent at the root).
  if(dir){
    const up = document.createElement("li"); up.className = "dir up";
    up.innerHTML = "<span class='ico'>⮬</span><span class='nm'>..</span>";
    const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";
    up.onclick = () => renderDir(parent);
    list.appendChild(up);
  }
  for(const fld of folders){
    const li = document.createElement("li"); li.className = "dir";
    li.innerHTML = `<span class='ico'>▸</span><span class='nm'>${escapeHtml(fld.name)}/</span>`+
                   `<span class='meta'>${fld.count}</span>`;
    li.title = fld.name;
    const target = dir ? dir + "/" + fld.name : fld.name;
    li.onclick = () => renderDir(target);
    list.appendChild(li);
  }
  for(const f of files){
    const li = document.createElement("li"); li.className = "file";
    const name = f.path.slice(f.path.lastIndexOf("/") + 1);
    li.innerHTML = `<span class='ico'>≡</span><span class='nm'>${escapeHtml(name)}</span>`+
                   `<span class='meta'>${f.size}b</span>`;
    li.title = f.path;
    li.onclick = () => viewFile(FILES_ID, f.path, li);
    list.appendChild(li);
  }
}
function renderChecks(checks){
  const ul = document.getElementById("checks-list");
  const cnt = document.getElementById("checks-count");
  ul.innerHTML = "";
  const passed = checks.filter(c => c.ok).length;
  cnt.textContent = checks.length ? `${passed}/${checks.length}` : "";
  if(!checks.length){ ul.innerHTML = "<li class='muted'>No gate verdicts yet.</li>"; return; }
  for(const c of checks){
    const li = document.createElement("li");
    li.className = "check " + (c.ok ? "ok" : "bad");
    const icon = c.ok ? "✓" : (c.status === "error" ? "!" : "✕");
    const adv = c.advisory ? " <span class='adv'>advisory</span>" : "";
    let html = `<span class="ck-icon">${icon}</span><span class="ck-name">${c.name}</span>`+
               `<span class="ck-status">${c.status}</span>${adv}`;
    if(c.findings && c.findings.length){
      html += `<div class="ck-findings">`+ c.findings.map(escapeHtml).join("<br>") +`</div>`;
    }
    li.innerHTML = html;
    ul.appendChild(li);
  }
}
function renderSignOffs(sign_offs){
  const ul = document.getElementById("signoffs-list");
  const cnt = document.getElementById("signoffs-count");
  if(!ul) return;
  ul.innerHTML = "";
  cnt.textContent = sign_offs.length ? String(sign_offs.length) : "";
  if(!sign_offs.length){
    ul.innerHTML = "<li class='muted'>No sign-offs recorded yet — the CTO signs off at the Design &amp; Release checkpoints.</li>";
    return;
  }
  for(const s of sign_offs){
    const li = document.createElement("li");
    li.className = "check " + (s.approved ? "ok" : "bad");
    const icon = s.approved ? "✓" : "✕";
    const who = [s.approver, s.role].filter(Boolean).map(escapeHtml).join(" · ") || "(unnamed)";
    const prov = s.provenance ? ` <span class='adv'>${escapeHtml(s.provenance)}</span>` : "";
    let html = `<span class="ck-icon">${icon}</span><span class="ck-name">${escapeHtml(s.point||"sign-off")}</span>`+
               `<span class="ck-status">${who}</span>${prov}`;
    const detail = [];
    if(s.evidence_hash) detail.push("evidence "+escapeHtml(String(s.evidence_hash).slice(0,16)));
    if(s.note) detail.push(escapeHtml(s.note));
    if(detail.length) html += `<div class="ck-findings">`+ detail.join("<br>") +`</div>`;
    li.innerHTML = html;
    ul.appendChild(li);
  }
}
function renderPulls(diffs){
  const box = document.getElementById("pulls-list");
  const cnt = document.getElementById("pulls-count");
  box.innerHTML = "";
  cnt.textContent = diffs.length ? String(diffs.length) : "";
  if(!diffs.length){ box.innerHTML = "<div class='muted'>No pull requests (no FeatureDiff in this build).</div>"; return; }
  diffs.forEach((d, i) => {
    const card = document.createElement("div"); card.className = "pull";
    const head = document.createElement("div"); head.className = "pull-head";
    head.innerHTML = `<span class="pull-no">#${i+1}</span> <span class="pull-sum">${escapeHtml(d.summary||"feature diff")}</span> <span class="pull-merged">merged</span>`;
    const pre = document.createElement("pre"); pre.className = "pull-diff";
    pre.innerHTML = colorizeDiff(d.text || "");
    head.onclick = () => pre.classList.toggle("open");
    card.appendChild(head); card.appendChild(pre);
    box.appendChild(card);
  });
}
function escapeHtml(s){ return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function colorizeDiff(text){
  return text.split("\n").map(line => {
    const e = escapeHtml(line);
    if(line.startsWith("+") && !line.startsWith("+++")) return `<span class="d-add">${e}</span>`;
    if(line.startsWith("-") && !line.startsWith("---")) return `<span class="d-del">${e}</span>`;
    if(line.startsWith("@@")) return `<span class="d-hunk">${e}</span>`;
    if(line.startsWith("diff ") || line.startsWith("+++") || line.startsWith("---")) return `<span class="d-file">${e}</span>`;
    return e;
  }).join("\n");
}
// Release history of the open floor — one row per release; click to browse it.
function renderReleases(F){
  const rels = document.getElementById("files-releases");
  fetch("/api/releases/"+F.floor)
    .then(r => r.ok ? r.json() : {releases:[]})
    .then(({releases}) => {
      rels.innerHTML = "";
      if(!releases || releases.length <= 1) return;   // nothing to switch between
      for(const rel of releases){
        const b = document.createElement("button");
        b.type = "button"; b.className = "rel-chip";
        const ok = rel.passed ? "✓" : (rel.status === "done" ? "✗" : "…");
        b.textContent = `r${rel.release} ${ok}`;
        b.title = (rel.request || "") + (rel.created_at ? `  ·  ${rel.created_at}` : "");
        b.onclick = () => { for(const c of rels.children) c.classList.remove("sel");
                            b.classList.add("sel"); loadRepo(rel.id); };
        rels.appendChild(b);
      }
    })
    .catch(() => { rels.innerHTML = ""; });
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
  const body = {decision, checkpoint: F.cpIndex};
  const wrap = document.getElementById("cp-edit-wrap");
  if (wrap && !wrap.classList.contains("hidden")){
    const edit = (document.getElementById("cp-edit").value || "").trim();
    if (edit) body.request_edit = edit;   // the human's amended next-iteration request
  }
  fetch("/api/decision/"+F.id, {method:"POST", headers:postHeaders(),
    body: JSON.stringify(body)});
  F.awaiting = false; renderElevator(); hideCheckpoint();
}

// ---- interview a lead (click an agent) -------------------------------------
let IV_TARGET = null;
function openInterview(name){
  const F = FLOORS.get(ACTIVE); if(!F) return;
  const journal = F.journal || [];
  const mine = journal.filter(e=>e.role===name || e.agent===name);
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
document.getElementById("new-close").onclick=()=>newModal.classList.add("hidden");
// ---- folded sections report what they're set to ------------------------------
// ③ How it runs and ④ Scale & limits stay shut on their defaults, so their summary
// line has to name those defaults — a fold that hides the setting AND its value is
// just a setting you can't find.
function _selLabel(id){
  const s=document.getElementById(id);
  if(!s || !s.selectedOptions || !s.selectedOptions.length) return "";
  return (s.selectedOptions[0].textContent||"").split("—")[0].trim();
}
function syncFoldSummaries(){
  const val=(id)=>String((document.getElementById(id)||{}).value||"").trim();
  const run=document.getElementById("run-sum");
  if(run) run.textContent=[_selLabel("backend-select"), _selLabel("mode-select"),
    _selLabel("build-select"), _selLabel("adapter-select"),
    "stack: "+(_selLabel("profile-select")||"auto")].filter(Boolean).join(" · ");
  const scale=document.getElementById("scale-sum");
  if(scale){
    const ap=parseInt(val("autopilot-input"),10);
    const parts=[(val("extra-workers")||"auto")+" workers",
                 (val("fix-input")||"0")+" auto-fix"];
    if(val("cap-input")) parts.push("$"+val("cap-input")+" cap");
    parts.push(Number.isFinite(ap)&&ap>0 ? "autopilot "+ap : "autopilot off");
    if(val("packs-input")) parts.push(val("packs-input"));
    const sup=document.getElementById("supervised");
    parts.push(sup && sup.checked ? "supervised" : "unsupervised");
    scale.textContent=parts.join(" · ");
  }
}
newModal.addEventListener("change", syncFoldSummaries);
newModal.addEventListener("input", syncFoldSummaries);

// ---- Presets: one-click example builds that fill every spec-side setting -------
// The PIR entry reuses the modal's HTML defaults (one source of truth); the rest are
// defined inline. A preset sets the SPEC side (request/spec/mode/build/ladder/stack/
// packs/workers/fix/autopilot) but NOT backend/adapter — those are host-dependent and
// the server already reconciles them from /api/config.
const _pirReq = (document.getElementById("req-input")||{}).value || "";
const _pirSpec = (document.getElementById("spec-text")||{}).value || "";
const _pirLadder = (document.getElementById("milestones-text")||{}).value || "";
const MOON_SPEC =
`A complete stardew-valley-style 2D farming/life sim, set on the moon.
Must include: tile-based moon surface with a scrolling camera; day/night + sol
cycle; player movement and tool use; tilling, planting, watering, and harvesting
at least 6 crop types with growth stages; oxygen/energy management; a colony hub
with at least 4 NPCs with daily schedules and dialog; an economy (sell crops,
buy seeds/upgrades at a store); base-building (place/upgrade modules); save/load
with 3 slots; a title screen, pause menu, and controls screen; sound hooks
(procedural or silent-safe); keyboard controls documented in-game. Code-drawn
graphics (shapes/procedural tiles) are fine — no external assets.`;
const PRESETS = [
  { id:"custom", label:"— Custom (blank) —",
    note:"A blank slate — describe your own build.",
    request:"", intake:"idea", spec:"", mode:"autonomous", build:"single",
    maxMilestones:6, milestones:"", profile:"auto", packs:"", workers:4, fix:5, autopilot:0 },
  { id:"pir", label:"PIR system · cryptography (complex)",
    note:"Information-theoretic 2-server + homomorphic single-server PIR, verified in-process.",
    request:_pirReq, intake:"spec", spec:_pirSpec, mode:"gated", build:"milestones",
    maxMilestones:6, milestones:_pirLadder, profile:"auto", packs:"crypto",
    workers:4, fix:5, autopilot:0 },
  { id:"moon", label:"Moon Colony farming game · pygame (very complex)",
    note:"The BUILD-A-FULL-GAME recipe: a complete Stardew-style sim as a milestone ladder.",
    request:"a complete stardew-style farming game about colonizing the moon",
    intake:"spec", spec:MOON_SPEC, mode:"gated", build:"milestones", maxMilestones:10,
    milestones:"", profile:"pygame", packs:"game", workers:4, fix:8, autopilot:5 },
  { id:"urlshort", label:"URL shortener · web service (simple)",
    note:"A small single service — quick end-to-end build.",
    request:"a URL shortener service with custom slugs, link expiry, click analytics, and per-IP abuse rate-limiting",
    intake:"idea", spec:"", mode:"autonomous", build:"single", maxMilestones:6,
    milestones:"", profile:"auto", packs:"", workers:4, fix:5, autopilot:0 },
  { id:"chip8", label:"CHIP-8 emulator · systems (simple)",
    note:"Cheap pipeline validation — objectively scoreable against public test ROMs.",
    request:"a CHIP-8 emulator in Python with pytest tests against public test ROMs",
    intake:"idea", spec:"", mode:"autonomous", build:"single", maxMilestones:6,
    milestones:"", profile:"auto", packs:"", workers:4, fix:6, autopilot:0 },
  { id:"taskmgr", label:"Team task manager · web+API+DB platform (complex)",
    note:"Multi-service — auto-built as a platform (web / backend / data), stitched into one repo.",
    request:"a team task manager: users sign up and log in, create projects, add tasks with assignees, due dates and labels, comment on tasks, and switch between a board and a list view — a REST API, a web UI, and a database",
    intake:"idea", spec:"", mode:"gated", build:"single", maxMilestones:6,
    milestones:"", profile:"auto", packs:"frontend", workers:6, fix:6, autopilot:0 },
  { id:"ssg", label:"Static-site generator · CLI tool (simple)",
    note:"A command-line tool + library — a different shape of software.",
    request:"a command-line static-site generator: turn a folder of Markdown with front-matter into a linked HTML site with a table of contents, page templates, and a live-reload dev server",
    intake:"idea", spec:"", mode:"autonomous", build:"single", maxMilestones:6,
    milestones:"", profile:"auto", packs:"", workers:4, fix:5, autopilot:0 },
  { id:"chat", label:"Real-time chat · WebSocket platform (complex)",
    note:"Rooms, presence and typing over WebSockets — web client + realtime backend + DB.",
    request:"a real-time group chat app: named rooms, user presence, typing indicators, persistent message history and search, and auth — a WebSocket backend, a web client, and a database",
    intake:"idea", spec:"", mode:"gated", build:"single", maxMilestones:6,
    milestones:"", profile:"auto", packs:"frontend", workers:6, fix:6, autopilot:0 },
];
function _setSelIfPresent(id, val){
  const s=document.getElementById(id); if(!s) return;
  if([...s.options].some(o=>o.value===val)){ s.value=val; s.dispatchEvent(new Event("change")); }
}
function applyPreset(id){
  const p=PRESETS.find(x=>x.id===id); if(!p) return;
  const note=document.getElementById("preset-note"); if(note) note.textContent=p.note||"";
  const set=(elId,val)=>{ const e=document.getElementById(elId); if(e) e.value=val; };
  set("req-input", p.request||"");
  set("spec-text", p.spec||"");
  set("milestones-text", p.milestones||"");
  set("max-milestones", p.maxMilestones!=null?p.maxMilestones:6);
  set("packs-input", p.packs||"");
  set("extra-workers", p.workers!=null?p.workers:"");
  set("fix-input", p.fix!=null?p.fix:5);
  set("autopilot-input", p.autopilot!=null?p.autopilot:0);
  _setSelIfPresent("mode-select", p.mode||"autonomous");
  _setSelIfPresent("build-select", p.build||"single");   // change event toggles the ladder opts
  _setSelIfPresent("profile-select", p.profile||"auto");
  // intake radio: reveal the matching panel (idea hides the spec box, spec shows it).
  const rb=document.querySelector(`input[name="intake"][value="${p.intake||"idea"}"]`);
  if(rb){ rb.checked=true; rb.dispatchEvent(new Event("change")); }
  if(typeof updateSlotHint==="function") updateSlotHint();
  if(typeof syncMode==="function") syncMode();
}
{ const ps=document.getElementById("preset-select");
  if(ps){
    for(const p of PRESETS){ const o=document.createElement("option"); o.value=p.id; o.textContent=p.label; ps.appendChild(o); }
    ps.value="pir";   // the modal's HTML defaults already show the PIR demo
    const pn=document.getElementById("preset-note");
    if(pn){ const cur=PRESETS.find(x=>x.id==="pir"); if(cur) pn.textContent=cur.note; }
    ps.addEventListener("change", ()=>applyPreset(ps.value));
  } }

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
  // Iterate → pre-fill the converse draft with the PARENT's saved spec, so a new
  // conversation with the leads EXTENDS it (and the release saves it cumulatively).
  // Guarded by !chatDraft so it never clobbers an in-progress draft.
  if(iterating){
    fetch("/api/spec/"+startFromSel.value).then(r=>r.ok?r.json():{spec:"",built:""})
      .then(d=>{
        if(typeof d.spec==="string" && d.spec.trim() && !chatDraft.trim()){
          chatDraft=d.spec;
          const el=document.getElementById("chat-spec"); if(el) el.textContent=chatDraft;
        }
        iterBuilt = (typeof d.built==="string") ? d.built : "";   // seeds the leads on iterate
      }).catch(()=>{});
  } else {
    iterBuilt = "";
  }
}
startFromSel.addEventListener("change", syncStartFrom);
function showNewModal(parentId){
  newModal.classList.remove("hidden");
  loadStartFrom().then(()=>{
    startFromSel.value=(parentId && [...startFromSel.options].some(o=>o.value===parentId))?parentId:"";
    syncStartFrom();
  });
  updateSlotHint();
  syncFoldSummaries();   // reconciled selects (/api/config) don't fire change events
}
// Advisory IC-pool sizing: ask the server (/api/suggest-slots) how many parallel
// workers the request + packs are likely to need, and fold it into the note. The
// real split is decided during the build — this just previews the scale.
const PARALLEL_BASE = "Automatic — the tech-lead decomposes the work and the system runs the independent tasks in parallel.";
let slotHintTimer = null;
function updateSlotHint(){
  if(slotHintTimer) clearTimeout(slotHintTimer);
  slotHintTimer = setTimeout(()=>{
    const note = document.getElementById("parallel-note");
    const request = (document.getElementById("req-input").value || "").trim();
    const packs = (document.getElementById("packs-input").value || "")
      .split(",").map(s=>s.trim()).filter(Boolean);
    if(!note || !request){ if(note) note.textContent = PARALLEL_BASE; return; }
    fetch("/api/suggest-slots",{method:"POST",headers:postHeaders(),
      body:JSON.stringify({request, packs})})
     .then(r=>r.ok?r.json():null)
     .then(d=>{
       if(!d || !d.slots){ note.textContent = PARALLEL_BASE; return; }
       const areas = (d.areas||[]).length ? ` across ${d.areas.join(", ")}` : "";
       note.textContent = `${PARALLEL_BASE} For this request, expect ≈ ${d.slots} parallel worker${d.slots===1?"":"s"}${areas}.`;
     })
     .catch(()=>{ note.textContent = PARALLEL_BASE; });
  }, 350);
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
let chatSession = null, chatDraft = "", iterBuilt = "";
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
  // On the FIRST turn of an iterate, seed the session with the inherited spec
  // (what it SHOULD be) and what the last build actually produced (what's DONE),
  // so the leads EXTEND the spec and focus the new work on the gaps.
  const seedSpec  = (!chatSession && chatDraft.trim()) ? chatDraft : undefined;
  const seedBuilt = (!chatSession && iterBuilt.trim()) ? iterBuilt : undefined;
  fetch("/api/ideate", {method:"POST", headers:postHeaders(),
    body: JSON.stringify({session:chatSession, message:msg, backend,
                          base_spec:seedSpec, base_built:seedBuilt})})
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
  extra.adapter=document.getElementById("adapter-select").value;
  { const ap=document.getElementById("autopilot-input");
    const n=ap?parseInt(ap.value,10):0; if(Number.isFinite(n)&&n>0) extra.autopilot=n; }
  // Extra workers: the pool ON TOP of one-per-team. Blank ⇒ the server's automatic
  // cap. Each role always gets its baseline worker; these scale the busy teams.
  const xw=parseInt((document.getElementById("extra-workers")||{}).value,10);
  if(Number.isFinite(xw) && xw>=0) extra.max_slots=xw;
  const fix=parseInt(document.getElementById("fix-input").value,10);
  if(Number.isFinite(fix) && fix>=0) extra.max_fix_attempts=fix;
  const cap=parseFloat(document.getElementById("cap-input").value);
  if(Number.isFinite(cap) && cap>0) extra.cost_cap_usd=cap;
  if(startFromSel.value) extra.parent_id=startFromSel.value;   // iterate on a finished build
  // platform vs single is INFERRED server-side from the request (multi-service => platform).
  // These advanced knobs only take effect when the request builds as a platform.
  const mp=parseInt((document.getElementById("max-passes")||{}).value,10);
  if(Number.isFinite(mp) && mp>=1) extra.max_passes=mp;
  const criticEl=document.getElementById("critic-on");
  if(criticEl && !criticEl.checked) extra.no_critic=true;
  const mpar=parseInt((document.getElementById("max-parallel")||{}).value,10);
  if(Number.isFinite(mpar) && mpar>=1) extra.max_parallel=mpar;
  const mss=parseInt((document.getElementById("max-slots-svc")||{}).value,10);
  if(Number.isFinite(mss) && mss>=1) extra.max_slots_per_service=mss;
  // Milestone ladder: plan-and-chain releasable increments (one release per rung).
  // Iterating on a finished build stays a single release, so the ladder select
  // only applies to fresh builds.
  { const bs=document.getElementById("build-select");
    if(bs && bs.value==="milestones" && !startFromSel.value){
      extra.build="milestones";
      const mm=parseInt((document.getElementById("max-milestones")||{}).value,10);
      extra.max_milestones=(Number.isFinite(mm)&&mm>=1)?Math.min(mm,12):10;
      const lines=(document.getElementById("milestones-text").value||"")
        .split("\n").map(s=>s.trim()).filter(Boolean);
      if(lines.length) extra.milestones=lines;
    } }
  { const ps=document.getElementById("profile-select");
    if (ps && ps.value && ps.value!=="auto") extra.profile=ps.value; }
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
  chatSession=null; chatDraft=""; iterBuilt="";
  document.getElementById("chat-log").innerHTML="";
  document.getElementById("chat-spec").textContent="(nothing yet)";
};
document.getElementById("req-input").addEventListener("input", updateSlotHint);
document.getElementById("packs-input").addEventListener("input", updateSlotHint);
// Build-as select: the milestone options only make sense for the ladder.
{ const bs=document.getElementById("build-select"), mo=document.getElementById("milestones-opts");
  if(bs && mo) bs.addEventListener("change", ()=>mo.classList.toggle("hidden", bs.value!=="milestones")); }
document.getElementById("chat-send").onclick=sendChat;
document.getElementById("chat-msg").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); sendChat(); }});
document.getElementById("cp-approve").onclick=()=>sendDecision("approve");
document.getElementById("cp-changes").onclick=()=>sendDecision("needs_changes");
document.getElementById("cp-reject").onclick=()=>sendDecision("reject");
document.getElementById("output-btn").onclick=openFiles;
for (const t of document.querySelectorAll(".log-tab")) t.onclick = () => selectLogTab(t.dataset.tab);
document.getElementById("files-close").onclick=hideFiles;
document.getElementById("repos-close").onclick=closeRepoHub;
document.getElementById("repos-new").onclick=()=>{ closeRepoHub(); showNewModal(); };
document.getElementById("repos-clear").onclick=async ()=>{
  if(!confirm("Clear ALL floors and repositories? This wipes every build from the catalog and cannot be undone.")) return;
  try{
    const r = await fetch("/api/reset", {method:"POST", headers:postHeaders()});
    if(!r.ok){
      const d = await r.json().catch(()=>({}));
      alert(d.error || ("Clear failed ("+r.status+")."));   // e.g. builds still running
      return;
    }
    location.reload();   // floors/repos are gone server-side — rebuild the empty UI
  }catch(_){ alert("Clear failed — could not reach the server."); }
};
// interview modal
document.getElementById("iv-ask").onclick=askInterview;
document.getElementById("iv-close").onclick=()=>document.getElementById("iv-modal").classList.add("hidden");
document.getElementById("iv-question").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); askInterview(); }});

// Pull runtime config from the server: preselect the configured default backend +
// adapter (FOUNDRY_MODEL_BACKEND/_CODING_ADAPTER; mock/stub if unset), drop any
// option the server doesn't actually support (keeps the picker 1-1 with BACKENDS /
// ADAPTERS), and surface the per-tier model ids each backend would serve.
let MODELS_BY_BACKEND = {};
function reconcileSelect(sel, supported, fallback){
  if(!sel || !Array.isArray(supported) || !supported.length) return;
  // drop options the server no longer supports…
  for(const o of [...sel.options]) if(!supported.includes(o.value)) o.remove();
  // …and add any the server supports but the static markup didn't list (the rich
  // descriptions stay on the known ones; new ones get a plain label).
  const have = new Set([...sel.options].map(o=>o.value));
  for(const v of supported) if(!have.has(v)){
    const o=document.createElement("option"); o.value=v; o.textContent=v; sel.appendChild(o);
  }
  if(![...sel.options].some(o=>o.value===sel.value))
    sel.value = (fallback && supported.includes(fallback)) ? fallback : supported[0];
}
function updateModelNote(){
  const note=document.getElementById("model-note");
  if(!note) return;
  const backend=document.getElementById("backend-select").value;
  const m=MODELS_BY_BACKEND[backend];
  if(!m || !Object.keys(m).length){ note.textContent = backend==="mock"
    ? "mock runs no real model — deterministic, offline." : ""; return; }
  note.textContent = "Models · " + Object.entries(m).map(([t,id])=>`${t}: ${id}`).join("  ·  ");
}
fetch("/api/config").then(r=>r.json()).then(c=>{
  if(!c) return;
  const sel=document.getElementById("backend-select");
  reconcileSelect(sel, c.backends, c.default_backend);
  if(c.default_backend && [...sel.options].some(o=>o.value===c.default_backend))
    sel.value=c.default_backend;
  const asel=document.getElementById("adapter-select");
  reconcileSelect(asel, c.adapters, c.default_adapter);
  // Stack picker: "auto" + every verification profile the server ships.
  { const ps=document.getElementById("profile-select");
    if (ps && Array.isArray(c.profiles)){
      for (const name of c.profiles){
        const o=document.createElement("option"); o.value=name; o.textContent=name;
        ps.appendChild(o);
      } } }
  // The coding substrate is DERIVED from the backend (parity with the CLI:
  // mock→stub, any real backend→raw-react), so a real backend writes real code
  // without a second pick. syncAdapter() applies it unless the user overrode it.
  syncAdapter();
  MODELS_BY_BACKEND = c.models_by_backend || {};
  updateModelNote();
  syncCostCap();
  syncFoldSummaries();   // these selects were set programmatically — no change event
}).catch(()=>{});

// The USD cost cap only binds *metered* API backends (real per-token charges).
// mock is offline, ollama is local, and claude-cli bills a flat subscription —
// so for those a per-build USD cap is meaningless: disable + annotate the field.
const METERED_BACKENDS = ["anthropic","openai","codex"];
function syncCostCap(){
  const backend = document.getElementById("backend-select").value;
  const cap = document.getElementById("cap-input");
  if(!cap) return;
  const metered = METERED_BACKENDS.includes(backend);
  cap.disabled = !metered;
  cap.placeholder = metered ? "no cap" : `${backend} isn't metered — no USD cost`;
  if(!metered) cap.value = "";
}
// Coding substrate derived from the backend (CLI parity): mock→stub no-op,
// claude-cli→claude-code (AGENTIC: claude edits/builds/tests inside the task
// sandbox), other real backends→raw-react (one completion → one diff). A user who
// manually picks an adapter pins it (adapterTouched), so derivation never
// clobbers an explicit choice.
function defaultAdapterFor(backend){
  if(backend==="mock") return "stub";
  return backend==="claude-cli" ? "claude-code" : "raw-react";
}
let adapterTouched = false;
function syncAdapter(){
  const asel = document.getElementById("adapter-select");
  if(!asel || adapterTouched) return;
  const want = defaultAdapterFor(document.getElementById("backend-select").value);
  if([...asel.options].some(o=>o.value===want)) asel.value = want;
}
{ const asel = document.getElementById("adapter-select");
  if(asel) asel.addEventListener("change", ()=>{ adapterTouched = true; }); }
document.getElementById("backend-select").addEventListener("change", ()=>{ syncCostCap(); syncAdapter(); updateModelNote(); });
syncCostCap();

// Autonomous mode is HANDS-OFF by definition: the trust-root admission controller
// is the sole release gate (build · admit · deploy, no human sign-off). So when
// `autonomous` is picked, supervision (per-checkpoint human approval) is forced
// OFF — keeping it on would pause an unattended build. gated re-enables it so the
// CTO drives the Design/Release checkpoints in the floor screen.
function syncMode(){
  const mode = document.getElementById("mode-select").value;
  const sup = document.getElementById("supervised");
  const hint = document.getElementById("supervised-hint");
  if(!sup) return;
  if(mode === "autonomous"){
    sup.checked = false;
    sup.disabled = true;
    if(hint) hint.textContent = "Autonomous is hands-off — admission is the sole release gate; no checkpoint approvals.";
    // Only the REGULATED pack vetoes hands-off release (compliance sign-off needs
    // a human). Warn HERE instead of letting the server refuse the build with a
    // cryptic mode-veto error. Other packs (game/infra/crypto) allow autonomous —
    // their human sign-offs simply don't bind when no human is in the loop.
    const packs=(document.getElementById("packs-input").value||"").toLowerCase();
    if(hint && packs.includes("regulated"))
      hint.textContent = "⚠ The regulated pack VETOES autonomous (compliance sign-off needs "
        + "a human) — the build will refuse to start. Switch to gated, or clear the pack.";
  } else {
    sup.disabled = false;
    if(hint) hint.textContent = "Gated — the CTO approves the Design & Release checkpoints right here in the floor screen (no Git merge).";
  }
}
document.getElementById("mode-select").addEventListener("change", syncMode);
document.getElementById("packs-input").addEventListener("input", syncMode);
syncMode();

// Rehydrate on (re)load: floors run server-side independently of this tab, so a
// refresh / reconnect from another device rebuilds every floor + reconnects the
// live stream of any still-running build. No floors yet => open the new dialog.
function adoptFloor(row){
  if (FLOORS.has(row.floor)) return;
  const F = {
    id:row.id, floor:row.floor, release:row.release||1, releases:row.releases||1,
    request:row.request, mode:row.mode, backend:row.backend,
    scene:null, log:[], progress:[], gates:row.gates||{}, org:null, cp:null, awaiting:!!row.awaiting,
    status:row.status, passed:!!row.passed, slots:row.slots||1, journal:[], seenUris:new Set(),
  };
  FLOORS.set(row.floor, F);
  GAME.scene.add(floorKey(row.floor), FloorScene, true,
    {floor:row.floor, id:row.id, request:row.request, mode:row.mode, slots:F.slots});
  if (row.status !== "done"){          // running build: reconnect its live stream
    openFloorStream(F);
  }
}
// ONE live stream per floor, shared by every path (new build / rehydrate / next
// release). When the stream ENDS, the server may immediately spawn the floor's
// NEXT release (a milestone-ladder rung or an autopilot iteration) — follow along
// by briefly polling for a newer release and adopting it in place, so an open tab
// streams a whole overnight ladder without a refresh.
function openFloorStream(F){
  const es = new EventSource("/api/stream/"+F.id);
  F.lastBeat = Date.now();
  es.onmessage = e => { F.lastBeat = Date.now();
    try { routeEvent(F.floor, JSON.parse(e.data)); } catch(_){} };
  es.addEventListener("ka", ()=>{ F.lastBeat = Date.now(); updateLiveDot(); });
  es.addEventListener("end", ()=>{ es.close(); adoptNextRelease(F); });
  F.es = es;
}
async function adoptNextRelease(F){
  // Poll for ~5 minutes (autopilot's Director/panel deliberation can take a while
  // on a real backend; a ladder rung spawns in seconds), then give up quietly.
  for (let tries = 0; tries < 60; tries++){
    await new Promise(r=>setTimeout(r, 5000));
    let rows;
    try { rows = (await fetch("/api/projects").then(r=>r.json())).projects; }
    catch(_){ continue; }
    const row = (rows||[]).find(r=>r.floor===F.floor);
    if (!row || row.id === F.id){
      if (row && row.status === "done") return;   // floor truly finished
      continue;
    }
    // A fresh release took over the floor: reset per-release state and reconnect
    // (the same shape newProject's iterate path uses).
    if (F.es){ try{ F.es.close(); }catch(_){} }
    Object.assign(F, {
      id:row.id, release:row.release||F.release+1, releases:(F.releases||1)+1,
      request:row.request, log:[], gates:{}, org:null, cp:null, awaiting:false,
      status:"running", passed:false, journal:[], seenUris:new Set(),
      liveRoom:null, liveAgent:null, _agrp:null, _svcOpen:null,
    });
    const sc = GAME.scene.getScene(floorKey(F.floor));
    if (sc) sc.scene.restart({floor:F.floor, id:row.id, request:row.request, mode:row.mode, slots:F.slots});
    renderElevator();
    if (F.floor === ACTIVE) updateHud(F);
    openFloorStream(F);
    return;
  }
}
async function rehydrate(){
  await bootReady;  // shared textures must exist before a FloorScene builds
  try {
    const {projects} = await fetch("/api/projects").then(r=>r.json());
    if(!projects || !projects.length){ showNewModal(); return; }
    for(const row of projects) adoptFloor(row);
    switchToFloor(projects[projects.length-1].floor);
    renderElevator();
  } catch(_){ showNewModal(); }
}
rehydrate();
