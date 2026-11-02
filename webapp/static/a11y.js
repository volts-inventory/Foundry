/* Modal accessibility for every `.overlay` in the page — the static dialogs and the
 * ones insight.js / settings.js build later. Each overlay is labelled as a modal
 * dialog by its first heading; opening one moves focus into it and makes the app
 * behind it inert; Tab stays inside it; Esc presses its [data-dismiss] button; and
 * closing it hands focus back to whatever opened it. The modules keep opening and
 * closing overlays by toggling `hidden` — this watches for that. */
(function(){
  "use strict";
  const FOCUSABLE = "a[href],button:not([disabled]),input:not([disabled]):not([type=hidden])," +
    "select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex='-1'])";
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const stack = [];          // open overlays, topmost last: {ov, opener, openerId}
  const trail = [];          // recently focused elements, newest last
  let uid = 0;
  document.addEventListener("focusin", e => {
    trail.push(e.target);
    if (trail.length > 8) trail.shift();
  }, true);
  // Whoever opened `ov`: the newest focused element outside it. Openers can move
  // focus into the dialog before this runs (Settings focuses its mode button).
  function openerOf(ov){
    for (let i = trail.length - 1; i >= 0; i--){
      const el = trail[i];
      if (el && el !== document.body && !ov.contains(el) && (el.isConnected || el.id)) return el;
    }
    return null;
  }

  const isOpen = ov => ov.isConnected && !ov.classList.contains("hidden");
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const focusables = root => [...root.querySelectorAll(FOCUSABLE)].filter(shown);

  function label(ov){
    const box = ov.querySelector("[role=dialog],[role=alertdialog]") || ov;
    if (box === ov && !ov.hasAttribute("role")) ov.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    if (!box.hasAttribute("aria-labelledby") && !box.hasAttribute("aria-label")){
      const h = ov.querySelector("h2, h3");
      if (h){ if (!h.id) h.id = "dlg-title-" + (++uid); box.setAttribute("aria-labelledby", h.id); }
    }
  }

  function focusInto(ov){
    if (ov.contains(document.activeElement)) return;   // the opener already placed focus
    const first = ov.querySelector("[data-autofocus]");
    const target = (first && shown(first)) ? first : focusables(ov)[0];
    if (target) target.focus(); else { ov.tabIndex = -1; ov.focus(); }
  }

  function sync(){
    const open = [...document.querySelectorAll(".overlay")].filter(isOpen);
    const app = document.getElementById("app");
    let back = null;
    for (let i = stack.length - 1; i >= 0; i--){
      const s = stack[i];
      if (open.includes(s.ov)) continue;
      stack.splice(i, 1);
      const lost = !document.activeElement || document.activeElement === document.body
        || !document.activeElement.isConnected || s.ov.contains(document.activeElement);
      if (lost && i === stack.length){
        // A re-rendered opener (the elevator redraws its buttons) is found again by id.
        const again = s.opener && !s.opener.isConnected && s.opener.id ? document.getElementById(s.opener.id) : null;
        back = s.opener && s.opener.isConnected ? s.opener : again
          || (stack.length ? focusables(stack[stack.length-1].ov)[0] : document.getElementById("new-btn"));
      }
    }
    if (app) app.inert = stack.length > 0;       // un-inert first: an inert opener can't take focus
    if (back && shown(back)) back.focus();
    for (const ov of open){
      if (stack.some(s => s.ov === ov)) continue;
      label(ov);
      stack.push({ov, opener: openerOf(ov)});
      setTimeout(() => { if (isOpen(ov)) focusInto(ov); }, 0);
    }
    if (app) app.inert = stack.length > 0;
  }

  new MutationObserver(muts => {
    for (const m of muts){
      if (m.type === "attributes" ? m.target.classList.contains("overlay")
          : [...m.addedNodes, ...m.removedNodes].some(n => n.classList && n.classList.contains("overlay"))){
        sync(); return;
      }
    }
  }).observe(document.body, {subtree:true, childList:true, attributes:true, attributeFilter:["class"]});

  document.addEventListener("keydown", e => {
    const top = stack.length ? stack[stack.length-1].ov : null;
    if (e.key === "Escape"){
      const scope = top || document.getElementById("ctl-panel");
      if (!scope || (!top && (scope.classList.contains("hidden") || !scope.contains(e.target)))) return;
      const x = scope.querySelector("[data-dismiss]");
      if (!x) return;
      e.preventDefault(); e.stopPropagation();
      x.click();
      if (!top){ const b = document.querySelector("#floor-ctl button"); if (b) b.focus(); }
      return;
    }
    if (e.key !== "Tab" || !top) return;
    const items = focusables(top);
    if (!items.length){ e.preventDefault(); return; }
    const first = items[0], last = items[items.length-1], at = document.activeElement;
    if (!top.contains(at)){ e.preventDefault(); (e.shiftKey ? last : first).focus(); }
    else if (e.shiftKey && at === first){ e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && at === last){ e.preventDefault(); first.focus(); }
  }, true);

  document.addEventListener("DOMContentLoaded", sync);
  window.A11y = {reducedMotion: () => motion.matches, sync};
})();
