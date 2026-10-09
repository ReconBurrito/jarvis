// Jarvis's panel. It talks to the service it was loaded from and to nothing else.
"use strict";

const el = (id) => document.getElementById(id);
const log = el("log"), text = el("text"), send = el("send"), fresh = el("new");
const READ_EVERY = 30000;   // the readings, while all is well
const TRY_EVERY = 5000;     // while the brain does not answer
const WATCH_EVERY = 2000;   // while an answer is being written in another window
const LOCKED_EVERY = 5000;  // while the vault is locked, so that an unlock shows soon

// ---------------------------------------------------------------- the clock

const ticks = el("ticks");
for (let n = 0; n < 60; n++) {
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  line.setAttribute("x1", 160); line.setAttribute("x2", 160);
  line.setAttribute("y1", n % 5 === 0 ? 8 : 12); line.setAttribute("y2", 20);
  line.setAttribute("transform", "rotate(" + n * 6 + " 160 160)");
  line.setAttribute("class", "tick");
  ticks.appendChild(line);
}
const arc = (id, radius, part) => {
  const round = 2 * Math.PI * radius;
  el(id).setAttribute("stroke-dasharray", (round * part).toFixed(1) + " " + round.toFixed(1));
};
const two = (n) => String(n).padStart(2, "0");
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"];
function drawClock() {
  const now = new Date();
  const s = now.getSeconds(), m = now.getMinutes(), h = now.getHours();
  const clock = el("clock");
  clock.textContent = two(h) + ":" + two(m);
  const seconds = document.createElement("small");
  seconds.textContent = two(s);
  clock.appendChild(seconds);
  el("date").textContent = now.getDate() + " " + MONTHS[now.getMonth()] + " " + now.getFullYear();
  el("day").textContent = DAYS[now.getDay()];
  Array.from(ticks.children).forEach((tick, n) => {
    const behind = (s - n + 60) % 60;
    tick.setAttribute("class", "tick" + (behind === 0 ? " now" : behind <= 3 ? " near" : ""));
  });
  arc("minutes", 116, (m + s / 60) / 60);
  arc("hours", 103, ((h % 12) + m / 60) / 12);
  setTimeout(drawClock, 1000 - now.getMilliseconds() + 5);
}
drawClock();

// ---------------------------------------------------------------- what is shown

let answering = null;   // the request that is being answered, so that it can be stopped
let reachable = true;
let shown = null;       // the conversation as the brain last told it and this window drew it
let elsewhere = false;  // an answer is being written in another window
let timer = null;

function say(words, kind) {
  el("state-text").textContent = words;
  el("state").className = "state" + (kind ? " " + kind : "");
  document.body.classList.toggle("busy", kind === "busy");
}

function nearEnd() { return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }

function show(entry) {
  el("empty").hidden = true;
  const follow = nearEnd();
  const block = document.createElement("p");
  block.className = "said " + (entry.role === "you" ? "you" : "jarvis " + (entry.state || "done"));
  block.textContent = entry.text || "";
  if (entry.role !== "you") { mark(block, entry.state || "done", entry.note); }
  log.appendChild(block);
  if (follow) { log.scrollTop = log.scrollHeight; }
  return block;
}

// How an answer ended, when it did not end well.
function mark(block, state, note) {
  block.className = "said jarvis " + state;
  const old = block.querySelector(".note");
  if (old) { old.remove(); }
  const words = state === "stopped" ? "Stopped." : state === "failed" ? (note || "That did not work.") : "";
  if (words) {
    const line = document.createElement("span");
    line.className = "note";
    line.textContent = words;
    block.appendChild(line);
  }
}

function add(block, more) {
  const follow = nearEnd();
  block.insertBefore(document.createTextNode(more), block.querySelector(".note"));
  if (follow) { log.scrollTop = log.scrollHeight; }
}

function hours(h) {
  if (typeof h !== "number") { return "--"; }
  if (h < 1) { return Math.max(1, Math.round(h * 60)) + " min"; }
  if (h < 48) { return (Math.round(h * 10) / 10) + " h"; }
  return Math.round(h / 24) + " days";
}

