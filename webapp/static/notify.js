/* Tells you a build finished while you were in another tab: a browser notification
 * (permission is asked once, on the first build you start — a "no" is respected
 * silently), a title badge ("✔ shipped" / "⏹ halted" / "⏹ capped") and a favicon
 * with a status dot. The badge and icon clear when the tab comes back. */
(function(){
  "use strict";
  const BASE_TITLE = document.title;
  const ASKED_KEY = "foundry.notify.asked";
  const LIVE_WINDOW_S = 120;     // a `done` older than this is a replay, not news
  let pending = [];              // [{label, ok}] since the tab was hidden
  let origIcons = null;

  const supported = () => "Notification" in window;
  function asked(){ try { return !!localStorage.getItem(ASKED_KEY); } catch(_){ return false; } }
  function markAsked(){ try { localStorage.setItem(ASKED_KEY, "1"); } catch(_){ /* private mode */ } }

  // Called from the Start button's click, so browsers that need a user gesture allow it.
  function askOnce(){
    if (!supported() || Notification.permission !== "default" || asked()) return;
    markAsked();
    try {
      const p = Notification.requestPermission();
      if (p && p.catch) p.catch(() => {});
    } catch(_){ /* old Safari: callback form only; nothing to do on refusal */ }
  }

  function outcome(ev){
    if (ev.passed) return {label:"✔ shipped", ok:true, word:"shipped"};
    if (ev.phase === "token_cap" || ev.phase === "time_cap")
      return {label:"⏹ capped", ok:false, word:"hit its "+(ev.phase === "time_cap" ? "time" : "token")+" cap"};
    if (ev.phase === "stopped" || ev.phase === "paused") return null;   // you did that yourself
    return {label:"⏹ halted", ok:false, word:"halted — nothing shipped"};
  }

  function iconFor(ok){
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const g = c.getContext("2d");
    g.fillStyle = "#0e1320"; g.fillRect(0, 0, 64, 64);
    g.fillStyle = "#ffcf52"; g.font = "bold 40px monospace"; g.textAlign = "center";
    g.textBaseline = "middle"; g.fillText("F", 26, 32);
    g.fillStyle = ok ? "#5ddc7a" : "#ff5d6c";
    g.beginPath(); g.arc(48, 48, 15, 0, Math.PI*2); g.fill();
    g.fillStyle = "#0e1320"; g.font = "bold 20px monospace"; g.fillText(ok ? "✓" : "!", 48, 49);
    return c.toDataURL("image/png");
  }
  function setIcon(href){
    const links = [...document.querySelectorAll("link[rel~=icon]")];
    if (!origIcons) origIcons = links.map(l => [l, l.href, l.type]);
    for (const l of links){ l.href = href; l.type = "image/png"; }
  }
  function restore(){
    pending = [];
    document.title = BASE_TITLE;
    if (origIcons) for (const [l, href, type] of origIcons){ l.href = href; l.type = type; }
    origIcons = null;
  }

  function buildEnded(F, ev){
    const o = outcome(ev);
    if (!o || !document.hidden) return;
    const at = ev.meter && Number(ev.meter.at);
    if (!at || Math.abs(Date.now()/1000 - at) > LIVE_WINDOW_S) return;
    pending.push(o);
    const bad = pending.some(p => !p.ok);
    const head = pending.length > 1 ? pending.length+" builds finished" : o.label;
    document.title = head + " · " + BASE_TITLE;
    setIcon(iconFor(!bad));
    if (supported() && Notification.permission === "granted"){
      try {
        const n = new Notification("Floor "+F.floor+" "+o.word, {
          body: F.title || F.request || "", tag: "foundry-floor-"+F.floor, icon: "/icon-192.png"});
        n.onclick = () => { window.focus(); if (window.Workshop) window.Workshop.showFloor(F.floor); n.close(); };
      } catch(_){ /* e.g. Android Chrome only notifies from a service worker */ }
    }
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) restore(); });
  window.Notify = {askOnce, buildEnded};
})();
