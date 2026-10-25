// Ρώτα το: a static page over POST /ask/stream (src/mitos_rag/api.py), which answers in
// newline-delimited JSON: "found" titles, "text" pieces of the answer, then "done" or "error".
//
// Rules this file keeps:
// - Model text is untrusted. It is built node by node with textContent, never parsed as HTML,
//   and only the API's `sources` and `options` become links: a URL in model text stays text.
//   The one other link is a source's `apply_online.url`, from the facts row, never the model,
//   and api.py passes only https (checked here too).
// - Questions are never stored: nothing in browser storage, nothing in the URL.
// - Streamed text is provisional. The "done" event rebuilds the whole card from the checked
//   answer: the status can change, and the sources and any warning only come with it.
// - One question at a time. Every question is a paid model call, so the controls stay
//   disabled until the answer (or an error) is in.
"use strict";

// On AWS, CloudFront signs each request to the Lambda function URL (origin access control), and
// Lambda accepts a signed POST only with the SHA-256 of its body in this header (decision 013).
// FastAPI ignores it locally. crypto.subtle needs a secure context: https, or localhost.
async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const API_URL = "ask/stream"; // same origin: FastAPI locally, CloudFront in front of S3 and Lambda on AWS
const MIN_CHARS = 3;
const MAX_CHARS = 300; // matches MAX_QUESTION_CHARS in api.py
const REQUEST_TIMEOUT_MS = 120000; // model calls with one retry (locally also the 20 s busy wait)
const SLOW_AFTER_MS = 6000;

const TEXT = {
  searching: "Ψάχνω διαδικασίες στο Μητρώο Μίτος...",
  slow: "Η αναζήτηση παίρνει λίγο περισσότερο από το συνηθισμένο...",
  found: "Διαβάζω τις διαδικασίες και γράφω την απάντηση:",
  writing: "Γράφεται η απάντηση...",
  length: `Γράψτε μια ερώτηση από ${MIN_CHARS} έως ${MAX_CHARS} χαρακτήρες.`,
  offline: "Δεν ήταν δυνατή η σύνδεση με την υπηρεσία. Ελέγξτε τη σύνδεσή σας και δοκιμάστε ξανά.",
  failed: "Κάτι πήγε στραβά. Δοκιμάστε ξανά σε λίγο.",
  timeout: "Η απάντηση άργησε πολύ. Δοκιμάστε ξανά σε λίγο.",
};

const KICKER = {
  answered: "Απάντηση",
  ambiguous: "Χρειάζομαι μια διευκρίνιση",
  not_found: "Δεν βρέθηκε",
};

const $ = (selector) => document.querySelector(selector);

const view = {
  back: $("#back"),
  home: $("#home"),
  result: $("#result"),
  asked: $("#asked"),
  answer: $("#answer"),
  homeForm: $("#home-form"),
  followForm: $("#follow-form"),
};

let inFlight = null; // the AbortController of the question being answered

// ---------- small DOM helpers ----------

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [name, value] of Object.entries(options.attrs || {})) node.setAttribute(name, value);
  for (const child of children) if (child) node.append(child);
  return node;
}

function svgIcon(paths, stroke, size = 20) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [name, value] of Object.entries({
    width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke,
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  })) svg.setAttribute(name, value);
  for (const d of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

const ICON_DOC = ["M6 3h9l3 3v15H6z", "M9 11h6", "M9 15h4"];
const ICON_ALERT = ["M12 3l9 16H3z", "M12 10v4", "M12 17v.01"];
const ICON_CHEVRON = ["M9 6l6 6-6 6"];
const ICON_EXTERNAL = ["M7 17L17 7", "M9 7h8v8"];
const ICON_THUMB_UP = ["M7 10v11H4V10z", "M7 10l4-7c1.7 0 2.8 1.3 2.4 3L13 9h5.5a2 2 0 0 1 2 2.4l-1.5 7.5a2 2 0 0 1-2 1.6H7"];
const ICON_THUMB_DOWN = ["M17 14V3h3v11z", "M17 14l-4 7c-1.7 0-2.8-1.3-2.4-3L11 15H5.5a2 2 0 0 1-2-2.4L5 5.1a2 2 0 0 1 2-1.6h10"];

// ---------- feedback: one vote per answer, optional reason; never the question ----------
// POST ask/feedback {answer_id, helpful, reason?} (api.py, decision 014). The server keeps the
// vote next to the answer's procedure ids and status, written when the answer was made, so a
// vote needs a real answer and counts once. No free text: the reasons are fixed.

const FEEDBACK_URL = "ask/feedback"; // under /ask: CloudFront sends only /ask* to the API
const REASONS = [
  ["wrong_procedure", "Βρήκε λάθος διαδικασία"],
  ["missing_info", "Λείπουν πληροφορίες"],
  ["wrong_numbers", "Λάθος ποσό ή προθεσμία"],
  ["hard_to_read", "Δύσκολο να το καταλάβω"],
  ["other", "Κάτι άλλο"],
];

async function sendFeedback(body) {
  const text = JSON.stringify(body);
  try {
    const response = await fetch(FEEDBACK_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "x-amz-content-sha256": await sha256Hex(text) },
      body: text,
    });
    return response.ok;
  } catch {
    return false;
  }
}