function readings(state) {
  const pulse = state.pulse || {};
  el("release").textContent = state.release || "";
  el("up").textContent = hours(pulse.uptime_hours);
  el("load").textContent = typeof pulse.load_1m === "number" ? pulse.load_1m.toFixed(2) + " of " + pulse.cpu_count : "--";
  const model = el("model");
  let words, warn = false;
  if (!state.model) { words = "none set"; warn = true; }
  else if (pulse.error || !pulse.model_server_up) { words = state.model + ", server down"; warn = true; }
  else if (!pulse.model_loaded) { words = state.model + ", resting"; }
  else if (!pulse.gpu) { words = state.model + ", on the processor"; }
  else if (pulse.model_on_gpu_percent === 100) { words = state.model + ", on the GPU"; }
  else { words = state.model + ", " + pulse.model_on_gpu_percent + "% on the GPU"; warn = true; }
  model.textContent = words;
  model.className = warn ? "warn" : "";
  const vault = state.vault || {}, cluster = el("cluster"), safe = el("vault");
  safe.textContent = {unlocked: "unlocked", locked: "locked", error: "cannot be read", none: "none"}[vault.status] || "--";
  safe.className = vault.status === "locked" || vault.status === "error" ? "warn" : "";
  safe.title = vault.status === "locked" ? "As root on the brain, run jarvis-unlock" : (vault.detail || "");
  const lab = pulse.cluster;
  if (lab && lab.error) { cluster.textContent = "does not answer"; cluster.className = "warn"; cluster.title = lab.error; }
  else if (lab) {
    cluster.textContent = lab.online + " of " + lab.nodes + " nodes" + (lab.quorate ? "" : ", no quorum");
    cluster.className = lab.online < lab.nodes || !lab.quorate ? "warn" : "";
    cluster.title = "";
  } else {
    cluster.textContent = vault.status === "locked" ? "locked" : "--";
    cluster.className = "";
    cluster.title = ((state.lab || {}).proxmox) || "";
  }
  const SYSTEMS = {proxmox: "the Proxmox cluster", pbs: "the backup server", opnsense: "the firewall", dns: "the DNS servers", switch: "the switch", notes: "your notes", browser: "the browser on your desktop"};
  const ready = Object.keys(SYSTEMS).filter((name) => (state.lab || {})[name] === "ready").map((name) => SYSTEMS[name]);
  el("scope").textContent = ready.length
    ? "This machine and " + (ready.length > 1 ? ready.slice(0, -1).join(", ") + " and " + ready[ready.length - 1] : ready[0]) + ", so far."
    : pulse.host ? "This machine (" + pulse.host + ") only, so far." : "This machine only, so far.";
  const waiting = state.waiting || [];
  el("waiting").textContent = waiting.length ? waiting.length + " waiting for your yes or no." : "Nothing waits for your yes or no.";
}

// ---------------------------------------------------------------- talking to the brain

async function ask(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  return fetch(path, Object.assign({cache: "no-store"}, options));
}

// The conversation is the brain's: whatever this window shows is drawn from what the brain says it is,
// whenever that differs. Only while this window itself is being answered does it draw as the words arrive.
function draw(state) {
  const told = JSON.stringify(state.conversation);
  if (told !== shown) {
    log.querySelectorAll(".said").forEach((block) => block.remove());
    el("empty").hidden = state.conversation.length > 0;
    state.conversation.forEach(show);
    log.scrollTop = log.scrollHeight;
    shown = told;
  }
  if (state.busy) { say("Answering in another window.", "busy"); }
  else if (elsewhere || !reachable || el("state-text").textContent === "Connecting.") { say("Ready.", ""); }
  elsewhere = state.busy;
}

async function read() {
  clearTimeout(timer);
  let wait = READ_EVERY;
  try {
    const answer = await ask("/api/state");
    if (!answer.ok) { throw new Error("answer " + answer.status); }
    const state = await answer.json();
    readings(state);
    if (!answering) { draw(state); }
    reachable = true;
    if (elsewhere) { wait = WATCH_EVERY; }
    else if ((state.vault || {}).status === "locked") { wait = LOCKED_EVERY; }
  } catch (error) {
    reachable = false;
    wait = TRY_EVERY;
    if (!answering) { say("The brain does not answer. Trying again.", "trouble"); }
  }
  controls();
  timer = setTimeout(read, wait);
}

