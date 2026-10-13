// Light Monitor page. Reads api/v1/state twice a second and draws it.
// Every URL is relative, so the page works at its own port and behind
// BlueOS's /extensionv2/ proxy alike.
"use strict";

const POLL_MS = 600;
const FILES_EVERY_MS = 10000;
const LAMPS = {             // where each light sits on the drawing (viewBox 360 x 440)
  1: { x: 48, y: 120, label: "Front left", labelY: -60 },
  2: { x: 48, y: 336, label: "Rear left", labelY: 52 },
  3: { x: 312, y: 336, label: "Rear right", labelY: 52 },
  4: { x: 312, y: 120, label: "Front right", labelY: -60 },
};
const RING_R = 31;
const RING_C = 2 * Math.PI * RING_R;
const BAD = new Set(["not_answering", "stale", "fault", "mismatch", "standby"]);

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmt(v, nd = 1, unit = "") {
  if (v === null || v === undefined || v === "" || Number.isNaN(v)) return "—";
  if (typeof v === "number") {
    const s = v.toLocaleString(undefined, { minimumFractionDigits: nd, maximumFractionDigits: nd });
    return unit ? `${s} ${unit}` : s;
  }
  return String(v);
}
function ago(s) {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `${s.toFixed(1)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}
function bytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1e3) return `${n} B`;
  if (n < 1e6) return `${(n / 1e3).toFixed(1)} kB`;
  if (n < 1e9) return `${(n / 1e6).toFixed(1)} MB`;
  return `${(n / 1e9).toFixed(2)} GB`;
}
function lampClass(state) {
  if (BAD.has(state)) return "bad";
  if (state === "on") return "on";
  if (state === "off") return "off";
  return "unknown";
}

// ---- theme ----------------------------------------------------------------

function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyTheme() {
  const t = effectiveTheme();
  $("logo").src = t === "light" ? "static/logo_mediterranean.png" : "static/logo_white.png";
  $("theme").textContent = t === "light" ? "Dark" : "Light";
}
$("theme").addEventListener("click", () => {
  const next = effectiveTheme() === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("lm-theme", next); } catch (e) { /* private window */ }
  applyTheme();
});
applyTheme();

// ---- building ---------------------------------------------------------------

function buildLights() {
  for (const id of [1, 2, 3, 4]) {
    $(`light-${id}`).innerHTML = `
      <div class="card-head">
        <div class="badge" id="b${id}">${id}</div>
        <div class="who"><h2 id="n${id}">${esc(LAMPS[id].label)}</h2>
          <div class="state" id="s${id}"><span class="dot"></span><span>Waiting for the controller</span></div></div>
      </div>
      <div class="level"><span class="big" id="lv${id}">—</span><span class="told" id="told${id}"></span></div>
      <div class="bar" aria-hidden="true"><div class="fill" id="bf${id}"></div><div class="tick" id="bt${id}" hidden></div></div>
      <dl class="kv" id="kv${id}"></dl>`;
  }
  const g = $("lamps");
  const ns = "http://www.w3.org/2000/svg";
  for (const id of [1, 2, 3, 4]) {
    const L = LAMPS[id];
    const grp = document.createElementNS(ns, "g");
    grp.setAttribute("transform", `translate(${L.x} ${L.y})`);
    grp.innerHTML = `
      <circle class="lamp-track" r="${RING_R}"/>
      <circle class="lamp-ring" id="ring${id}" r="${RING_R}" transform="rotate(-90)"
              stroke-dasharray="0 ${RING_C.toFixed(1)}"/>
      <circle class="lamp-body unknown" id="lamp${id}" r="21"/>
      <text class="lamp-num unknown" id="lampn${id}">${id}</text>
      <text class="lamp-label" y="${L.labelY}">${esc(L.label)}</text>
      <text class="lamp-pct" id="lampp${id}" y="${L.labelY + 15}">—</text>`;
    g.appendChild(grp);
  }
}

function kv(el, rows) {
  el.innerHTML = rows.map(([k, v, cls]) =>
    `<dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ""} title="${esc(v)}">${esc(v)}</dd>`).join("");
}

// ---- drawing ------------------------------------------------------------------

function drawLight(lt, rail) {
  const id = lt.id;
  const cls = lampClass(lt.state);
  const card = $(`light-${id}`);
  card.classList.toggle("alert", cls === "bad");
  $(`n${id}`).textContent = lt.name;
  $(`b${id}`).className = `badge ${cls === "unknown" ? "" : cls}`;
  const st = $(`s${id}`);
  st.className = `state ${cls === "bad" ? "bad" : ""}`;
  st.innerHTML = `<span class="dot ${cls === "bad" ? "warn" : cls === "unknown" ? "" : "ok"}"></span><span>${esc(lt.state_text)}</span>`;

  const lvl = lt.level_pct;
  $(`lv${id}`).innerHTML = lvl === null || lvl === undefined ? "—" : `${Math.round(lvl)}<small>%</small>`;
  $(`told${id}`).textContent = lt.commanded_pct === null || lt.commanded_pct === undefined
    ? "" : `told ${Math.round(lt.commanded_pct)}%`;
  $(`bf${id}`).style.width = `${Math.max(0, Math.min(100, lvl || 0))}%`;
  const tick = $(`bt${id}`);
  if (lt.commanded_pct !== null && lt.commanded_pct !== undefined) {
    tick.hidden = false;
    tick.style.left = `${Math.max(0, Math.min(100, lt.commanded_pct))}%`;
  } else tick.hidden = true;

  const temps = [lt.temp2_c, lt.temp3_c].filter((x) => x !== null && x !== undefined);
  const restarts = lt.restarts_this_session || 0;
  const info = lt.info || {};
  kv($(`kv${id}`), [
    ["Expected draw", lt.expected_w === null ? "—" : `≈ ${fmt(lt.expected_w, 0, "W")} · ≈ ${fmt(lt.expected_a, 1, "A")}`],
    ["Temperatures", temps.length ? `${temps.map((t) => fmt(t, 1)).join(" · ")} °C` : "—"],
    ["Highest recorded", fmt(lt.max_temp_c, 1, "°C")],
    ["Humidity", fmt(lt.humidity_pct, 0, "%")],
    ["Power-ons", lt.power_cycles === null || lt.power_cycles === undefined ? "—"
      : `${lt.power_cycles}${restarts ? `  (+${restarts} this session)` : ""}`, restarts ? "warn" : ""],
    ["Run time", fmt(lt.runtime_h, 1, "h")],
    ["Fault flags", lt.flags_hex || "—", lt.flags ? "warn" : ""],
    ["Last answer", ago(lt.last_answer_age_s)],
    ["Serial · firmware", info.serial ? `${info.serial} · ${info.firmware || "?"}` : "—"],
  ]);

  // the drawing
  const frac = Math.max(0, Math.min(1, (lt.answering === false ? 0 : (lvl || 0)) / 100));
  const ring = $(`ring${id}`);
  ring.setAttribute("stroke-dasharray", `${(frac * RING_C).toFixed(1)} ${RING_C.toFixed(1)}`);
  ring.style.opacity = frac > 0 ? "1" : "0";     // a round cap draws a dot at 0
  $(`lamp${id}`).setAttribute("class", `lamp-body ${cls}`);
  $(`lampn${id}`).setAttribute("class", `lamp-num ${cls}`);
  $(`lampp${id}`).textContent = cls === "bad" ? lt.state_text.split(" (")[0]
    : lvl === null || lvl === undefined ? "—" : `${Math.round(lvl)}%`;
}

function drawPower(s) {
  const p = s.power || {};
  const veh = p.vehicle;
  const lo = 8, hi = 16;
  const warnV = p.vehicle_warn_v || 10.5;
  $("veh-zone").style.width = `${((warnV - lo) / (hi - lo)) * 100}%`;
  $("veh-scale-mid").textContent = `dropout ${fmt(p.converter_dropout_v, 1)} V · warn ${fmt(warnV, 1)} V`;
  if (veh && veh.v !== null && veh.v !== undefined) {
    $("veh-v").innerHTML = `${fmt(veh.v, 2)}<small> V</small>`;
    $("veh-needle").style.left = `${Math.max(0, Math.min(100, ((veh.v - lo) / (hi - lo)) * 100))}%`;
    $("veh-needle").hidden = false;
    $("veh-a").textContent = fmt(veh.a, 1, "A");
    $("veh-w").textContent = fmt(veh.w, 0, "W");
    $("power").classList.toggle("alert", veh.v < warnV);
  } else {
    $("veh-v").innerHTML = `—<small> not read</small>`;
    $("veh-needle").hidden = true;
    $("veh-a").textContent = $("veh-w").textContent = "—";
    $("power").classList.remove("alert");
  }
  const ard = p.ardusub || {};
  $("pilot-gain").textContent = fmt(ard.pilot_gain_pct, 0, "%");
  $("pilot-lights").textContent = fmt(ard.lights_pct, 0, "%");
  const rail = p.rail;
  $("rail").innerHTML = rail
    ? `<div class="big-num">${fmt(rail.v, 2)}<small> V</small></div>
       <div class="note">${rail.a === null || rail.a === undefined ? "Current and power need the shunt value set in the firmware."
          : `${fmt(rail.a, 2, "A")} · ${fmt(rail.w, 0, "W")}`} · ${esc(rail.chip || "")}${rail.uv ? " · <strong>dipped below its limit</strong>" : ""}</div>`
    : `<div class="note">No power monitor fitted, so the lights' own voltage and current are not measured.
       Keenan's board has a place for one (INA238, J5).</div>`;
  $("exp-w").textContent = `≈ ${fmt(p.lights_expected_w, 0, "W")}`;
  $("exp-a").textContent = `≈ ${fmt(p.lights_expected_a, 1, "A")}`;
  $("power-note").textContent = `Expected = ${fmt(p.full_w_per_light, 0)} W per light at 100%, scaled by DeepSea's dimming curve. A model, not a measurement.`;
}