function feedbackBlock(answerId) {
  const block = el("section", { className: "feedback", attrs: { "aria-label": "Αξιολόγηση απάντησης" } });
  const thanks = (message) => block.replaceChildren(el("p", { className: "feedback-thanks", attrs: { role: "status" }, text: message }));

  const yes = el("button", { className: "vote vote-yes", attrs: { type: "button" } }, [svgIcon(ICON_THUMB_UP, "currentColor"), "Ναι"]);
  const no = el("button", { className: "vote vote-no", attrs: { type: "button" } }, [svgIcon(ICON_THUMB_DOWN, "currentColor"), "Όχι"]);
  block.append(
    el("p", { className: "feedback-title", text: "Σας βοήθησε αυτή η απάντηση;" }),
    el("div", { className: "votes" }, [yes, no]),
  );

  yes.addEventListener("click", async () => {
    yes.disabled = no.disabled = true;
    const sent = await sendFeedback({ answer_id: answerId, helpful: true });
    thanks(sent ? "Ευχαριστούμε! Χαιρόμαστε που βοήθησε." : "Δεν ήταν δυνατή η αποστολή. Ευχαριστούμε πάντως.");
  });

  no.addEventListener("click", async () => {
    yes.disabled = no.disabled = true;
    const sent = await sendFeedback({ answer_id: answerId, helpful: false });
    if (!sent) return thanks("Δεν ήταν δυνατή η αποστολή. Ευχαριστούμε πάντως.");
    // The "no" is already counted; the reason is an optional second step.
    const chips = REASONS.map(([key, label]) => {
      const chip = el("button", { className: "chip reason", text: label, attrs: { type: "button" } });
      chip.addEventListener("click", async () => {
        for (const other of block.querySelectorAll(".reason")) other.disabled = true;
        await sendFeedback({ answer_id: answerId, helpful: false, reason: key });
        thanks("Ευχαριστούμε. Θα το κοιτάξουμε για να βελτιώσουμε τις απαντήσεις.");
      });
      return chip;
    });
    block.replaceChildren(
      el("p", { className: "feedback-title", text: "Λυπούμαστε. Τι δεν πήγε καλά;" }),
      el("div", { className: "chips" }, chips),
      el("p", { className: "hint", text: "Προαιρετικό. Δεν αποθηκεύουμε την ερώτησή σας." }),
    );
  });
  return block;
}

// Mitos links open in a new tab, so the answer (a paid question) is still here afterwards.
const NEW_TAB = { target: "_blank", rel: "noopener noreferrer" };
const newTabNote = () => el("span", { className: "visually-hidden", text: " (ανοίγει σε νέα καρτέλα)" });

// ---------- Markdown: only what the answer prompt produces ----------
// Headings (##, ###), bullet and numbered lists (one nesting level), > quotes, --- rules,
// **bold** and *italic*. Anything else is shown as plain text.

function inline(text) {
  const fragment = document.createDocumentFragment();
  const pattern = /\*\*([^*]+)\*\*|\*([^*\s][^*]*?)\*/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) fragment.append(text.slice(last, match.index));
    fragment.append(match[1] !== undefined ? el("strong", { text: match[1] }) : el("em", { text: match[2] }));
    last = match.index + match[0].length;
  }
  if (last < text.length) fragment.append(text.slice(last));
  return fragment;
}

function withBreaks(lines) {
  const fragment = document.createDocumentFragment();
  lines.forEach((line, index) => {
    if (index > 0) fragment.append(el("br"));
    fragment.append(inline(line));
  });
  return fragment;
}