function controls() {
  send.textContent = answering ? "Stop" : "Send";
  send.className = answering ? "stop" : "";
  send.disabled = !answering && (!reachable || elsewhere || !text.value.trim());
  fresh.disabled = Boolean(answering) || elsewhere || !reachable;
}

function grow() {
  text.style.height = "auto";
  text.style.height = Math.min(text.scrollHeight + 2, 160) + "px";
}

async function talk(words) {
  const mine = show({role: "you", text: words});
  const block = show({role: "jarvis", text: "", state: "answering"});
  log.scrollTop = log.scrollHeight;
  const stop = new AbortController();
  answering = stop;
  say("Thinking.", "busy");
  controls();
  let ended = "";
  try {
    const answer = await fetch("/api/chat", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: words}),
      signal: stop.signal, cache: "no-store",
    });
    if (answer.status === 409) {
      // The brain is still finishing something (an answer stopped a moment ago, or another window's):
      // nothing was sent, so nothing is shown as sent and the words go back into the box.
      mine.remove();
      block.remove();
      el("empty").hidden = log.querySelectorAll(".said").length > 0;
      if (!text.value) { text.value = words; grow(); }
      ended = "busy";
    } else if (!answer.ok || !answer.body) {
      let why = "The brain answered " + answer.status + ".";
      try { why = (await answer.json()).error || why; } catch (ignored) { /* not a message of the brain's */ }
      throw new Error(why);
    } else {
      const reader = answer.body.getReader(), decode = new TextDecoder();
      let held = "";
      for (;;) {
        const piece = await reader.read();
        if (piece.done) { break; }
        held += decode.decode(piece.value, {stream: true});
        let cut;
        while ((cut = held.indexOf("\n\n")) >= 0) {
          const lines = held.slice(0, cut).split("\n");
          held = held.slice(cut + 2);
          const data = lines.find((line) => line.startsWith("data: "));
          if (!data) { continue; }
          const event = JSON.parse(data.slice(6));
          if (event.type === "token") { add(block, event.text); say("Answering.", "busy"); }
          else if (event.type === "working") { say(event.say + " (" + event.tools.join(", ") + ")", "busy"); }
          else if (event.type === "tool" && !event.ok) { say("The tool " + event.name + " failed.", "busy"); }
          else if (event.type === "error") { ended = "failed"; mark(block, "failed", event.message); }
          else if (event.type === "done") { ended = "done"; mark(block, "done"); }
        }
      }
      if (!ended) { ended = "stopped"; mark(block, "stopped"); }   // the stream ended without saying how
    }
  } catch (error) {
    if (stop.signal.aborted) { ended = "stopped"; mark(block, "stopped"); }
    else {
      ended = "failed";
      mark(block, "failed", error.message || "The brain did not answer.");
      stop.abort();   // whatever was still coming is given up, so the brain is not left answering nobody
    }
  }
  answering = null;
  say({done: "Ready.", stopped: "Stopped. Ready.", busy: "Jarvis is still finishing the last answer. Send again in a moment."}[ended]
      || "That did not work. Ready.", ended === "failed" || ended === "busy" ? "trouble" : "");
  shown = null;   // drawn again from what the brain says the conversation now is
  controls();
  text.focus();
  clearTimeout(timer);
  timer = setTimeout(read, 700);   // once the brain has let go of the answer
}

el("ask").addEventListener("submit", (event) => {
  event.preventDefault();
  if (answering) { answering.abort(); return; }
  const words = text.value.trim();
  if (!words || !reachable || elsewhere) { return; }
  text.value = "";
  grow();
  talk(words);
});
text.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!answering) { el("ask").requestSubmit(); }
  }
});
text.addEventListener("input", () => { grow(); controls(); });
fresh.addEventListener("click", async () => {
  if (answering) { return; }
  try {
    const answer = await ask("/api/new", {});
    if (!answer.ok) { throw new Error(); }
    log.querySelectorAll(".said").forEach((block) => block.remove());
    el("empty").hidden = false;
    shown = "[]";
    say("Ready.", "");
  } catch (error) {
    say("A new conversation could not be started.", "trouble");
  }
  text.focus();
});

controls();
read();
text.focus();

// The notes open in a window of their own beside the panel; asked again, the same window comes to the front.
el("notes").addEventListener("click", () => {
  const notes = window.open("notes.html", "jarvis-notes", "popup,width=1120,height=780");
  if (notes) notes.focus();
});