function drawController(s) {
  const c = s.controller || {};
  const l = s.link || {};
  const portWords = { data: "telemetry port", console: "console, read-only", simulator: "simulated",
                      released: "released for maintenance", none: "not found" };
  const st = c.stats || {};
  kv($("ctl-kv"), [
    ["Port", `${portWords[l.source] || l.source || "—"}`],
    ["Board", c.machine || c.board || (l.legacy_firmware ? "debug firmware (no hello)" : "—")],
    ["Firmware", c.fw || "—"],
    ["MicroPython", c.micropython || "—"],
    ["Up for", c.uptime_s === null || c.uptime_s === undefined ? "—" : `${fmt(c.uptime_s / 60, 1)} min`],
    ["PWM in", c.pwm_us ? `${c.pwm_us} µs${c.signal === false ? " · no signal" : ""}` : (c.signal === false ? "no signal" : "—"),
      c.signal === false ? "warn" : ""],
    ["Commanding", fmt(c.commanded_pct, 0, "%")],
    ["Lines", `${fmt(l.lines_per_s, 1)}/s · ${(l.lines || 0).toLocaleString()} total`],
    ["Lost lines", (l.lost_lines || 0).toLocaleString(), l.lost_lines ? "warn" : ""],
    ["Light bus", st.asked ? `${st.answered}/${st.asked} answered · ${st.failed} failed` : "—"],
    ["Restarts seen", String(c.restarts || 0), c.restarts ? "warn" : ""],
  ]);
  const notes = [];
  if (l.note) notes.push(l.note);
  if (c.datalink_note) notes.push(`Telemetry port: ${c.datalink_note}`);
  if (l.legacy_firmware) notes.push("Reading the debug firmware's console text. Install the telemetry firmware for the full picture.");
  $("ctl-note").textContent = notes.join(" · ");
  $("ctl-note").className = `note ${l.connected ? "" : "warn"}`;
}