function renderMarkdown(source) {
  const root = el("div", { className: "markdown" });
  let paragraph = [];
  let quote = [];
  let list = null; // { node, kind }

  const flush = () => {
    if (paragraph.length) root.append(el("p", {}, [withBreaks(paragraph)]));
    if (quote.length) root.append(el("blockquote", {}, [withBreaks(quote)]));
    paragraph = [];
    quote = [];
    list = null;
  };

  const addItem = (kind, indent, text, start) => {
    if (paragraph.length || quote.length || !list || list.kind !== kind) {
      flush();
      const node = el(kind);
      if (kind === "ol" && start !== 1) node.setAttribute("start", String(start));
      root.append(node);
      list = { node, kind };
    }
    list.node.append(el("li", { className: indent >= 2 ? "nested" : "" }, [inline(text)]));
  };

  for (const line of source.replace(/\r\n?/g, "\n").split("\n")) {
    let match;
    if (!line.trim()) {
      flush();
    } else if ((match = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      flush();
      root.append(el(match[1].length <= 2 ? "h2" : "h3", {}, [inline(match[2])]));
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      root.append(el("hr"));
    } else if ((match = line.match(/^\s*>\s?(.*)$/))) {
      if (paragraph.length || list) flush();
      quote.push(match[1]);
    } else if ((match = line.match(/^(\s*)[-*•]\s+(.*)$/))) {
      addItem("ul", match[1].length, match[2], 1);
    } else if ((match = line.match(/^(\s*)(\d+)[.)]\s+(.*)$/))) {
      addItem("ol", match[1].length, match[3], Number(match[2]));
    } else if (list && /^\s+/.test(line)) {
      list.node.lastElementChild.append(" ", inline(line.trim())); // a wrapped list item
    } else {
      if (list || quote.length) flush();
      paragraph.push(line.trim());
    }
  }
  flush();
  return root;
}

// ---------- answer card ----------

function greekDate(isoDate) {
  const [year, month, day] = String(isoDate).split("-");
  return day && month && year ? `${day}/${month}/${year}` : String(isoDate);
}

// The main link is the procedure's page on mitos.gov.gr; id.mitos.gov.gr (`url`) is the
// permanent id, kept as a second link because it was down on 2026-10-22.
function sourceLink(procedure, label) {
  return el("a", { className: "source", attrs: { href: procedure.page_url, ...NEW_TAB } }, [
    svgIcon(ICON_DOC, "currentColor", 22),
    el("span", { className: "source-text" }, [
      el("span", { className: "source-label", text: label }),
      el("span", { className: "source-title", text: procedure.title }),
      el("span", { className: "source-date", text: `Ενημερώθηκε: ${greekDate(procedure.last_updated)}` }),
      newTabNote(),
    ]),
    svgIcon(ICON_CHEVRON, "currentColor", 18),
  ]);
}

// `withGlance`: each source carries its own tiles (an answer with several sources, so the
// tiles do not stack up above the answer text).
function sourcesBlock(title, label, procedures, withGlance = false) {
  return el("section", { className: "sources" }, [
    el("h3", { text: title }),
    ...procedures.map((procedure) => el("div", { className: "source-item" }, [
      sourceLink(procedure, label),
      el("a", { className: "source-permalink", text: "Μόνιμος σύνδεσμος (id.mitos.gov.gr)", attrs: { href: procedure.url, ...NEW_TAB } }, [newTabNote()]),
      withGlance ? glancePanel(procedure, false) : null,
    ])),
  ]);
}

function optionsBlock(question, options) {
  return el("div", { className: "options" }, options.map((option) => {
    const button = el("button", { text: option.label, attrs: { type: "button" } });
    button.addEventListener("click", () => {
      // "question: label" keeps the asker's own words for the answer. On the dev ambiguous
      // items it ranks the option's procedure first as often as the label alone (10 of 10 each).
      // Drop the question's own end mark first (Greek ";" is "?"), or it reads "...;: label".
      const followUp = `${question.replace(/[\s;?.!·:]+$/u, "")}: ${option.label}`;
      ask(followUp.length <= MAX_CHARS ? followUp : option.label);
    });
    return el("div", { className: "option" }, [
      button,
      el("a", { text: "Η διαδικασία στο Μίτος", attrs: { href: option.page_url, ...NEW_TAB } }, [newTabNote()]),
    ]);
  }));
}

