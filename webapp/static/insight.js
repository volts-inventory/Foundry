// Foundry Workshop — insight: the live meter, queue position, why a floor stopped
// (halt panel), how to run what shipped (run card + manifest), and connection
// handling (disconnect banner, reconnect, failed rehydrate).
//
// Loaded BEFORE game.js; game.js calls in through window.Insight at a few hooks.
// Everything here reaches game.js's globals (FLOORS, ACTIVE, resumeFloor, …) only
// at call time, never at load time.
"use strict";

(function(){
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const OP_STOP = new Set(["stopped", "paused"]);

  // ---- formatting -----------------------------------------------------------
  function fmtTok(n){
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 1e6) return (n/1e3).toFixed(n < 1e4 ? 1 : 0)+"K";
    return (n/1e6).toFixed(n < 1e7 ? 2 : 1)+"M";
  }
  function fmtClock(s){
    s = Math.max(0, Math.floor(Number(s) || 0));
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
    const mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h+":" : "")+mm+":"+String(sec).padStart(2, "0");
  }
  const comma = (n) => (Number(n) || 0).toLocaleString("en-US");

  // ---- live meter + queue position (HUD) ------------------------------------
  function elapsedOf(F){
    const m = F.meter; if (!m) return 0;
    let s = Number(m.elapsed_s) || 0;
    if (F.status === "running" && !F.queuePos && m.at){
      s += Math.max(0, Date.now()/1000 - m.at);
    }
    return s;
  }
  function renderMeter(){
    const tag = $("meter-tag"); if (!tag) return;
    const F = (typeof FLOORS !== "undefined") ? FLOORS.get(ACTIVE) : null;
    tag.className = "meter"; tag.textContent = ""; tag.style.removeProperty("--fill");
    if (!F){ tag.hidden = true; return; }
    if (F.queuePos > 0 && F.status !== "done"){
      tag.hidden = false; tag.classList.add("queued");
      tag.append(el("span", "m-long", "QUEUED — POSITION "+F.queuePos),
                 el("span", "m-short", "QUEUED #"+F.queuePos));
      tag.title = "Every build slot is busy; this floor starts when its turn comes.";
      return;
    }
    const m = F.meter;
    if (!m){ tag.hidden = true; return; }
    tag.hidden = false;
    const done = F.status === "done";
    const tok = el("span", "m-tok", fmtTok(m.used)+(m.cap ? " / "+fmtTok(m.cap) : "")+" TOK");
    const clk = el("span", "m-clk", fmtClock(elapsedOf(F))+(m.time_cap_s ? " / "+fmtClock(m.time_cap_s) : ""));
    if (done) tag.append(el("span", "m-lbl", "TOTAL"));
    tag.append(tok, el("span", "m-sep", "·"), clk);
    if (m.cap){
      const frac = Math.min(1, (Number(m.used) || 0) / m.cap);
      tag.style.setProperty("--fill", (frac*100).toFixed(1)+"%");
      tag.classList.add("capped");
      if (frac >= 0.9) tag.classList.add("hot");
    }
    tag.title = (done ? "This build used " : "Used so far: ")+comma(m.used)+" tokens"
      +(m.cap ? " of a "+comma(m.cap)+"-token cap" : " (no cap)")
      +" · "+fmtClock(elapsedOf(F))+" elapsed"
      +" (cache reads count 1/10).";
  }
  setInterval(() => {
    const F = (typeof FLOORS !== "undefined") ? FLOORS.get(ACTIVE) : null;
    if (F && F.status === "running" && F.meter) renderMeter();
  }, 1000);

  // ---- the insight card (quest log area) + full report (modal) ---------------
  function hideCard(){
    const c = $("insight-card"); if (!c) return;
    c.classList.add("hidden"); c.innerHTML = "";
  }
  function loadInsight(F){
    if (!F || F.status !== "done") return;
    if (F.insight){ renderCard(F); return; }
    if (F.insightLoading) return;
    F.insightLoading = fetch("/api/projects/"+F.id+"/insight")
      .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP "+r.status)))
      .then(d => {
        F.insightLoading = null;
        if (F.status !== "done") return;                          // resumed meanwhile
        if (d.status !== "done"){
          // `done` streams a moment before the server closes the run out; ask again.
          F.insightTries = (F.insightTries || 0) + 1;
          if (F.insightTries <= 20) setTimeout(() => loadInsight(F), 500);
          return;
        }
        F.insightTries = 0;
        F.insight = d;
        if (d.meter) F.meter = d.meter;
        if (F.floor === ACTIVE){ renderCard(F); renderMeter(); }
      })
      .catch(() => {
        F.insightLoading = null;
        if (F.floor !== ACTIVE) return;
        const c = $("insight-card"); c.innerHTML = ""; c.className = "insight-card bad";
        const row = el("div", "ic-row");
        const retry = el("button", "", "Retry"); retry.type = "button";
        retry.onclick = () => loadInsight(F);
        row.append(el("span", "ic-cause", "Couldn't load this floor's report."), retry);
        c.append(row);
      });
  }
  function actionButton(F, a){
    const b = el("button", a.primary ? "primary" : "", a.label); b.type = "button";
    if (a.kind === "resume") b.onclick = () => { closeReport(); resumeFloor(F, a.body || {}); };
    else if (a.kind === "retry") b.onclick = () => { closeReport(); showRetryForm(F); };
    else if (a.kind === "files") b.onclick = () => { closeReport(); openFilesFor({id:F.id, floor:F.floor}); };
    return b;
  }
  function cardShell(F, cls, title){
    const c = $("insight-card"); c.innerHTML = "";
    c.className = "insight-card "+cls;
    const d = el("details", "ic"); d.open = F.insightOpen !== false;
    d.ontoggle = () => { F.insightOpen = d.open; };
    const s = el("summary", "ic-title", title);
    d.append(s); c.append(d);
    c.scrollTop = 0;
    return d;
  }
  function renderCard(F){
    const d = F.insight;
    if (!d || F.floor !== ACTIVE){ return; }
    if (d.halt){
      const h = d.halt;
      const box = cardShell(F, OP_STOP.has(h.phase) ? "paused" : "held", "✕ "+h.title.toUpperCase());
      if (OP_STOP.has(h.phase)) box.querySelector("summary").textContent = "❚❚ "+h.title.toUpperCase();
      box.append(el("p", "ic-cause", h.cause));
      const best = keyFinding(h.gates || []);
      if (best) box.append(el("pre", "ic-finding", best.split("\n").slice(0, 4).join("\n")));
      box.append(el("p", "ic-next", "NEXT: "+h.next));
      const acts = el("div", "ic-acts");
      for (const a of (h.actions || []).filter(a => a.kind !== "files").slice(0, 2)) acts.append(actionButton(F, a));
      const more = el("button", "", "Details"); more.type = "button";
      more.onclick = () => openReport(F);
      acts.append(more);
      box.append(acts);
    } else if (d.card){
      const k = d.card;
      const box = cardShell(F, "shipped", "★ SHIPPED — HOW TO RUN IT");
      if (k.stack) box.append(el("p", "ic-stack", k.stack));
      const steps = (k.steps || []).filter(s => s.label === "Run" || s.label === "Test" || s.label === "Install");
      if (steps.length){
        const dl = el("dl", "ic-steps");
        for (const s of steps) dl.append(el("dt", "", s.label), cmdNode(s.cmd));
        box.append(dl);
      } else {
        box.append(el("p", "ic-cause", "The build didn't record run or test commands — see the manifest."));
      }
      const acts = el("div", "ic-acts");
      const man = el("button", "primary", "Manifest"); man.type = "button";
      man.onclick = () => openReport(F);
      const files = el("button", "", "Files"); files.type = "button";
      files.onclick = () => openFilesFor({id:F.id, floor:F.floor});
      acts.append(man, files);
      box.append(acts);
    } else {
      hideCard();
    }
  }
  // The one finding worth showing in the small card: a named error line beats the
  // pytest banner ("____ ERROR collecting x ____") that usually comes first.
  function keyFinding(gates){
    const all = gates.flatMap(g => g.findings || []);
    return all.find(f => /\b\w+(Error|Exception):/.test(f))
      || all.find(f => /error|failed|assert/i.test(f) && !/^[\s_=-]{6,}/.test(f))
      || all[0] || "";
  }
  function cmdNode(cmd){
    const dd = el("dd", "ic-cmd");
    const code = el("code", "", cmd);
    const copy = el("button", "ic-copy", "copy"); copy.type = "button";
    copy.title = "Copy the command"; copy.setAttribute("aria-label", "Copy the command");
    copy.onclick = () => {
      const done = () => { copy.textContent = "copied"; setTimeout(() => { copy.textContent = "copy"; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(cmd).then(done, () => {});
    };
    dd.append(code, copy);
    return dd;
  }

  // The full report: every failing gate's findings, the fixer's notes, the meter
  // (halted) — or the run steps and the manifest (shipped).
  function ensureReportModal(){
    let ov = $("insight-modal");
    if (ov) return ov;
    ov = el("div", "overlay hidden"); ov.id = "insight-modal";
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-labelledby", "insight-h");
    const dlg = el("div", "dialog insight");
    const head = el("div", "repo-head");
    const h = el("h2", "", ""); h.id = "insight-h";
    const x = el("button", "icon-btn", "✕"); x.type = "button";
    x.title = "close"; x.setAttribute("aria-label", "close"); x.onclick = closeReport;
    head.append(h, x);
    const body = el("div", "insight-body"); body.id = "insight-body";
    dlg.append(head, body);
    ov.append(dlg);
    ov.addEventListener("click", e => { if (e.target === ov) closeReport(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !ov.classList.contains("hidden")) closeReport(); });
    document.body.append(ov);
    return ov;
  }
  function closeReport(){ const ov = $("insight-modal"); if (ov) ov.classList.add("hidden"); }
  function section(title){
    const s = el("section", "ir-sec");
    s.append(el("h3", "", title));
    return s;
  }
  function kv(dl, k, v){ dl.append(el("dt", "", k), el("dd", "", v)); }
  function openReport(F){
    const d = F.insight; if (!d) return;
    const ov = ensureReportModal();
    const body = $("insight-body"); body.innerHTML = "";
    if (d.halt){
      const h = d.halt;
      $("insight-h").textContent = "FLOOR "+F.floor+" · "+h.title.toUpperCase();
      body.append(el("p", "ic-cause", h.cause), el("p", "ic-next", "NEXT: "+h.next));
      const acts = el("div", "ic-acts");
      for (const a of h.actions || []) acts.append(actionButton(F, a));
      if (acts.childNodes.length) body.append(acts);
      if (h.gates && h.gates.length){
        const s = section("Failing gates");
        for (const g of h.gates){
          const det = el("details", "ir-gate"); det.open = g === h.gates[0];
          const n = (g.findings || []).length + (g.more || 0);
          det.append(el("summary", "", g.name.toUpperCase()+" — "+g.status.toUpperCase()
            +(n ? " · "+n+" finding"+(n === 1 ? "" : "s") : "")));
          if (!n) det.append(el("p", "muted", "No findings were recorded for this gate."));
          for (const f of g.findings || []) det.append(el("pre", "ic-finding", f));
          if (g.more) det.append(el("p", "muted", "…and "+g.more+" more (download the .zip for EVIDENCE.md)."));
          s.append(det);
        }
        if (h.passed_gates && h.passed_gates.length)
          s.append(el("p", "muted", "Passed: "+h.passed_gates.map(x => x.toUpperCase()).join(", ")));
        body.append(s);
      }
      const fx = section("Fix attempts");
      const fdl = el("dl", "ir-kv");
      kv(fdl, "Made", h.fixes && h.fixes.attempts != null ? String(h.fixes.attempts) : "—");
      kv(fdl, "Limit", (h.fixes ? h.fixes.limit : "—")+" per failing check");
      fx.append(fdl);
      if (h.notes && h.notes.length){
        const ul = el("ul", "ir-notes");
        for (const n of h.notes){
          const li = el("li");
          li.append(el("b", "", n.name+(n.by ? " · "+n.by : "")+": "), document.createTextNode(n.text));
          ul.append(li);
        }
        fx.append(ul);
      }
      body.append(fx);
      if (h.meter) body.append(meterSection(h.meter));
      if (h.detail){
        const det = el("details", "ir-detail");
        det.append(el("summary", "", "Technical details"), el("pre", "ic-finding", h.detail));
        body.append(det);
      }
    } else if (d.card){
      const k = d.card;
      $("insight-h").textContent = "FLOOR "+F.floor+" · BUILD MANIFEST";
      const run = section("How to run it");
      if (k.stack) run.append(el("p", "ic-stack", k.stack));
      if (k.steps && k.steps.length){
        const dl = el("dl", "ic-steps");
        for (const s of k.steps) dl.append(el("dt", "", s.label), cmdNode(s.cmd));
        run.append(dl);
      } else run.append(el("p", "muted", "No commands were recorded for this stack."));
      run.append(el("p", "muted", "Download the .zip from REPO, unpack it, and run these from its root."));
      body.append(run);
      const mod = section("Models");
      const mdl = el("dl", "ir-kv");
      const tiers = Object.keys(k.models || {}).sort();
      for (const t of tiers) kv(mdl, t+" ("+({T1:"complex", T2:"standard", T3:"trivial"}[t] || "level")+")",
        (k.models[t] || []).join(", ") || "—");
      if (!tiers.length) kv(mdl, "Served", (k.served_models || []).join(", ") || "—");
      kv(mdl, "Model calls", comma(k.calls));
      if (k.sessions) kv(mdl, "Coding sessions", comma(k.sessions));
      mod.append(mdl);
      body.append(mod);
      const tk = section("Tokens & time");
      const tdl = el("dl", "ir-kv");
      kv(tdl, "In / out", comma(k.tokens.in)+" / "+comma(k.tokens.out));
      kv(tdl, "Cache read / write", comma(k.tokens.cache_read)+" / "+comma(k.tokens.cache_write));
      if (k.meter){
        kv(tdl, "Against the cap", comma(k.meter.used)+(k.meter.cap ? " of "+comma(k.meter.cap) : " (no cap)"));
        kv(tdl, "Time", fmtClock(k.meter.elapsed_s));
      }
      if (k.cost_usd) kv(tdl, "Estimated cost", "$"+k.cost_usd.toFixed(4));
      tk.append(tdl);
      body.append(tk);
      if (k.tickets && k.tickets.length){
        const ts = section("Tickets ("+k.tickets.length+")");
        const ul = el("ul", "ir-tickets");
        for (const t of k.tickets){
          const li = el("li", "st-"+t.status.replace(/\s+/g, "-"));
          li.append(el("b", "", t.id), document.createTextNode(" "+(t.title || "")), el("span", "tk-st", t.status));
          ul.append(li);
        }
        ts.append(ul);
        body.append(ts);
      }
      if (k.gates && k.gates.length){
        const gs = section("Gates");
        gs.append(el("p", "", k.gates.map(g => g.name.toUpperCase()+" "+g.status).join(" · ")));
        body.append(gs);
      }
      if (k.source !== "manifest")
        body.append(el("p", "muted", "No BUILD-MANIFEST was recorded for this build; this is read from its saved state."));
    }
    ov.classList.remove("hidden");
    const x = ov.querySelector(".icon-btn"); if (x) x.focus();
  }
  function meterSection(m){
    const s = section("Tokens & time");
    const dl = el("dl", "ir-kv");
    kv(dl, "Tokens", comma(m.used)+(m.cap ? " of a "+comma(m.cap)+" cap" : " (no cap)"));
    kv(dl, "Time", fmtClock(m.elapsed_s)+(m.time_cap_s ? " of "+fmtClock(m.time_cap_s) : ""));
    s.append(dl);
    return s;
  }

  // ---- connection: disconnect banner + reconnect ------------------------------
  const DOWN = new Set();
  function banner(){
    let b = $("conn-banner");
    if (!DOWN.size){ if (b) b.remove(); return; }
    if (!b){
      b = el("div"); b.id = "conn-banner"; b.setAttribute("role", "status"); b.setAttribute("aria-live", "polite");
      const msg = el("span"); msg.id = "conn-banner-msg";
      const now = el("button", "", "Retry now"); now.type = "button";
      now.onclick = () => { for (const f of DOWN) { const F = FLOORS.get(f); if (F) reconnect(F, 0); } };
      b.append(msg, now);
      document.body.append(b);
    }
    $("conn-banner-msg").textContent = "Disconnected — reconnecting…";
  }
  function streamOk(F){
    if (F.reconnectTimer){ clearTimeout(F.reconnectTimer); F.reconnectTimer = null; }
    F.backoff = 0;
    if (DOWN.delete(F.floor)) banner();
  }
  function streamError(F, es){
    if (es !== F.es) return;                    // an old stream we replaced
    DOWN.add(F.floor); banner();
    // A live EventSource retries by itself (sending Last-Event-ID). A CLOSED one
    // (the server answered with an error, or restarted) is ours to reopen.
    if (es.readyState === EventSource.CLOSED) reconnect(F);
  }
  function reconnect(F, delay){
    if (F.reconnectTimer) clearTimeout(F.reconnectTimer);
    F.backoff = Math.min(30000, F.backoff ? F.backoff*2 : 1000);
    const wait = delay != null ? delay : F.backoff;
    F.reconnectTimer = setTimeout(async () => {
      F.reconnectTimer = null;
      let rows;
      try {
        const r = await fetch("/api/projects");
        if (r.status === 401){ location.reload(); return; }    // signed out: the login page
        if (!r.ok) throw new Error("HTTP "+r.status);
        rows = (await r.json()).projects || [];
      } catch(_){ reconnect(F); return; }
      if (FLOORS.get(F.floor) !== F) { DOWN.delete(F.floor); banner(); return; }
      if (!rows.some(x => x.id === F.id)){ DOWN.delete(F.floor); banner(); return; }   // gone
      if (F.es) F.es.close();
      openFloorStream(F, F.cursor);              // resumes at ?from=<cursor>
    }, wait);
  }

  // ---- a failed rehydrate: an error state with retry, not the New Project modal ---
  function rehydrateFailed(err, retry){
    if (err && err.status === 401){ location.reload(); return; }
    let ov = $("rehydrate-error");
    if (!ov){
      ov = el("div", "overlay"); ov.id = "rehydrate-error";
      const dlg = el("div", "dialog rehydrate");
      dlg.setAttribute("role", "alertdialog"); dlg.setAttribute("aria-labelledby", "rehydrate-h");
      const h = el("h2", "", "Can't reach the Workshop"); h.id = "rehydrate-h";
      const p = el("p", "cp-note"); p.id = "rehydrate-msg";
      const acts = el("div", "dialog-actions");
      const again = el("button", "primary", "Retry"); again.type = "button"; again.id = "rehydrate-retry";
      acts.append(again);
      dlg.append(h, p, acts); ov.append(dlg); document.body.append(ov);
    }
    ov.classList.remove("hidden");
    ov._tries = (ov._tries || 0) + 1;
    const wait = Math.min(30, 2 ** ov._tries);
    $("rehydrate-msg").textContent = "Your floors couldn't be loaded ("+(err && err.message || "network error")
      +"). The server may be restarting — trying again in "+wait+"s.";
    const go = () => { clearTimeout(ov._timer); ov.classList.add("hidden"); retry(); };
    $("rehydrate-retry").onclick = go;
    clearTimeout(ov._timer);
    ov._timer = setTimeout(go, wait*1000);
  }
  function rehydrated(){
    const ov = $("rehydrate-error");
    if (ov){ clearTimeout(ov._timer); ov._tries = 0; ov.classList.add("hidden"); }
  }

  // ---- files: the draft label + the truncation notice ----------------------------
  function filesLoaded(d){
    const head = document.querySelector("#files-modal .repo-head"); if (!head) return;
    let tag = $("files-draft");
    if (!d || !d.draft){ if (tag) tag.remove(); return; }
    if (!tag){ tag = el("span", "draft-tag"); tag.id = "files-draft"; head.append(tag); }
    tag.textContent = "DRAFT — NOT SHIPPED";
    tag.title = "This floor did not pass its gates: these are the files of its latest attempt, "
      + "kept so you can see how far it got. Nothing here was released.";
  }
  function fileShown(view, truncated){
    if (!truncated) return;
    const note = el("span", "trunc-note",
      "Truncated — this file is "+comma(truncated)+" bytes and only its start is shown. "
      +"Download the .zip for the whole file.");
    view.prepend(note);
  }

  // ---- event hooks from game.js --------------------------------------------------
  function onEvent(F, ev, isActive){
    switch (ev.type){
      case "meter":
        F.meter = ev;
        if (isActive) renderMeter();
        break;
      case "queue":
        F.queuePos = Number(ev.position) || 0;
        if (F.queuePos > 0) logTo(F, "gold", "QUEUED — POSITION "+F.queuePos+" (all build slots busy)");
        else logTo(F, "gold", "A build slot is free — starting.");
        if (isActive) renderMeter();
        break;
      case "resumed":
        F.insight = null; F.queuePos = 0;
        if (isActive){ hideCard(); closeReport(); renderMeter(); }
        break;
      case "done":
        if (ev.meter) F.meter = ev.meter;
        F.queuePos = 0; F.insight = null;
        if (isActive){ renderMeter(); loadInsight(F); }
        break;
    }
  }
  function onFloor(F){
    renderMeter();
    closeReport();
    if (!F){ hideCard(); return; }
    if (F.status === "done") loadInsight(F);
    else hideCard();
  }

  window.Insight = {onEvent, onFloor, streamOk, streamError, rehydrateFailed, rehydrated,
                    filesLoaded, fileShown, renderMeter};
})();
