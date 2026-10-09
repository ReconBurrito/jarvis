// A small Markdown renderer for the notes window. It builds the page with DOM calls and text nodes only, never
// with HTML strings, so nothing in a note (which Jarvis also writes) can become markup or script.
// What it knows: headings, paragraphs, emphasis, inline code, code blocks, lists (nested by indentation, task
// boxes), quotes, rules, tables and links. A link to another note opens it in the window; a web link opens
// outside it; anything else stays text.
"use strict";

const Markdown = (() => {
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // Inline text: `code`, **strong** or __strong__, *em* or _em_, ~~del~~, [text](target), ![alt](picture), <https://bare>.
  // Every span is bounded (SPAN characters at most), so no text, however it is made, takes long to read: a run that
  // finds no end within the bound is plain text. A paragraph longer than PLAIN is shown as plain text.
  const SPAN = 400, PLAIN = 20000, DEPTH = 24;
  const INLINE = new RegExp([
    "(?<tick>`+)(?<code>[^`][\\s\\S]{0," + SPAN + "}?)\\k<tick>(?!`)",
    "\\*\\*(?=\\S)(?<strong>[\\s\\S]{0," + SPAN + "}?\\S)\\*\\*",
    "(?<lead2>^|[^\\w])__(?=\\S)(?<strong2>[\\s\\S]{0," + SPAN + "}?\\S)__(?!\\w)",
    "\\*(?=[^\\s*])(?<em>[\\s\\S]{0," + SPAN + "}?[^\\s*])\\*(?!\\*)",
    "(?<lead1>^|[^\\w])_(?=[^\\s_])(?<em2>[\\s\\S]{0," + SPAN + "}?[^\\s_])_(?!\\w)",
    "~~(?=\\S)(?<del>[\\s\\S]{0," + SPAN + "}?\\S)~~",
    "!\\[(?<alt>[^\\]]{0," + SPAN + "})\\]\\([^()\\s]{1," + SPAN + "}\\)",
    "\\[(?<label>[^\\]]{1," + SPAN + "})\\]\\((?<target>[^()\\s]{1," + SPAN + "})\\)",
    "<(?<bare>https?:\\/\\/[^\\s<>]{1," + SPAN + "})>",
  ].join("|"));

  function inline(parent, text, ctx, depth) {
    depth = depth || 0;
    if (text.length > PLAIN || depth > DEPTH) { parent.append(text); return; }
    while (text) {
      const m = INLINE.exec(text);
      if (!m) { parent.append(text); return; }
      const g = m.groups;
      let at = m.index + (g.lead1 || g.lead2 || "").length;   // the character before _em_ or __strong__ stays text
      if (at > 0) parent.append(text.slice(0, at));
      if (g.tick) parent.append(make("code", "", g.code.replace(/^ (.*) $/, "$1")));
      else if (g.strong !== undefined || g.strong2 !== undefined) inline(parent.appendChild(make("strong")), g.strong ?? g.strong2, ctx, depth + 1);
      else if (g.em !== undefined || g.em2 !== undefined) inline(parent.appendChild(make("em")), g.em ?? g.em2, ctx, depth + 1);
      else if (g.del !== undefined) inline(parent.appendChild(make("del")), g.del, ctx, depth + 1);
      else if (g.alt !== undefined) parent.append(make("span", "picture", g.alt ? "[picture: " + g.alt + "]" : "[picture]"));
      else if (g.label !== undefined) link(parent, g.label, g.target, ctx, depth);
      else if (g.bare !== undefined) link(parent, g.bare, g.bare, ctx, depth);
      text = text.slice(m.index + m[0].length);
    }
  }

  function link(parent, label, target, ctx, depth) {
    if (/^https?:\/\//i.test(target)) {
      const a = make("a");
      a.href = target;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      inline(a, label, ctx, depth + 1);
      parent.append(a);
      return;
    }
    const note = ctx.resolve(target);
    if (note) {
      const a = make("a", "note-link");
      a.href = "#";
      a.dataset.note = note;
      inline(a, label, ctx, depth + 1);
      parent.append(a);
      return;
    }
    inline(parent, label, ctx, depth + 1);   // a link to anything else is shown as its words
  }

  const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)/;
  const HEADING = /^ {0,3}(#{1,6})(?:[ \t]|$)/;
  const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
  const ITEM = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const QUOTE = /^ {0,3}>\s?(.*)$/;
  // A table's rows and the line under its head, judged by plain string work: no pattern here can take long.
  const ROW = { test: (line) => { const t = line.trim(); return t.length > 1 && t.startsWith("|") && t.endsWith("|"); } };
  const DIVIDER = {
    test: (line) => {
      let t = line.trim();
      if (t.startsWith("|")) t = t.slice(1);
      if (t.endsWith("|")) t = t.slice(0, -1);
      return t.length > 0 && t.split("|").every((cell) => /^:?-+:?$/.test(cell.trim()));
    },
  };

  // A heading's words: what follows the #s, without a closing run of #s that stands apart ("# Learning C#" keeps its #).
  function headingText(line, marks) {
    let text = line.trim().slice(marks).trim();
    let end = text.length;
    while (end > 0 && text[end - 1] === "#") end--;
    if (end === 0) return "";
    if (end < text.length && (text[end - 1] === " " || text[end - 1] === "\t")) text = text.slice(0, end).trim();
    return text;
  }

  const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));

  function blocks(parent, lines, ctx, depth) {
    depth = depth || 0;
    if (depth > DEPTH) { parent.append(make("pre", "", lines.join("\n"))); return; }
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = FENCE.exec(line))) {
        const fence = m[1], body = [];
        i++;
        while (i < lines.length && !(lines[i].trim().startsWith(fence[0].repeat(fence.length)) && !lines[i].trim().replace(/^[`~]+/, "").trim())) body.push(lines[i++]);
        i++;
        const pre = make("pre");
        pre.append(make("code", m[2] ? "language-" + m[2] : "", body.join("\n")));
        parent.append(pre);
      } else if ((m = HEADING.exec(line))) {
        inline(parent.appendChild(make("h" + m[1].length)), headingText(line, m[1].length), ctx);
        i++;
      } else if (RULE.test(line)) {
        parent.append(make("hr"));
        i++;
      } else if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && (m = QUOTE.exec(lines[i]))) { inner.push(m[1]); i++; }
        blocks(parent.appendChild(make("blockquote")), inner, ctx, depth + 1);
      } else if (ROW.test(line) && i + 1 < lines.length && DIVIDER.test(lines[i + 1])) {
        const table = make("table"), head = cells(line);
        const aligns = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        const tr = table.appendChild(make("thead")).appendChild(make("tr"));
        head.forEach((cell, n) => { const th = tr.appendChild(make("th")); if (aligns[n]) th.style.textAlign = aligns[n]; inline(th, cell, ctx); });
        const tbody = table.appendChild(make("tbody"));
        i += 2;
        while (i < lines.length && ROW.test(lines[i])) {
          const row = tbody.appendChild(make("tr"));
          cells(lines[i]).slice(0, head.length).forEach((cell, n) => { const td = row.appendChild(make("td")); if (aligns[n]) td.style.textAlign = aligns[n]; inline(td, cell, ctx); });
          i++;
        }
        const wrap = make("div", "table");
        wrap.append(table);
        parent.append(wrap);
      } else if (ITEM.test(line)) {
        i = list(parent, lines, i, ctx, depth);
      } else {
        const text = [];
        while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i]) && !RULE.test(lines[i])
               && !QUOTE.test(lines[i]) && !ITEM.test(lines[i]) && !(ROW.test(lines[i]) && i + 1 < lines.length && DIVIDER.test(lines[i + 1]))) {
          text.push(lines[i++].trim());
        }
        inline(parent.appendChild(make("p")), text.join(" "), ctx);
      }
    }
  }

  // One list, from line i: items at its own indentation, with what is indented further under each item.
  function list(parent, lines, i, ctx, depth) {
    const first = ITEM.exec(lines[i]);
    const indent = first[1].length, ordered = /\d/.test(first[2]);
    const node = parent.appendChild(make(ordered ? "ol" : "ul"));
    if (ordered && parseInt(first[2], 10) !== 1) node.start = parseInt(first[2], 10);
    while (i < lines.length) {
      // Items with an empty line between them are still one list.
      let next = i;
      while (next < lines.length && !lines[next].trim()) next++;
      const m = next < lines.length ? ITEM.exec(lines[next]) : null;
      if (!m || m[1].length !== indent || /\d/.test(m[2]) !== ordered || (next > i && node.childElementCount === 0)) break;
      i = next;
      const li = node.appendChild(make("li"));
      let text = m[3];
      const box = /^\[([ xX])\]\s+(.*)$/.exec(text);
      if (box) {
        li.className = "task";
        const mark = li.appendChild(make("span", "box" + (box[1] === " " ? "" : " done")));
        mark.setAttribute("aria-label", box[1] === " " ? "not done" : "done");
        text = box[2];
      }
      const inner = [text];
      i++;
      while (i < lines.length) {
        const next = lines[i];
        const sub = ITEM.exec(next);
        if (sub && sub[1].length <= indent) break;
        if (!next.trim()) {
          // A blank line ends the item unless what follows is indented under it.
          if (i + 1 < lines.length && lines[i + 1].match(/^ */)[0].length > indent && lines[i + 1].trim()) { inner.push(""); i++; continue; }
          break;
        }
        if (!sub && next.match(/^ */)[0].length <= indent && !inner[inner.length - 1]) break;
        inner.push(next.slice(Math.min(next.match(/^ */)[0].length, indent + 2)));
        i++;
      }
      const sublines = inner.slice(1);
      const lead = [inner[0]];
      while (sublines.length && sublines[0].trim() && !ITEM.test(sublines[0]) && !FENCE.test(sublines[0])) lead.push(sublines.shift().trim());
      inline(li, lead.join(" "), ctx);
      if (sublines.some((l) => l.trim())) blocks(li, sublines, ctx, depth + 1);
    }
    return i;
  }

  // render(text, resolve): a fragment. resolve(target) names the note a relative link points at, or null.
  function render(text, resolve) {
    const fragment = document.createDocumentFragment();
    blocks(fragment, String(text).replace(/\r\n?/g, "\n").split("\n"), { resolve: resolve || (() => null) });
    return fragment;
  }

  return { render };
})();
