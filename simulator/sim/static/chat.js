/* Cat Nanny Alexa+ simulator, browser side.
   Layout, voice handling and rendering conventions come from CatNannyFramework's simulator (Shu Wang).
   Everything the page knows comes through the simulator backend, which is an MCP client of the Cat Nanny
   server: the conversation goes through a language model that picks MCP tools; the side panels call the
   read-only tools directly. The Echo Show screen renders MCP Apps views in a sandboxed iframe. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const labels = { content: "Vomit contents", has_blood: "Blood observed", episode_count: "Episodes", low_energy: "Lower energy",
                   minutes_after_meal: "Minutes after food", status: "Appetite", hours_since_food: "Hours since normal meal" };
  const values = { food: "Food", hairball: "Hairball", liquid: "Liquid", foam: "Foam", other: "Other",
                   not_eating: "Not eating", eating_less: "Eating less", normal: "Normal" };
  const types = { vomit: "Vomiting", appetite: "Appetite" };

  let household = { cats: [], caregivers: [] };
  let selectedCat = null, history = [], currentEvent = null, lastReply = "", busy = false, listening = false;
  let appResult = null, reportRun = 0;

  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const read = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const write = (key, value) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch {} };
  const readJSON = key => { try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch { return null; } };
  const writeJSON = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch {} };
  const error = text => { $("chat-error").textContent = text; $("chat-error").hidden = !text; };
  const time = value => new Date(value).toLocaleString("en-US");
  const format = value => value == null ? "Unknown" : typeof value === "boolean" ? (value ? "Yes" : "No") : values[value] || String(value);

  async function api(path, options = {}) {
    let r;
    try { r = await fetch(path, options); } catch { throw new Error("Connection interrupted. Your input is kept for retry."); }
    const data = await r.json();
    if (!r.ok) { const d = data.detail; throw new Error(typeof d === "string" ? d : d?.message || "Please check your input and try again."); }
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  // ---- device and audio -------------------------------------------------------------------
  const synth = window.speechSynthesis, Utterance = window.SpeechSynthesisUtterance;
  const audioSupported = Boolean(synth && Utterance);
  let audioEnabled = read("catnanny.audio") !== "off", activeSpeech = null;
  function deviceState(state, label) { $("alexa-device").setAttribute("data-state", state); $("device-status").textContent = label; }
  function restDevice() { if (!listening) deviceState(busy ? "thinking" : "ready", busy ? "Thinking…" : "Ready when you are"); }
  function stopReply() { activeSpeech = null; if (audioSupported) synth.cancel(); restDevice(); }
  function renderAudio() {
    $("audio-toggle").disabled = !audioSupported;
    $("audio-toggle").setAttribute("aria-pressed", String(audioEnabled && audioSupported));
    $("audio-toggle").textContent = audioSupported ? `Voice replies: ${audioEnabled ? "on" : "off"}` : "Voice replies unavailable";
    $("audio-replay").disabled = !audioSupported || !lastReply || busy || listening;
  }
  function speakReply(text) {
    if (!audioSupported || !text || listening) return;
    stopReply();
    const u = new Utterance(text); activeSpeech = u; u.lang = $("voice-language").value; u.rate = 1;
    u.onstart = () => { if (activeSpeech === u) { deviceState("speaking", "Speaking…"); $("audio-status").textContent = "Alexa is speaking…"; } };
    u.onend = () => { if (activeSpeech === u) { activeSpeech = null; restDevice(); $("audio-status").textContent = "Reply finished. Speak or type your next answer."; } };
    u.onerror = u.onend;
    synth.speak(u);
  }
  $("audio-toggle").addEventListener("click", () => { audioEnabled = !audioEnabled; write("catnanny.audio", audioEnabled ? "on" : "off"); if (!audioEnabled) stopReply(); renderAudio(); });
  $("audio-replay").addEventListener("click", () => { if (!busy && !listening) speakReply(lastReply); });
  window.addEventListener("pagehide", stopReply);

  // ---- voice input (browser speech recognition; auto-sends after a pause, like a wake-word device) ----
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null, voiceBase = "", voiceFailed = false, voiceHasFinal = false, autoSendTimer = null, autoSendGen = 0;
  function cancelAutoSend() { autoSendGen++; if (autoSendTimer !== null) clearTimeout(autoSendTimer); autoSendTimer = null; $("voice-cancel").hidden = true; }
  function scheduleAutoSend() {
    cancelAutoSend(); const gen = autoSendGen, draft = $("message-input").value; let remaining = 3;
    $("voice-cancel").hidden = false;
    (function tick() {
      if (gen !== autoSendGen) return;
      if (busy || listening || document.hidden || $("message-input").value !== draft) { cancelAutoSend(); return; }
      if (remaining === 0) { cancelAutoSend(); $("chat-form").requestSubmit(); return; }
      $("voice-hint").textContent = `Sending in ${remaining}… Edit the text or cancel to keep it as a draft.`; remaining--; autoSendTimer = setTimeout(tick, 1000);
    })();
  }
  $("voice-cancel").addEventListener("click", () => { cancelAutoSend(); $("voice-hint").textContent = "Auto-send cancelled. Your text is kept."; });
  $("message-input").addEventListener("input", cancelAutoSend);
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancelAutoSend(); });
  function finishVoice() {
    listening = false; $("voice-toggle").setAttribute("aria-pressed", "false"); $("voice-toggle").textContent = "● Start speaking";
    $("voice-language").disabled = false; $("message-input").disabled = busy; renderAudio();
    if (!busy) deviceState(voiceFailed ? "error" : "ready", voiceFailed ? "Microphone unavailable" : "Ready when you are");
  }
  if (!Recognition) { $("voice-toggle").disabled = true; $("voice-hint").textContent = "Speech recognition is unavailable in this browser. Try Chrome, or type your observation."; }
  else {
    $("voice-toggle").addEventListener("click", () => {
      if (busy) return;
      if (listening) { recognition.stop(); return; }
      cancelAutoSend(); stopReply(); voiceHasFinal = false; voiceBase = $("message-input").value.trim(); voiceFailed = false;
      recognition = new Recognition(); recognition.lang = $("voice-language").value; recognition.interimResults = true; recognition.continuous = false;
      recognition.onresult = e => {
        let transcript = "";
        for (let i = 0; i < e.results.length; i++) { transcript += e.results[i][0].transcript + " "; if (e.results[i].isFinal && e.results[i][0].transcript.trim()) voiceHasFinal = true; }
        $("message-input").value = [voiceBase, transcript.trim().replace(/^alexa[,\s]*/i, "")].filter(Boolean).join(" ").slice(0, 4000);
        $("device-subtitle").textContent = transcript.trim() || "Listening…";
      };
      recognition.onerror = e => { cancelAutoSend(); voiceFailed = true; $("voice-hint").textContent = ({ "not-allowed": "Microphone permission was denied.", "no-speech": "No speech detected. Try again." })[e.error] || "Speech recognition stopped. Retry or type."; };
      recognition.onend = () => { finishVoice(); if (!voiceFailed && voiceHasFinal && !busy) scheduleAutoSend(); };
      listening = true; $("message-input").disabled = true; $("voice-language").disabled = true;
      $("voice-toggle").textContent = "■ Stop listening"; $("voice-toggle").setAttribute("aria-pressed", "true");
      deviceState("listening", "Listening…"); renderAudio(); $("voice-hint").textContent = "Speak now.";
      recognition.start();
    });
  }

  // ---- MCP Apps host: render a ui:// view inside the Echo Show screen ----------------------------
  const frame = $("show-app");
  function showApp(ui) {
    appResult = ui.tool_result;
    frame.hidden = false; frame.srcdoc = ui.html;
  }
  function hideApp() { frame.hidden = true; frame.srcdoc = ""; appResult = null; }
  window.addEventListener("message", e => {
    if (e.source !== frame.contentWindow) return;
    const m = e.data || {};
    if (m.method === "ui/initialize") {
      frame.contentWindow.postMessage({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2026-01-26",
        hostInfo: { name: "cat-nanny-alexa-simulator", version: "0.2.0" }, hostCapabilities: {},
        hostContext: { theme: "light", displayMode: "inline", platform: "web", deviceClass: "small" } } }, "*");
    } else if (m.method === "ui/notifications/initialized" && appResult) {
      frame.contentWindow.postMessage({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: appResult }, "*");
    }
  });

  // ---- conversation ---------------------------------------------------------------------------
  function scroll() { $("messages").scrollTop = $("messages").scrollHeight; }
  function message(role, text, name) {
    $("device-subtitle").textContent = text; $("welcome").hidden = true;
    const row = node("div", `message ${role}`), content = node("div", "message-content");
    if (role === "assistant") row.append(node("div", "message-avatar", "✦"));
    content.append(node("div", "message-label", role === "user" ? (name || "You") : "CAT NANNY · Alexa+ simulation"), node("div", "bubble", text));
    row.append(content); $("messages").append(row); scroll(); return content;
  }
  function uploadCard(container, url, catName) {
    const card = node("div", "delivery-card");
    card.append(node("span", "eyebrow", "SHOWN ON SCREEN"), node("strong", null, `Add a photo for ${catName || "this cat"}`),
                node("p", null, "Photos attach to this observation. Nothing is texted or emailed."));
    const a = node("a", null, "Open photo upload ↗"); a.href = url; a.target = "_blank"; a.rel = "noopener";
    card.append(a); container.append(card); scroll();
  }
  function renderCalls(steps) {
    const list = $("calls"); list.replaceChildren();
    for (const s of steps) {
      const li = node("li"); li.append(node("div", `name${s.is_error ? " err" : ""}`, s.tool + (s.is_error ? " (error)" : "")));
      const args = node("pre", null, "arguments " + JSON.stringify(s.arguments, null, 1));
      const res = node("pre", null, "result " + JSON.stringify(s.result.structuredContent || s.result.content, null, 1));
      li.append(args, res); list.append(li);
    }
    $("calls-box").hidden = !steps.length;
  }

  function clearRecord() {
    currentEvent = null; $("record").hidden = true; $("record-empty").hidden = false;
    for (const id of ["event-type", "event-status", "event-time", "event-caregiver"]) $(id).textContent = "";
    $("event-details").replaceChildren(); $("photos").replaceChildren();
  }
  function renderRecord(o) {
    currentEvent = o; $("record").hidden = false; $("record-empty").hidden = true;
    $("event-type").textContent = `${types[o.event_type] || o.event_type} · ${o.cat_name}`;
    $("event-status").textContent = o.status === "completed" ? "Complete" : "Needs detail";
    $("event-time").textContent = time(o.occurred_at);
    $("event-caregiver").textContent = `First reported by ${o.reporter || "Not recorded"}`;
    $("event-details").replaceChildren();
    for (const [key, value] of Object.entries(o.details || {})) {
      if (!labels[key]) continue;
      const row = node("div", "detail-row"); row.append(node("dt", null, labels[key]), node("dd", null, o.unknown_fields?.includes(key) ? "Not sure" : format(value)));
      $("event-details").append(row);
    }
    $("photos").replaceChildren();
    for (const p of o.photos || []) { const a = node("a"); a.href = p.url; a.target = "_blank"; a.rel = "noopener"; const img = node("img"); img.src = p.url; img.alt = "Photo attached to this observation"; a.append(img); $("photos").append(a); }
    $("photo-note").textContent = (o.photos || []).length ? `${o.photos.length} photo(s) attached.` : "Photos stay with this observation.";
  }
  async function refreshRecord() {
    if (!currentEvent) return;
    const data = await api(`/api/observation?cat=${encodeURIComponent(currentEvent.cat_name)}&event_id=${encodeURIComponent(currentEvent.event_id)}`);
    if (data) renderRecord(data);
  }

  function renderProfiles() {
    const host = $("cat-list"); host.replaceChildren();
    for (const cat of household.cats) {
      const b = node("button", `companion-option${cat.cat_id === selectedCat ? " selected" : ""}`); b.type = "button";
      b.append(node("span", "cat-avatar", cat.name[0].toUpperCase()), node("span", null, cat.name));
      b.setAttribute("aria-pressed", String(cat.cat_id === selectedCat));
      b.addEventListener("click", () => { selectedCat = cat.cat_id; write("catnanny.cat", selectedCat); renderProfiles(); });
      host.append(b);
    }
    const cat = household.cats.find(c => c.cat_id === selectedCat);
    $("chat-title").textContent = cat ? `How is ${cat.name} today?` : "How is your companion today?";
    $("record-heading").textContent = cat ? `${cat.name}'s current observation` : "Current observation";
    $("subject-note").textContent = cat ? `Selected: ${cat.name}. Name another cat to talk about them instead.` : "Name a cat in your message.";
  }
  // Caregivers exist on the server once they report something (the reporter argument), so names added here
  // before their first report are kept in this browser only.
  const localPeople = () => { try { return JSON.parse(read("catnanny.people") || "[]"); } catch { return []; } };
  function renderPeople(selected) {
    const sel = $("caregiver-select"); sel.replaceChildren();
    const names = [...new Set([...household.caregivers.map(p => p.name), ...localPeople()])];
    for (const name of names) { const o = node("option", null, name); o.value = name; sel.append(o); }
    if (selected && [...sel.options].some(o => o.value === selected)) sel.value = selected;
  }
  async function loadHousehold() {
    household = await api("/api/household");
    if (!household.cats.some(c => c.cat_id === selectedCat)) selectedCat = household.cats[0]?.cat_id || null;
    renderProfiles(); renderPeople(read("catnanny.caregiver") || household.caregivers[0]?.name);
  }

  function setBusy(on) {
    busy = on; $("send-message").disabled = on; $("message-input").disabled = on || listening; $("voice-toggle").disabled = on || !Recognition;
    $("composer-status").textContent = on ? "Alexa is thinking…" : "Enter to send · Shift + Enter for a new line"; restDevice(); renderAudio();
  }

  async function send(text) {
    const speaker = $("caregiver-select").value || "Alex";
    const cat = household.cats.find(c => c.cat_id === selectedCat);
    message("user", text, speaker);
    setBusy(true); error("");
    try {
      const data = await post("/api/turn", { text, speaker, selected_cat: cat?.name || null, history });
      history = data.history; writeJSON("catnanny.history", history);
      const content = message("assistant", data.reply);
      lastReply = data.reply; renderCalls(data.steps);
      // Side effects of the tools the model chose: update the record card, show cards, render the MCP Apps view.
      let observation = null, uploadFor = null, householdChanged = false;
      for (const s of data.steps) {
        const sc = s.result.structuredContent;
        if (s.is_error || !sc) continue;
        if (s.tool === "log_observation" || s.tool === "correct_cat") { if (sc.observation) observation = sc.observation; }
        if (s.tool === "get_photo_upload_link") uploadFor = sc.photo_upload_url;
        if (s.tool === "add_cat" || s.tool === "correct_cat") householdChanged = true;
      }
      if (observation) { renderRecord(observation); if (observation.cat_id !== selectedCat) { selectedCat = observation.cat_id; write("catnanny.cat", selectedCat); } }
      if (uploadFor) uploadCard(content, uploadFor, observation?.cat_name || cat?.name);
      if (householdChanged) await loadHousehold(); else renderProfiles();
      if (data.ui) showApp(data.ui); else hideApp();
      if (audioEnabled) speakReply(data.reply);
    } catch (e) {
      error(e.message); $("message-input").value = text;
    } finally { setBusy(false); renderAudio(); $("message-input").focus(); }
  }

  $("chat-form").addEventListener("submit", e => { e.preventDefault(); const text = $("message-input").value.trim(); if (busy || listening || !text) return; cancelAutoSend(); $("message-input").value = ""; send(text); });
  $("message-input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !busy) { e.preventDefault(); $("chat-form").requestSubmit(); } });
  for (const b of document.querySelectorAll("[data-example]")) b.addEventListener("click", () => { if (busy) return; const cat = household.cats.find(c => c.cat_id === selectedCat); $("message-input").value = b.dataset.example.replace("Mochi", cat?.name || "Mochi"); $("message-input").focus(); });
  $("new-conversation").addEventListener("click", () => {
    if (busy) return;
    history = []; writeJSON("catnanny.history", history); cancelAutoSend(); stopReply(); lastReply = "";
    for (const item of $("messages").querySelectorAll(".message")) item.remove();
    $("welcome").hidden = false; clearRecord(); hideApp(); renderCalls([]); error(""); $("message-input").focus();
  });
  $("caregiver-select").addEventListener("change", () => write("catnanny.caregiver", $("caregiver-select").value));
  $("refresh-photos").addEventListener("click", () => refreshRecord().catch(() => error("Could not refresh photos. Please retry.")));
  window.addEventListener("storage", e => { if (e.key === "catnanny.photo-uploaded") refreshRecord().catch(() => {}); });
  window.addEventListener("message", e => { if (e.data?.type === "catnanny:photo-uploaded") refreshRecord().catch(() => {}); });
  window.addEventListener("focus", () => refreshRecord().catch(() => {}));

  // ---- profiles: add a cat or a caregiver (direct MCP calls, no language model) --------------------
  let profileKind = "cat";
  function openProfile(kind) {
    profileKind = kind; $("profile-title").textContent = kind === "cat" ? "Add companion" : "Add caregiver";
    $("profile-sex-row").hidden = kind !== "cat"; $("profile-name").value = ""; $("profile-error").hidden = true; $("profile-dialog").showModal(); $("profile-name").focus();
  }
  $("add-cat").addEventListener("click", () => openProfile("cat"));
  $("add-caregiver").addEventListener("click", () => openProfile("caregiver"));
  $("profile-cancel").addEventListener("click", () => $("profile-dialog").close());
  $("profile-form").addEventListener("submit", async e => {
    e.preventDefault();
    const name = $("profile-name").value.trim(); if (!name) return;
    try {
      if (profileKind === "cat") { const cat = await post("/api/add-cat", { name, sex: $("profile-sex").value || "unknown" }); selectedCat = cat.cat_id; write("catnanny.cat", selectedCat); }
      else { write("catnanny.people", JSON.stringify([...new Set([...localPeople(), name])])); write("catnanny.caregiver", name); }
      await loadHousehold(); $("profile-dialog").close();
    } catch (err) { $("profile-error").textContent = err.message; $("profile-error").hidden = false; }
  });

  // ---- care log and vet summary (read-only MCP tools called directly) -------------------------------
  function table(items) {
    const wrap = node("div", "table-scroll"), t = node("table", "care-table"), head = node("tr");
    for (const h of ["Time", "Observation", "Caregiver", "Status", "Photos"]) head.append(node("th", null, h));
    const thead = node("thead"); thead.append(head); t.append(thead);
    const body = node("tbody");
    for (const o of items) {
      const row = node("tr"); row.append(node("td", null, time(o.occurred_at)));
      const sym = node("td"); sym.append(node("strong", null, types[o.event_type] || o.event_type));
      for (const [k, v] of Object.entries(o.details || {})) if (labels[k] && v != null) sym.append(node("div", "muted", `${labels[k]}: ${format(v)}`));
      const photos = node("td"); const grid = node("div", "photo-grid");
      for (const p of o.photos || []) { const img = node("img"); img.src = p.url; img.alt = "Photo"; img.loading = "lazy"; grid.append(img); }
      if (!(o.photos || []).length) grid.append(node("span", "muted", "No photo"));
      photos.append(grid);
      row.append(sym, node("td", null, o.reporter || "—"), node("td", null, o.status === "completed" ? "Complete" : "Needs detail"), photos);
      body.append(row);
    }
    t.append(body); wrap.append(t); return wrap;
  }
  async function showHistory() {
    const cat = household.cats.find(c => c.cat_id === selectedCat); if (!cat) return;
    $("history-dialog").showModal(); $("history-title").textContent = `${cat.name} · care log`; $("history-body").textContent = "Loading…";
    try { const log = await api(`/api/care-log?cat=${encodeURIComponent(cat.name)}&days=90`); $("history-body").replaceChildren(log.total ? table(log.observations) : node("p", "muted", "Nothing logged yet.")); }
    catch (e) { $("history-body").textContent = e.message; }
  }
  for (const id of ["history-open", "history-mobile"]) $(id).addEventListener("click", showHistory);
  $("history-close").addEventListener("click", () => $("history-dialog").close());

  let latestReport = null;
  async function showReport() {
    const cat = household.cats.find(c => c.cat_id === selectedCat); if (!cat) return;
    if (!$("report-dialog").open) $("report-dialog").showModal();
    const run = ++reportRun, days = $("report-days").value;
    $("report-title").textContent = `${cat.name} · Last ${days} days`; $("report-body").textContent = "Preparing the summary…"; $("report-html").disabled = true;
    try {
      const data = await api(`/api/vet-summary?cat=${encodeURIComponent(cat.name)}&days=${days}`);
      if (run !== reportRun) return;
      latestReport = data.summary;
      $("report-body").replaceChildren(node("p", "muted", `${data.summary.counts.total_events} observation(s). Also shown on the Echo Show screen.`), table(data.summary.timeline));
      $("report-html").disabled = false;
      if (data.ui) showApp(data.ui);
    } catch (e) { $("report-body").textContent = e.message; }
  }
  for (const id of ["report-open", "report-mobile"]) $(id).addEventListener("click", showReport);
  $("report-days").addEventListener("change", () => { if ($("report-dialog").open) showReport(); });
  $("report-close").addEventListener("click", () => { $("report-dialog").close(); ++reportRun; });
  $("report-html").addEventListener("click", () => {
    if (!latestReport) return;
    const doc = document.implementation.createHTMLDocument(`${latestReport.cat} — Vet summary`);
    const style = doc.createElement("style"); style.textContent = "body{font:14px/1.5 system-ui;margin:35px;color:#273d35}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d9dfd4;padding:10px;text-align:left;vertical-align:top}img{width:120px;max-height:150px;object-fit:contain;margin:3px}.muted{font-size:12px;color:#657365}";
    doc.head.append(style);
    const h = doc.createElement("h1"); h.textContent = `${latestReport.cat} — Vet summary, last ${latestReport.days} days`; doc.body.append(h);
    for (const child of $("report-body").children) doc.body.append(child.cloneNode(true));
    const p = doc.createElement("p"); p.className = "muted"; p.textContent = latestReport.safety_note; doc.body.append(p);
    const blob = new Blob(["<!doctype html>" + doc.documentElement.outerHTML], { type: "text/html" });
    const url = URL.createObjectURL(blob), a = node("a"); a.href = url; a.download = "cat-nanny-vet-summary.html"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // ---- start ---------------------------------------------------------------------------------
  let config = { llm: "scripted" };
  (async () => {
    try {
      config = await api("/api/config");
      $("mode").textContent = config.llm === "bedrock" ? "Bedrock · live model" : "Offline · demo phrases only";
      $("mode-detail").textContent = config.llm === "bedrock" ? `${config.model} via Amazon Bedrock · MCP server: ${config.mcp_url}` : `Set BEDROCK_MODEL_ID for a real model · MCP server: ${config.mcp_url}`;
      selectedCat = read("catnanny.cat");
      await loadHousehold();
      history = readJSON("catnanny.history") || [];
      for (const m of history) {
        if (m.role === "user" && m.content?.[0]?.text) message("user", m.content[0].text);
        if (m.role === "assistant") { const t = (m.content || []).filter(b => b.text).map(b => b.text).join(" "); if (t) message("assistant", t); }
      }
    } catch (e) { $("mode").textContent = "Not connected"; error(e.message); }
    renderAudio(); restDevice();
  })();
})();