function drawRecording(s) {
  const r = s.recording || {};
  const m = s.mavlink || {};
  kv($("rec-kv"), [
    ["File", r.file || "—"],
    ["Size", `${bytes(r.bytes)} · ${(r.records || 0).toLocaleString()} records`],
    ["Free on the Pi", r.disk_free_gb === null || r.disk_free_gb === undefined ? "—" : `${fmt(r.disk_free_gb, 1)} GB`,
      r.paused ? "warn" : ""],
    ["MAVLink", !m.enabled ? "off"
      : `${m.publishing ? "publishing" : "waiting for the controller"} as ${m.system}/${m.component} · ${(m.sent || 0).toLocaleString()} sent`],
    ["MAVLink errors", m.enabled ? `${m.errors || 0}${m.last_error ? ` · ${m.last_error}` : ""}` : "—", m.errors ? "warn" : ""],
  ]);
  const until = s.link && s.link.released_until;
  $("release-note").textContent = until
    ? `Released until ${new Date(until * 1000).toLocaleTimeString()}. mpremote may use the port now.`
    : "Releasing closes the controller's port so mpremote can use it. It reopens by itself.";
  $("release-note").className = `note ${until ? "warn" : ""}`;
  const can = !!(s.extension && s.extension.can_release);
  $("release").disabled = !can; $("resume").disabled = !can;
}