// Tiles and the online-application link come from the facts row (api.py at_a_glance and
// apply_online), shown for answered sources only: a not_found suggestion is not the answer.
// Untitled when it sits under its source card, which already names the procedure.
function glancePanel(procedure, titled = true) {
  const tiles = procedure.at_a_glance || [];
  const online = procedure.apply_online;
  const canLink = online && typeof online.url === "string" && online.url.startsWith("https://");
  if (!tiles.length && !canLink) return null;
  const panel = el("div", { className: "glance" });
  if (titled) panel.append(el("h2", { className: "glance-title" }, [el("small", { text: "Από το Μητρώο Μίτος" }), procedure.title]));
  if (tiles.length) {
    panel.append(el("dl", { className: "tiles" }, tiles.map((tile) => el("div", { className: "tile" }, [
      el("dt", { text: tile.label }),
      el("dd", { text: tile.value }),
    ]))));
  }
  if (canLink) {
    panel.append(el("a", { className: "apply", attrs: { href: online.url, ...NEW_TAB } }, [
      el("span", { className: "apply-text" }, [
        el("span", { className: "apply-label", text: "Κάντε την αίτηση ηλεκτρονικά" }),
        el("span", { className: "apply-host", text: `στο ${online.host}` }),
        newTabNote(),
      ]),
      svgIcon(ICON_EXTERNAL, "#FFFFFF", 18),
    ]));
  }
  return panel;
}

function answerCard(question, data) {
  const card = el("article", { className: "card" }, [el("p", { className: "kicker", text: KICKER[data.status] })]);
  const sources = data.sources || [];
  const answered = data.status === "answered";
  // One source: its tiles lead the card, as in the mockup. Several: each source card carries its own.
  const tilesOnTop = answered && sources.length === 1;
  if (tilesOnTop) {
    const panel = glancePanel(sources[0]);
    if (panel) card.append(panel);
  }
  card.append(renderMarkdown(data.answer || ""));

  if (data.status === "ambiguous") {
    if (data.clarifying_question) card.append(el("p", { className: "clarify", text: data.clarifying_question }));
    if (data.options && data.options.length) card.append(optionsBlock(question, data.options));
  }

  if (data.warning) {
    card.append(el("div", { className: "warning", attrs: { role: "note" } }, [
      svgIcon(ICON_ALERT, "#7A4310"),
      el("p", { text: data.warning }),
    ]));
  }

  if (sources.length) {
    if (answered) {
      card.append(sourcesBlock(sources.length > 1 ? "Πηγές" : "Πηγή", "Μίτος, επίσημη σελίδα", sources, !tilesOnTop));
    } else {
      // For not_found these are suggestions, not citations.
      card.append(sourcesBlock("Ίσως σας ενδιαφέρει", "Μίτος, επίσημη σελίδα", sources));
    }
  }

  if (data.answer_id && data.status !== "ambiguous") card.append(feedbackBlock(data.answer_id));
  if (data.attribution) card.append(el("p", { className: "attribution", text: data.attribution }));
  return card;
}

// `retryQuestion`: offer to send the same question again (a 503: busy, or the service was
// unavailable before any model call), so nobody retypes it on a phone.
function messageCard(message, retryQuestion = null) {
  const card = el("div", { className: "card error-card", attrs: { role: "alert" } }, [el("p", { text: message })]);
  if (retryQuestion) {
    const retry = el("button", { className: "retry", text: "Δοκιμάστε ξανά", attrs: { type: "button" } });
    retry.addEventListener("click", () => ask(retryQuestion));
    card.append(retry);
  }
  return card;
}

function progressCard(message) {
  const text = el("p", { text: message });
  const card = el("div", { className: "card progress" }, [el("span", { className: "spinner", attrs: { "aria-hidden": "true" } }), text]);
  return { card, text };
}

function foundCard(titles) {
  return el("div", { className: "card found-card" }, [
    el("span", { className: "spinner", attrs: { "aria-hidden": "true" } }),
    el("div", {}, [
      el("p", { text: TEXT.found }),
      el("ul", { className: "found" }, titles.map((title) => el("li", { text: title }))),
    ]),
  ]);
}

