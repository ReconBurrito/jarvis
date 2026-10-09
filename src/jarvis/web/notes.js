// Jarvis's notes window: lists the notes, shows one as it reads, and lets the owner edit and save it.
// A save names the version it started from; when the note changed meanwhile (Jarvis wrote to it, or it changed
// on the remote) nothing is overwritten: the window says so, keeps what was typed, and offers to reload.
"use strict";

const el = (id) => document.getElementById(id);
const list = el("list"), filter = el("filter"), view = el("view"), editor = el("editor");
const mode = el("mode"), save = el("save"), status = el("status"), badge = el("badge");

let notes = [];          // [{path, size, protected, read_only}]
let open = null;         // {path, text, version, protected, read_only}, or a new note {path, text: "", version: ""}
let editing = false;
let saving = false;      // one save at a time; a second one would carry an old version

function say(text, trouble, action) {
  status.textContent = text || "";
  status.className = "status" + (trouble ? " trouble" : "");
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", action.run);
    status.append(button);
  }
}

async function call(path, options) {
  const answer = await fetch(path, Object.assign({ cache: "no-store", headers: { "content-type": "application/json" } }, options || {}));
  let body = {};
  try { body = await answer.json(); } catch (_) { /* an empty or broken answer is told below */ }
  if (!answer.ok) {
    const error = new Error(body.error || "Jarvis answered " + answer.status);
    error.conflict = Boolean(body.conflict);
    throw error;
  }
  return body;
}

// What is typed lives in the editor, shown or not; the note as saved is open.text, with its line ends as the editor
// keeps them (a text box turns Windows line ends into plain ones, and that alone is not a change).
const plain = (text) => text.replace(/\r\n?/g, "\n");
const unsaved = () => Boolean(open) && !open.read_only && editor.value !== open.text;

function mark() {
  save.disabled = saving || !unsaved();
  save.classList.toggle("unsaved", unsaved());
}

