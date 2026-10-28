"""The student's exam page (one self-contained HTML document served by the CBT server).

It uses no external files or libraries. All text from the database is inserted with ``textContent`` and never
as HTML, so a question cannot inject script. The page runs under a nonce-based Content-Security-Policy.
"""

import html

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>__SCHOOL__ – Computer Based Test</title>
<style nonce="__NONCE__">
:root{--bg:#f4f6fb;--card:#fff;--ink:#16213a;--muted:#5b6784;--line:#dbe1ee;--brand:#2456d6;--brand-ink:#fff;--ok:#12805c;--warn:#b25e00;--bad:#c0392b;--soft:#eaf0ff;--shadow:0 6px 24px rgba(20,35,80,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1524;--card:#18203a;--ink:#e8ecf8;--muted:#9aa6c6;--line:#2b3658;--brand:#6c93ff;--brand-ink:#0b1022;--ok:#46d1a0;--warn:#ffb454;--bad:#ff7b6b;--soft:#202c52;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh}
button,input{font:inherit;color:inherit}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header.top h1{font-size:1rem;margin:0;font-weight:650}
header.top .who{color:var(--muted);font-size:.875rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:var(--shadow)}
.center{max-width:440px;margin:6vh auto}
h2{margin:.2em 0 .4em;font-size:1.35rem}
.muted{color:var(--muted)}
label{display:block;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=password]{width:100%;padding:12px 14px;border:1.5px solid var(--line);border-radius:12px;background:var(--bg);min-height:48px}
input:focus-visible,button:focus-visible{outline:3px solid var(--brand);outline-offset:2px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding:0 20px;border:1.5px solid var(--line);border-radius:12px;background:var(--card);cursor:pointer;font-weight:600}
.btn.primary{background:var(--brand);color:var(--brand-ink);border-color:var(--brand)}
.btn.danger{background:var(--bad);border-color:var(--bad);color:#fff}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn.block{width:100%;margin-top:18px}
.err{color:var(--bad);margin:12px 0 0;min-height:1.4em}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}
.chip{background:var(--soft);border-radius:999px;padding:6px 14px;font-weight:600;font-size:.9rem}
.pre{white-space:pre-wrap}
.exam{display:grid;grid-template-columns:1fr 260px;gap:16px;align-items:start}
@media (max-width:820px){.exam{grid-template-columns:1fr}.nav{order:-1}}
.qhead{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:.9rem;margin-bottom:8px}
.prompt{font-size:1.15rem;font-weight:600;white-space:pre-wrap;overflow-wrap:anywhere}
.opts{display:grid;gap:10px;margin-top:16px}
.opt{display:flex;gap:12px;align-items:flex-start;padding:14px;border:1.5px solid var(--line);border-radius:12px;cursor:pointer;background:var(--bg);text-align:left;width:100%}
.opt .key{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--soft);font-weight:700}
.opt[aria-checked=true]{border-color:var(--brand);background:var(--soft)}
.opt[aria-checked=true] .key{background:var(--brand);color:var(--brand-ink)}
.opt .txt{overflow-wrap:anywhere;white-space:pre-wrap;padding-top:3px}
.controls{display:flex;gap:10px;justify-content:space-between;flex-wrap:wrap;margin-top:20px}
.timer{font-variant-numeric:tabular-nums;font-weight:700;font-size:1.1rem;padding:6px 14px;border-radius:999px;background:var(--soft)}
.timer.low{background:var(--warn);color:#fff}
.timer.crit{background:var(--bad);color:#fff;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.6}}
@media (prefers-reduced-motion:reduce){.timer.crit{animation:none}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(42px,1fr));gap:8px;margin-top:10px}
.cell{min-height:42px;border-radius:10px;border:1.5px solid var(--line);background:var(--bg);cursor:pointer;font-weight:600}
.cell.done{background:var(--ok);border-color:var(--ok);color:#fff}
.cell.flag{box-shadow:inset 0 -4px 0 var(--warn)}
.cell.now{outline:3px solid var(--brand);outline-offset:1px}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:.8rem;color:var(--muted);margin-top:12px}
.banner{position:fixed;left:12px;right:12px;bottom:12px;max-width:560px;margin:auto;padding:12px 16px;border-radius:12px;background:var(--warn);color:#fff;font-weight:600;z-index:9;display:none}
.banner.show{display:block}
.saved{font-size:.85rem;color:var(--muted)}
dialog{border:1px solid var(--line);border-radius:16px;padding:22px;max-width:440px;width:calc(100% - 32px);background:var(--card);color:var(--ink)}
dialog::backdrop{background:rgba(10,16,35,.55)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.tc{text-align:center}
.big{font-size:3rem;font-weight:800;margin:.1em 0}
</style>
</head>
<body>
<header class="top"><h1>__SCHOOL__ · Computer Based Test</h1><div class="who" id="who"></div></header>
<main class="wrap" id="app" aria-live="polite"></main>
<div class="banner" id="banner" role="status"></div>
<dialog id="confirm"><h2 id="cTitle"></h2><p id="cBody" class="muted pre"></p>
<div class="controls"><button class="btn" id="cNo" type="button">Go back</button><button class="btn primary" id="cYes" type="button">Yes, submit</button></div></dialog>
<script nonce="__NONCE__">
"use strict";
const $app = document.getElementById("app"), $who = document.getElementById("who"), $banner = document.getElementById("banner");
const S = {info: null, exam: null, answers: {}, flags: new Set(), index: 0, deadline: 0, tick: null, pending: new Map(), saving: false, finished: false, submitting: false};

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") el.className = v; else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else if (v === true) el.setAttribute(k, ""); else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
function show(...nodes) { $app.replaceChildren(...nodes); window.scrollTo(0, 0); }
function banner(msg) { $banner.textContent = msg || ""; $banner.classList.toggle("show", !!msg); }
async function api(path, body) {
  const opt = body === undefined ? {method: "GET", credentials: "same-origin"} :
    {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Requested-With": "cbt"}, body: JSON.stringify(body)};
  let res;
  try { res = await fetch(path, opt); } catch (e) { const err = new Error("Cannot reach the exam server."); err.network = true; throw err; }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON error */ }
  if (!res.ok) { const err = new Error(data.error || "Something went wrong."); err.code = data.code; err.status = res.status; throw err; }
  return data;
}
function fmt(sec) { sec = Math.max(0, sec | 0); const m = Math.floor(sec / 60), s = sec % 60, hr = Math.floor(m / 60);
  return (hr ? hr + ":" + String(m % 60).padStart(2, "0") : String(m)) + ":" + String(s).padStart(2, "0"); }

function loginScreen(message) {
  const adm = h("input", {type: "text", id: "adm", autocomplete: "username", autocapitalize: "characters", spellcheck: "false", maxlength: "40", required: true});
  const code = h("input", {type: "text", id: "code", autocomplete: "off", autocapitalize: "characters", spellcheck: "false", maxlength: "20", required: true});
  const err = h("p", {class: "err", role: "alert", text: message || ""});
  const btn = h("button", {class: "btn primary block", type: "submit", text: "Continue"});
  const form = h("form", {onsubmit: async (e) => {
    e.preventDefault(); btn.disabled = true; err.textContent = "";
    try { S.info = await api("/api/login", {admission_no: adm.value, code: code.value}); introScreen(); }
    catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  }}, h("label", {for: "adm", text: "Admission number"}), adm, h("label", {for: "code", text: "Exam code (from your teacher)"}), code, err, btn);
  show(h("div", {class: "card center"}, h("h2", {text: "Sign in to your exam"}), h("p", {class: "muted", text: "Enter your admission number and the exam code shown by your teacher."}), form));
  $who.textContent = ""; adm.focus();
}

function introScreen() {
  const i = S.info; $who.textContent = i.student_name;
  const resume = i.attempt_status === "in_progress";
  const err = h("p", {class: "err", role: "alert"});
  const btn = h("button", {class: "btn primary block", type: "button", text: resume ? "Resume exam" : "Start exam", onclick: async () => {
    btn.disabled = true; err.textContent = "";
    try { S.exam = await api("/api/start", {}); beginExam(); }
    catch (ex) { err.textContent = ex.message; btn.disabled = false; if (ex.code === "already_submitted") { btn.remove(); } }
  }});
  show(h("div", {class: "card center"}, h("h2", {text: i.title}),
    h("div", {class: "stats"}, h("span", {class: "chip", text: i.question_count + " questions"}), h("span", {class: "chip", text: i.total_marks + " marks"}), h("span", {class: "chip", text: i.duration_minutes + " minutes"})),
    h("p", {class: "pre", text: i.instructions || "Answer every question. Your answers are saved automatically. You can move between questions and change an answer until you submit or time runs out."}),
    h("p", {class: "muted", text: resume ? "You already started this exam. Your time keeps running while you are away." : "The timer starts as soon as you press Start and cannot be paused."}),
    err, btn));
}

function beginExam() {
  const e = S.exam;
  if (e.finished) return finishScreen(e);
  S.answers = Object.fromEntries(Object.entries(e.answers || {}).map(([k, v]) => [Number(k), v]));
  S.index = 0; S.deadline = performance.now() + e.remaining_seconds * 1000; S.finished = false; S.submitting = false;
  renderExam(); clearInterval(S.tick); S.tick = setInterval(updateTimer, 500);
}
function remaining() { return Math.max(0, Math.round((S.deadline - performance.now()) / 1000)); }
function updateTimer() {
  const el = document.getElementById("timer"); if (!el) return;
  const r = remaining(); el.textContent = fmt(r);
  el.className = "timer" + (r <= 60 ? " crit" : r <= 300 ? " low" : "");
  if (r <= 0 && !S.finished) { S.finished = true; doSubmit(true); }
}
function answered(q) { const a = S.answers[q.id]; return a !== undefined && String(a).trim() !== ""; }

function renderExam() {
  const qs = S.exam.questions, q = qs[S.index];
  const body = h("div", {class: "card"});
  body.append(h("div", {class: "qhead"}, h("span", {text: "Question " + (S.index + 1) + " of " + qs.length}), h("span", {text: q.marks + (q.marks === 1 ? " mark" : " marks")})), h("div", {class: "prompt", text: q.prompt}));
  if (q.type === "mcq" || q.type === "true_false") {
    const options = q.type === "mcq" ? q.options : [{key: "true", text: "True"}, {key: "false", text: "False"}];
    const list = h("div", {class: "opts", role: "radiogroup", "aria-label": "Answer options"});
    options.forEach((o, n) => list.append(h("button", {type: "button", class: "opt", role: "radio", "aria-checked": String(S.answers[q.id] === o.key), onclick: () => choose(q, o.key)},
      h("span", {class: "key", text: q.type === "mcq" ? "ABCDE"[n] : (o.key === "true" ? "T" : "F")}), h("span", {class: "txt", text: o.text}))));
    body.append(list);
  } else {
    const input = h("input", {type: "text", id: "short", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "500", "aria-label": "Your answer", value: S.answers[q.id] || ""});
    input.addEventListener("input", () => { S.answers[q.id] = input.value; queueSave(q.id); refreshNav(); });
    body.append(h("div", {class: "opts"}, input));
  }
  const flagged = S.flags.has(q.id);
  body.append(h("div", {class: "controls"},
    h("button", {class: "btn", type: "button", text: "← Previous", disabled: S.index === 0, onclick: () => go(S.index - 1)}),
    h("button", {class: "btn", type: "button", text: flagged ? "Unmark review" : "Mark for review", onclick: () => { flagged ? S.flags.delete(q.id) : S.flags.add(q.id); renderExam(); }}),
    S.index < qs.length - 1 ? h("button", {class: "btn primary", type: "button", text: "Next →", onclick: () => go(S.index + 1)}) : h("button", {class: "btn primary", type: "button", text: "Finish & submit", onclick: askSubmit})));
  body.append(h("div", {class: "saved", id: "saved", "aria-live": "polite"}));
  const nav = h("aside", {class: "card nav", "aria-label": "Question navigator"}, h("div", {class: "timer", id: "timer", role: "timer", text: fmt(remaining())}),
    h("div", {class: "grid", id: "grid"}), h("div", {class: "legend"}, h("span", {text: "■ answered"}), h("span", {text: "▁ marked"}), h("span", {text: "□ not yet"})),
    h("button", {class: "btn block", type: "button", text: "Submit exam", onclick: askSubmit}));
  show(h("div", {class: "exam"}, body, nav)); refreshNav(); updateTimer();
  const first = document.querySelector(".opt,#short"); if (first && q.type === "short_answer") first.focus();
}
function refreshNav() {
  const grid = document.getElementById("grid"); if (!grid) return;
  grid.replaceChildren(...S.exam.questions.map((q, n) => h("button", {type: "button", class: "cell" + (answered(q) ? " done" : "") + (S.flags.has(q.id) ? " flag" : "") + (n === S.index ? " now" : ""),
    "aria-label": "Question " + (n + 1) + (answered(q) ? ", answered" : ", not answered") + (S.flags.has(q.id) ? ", marked for review" : ""), text: String(n + 1), onclick: () => go(n)})));
}
function go(n) { S.index = Math.min(Math.max(0, n), S.exam.questions.length - 1); renderExam(); }
function choose(q, key) { S.answers[q.id] = S.answers[q.id] === key && q.type !== "mcq" ? "" : key; queueSave(q.id, true); renderExam(); }

let saveTimer = null;
function queueSave(id, now) { S.pending.set(id, S.answers[id] || ""); clearTimeout(saveTimer); saveTimer = setTimeout(flush, now ? 0 : 600); setSaved("Saving…"); }
function setSaved(t) { const el = document.getElementById("saved"); if (el) el.textContent = t; }
async function flush() {
  if (S.saving || S.finished && !S.pending.size) return; S.saving = true;
  try {
    while (S.pending.size) {
      const [id, value] = S.pending.entries().next().value;
      const r = await api("/api/answer", {question_id: id, answer: value});
      if (S.pending.get(id) === value) S.pending.delete(id);
      S.deadline = performance.now() + r.remaining_seconds * 1000;
    }
    banner(""); setSaved("All answers saved");
  } catch (ex) {
    if (ex.code === "time_up" || ex.code === "already_submitted") { S.pending.clear(); S.finished = true; await doSubmit(true); }
    else if (ex.network || ex.status >= 500) { banner("Connection problem – your answers will be sent again automatically."); clearTimeout(saveTimer); saveTimer = setTimeout(flush, 2500); setSaved("Not saved yet"); }
    else { banner(ex.message); }
  } finally { S.saving = false; }
}

function ask(title, body, yes) {
  const d = document.getElementById("confirm"); document.getElementById("cTitle").textContent = title; document.getElementById("cBody").textContent = body;
  document.getElementById("cYes").onclick = () => { d.close(); yes(); }; document.getElementById("cNo").onclick = () => d.close(); d.showModal();
}
function askSubmit() {
  const left = S.exam.questions.filter((q) => !answered(q)).length;
  ask("Submit your exam?", (left ? left + " question" + (left === 1 ? " is" : "s are") + " not answered.\n" : "You answered every question.\n") + "You cannot change your answers after submitting.", () => doSubmit(false));
}
async function doSubmit(auto) {
  if (S.submitting) return; S.submitting = true;
  S.finished = true; clearInterval(S.tick); clearTimeout(saveTimer);
  for (let tries = 0; tries < 5; tries++) {
    try { await flushAll(); const r = await api("/api/submit", {}); r.auto_submitted = r.auto_submitted || auto; return finishScreen(r); }
    catch (ex) { if (ex.code === "no_attempt") return loginScreen("Your session ended. Sign in again."); banner("Submitting… waiting for the connection."); await new Promise((ok) => setTimeout(ok, 2000)); }
  }
  S.submitting = false; banner("Could not reach the server. Keep this page open and call your teacher.");
}
async function flushAll() { S.saving = false; try { await flush(); } catch (e) { /* retried by caller */ } }

function finishScreen(r) {
  banner(""); $who.textContent = S.info ? S.info.student_name : "";
  const parts = [h("h2", {text: r.auto_submitted ? "Time is up – your exam was submitted" : "Your exam has been submitted"}), h("p", {class: "muted", text: r.title || ""})];
  if (r.show_score && r.score != null) parts.push(h("div", {class: "big", text: r.score + " / " + r.max_score}), h("p", {class: "muted", text: Math.round(100 * r.score / Math.max(1, r.max_score)) + "%"}));
  else parts.push(h("p", {text: "Your teacher will share your result. You can close this page."}));
  parts.push(h("button", {class: "btn block", type: "button", text: "Sign out", onclick: async () => { try { await api("/api/logout", {}); } catch (e) {} S.info = null; loginScreen(); }}));
  show(h("div", {class: "card center tc"}, parts));
}

document.addEventListener("visibilitychange", () => { if (document.hidden && S.exam && !S.finished) { api("/api/focus", {}).catch(() => {}); } });
window.addEventListener("beforeunload", (e) => { if (S.exam && !S.finished) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("contextmenu", (e) => { if (S.exam && !S.finished) e.preventDefault(); });
document.addEventListener("keydown", (e) => {
  if (!S.exam || S.finished || e.target.tagName === "INPUT" || e.ctrlKey || e.metaKey || e.altKey) return;
  const q = S.exam.questions[S.index];
  if (e.key === "ArrowRight") go(S.index + 1); else if (e.key === "ArrowLeft") go(S.index - 1);
  else if (q.type === "mcq" && /^[a-e]$/i.test(e.key)) { const n = e.key.toLowerCase().charCodeAt(0) - 97; if (q.options[n]) choose(q, q.options[n].key); }
  else if (q.type === "true_false" && /^[tf]$/i.test(e.key)) choose(q, e.key.toLowerCase() === "t" ? "true" : "false");
});
loginScreen();
</script>
</body>
</html>
"""


def render(school_name: str, nonce: str) -> bytes:
    return PAGE.replace("__SCHOOL__", html.escape(school_name or "School")).replace("__NONCE__", nonce).encode("utf-8")
