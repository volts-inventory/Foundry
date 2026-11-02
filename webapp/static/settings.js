// Build settings (workshop plan Phases 4, 4b, 5.1): the ⚙ Settings panel (model
// mode, level table, difficulty → level, local server check), the New Project
// form's Advanced fold, "Change this build", and the mode/models line on the HUD.
// game.js calls in through window.FoundrySettings; postHeaders() and showNewModal()
// are game.js globals.
(function(){
"use strict";
const TIERS = ["T1", "T2", "T3"];
const DIFFS = ["trivial", "standard", "complex"];
const MODES = [
  ["cloud", "Cloud", "Every level on the cloud backend."],
  ["hybrid", "Hybrid", "T1 in the cloud; T2 and T3 on your local server."],
  ["local", "Local", "Every level on your local server."],
];
const TIER_ROLE = {T1:"planner · complex · fixer", T2:"standard · reviewer", T3:"trivial"};
let CFG = null;        // GET /api/settings
let CHANGE = null;     // the shipped floor the form is changing, or null
let SPEC = null;       // {text, name} attached to the form

function h(tag, attrs, ...kids){
  const e = document.createElement(tag);
  for (const k in attrs || {}){
    const v = attrs[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids) if (c !== null && c !== undefined) e.append(c);
  return e;
}
const $ = id => document.getElementById(id);

async function loadCfg(){
  const r = await fetch("/api/settings");
  if (!r.ok) throw new Error("settings unavailable ("+r.status+")");
  CFG = await r.json();
  return CFG;
}

// ---- model names on the HUD and the form ------------------------------------
function shortModel(id){
  let s = String(id || "").split("/").pop().toLowerCase();
  const fam = s.match(/opus|sonnet|haiku/);
  if (fam) return fam[0];
  let prev;
  do { prev = s; s = s.replace(/[-_.](\d+bit|q\d\w*|mlx|gguf|instruct|awq|fp\d+|bf16)$/, ""); } while (s !== prev);
  return s;
}
function levelsLine(mode, levels){
  if (!mode) return "";
  const groups = [];
  for (const t of TIERS){
    const lv = (levels || {})[t]; if (!lv) continue;
    const name = shortModel(lv.model);
    const g = groups.find(x => x.name === name);
    if (g) g.tiers.push(t); else groups.push({name, tiers:[t]});
  }
  return [mode.toUpperCase(), ...groups.map(g => g.tiers.join("/")+" "+g.name)].join(" · ");
}
function hudMeta(F){
  return F && F.build ? levelsLine(F.build.model_mode, F.build.levels) : "";
}
function decorateProj(F, proj){
  const b = F && F.build;
  $("mode-tag").title = b ? hudMeta(F) : "";
  if (!b) return;
  if (b.base_floor){
    const link = h("button", {type:"button", class:"proj-base",
      title:"Change of floor "+b.base_floor+" — ride to it",
      text:"✎ F"+b.base_floor+" ›",
      onclick: e => { e.stopPropagation(); rideTo(b.base_floor); }});
    proj.prepend(link);
  }
  const sub = h("span", {class:"proj-sub"});
  if (b.base_floor) sub.append(h("button", {type:"button", class:"proj-base-sub",
    text:"✎ change of F"+b.base_floor,
    onclick: e => { e.stopPropagation(); rideTo(b.base_floor); }}), " · ");
  sub.append(hudMeta(F));
  proj.append(sub);
}
function rideTo(floor){
  if (typeof FLOORS !== "undefined" && FLOORS.has(floor)) switchToFloor(floor);
}

// ---- ⚙ Settings panel ------------------------------------------------------
let panel = null;
function buildPanel(){
  const modeBtns = MODES.map(([m, label, hint]) =>
    h("button", {type:"button", class:"mode-opt", "data-mode":m, "aria-pressed":"false",
      onclick: () => pickMode(m, true)}, h("b", {text:label}), h("span", {text:hint})));
  const rows = TIERS.map(t => h("div", {class:"lvl-row"},
    h("span", {class:"lvl-tier"}, h("b", {text:t}), h("span", {class:"hint", text:TIER_ROLE[t]})),
    h("select", {id:"lvl-backend-"+t, "aria-label":t+" backend", onchange: syncLocalHint}),
    h("input", {id:"lvl-model-"+t, type:"text", "aria-label":t+" model", spellcheck:"false",
      autocomplete:"off"})));
  const diffs = DIFFS.map(d => h("label", {class:"wl", for:"diff-"+d}, d,
    h("select", {id:"diff-"+d}, ...TIERS.map(t => h("option", {value:t, text:t})))));
  panel = h("div", {id:"settings-modal", class:"overlay hidden", role:"dialog",
      "aria-modal":"true", "aria-labelledby":"settings-title"},
    h("div", {class:"dialog build settings"},
      h("div", {class:"dialog-head"},
        h("h2", {id:"settings-title", text:"⚙ Settings"}),
        h("button", {type:"button", class:"icon-btn", title:"Close", "aria-label":"Close",
          "data-dismiss":"", text:"✕", onclick: hidePanel})),
      h("p", {class:"cp-note", text:"Defaults for every new build. A running or finished floor keeps the settings it started with."}),
      h("section", {class:"fset"},
        h("span", {class:"fset-h", text:"Model mode"}),
        h("div", {class:"mode-pick", role:"group", "aria-label":"model mode"}, ...modeBtns)),
      h("section", {class:"fset"},
        h("span", {class:"fset-h", text:"Model levels"}),
        h("p", {class:"cp-note", text:"Prefilled from the mode — edit any level. The planner rates each ticket's difficulty; the mapping below picks its level."}),
        h("div", {class:"lvl-table"}, ...rows),
        h("button", {type:"button", class:"lvl-reset", text:"↺ Reset to the mode's preset",
          onclick: () => pickMode(curMode(), true)})),
      h("section", {class:"fset"},
        h("span", {class:"fset-h", text:"Difficulty → level"}),
        h("div", {class:"worker-grid diff-grid"}, ...diffs)),
      h("section", {class:"fset", id:"local-sec"},
        h("span", {class:"fset-h", text:"Local server"}),
        h("p", {class:"cp-note", id:"local-hint"}),
        h("div", {class:"field"},
          h("label", {for:"local-url", text:"Server URL "},
            h("span", {class:"hint", text:"— OpenAI-compatible, e.g. Ollama"})),
          h("input", {id:"local-url", type:"text", spellcheck:"false", autocomplete:"off",
            inputmode:"url", oninput: onLocalEdit})),
        h("div", {class:"field"},
          h("label", {for:"local-model", text:"Model"}),
          h("input", {id:"local-model", type:"text", spellcheck:"false", autocomplete:"off",
            oninput: onLocalEdit})),
        h("div", {class:"local-check"},
          h("button", {type:"button", id:"local-check-btn", text:"⟳ Check", onclick: checkLocal}),
          h("span", {id:"local-status", class:"local-status", role:"status", "aria-live":"polite"})),
        h("div", {id:"local-models", class:"local-models"})),
      h("div", {class:"dialog-actions build-actions"},
        h("button", {type:"button", id:"settings-save", class:"primary", text:"Save defaults",
          onclick: saveSettings}),
        h("button", {type:"button", text:"cancel", onclick: hidePanel}),
        h("span", {id:"settings-err", class:"ctl-err", role:"alert"}))));
  panel.addEventListener("click", e => { if (e.target === panel) hidePanel(); });
  document.body.appendChild(panel);
}
function curMode(){
  const b = panel.querySelector(".mode-opt[aria-pressed=true]");
  return b ? b.dataset.mode : "cloud";
}
function presetFor(mode){
  // The server's preset, with the local levels pointed at what is typed now.
  const p = JSON.parse(JSON.stringify(CFG.presets[mode]));
  for (const t of TIERS) if (p[t].backend === "local") p[t].model = $("local-model").value.trim() || p[t].model;
  return p;
}
function fillLevels(levels){
  for (const t of TIERS){
    const sel = $("lvl-backend-"+t);
    sel.innerHTML = "";
    for (const b of CFG.backends) sel.append(h("option", {value:b, text:b}));
    sel.value = levels[t].backend;
    $("lvl-model-"+t).value = levels[t].model;
  }
  syncLocalHint();
}
function pickMode(mode, refill){
  for (const b of panel.querySelectorAll(".mode-opt")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  if (refill) fillLevels(presetFor(mode));
}
function onLocalEdit(){
  // Local levels follow the local model box.
  const m = $("local-model").value.trim();
  for (const t of TIERS) if ($("lvl-backend-"+t).value === "local") $("lvl-model-"+t).value = m;
  $("local-status").textContent = ""; $("local-models").innerHTML = "";
}
function syncLocalHint(){
  const n = TIERS.filter(t => $("lvl-backend-"+t).value === "local").length;
  $("local-hint").textContent = n
    ? n+" level"+(n > 1 ? "s run" : " runs")+" here. Local calls are unmetered and run one at a time."
    : "No level runs locally in this table — Hybrid or Local mode would use it.";
}
function openPanel(){
  if (!panel) buildPanel();
  $("settings-err").textContent = "";
  loadCfg().then(cfg => {
    const s = cfg.settings;
    $("local-url").value = s.local_url;
    $("local-model").value = s.local_model;
    pickMode(s.model_mode, false);
    fillLevels(s.levels);
    for (const d of DIFFS) $("diff-"+d).value = s.ticket_tiers[d];
    $("local-status").textContent = ""; $("local-models").innerHTML = "";
    panel.classList.remove("hidden");
    panel.querySelector(".mode-opt[aria-pressed=true]").focus();
  }).catch(err => { if (typeof appendLogDom === "function") appendLogDom("bad", "ERR "+err.message); });
}
function hidePanel(){ if (panel) panel.classList.add("hidden"); }
async function postJSON(path, body){
  const r = await fetch(path, {method:"POST", headers:postHeaders(), body:JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ("request failed ("+r.status+")"));
  return d;
}
async function saveSettings(){
  const levels = {};
  for (const t of TIERS) levels[t] = {backend:$("lvl-backend-"+t).value, model:$("lvl-model-"+t).value.trim()};
  const ticket_tiers = {};
  for (const d of DIFFS) ticket_tiers[d] = $("diff-"+d).value;
  const body = {model_mode:curMode(), levels, ticket_tiers,
    local_url:$("local-url").value.trim(), local_model:$("local-model").value.trim()};
  const btn = $("settings-save"); btn.disabled = true;
  try {
    CFG = await postJSON("/api/settings", body);
    hidePanel();
    syncForm();
    if (typeof ctlMessage === "function") ctlMessage("Settings saved — new builds use "+levelsLine(body.model_mode, CFG.settings.levels)+".");
  } catch(err){ $("settings-err").textContent = err.message; }
  finally { btn.disabled = false; }
}
async function checkLocal(){
  const st = $("local-status"), list = $("local-models"), btn = $("local-check-btn");
  st.className = "local-status"; st.textContent = "checking…"; list.innerHTML = ""; btn.disabled = true;
  try {
    const d = await postJSON("/api/settings/local-check",
      {url:$("local-url").value.trim(), model:$("local-model").value.trim() || null});
    if (!d.reachable){
      st.className = "local-status bad";
      st.textContent = "✕ unreachable"+(d.error ? " — "+d.error : "");
      return;
    }
    st.className = "local-status ok";
    st.textContent = "● reachable — serves "+d.models.length+" model"+(d.models.length === 1 ? "" : "s")
      + (d.model_served === false ? " · ⚠ "+d.model+" is not among them" : d.model_served ? " · ✓ "+d.model : "");
    for (const m of d.models) list.append(h("button", {type:"button", class:"chip", title:"use this model",
      text:m, onclick: () => { $("local-model").value = m; onLocalEdit(); }}));
  } catch(err){ st.className = "local-status bad"; st.textContent = "✕ "+err.message; }
  finally { btn.disabled = false; }
}

// ---- New Project form: mode line, change banner, Advanced fold -------------
let formBuilt = false;
function buildForm(){
  if (formBuilt) return; formBuilt = true;
  const dialog = document.querySelector("#new-modal .dialog");
  const reqSec = $("req-input").closest(".fset");
  const banner = h("div", {id:"change-banner", class:"change-banner hidden"},
    h("span", {id:"change-text"}),
    h("button", {type:"button", class:"icon-btn", title:"build something new instead",
      "aria-label":"cancel the change", text:"✕", onclick: () => prepareForm(null)}));
  reqSec.prepend(banner);
  const modeLine = h("div", {class:"mode-line"},
    h("span", {class:"hint", text:"Model mode "}), h("b", {id:"form-mode"}),
    h("button", {type:"button", class:"icon-btn", title:"change the defaults in Settings",
      text:"⚙ Settings", onclick: () => openPanel()}));
  reqSec.append(modeLine);
  const sel = (id, opts) => h("select", {id}, ...opts.map(([v, t]) => h("option", {value:v, text:t})));
  const num = (id, attrs) => h("input", Object.assign({id, type:"number", inputmode:"decimal"}, attrs));
  const fld = (id, label, hint, control) => h("div", {class:"field"},
    h("label", {for:id, text:label+" "}, hint ? h("span", {class:"hint", text:"— "+hint}) : null), control);
  const adv = h("details", {class:"fset fold", id:"fold-adv"},
    h("summary", {}, h("span", {class:"fset-h", text:"Advanced"}), h("span", {class:"fset-sum", id:"adv-sum"})),
    h("div", {class:"fset-body"},
      h("div", {class:"adv-grid"},
        fld("adv-fix", "Max fix attempts", "blank = mode default", num("adv-fix", {min:"0", max:"20", step:"1", inputmode:"numeric"})),
        fld("adv-time", "Time cap", "minutes", num("adv-time", {min:"1", step:"1", placeholder:"no cap"})),
        fld("adv-cost", "Cost cap", "US dollars; each ticket reserves $1", num("adv-cost", {min:"0", step:"0.5", placeholder:"no cap"})),
        fld("adv-profile", "Stack", null, sel("adv-profile", [["auto", "auto — the planner picks"]])),
        fld("adv-scope", "Scope", null, sel("adv-scope", [["auto", "auto — judged from the request"],
          ["demo", "demo — the smallest working thing"], ["standard", "standard"]]))),
      h("div", {class:"field"},
        h("label", {for:"adv-spec", text:"Spec "}, h("span", {class:"hint", id:"spec-hint", text:"— paste a design doc, or attach a .md / .txt file"})),
        h("textarea", {id:"adv-spec", rows:"4", placeholder:"optional — the full brief the planner grounds the build in"}),
        h("div", {class:"spec-row"},
          h("label", {class:"btn-file"}, "📎 attach file",
            h("input", {id:"adv-spec-file", type:"file", accept:".md,.markdown,.txt,text/plain,text/markdown", onchange: onSpecFile})),
          h("span", {id:"spec-info", class:"hint"}),
          h("button", {type:"button", id:"spec-clear", class:"icon-btn hidden", text:"✕ remove", onclick: clearSpec}))),
      h("span", {id:"adv-err", class:"ctl-err", role:"alert"})));
  $("fold-scale").before(adv);
  $("adv-spec").addEventListener("input", () => { SPEC = null; specInfo(); });
  dialog.addEventListener("input", syncAdvSummary);
}
function specBytes(t){ return new TextEncoder().encode(t).length; }
function maxSpec(){ return (CFG && CFG.spec_max_bytes) || 200*1024; }
function specInfo(){
  const t = $("adv-spec").value, n = specBytes(t);
  const info = $("spec-info");
  info.textContent = !t ? "" : (SPEC ? SPEC.name+" · " : "")+Math.ceil(n/1024)+" KB of "+Math.round(maxSpec()/1024)+" KB";
  info.classList.toggle("bad", n > maxSpec());
  $("spec-clear").classList.toggle("hidden", !t);
}
function onSpecFile(e){
  const f = e.target.files && e.target.files[0]; if (!f) return;
  const err = $("adv-err"); err.textContent = "";
  if (f.size > maxSpec()){ err.textContent = "Spec file is over "+Math.round(maxSpec()/1024)+" KB."; e.target.value = ""; return; }
  f.text().then(t => { $("adv-spec").value = t; SPEC = {name:f.name}; specInfo(); syncAdvSummary(); })
    .catch(() => { err.textContent = "Could not read that file."; });
  e.target.value = "";
}
function clearSpec(){ $("adv-spec").value = ""; SPEC = null; specInfo(); syncAdvSummary(); }
function fixDefault(){
  const mode = CFG ? CFG.settings.model_mode : "cloud";
  return CFG ? CFG.fix_defaults[mode] : 3;
}
function syncAdvSummary(){
  if (!formBuilt) return;
  const parts = [];
  const fix = $("adv-fix").value.trim();
  parts.push((fix || fixDefault())+" fix attempts");
  if ($("adv-time").value.trim()) parts.push($("adv-time").value.trim()+" min cap");
  if ($("adv-cost").value.trim()) parts.push("$"+$("adv-cost").value.trim()+" cap");
  parts.push("stack "+$("adv-profile").value, "scope "+$("adv-scope").value);
  if ($("adv-spec").value.trim()) parts.push("spec attached");
  $("adv-sum").textContent = parts.join(" · ");
}
function syncForm(){
  if (!formBuilt || !CFG) return;
  const s = CFG.settings;
  $("form-mode").textContent = levelsLine(s.model_mode, s.levels);
  $("adv-fix").placeholder = "mode default ("+fixDefault()+")";
  const prof = $("adv-profile");
  if (prof.options.length === 1) for (const p of CFG.profiles) prof.append(h("option", {value:p, text:p}));
  syncAdvSummary();
}
function prepareForm(F){
  buildForm();
  CHANGE = F && F.passed ? F : null;
  const banner = $("change-banner"), req = $("req-input");
  const label = document.querySelector("label[for=req-input]");
  banner.classList.toggle("hidden", !CHANGE);
  document.querySelector("#new-modal .dialog-head h2").textContent = CHANGE ? "✎ Change floor "+CHANGE.floor : "▶ Start a build";
  if (CHANGE){
    $("change-text").textContent = "✎ Change floor "+CHANGE.floor+" — “"+CHANGE.request+"”. Only the delta is planned and built; the result is the full updated tree on a new floor.";
    label.firstChild.textContent = "What should change? ";
    req.placeholder = "e.g. add a dark-mode toggle and remember the choice";
    req.value = "";
    $("start-btn").textContent = "BUILD THE CHANGE";
  } else {
    label.firstChild.textContent = "What should the factory build? ";
    req.placeholder = "e.g. a URL shortener with custom slugs, link expiry, and per-IP abuse rate-limiting";
    $("start-btn").textContent = "BREAK GROUND";
  }
  $("adv-err").textContent = "";
  syncForm();
  loadCfg().then(syncForm).catch(() => {});
  if (CHANGE) setTimeout(() => req.focus(), 0);
}
// The form's advanced fields for POST /api/projects, or null (and a message) when
// one is out of range. The server validates them all again.
function formExtra(){
  if (!formBuilt) return {};
  const err = $("adv-err"); err.textContent = "";
  const fail = msg => { err.textContent = msg; $("fold-adv").open = true; return null; };
  const out = {};
  const fix = $("adv-fix").value.trim();
  if (fix !== ""){
    const n = Number(fix);
    if (!Number.isInteger(n) || n < 0 || n > 20) return fail("Max fix attempts: a whole number, 0 to 20.");
    out.max_fix_attempts = n;
  }
  const time = $("adv-time").value.trim();
  if (time !== ""){
    const n = Number(time);
    if (!(n > 0) || n > 10080) return fail("Time cap: minutes, more than 0 (at most a week).");
    out.time_cap_minutes = n;
  }
  const cost = $("adv-cost").value.trim();
  if (cost !== ""){
    const n = Number(cost);
    if (!(n > 0) || n > 100000) return fail("Cost cap: dollars, more than 0.");
    out.cost_cap_usd = n;
  }
  out.profile = $("adv-profile").value;
  out.scope = $("adv-scope").value;
  const spec = $("adv-spec").value;
  if (spec.trim()){
    if (specBytes(spec) > maxSpec()) return fail("Spec is over "+Math.round(maxSpec()/1024)+" KB.");
    out.spec = spec;
    if (SPEC) out.spec_name = SPEC.name;
  }
  if (CHANGE) out.base_id = CHANGE.id;
  return out;
}
// After a build starts: a spec and a change target belong to that one build.
function formStarted(){
  if (!formBuilt) return;
  clearSpec();
  prepareForm(null);
}

const gear = $("settings-btn");
if (gear) gear.onclick = openPanel;

window.FoundrySettings = {hudMeta, decorateProj, prepareForm, formExtra, formStarted, openPanel, shortModel, levelsLine};
})();