function drawList() {
  const wanted = filter.value.trim().toLowerCase();
  list.replaceChildren();
  let folder = null;
  for (const note of notes) {
    if (wanted && !note.path.toLowerCase().includes(wanted)) continue;
    const parts = note.path.split("/");
    const here = parts.length > 1 ? parts.slice(0, -1).join("/") : "";
    if (here !== folder) {
      folder = here;
      const heading = document.createElement("h2");
      heading.textContent = here || "Standing files";
      list.append(heading);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = parts[parts.length - 1].replace(/\.md$/, "");
    button.title = note.path;
    button.dataset.path = note.path;
    if (note.protected) button.classList.add("standing");
    if (open && open.path === note.path) button.classList.add("open");
    list.append(button);
  }
  if (!list.childElementCount) {
    const empty = document.createElement("p");
    empty.textContent = wanted ? "No note's name has that in it." : "There are no notes yet.";
    list.append(empty);
  }
}

async function loadList() {
  try {
    const answer = await call("/api/notes");
    const folderOf = (path) => path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    notes = answer.notes.slice().sort((a, b) => {
      const fa = folderOf(a.path), fb = folderOf(b.path);
      if (fa !== fb) return !fa ? -1 : !fb ? 1 : fa.localeCompare(fb);   // the standing files first, then each folder
      return a.path.localeCompare(b.path);
    });
    el("where").textContent = answer.detail || "";
    drawList();
    if (status.classList.contains("trouble")) say("");
    return true;
  } catch (error) {
    list.replaceChildren();
    say(error.message + ".", true, { label: "Try again", run: start });
    return false;
  }
}

// A link in a note names another note relative to it, as on the remote (wiki/a.md links "b.md" for wiki/b.md).
function resolver(from) {
  return (target) => {
    const clean = target.split("#")[0];
    if (!clean || /^[a-z]+:/i.test(clean)) return null;
    const parts = (clean.startsWith("/") ? [] : from.split("/").slice(0, -1));
    for (const part of clean.replace(/^\//, "").split("/")) {
      if (part === "..") parts.pop(); else if (part && part !== ".") parts.push(part);
    }
    const path = parts.join("/").replace(/(\.md)?$/, ".md");
    return notes.some((note) => note.path === path) ? path : null;
  };
}

function show() {
  el("path").textContent = open ? open.path : "No note open";
  badge.hidden = !open || !(open.protected || open.read_only);
  if (open && open.read_only) { badge.textContent = open.lossy ? "Not plain text, read only" : "Source, read only"; badge.className = "badge source"; }
  else if (open && open.protected) { badge.textContent = "Shapes how Jarvis behaves"; badge.className = "badge"; }
  mode.disabled = !open || open.read_only;
  mode.textContent = editing ? "Preview" : "Edit";
  editor.hidden = !editing;
  view.hidden = editing;
  if (!editing && open) {
    try {
      view.replaceChildren(Markdown.render(editor.value, resolver(open.path)));   // what is typed, as it will read
    } catch (_) {
      const pre = document.createElement("pre");   // a note the renderer cannot read is shown as its text
      pre.textContent = editor.value;
      view.replaceChildren(pre);
    }
    if (!editor.value.trim()) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "This note is empty.";
      view.append(empty);
    }
  }
  mark();
  for (const button of list.querySelectorAll("button")) button.classList.toggle("open", Boolean(open && button.dataset.path === open.path));
}

function leaveOk() {
  return !unsaved() || window.confirm("This note has changes that are not saved. Leave them?");
}

async function openNote(path, keepEditing, dropChanges) {
  if (saving) { say("Saving; the note stays open until the save is done, so nothing typed can be lost."); return; }
  if (!dropChanges && !leaveOk()) return;
  try {
    open = await call("/api/notes/note?path=" + encodeURIComponent(path));
  } catch (error) {
    say(error.message + ".", true);
    return;
  }
  open.text = plain(open.text);
  if (open.lossy) say("This note is not plain UTF-8 text, so it opens read only; change it by hand.", true);
  editing = Boolean(keepEditing) && !open.read_only;
  editor.value = open.text;
  history.replaceState(null, "", "#" + encodeURIComponent(open.path));
  if (!open.lossy) say("");
  show();
}

async function saveNote() {
  if (saving || !unsaved()) return;
  const target = open, text = editor.value;
  saving = true;
  mark();
  say("Saving.");
  try {
    const saved = await call("/api/notes/note", { method: "PUT", body: JSON.stringify({ path: target.path, text, base: target.version }) });
    const fresh = !notes.some((note) => note.path === target.path);
    // The note as it now stands is what the version names: it can hold a change from elsewhere, merged in.
    const merged = plain(saved.text) !== (text.endsWith("\n") ? text : text + "\n");
    const typedSince = open === target && editor.value !== text;
    let note = "";
    if (merged && typedSince) {
      // What is in the editor lacks the merged change; keeping the old version makes the next save stop and say so.
      note = " A change made elsewhere was merged in; reload the note to see it before saving again.";
    } else {
      Object.assign(target, { text: plain(saved.text), version: saved.version, protected: saved.protected });
      if (open === target && !typedSince) editor.value = target.text;
      if (merged) note = " A change made elsewhere meanwhile was merged in; it is shown now.";
    }
    say("Saved " + (open === target ? "" : target.path + " ") + "as " + saved.commit + "." + note +
        (saved.warning ? " " + saved.warning : ""), Boolean(saved.warning) || (merged && typedSince));
    if (fresh) await loadList();
  } catch (error) {
    if (error.conflict && open === target) {
      say("Not saved: " + error.message + ". What you typed is still here; copy it before you reload.", true,
          { label: "Reload, dropping my changes", run: () => openNote(target.path, true, true) });
    } else if (open !== target) {
      say(target.path + " was not saved: " + error.message + ". Its text from this window is gone unless you copied it.", true);
    } else {
      say("Not saved: " + error.message + ".", true);
    }
  } finally {
    saving = false;
  }
  show();
}

list.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-path]");
  if (button) openNote(button.dataset.path);
});
view.addEventListener("click", (event) => {
  const link = event.target.closest("a.note-link");
  if (link) { event.preventDefault(); openNote(link.dataset.note); }
});
filter.addEventListener("input", drawList);
mode.addEventListener("click", () => {
  if (!open || open.read_only) return;
  editing = !editing;
  show();
  if (editing) editor.focus();
});
save.addEventListener("click", saveNote);
editor.addEventListener("input", mark);
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); saveNote(); }
});
el("create").addEventListener("submit", (event) => {
  event.preventDefault();
  let path = el("new-path").value.trim().replace(/^\/+/, "");
  if (!path) return;
  if (!path.endsWith(".md")) path += ".md";
  if (!/^[A-Za-z0-9][A-Za-z0-9 _./-]{0,190}\.md$/.test(path) || path.split("/").some((part) => !part || part.startsWith(".") || part === "..")) {
    say("A note's name is letters, digits, spaces, dots, dashes and slashes, and ends in .md.", true);
    return;
  }
  if (path.startsWith("raw/")) { say("Sources under raw/ are filed, not written here.", true); return; }
  if (saving) { say("Saving; a new note can be made when the save is done."); return; }
  if (notes.some((note) => note.path === path)) { openNote(path, true); return; }
  if (!leaveOk()) return;
  open = { path, text: "", version: "", protected: false, read_only: false };
  editing = true;
  editor.value = "# " + path.split("/").pop().replace(/\.md$/, "").replace(/[-_]+/g, " ") + "\n\n";
  el("new-path").value = "";
  say("A new note. It exists once it is saved.");
  show();
  editor.focus();
});
window.addEventListener("beforeunload", (event) => { if (unsaved()) { event.preventDefault(); event.returnValue = ""; } });

async function start() {
  if (await loadList()) {
    let wanted = "";
    try { wanted = decodeURIComponent(location.hash.slice(1)); } catch (_) { /* a broken address opens nothing */ }
    if (wanted && notes.some((note) => note.path === wanted)) openNote(wanted);
    else say(notes.length + " notes.");
  }
}

start();