// The answer while it is being written: the text only, re-rendered at most once a frame.
function streamingCard() {
  const body = el("div");
  const card = el("article", { className: "card" }, [el("p", { className: "kicker", text: TEXT.writing }), body]);
  let source = "";
  let queued = false;
  return {
    card,
    append(text) {
      source += text;
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        body.replaceChildren(renderMarkdown(source));
      });
    },
  };
}

// Yields each JSON event of an NDJSON body. Greek is multi-byte UTF-8, so the decoder runs in
// stream mode, and a line cut between two network chunks waits for its end.
async function* ndjson(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) if (line.trim()) yield JSON.parse(line);
    if (done) break;
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

// ---------- asking ----------

function setBusy(busy) {
  for (const control of document.querySelectorAll(".send, .chip, .option button, .retry")) control.disabled = busy;
}

function showFormError(form, message) {
  const slot = form.querySelector(".form-error");
  slot.textContent = message || "";
  slot.hidden = !message;
}

function showHome() {
  if (inFlight) inFlight.abort();
  view.result.hidden = true;
  view.back.hidden = true;
  view.home.hidden = false;
  view.answer.replaceChildren();
  // A fresh start: on a shared device the next person should not see the last question.
  $("#question").value = "";
  showFormError(view.homeForm, "");
  $("#question").focus();
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (body && typeof body.error === "string") return body.error; // the API's Greek messages
  } catch {
    // a plain-text 500 or a proxy page: fall through
  }
  return TEXT.failed;
}

async function ask(rawQuestion) {
  if (inFlight) return;
  const question = rawQuestion.trim();

  view.home.hidden = true;
  view.result.hidden = false;
  view.back.hidden = false;
  view.asked.textContent = question;
  view.followForm.querySelector("textarea").value = "";
  showFormError(view.followForm, "");
  window.scrollTo({ top: 0 });

  const progress = progressCard(TEXT.searching);
  view.answer.replaceChildren(progress.card);
  const slow = setTimeout(() => { progress.text.textContent = TEXT.slow; }, SLOW_AFTER_MS);

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), REQUEST_TIMEOUT_MS);
  inFlight = controller;
  setBusy(true);

  try {
    const body = JSON.stringify({ question });
    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "x-amz-content-sha256": await sha256Hex(body) },
      body,
      signal: controller.signal,
    });
    if (!response.ok) {
      view.answer.replaceChildren(messageCard(await errorMessage(response), response.status === 503 ? question : null));
      return;
    }
    // Screen readers wait for the finished card instead of reading every streamed frame.
    view.answer.setAttribute("aria-busy", "true");
    let streaming = null;
    let finished = false;
    for await (const event of ndjson(response)) {
      if (event.type === "found") {
        clearTimeout(slow);
        if (!streaming && event.titles && event.titles.length) view.answer.replaceChildren(foundCard(event.titles));
      } else if (event.type === "text") {
        if (!streaming) {
          clearTimeout(slow);
          streaming = streamingCard();
          view.answer.replaceChildren(streaming.card);
        }
        streaming.append(event.text);
      } else if (event.type === "done") {
        finished = true;
        view.answer.replaceChildren(KICKER[event.status] ? answerCard(question, event) : messageCard(TEXT.failed));
      } else if (event.type === "error") {
        finished = true;
        view.answer.replaceChildren(messageCard(typeof event.error === "string" ? event.error : TEXT.failed));
      }
    }
    if (!finished) view.answer.replaceChildren(messageCard(TEXT.failed));
  } catch (failure) {
    if (controller.signal.aborted && controller.signal.reason !== "timeout") return; // the user went back
    view.answer.replaceChildren(messageCard(controller.signal.aborted ? TEXT.timeout : TEXT.offline));
  } finally {
    clearTimeout(slow);
    clearTimeout(timeout);
    view.answer.setAttribute("aria-busy", "false");
    inFlight = null;
    setBusy(false);
  }
}

function submitFrom(form) {
  const textarea = form.querySelector("textarea");
  const question = textarea.value.trim();
  if (question.length < MIN_CHARS || question.length > MAX_CHARS) {
    showFormError(form, TEXT.length); // checked here so a typo never spends a rate-limit slot
    textarea.focus();
    return;
  }
  showFormError(form, "");
  ask(question);
}

for (const form of [view.homeForm, view.followForm]) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitFrom(form);
  });
  form.querySelector("textarea").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitFrom(form);
    }
  });
}

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => ask(chip.dataset.question));
}

view.back.addEventListener("click", showHome);