function drawPills(s) {
  const l = s.link || {}, r = s.recording || {}, m = s.mavlink || {};
  const fresh = l.last_line_age_s !== null && l.last_line_age_s !== undefined && l.last_line_age_s < 5;
  const pill = (id, cls, text) => {
    const el = $(id);
    el.firstElementChild.className = `dot ${cls}`;
    el.lastElementChild.textContent = text;
  };
  pill("pill-link", fresh ? "ok" : l.connected ? "ring" : "warn",
       fresh ? `Controller · ${l.source === "data" ? "telemetry port" : l.source}` : l.connected ? "Controller · quiet" : "Controller · not found");
  pill("pill-rec", r.paused || r.error ? "warn" : r.file ? "ok" : "", r.paused ? "Recording paused" : "Recording");
  pill("pill-mav", !m.enabled ? "" : m.errors && !m.sent ? "warn" : m.publishing ? "ok" : "ring",
       !m.enabled ? "MAVLink off" : m.publishing ? "MAVLink" : "MAVLink · waiting");
  const sub = [];
  if (s.extension && s.extension.mode === "simulate") sub.push("SIMULATED controller, not a real light");
  else sub.push("Four DeepSea SeaLites and their controller, live and recorded");
  $("subtitle").textContent = sub.join(" · ");
  $("footer").textContent = `Light Monitor ${s.extension ? s.extension.version : ""} · recording to ${r.dir || "—"}`;
}

let lastEvents = "";
function drawEvents(s) {
  const ev = (s.events || []).slice().reverse();
  const key = ev.length ? `${ev.length}:${ev[0].t}` : "";
  if (key === lastEvents) return;
  lastEvents = key;
  $("events").innerHTML = ev.length ? ev.map((e) => `
    <li><time>${new Date(e.t * 1000).toLocaleTimeString([], { hour12: false })}</time>
        <span class="dot ${e.lvl === "warn" ? "warn" : "ok"}"></span><span>${esc(e.text)}</span></li>`).join("")
    : `<li class="empty">Nothing yet.</li>`;
}

async function refreshFiles() {
  try {
    const r = await fetch("api/v1/files", { cache: "no-store" });
    const j = await r.json();
    const files = (j.files || []).slice().sort((a, b) => b.modified - a.modified).slice(0, 4);
    $("files").innerHTML = files.length ? files.map((f) =>
      `<li><a href="files/${encodeURIComponent(f.name)}" download>${esc(f.name)}</a><span>${bytes(f.bytes)}</span></li>`).join("")
      : `<li class="empty">None yet.</li>`;
  } catch (e) { /* the state poll reports the connection */ }
}

let failures = 0;
async function poll() {
  try {
    const r = await fetch("api/v1/state", { cache: "no-store" });
    const s = await r.json();
    failures = 0;
    const rail = s.power && s.power.rail;
    for (const lt of s.lights || []) drawLight(lt, rail);
    drawPower(s); drawController(s); drawRecording(s); drawPills(s); drawEvents(s);
  } catch (e) {
    failures += 1;
    if (failures > 2) {
      $("pill-link").firstElementChild.className = "dot warn";
      $("pill-link").lastElementChild.textContent = "Extension not answering";
    }
  }
  setTimeout(poll, POLL_MS);
}

async function post(path, body) {
  try {
    await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body || {}) });
  } catch (e) { /* shown by the next poll */ }
}
$("release").addEventListener("click", () => post("api/v1/port/release", { minutes: Number($("release-min").value) }));
$("resume").addEventListener("click", () => post("api/v1/port/resume"));

buildLights();
poll();
refreshFiles();
setInterval(refreshFiles, FILES_EVERY_MS);
