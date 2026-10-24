const $ = (s) => document.querySelector(s);
const AUDIO = 1, FRAME = 2;
const counts = { books: 0, identified: 0, priced: 0, items: 0 };
let socket, stream, timer, speaker, micContext, speakerContext, startedAt, sent = 0, heading = null;

const turned = (e) => {
  if (e.alpha == null || !(e.absolute || e.type === "deviceorientationabsolute")) return;
  const r = Math.PI / 180, a = e.alpha * r, b = e.beta * r, g = e.gamma * r;
  const east = -(Math.cos(a) * Math.sin(g) + Math.sin(a) * Math.sin(b) * Math.cos(g));
  const north = -(Math.sin(a) * Math.sin(g) - Math.cos(a) * Math.sin(b) * Math.cos(g));
  heading = ((Math.atan2(east, north) / r) + 360) % 360;
};

const status = (text) => ($("#status").textContent = text);
const count = (name, value) => { counts[name] = value; $(`#${name}`).textContent = value; };

const line = (list, text, cls = "") => {
  const li = document.createElement("li");
  li.textContent = text;
  if (cls) li.className = cls;
  $(list).prepend(li);
};

let lastTalk = null;
const talk = (who, text) => {
  if (lastTalk && lastTalk.dataset.who === who) {
    lastTalk.textContent += text;
    return;
  }
  line("#talk", text, who);
  lastTalk = $("#talk").firstChild;
  lastTalk.dataset.who = who;
};

const jpeg = async (bitmap) => {
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  canvas.getContext("2d").drawImage(bitmap, 0, 0);
  bitmap.close?.();
  return new Uint8Array(await (await canvas.convertToBlob({ type: "image/jpeg", quality: 0.88 })).arrayBuffer());
};

const send = (kind, bytes) => {
  if (socket?.readyState !== WebSocket.OPEN) return;
  const message = new Uint8Array(bytes.byteLength + 1);
  message[0] = kind;
  message.set(new Uint8Array(bytes), 1);
  socket.send(message);
};

const onEvent = (e) => {
  if (e.kind === "books") {
    count("books", e.count);
    if (e.new) line("#log", `${e.new} new book${e.new > 1 ? "s" : ""} on the shelves (${e.count} so far)`);
  } else if (e.kind === "identified") {
    if (e.verdict === "identified") {
      count("identified", counts.identified + 1);
      line("#log", `${e.shelf} #${e.position}: ${e.title}${e.author ? ", " + e.author : ""}`);
    } else line("#log", `${e.shelf} #${e.position}: ${e.verdict.replace("_", " ")}`);
  } else if (e.kind === "priced") {
    if (e.new || e.used) count("priced", counts.priced + 1);
  } else if (e.kind === "items") {
    count("items", e.count);
    for (const d of e.new ?? []) line("#log", `Item: ${d}`);
  } else if (e.kind === "note") {
    line("#log", `Noted: "${e.said}"`);
  } else if (e.kind === "error") {
    line("#log", `Problem in ${e.stage}: ${e.message}`, "error");
  }
};

const onMessage = async (message) => {
  if (message.data instanceof ArrayBuffer) {
    const bytes = new Uint8Array(message.data);
    if (bytes[0] === AUDIO) speaker.port.postMessage(bytes.slice(1).buffer);
    return;
  }
  const m = JSON.parse(message.data);
  if (m.type === "transcript") talk(m.who, m.text);
  else if (m.type === "interrupted") speaker.port.postMessage("clear");
  else if (m.type === "check") {
    const bad = m.problems.filter((p) => p !== "no_spines");
    $("#check").textContent = bad.length ? `${bad.join(", ")}: ${m.advice}` : `Frame ${m.frame}: ${m.shows}`;
    $("#check").className = "check" + (bad.length ? " problem" : " muted");
  } else if (m.type === "inventory") onEvent(m);
  else if (m.type === "market") status(`Pricing in ${m.country}, ${m.currency}`);
  else if (m.type === "finishing") {
    clearInterval(timer);
    status("Building the claim packet...");
    $("#finish").disabled = true;
  } else if (m.type === "packet") {
    const head = m.final ? "Claim packet ready" : "First claim packet ready, still checking the rest";
    status(head);
    const base = `runs/${m.run}`;
    $("#result").innerHTML = `<div class="done">${head}: ${m.totals.book_count} books, ${m.totals.books_identified} identified.<br>
      <a href="${base}/report.html" target="_blank">Open the report</a> &middot; <a href="${base}/claim_packet.json" target="_blank">claim_packet.json</a></div>`;
  }
};

const capture = async (track, seconds) => {
  const grab = "ImageCapture" in window ? new ImageCapture(track) : null;
  timer = setInterval(async () => {
    if (socket.bufferedAmount > 0) return;
    try {
      const bitmap = grab ? await grab.grabFrame() : await createImageBitmap($("#view"));
      const at = heading;
      const data = await jpeg(bitmap);
      socket.send(JSON.stringify({ type: "heading", value: at }));
      send(FRAME, data);
      sent++;
      status(`Sweeping ${Math.round((Date.now() - startedAt) / 1000)} s, ${sent} frames`);
    } catch (e) {
      status(`Camera: ${e.message}`);
    }
  }, seconds * 1000);
};

$("#start").onclick = async () => {
  $("#start").disabled = true;
  const config = await (await fetch("api/client")).json();
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      video: { facingMode: { ideal: "environment" }, width: { ideal: 3840 }, height: { ideal: 2160 } },
    });
  } catch (e) {
    status(`Camera or microphone refused: ${e.message}`);
    $("#start").disabled = false;
    return;
  }
  $("#view").srcObject = stream;
  window.addEventListener("deviceorientationabsolute", turned);
  window.addEventListener("deviceorientation", turned);
  micContext = new AudioContext({ sampleRate: 16000 });
  speakerContext = new AudioContext({ sampleRate: 24000 });
  await micContext.audioWorklet.addModule("audio.js");
  await speakerContext.audioWorklet.addModule("audio.js");
  const mic = new AudioWorkletNode(micContext, "mic");
  micContext.createMediaStreamSource(stream).connect(mic);
  mic.port.onmessage = (e) => send(AUDIO, e.data);
  speaker = new AudioWorkletNode(speakerContext, "speaker");
  speaker.connect(speakerContext.destination);

  const url = new URL("ws/sweep", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  socket.onmessage = onMessage;
  socket.onclose = () => { clearInterval(timer); if (!$("#result").innerHTML) status("Disconnected"); };
  socket.onopen = () => {
    socket.send(JSON.stringify({ type: "hello", device: navigator.userAgent }));
    startedAt = Date.now();
    status(`Pricing in ${config.country}, ${config.currency}`);
    capture(stream.getVideoTracks()[0], config.frame_seconds);
    $("#finish").disabled = false;
  };
};

$("#finish").onclick = () => {
  socket?.send(JSON.stringify({ type: "finish" }));
  $("#finish").disabled = true;
};
