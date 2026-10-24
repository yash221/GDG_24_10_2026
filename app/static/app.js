const $ = (id) => document.getElementById(id);

const STEP_LABELS = {
  analyze_photo: "Looked at the photo",
  get_photo_location: "Checked the location",
  find_ward_by_coordinates: "Found the ward from GPS",
  find_ward_by_area: "Found the ward from the area name",
  get_department: "Picked the department",
  file_complaint: "Filed the complaint",
  get_complaint_status: "Fetched the status",
  draft_social_post: "Drafted a social post",
  get_my_rewards: "Checked your points",
};

const ISSUE_LABELS = {
  pothole: "Pothole", garbage: "Garbage", streetlight: "Streetlight",
  waterlogging: "Waterlogging", other_civic: "Other civic issue",
};

let sessionId = null;
let deviceLocation = null;
let lastPost = "";
let busy = false;
let selectedFile = null;
let capturedLive = false;
let stream = null;

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}

async function newSession() {
  const r = await fetch("/api/session", { method: "POST" });
  if (!r.ok) throw new Error("Could not start a session");
  sessionId = (await r.json()).session_id;
  storageSet("civic_session", sessionId);
}

async function api(path, options, retried = false) {
  if (!sessionId) await newSession();
  const r = await fetch(path, options(sessionId));
  if (r.status === 401 && !retried) {
    await newSession();
    return api(path, options, true);
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "Please check your input and try again.";
    throw new Error(detail);
  }
  return body;
}

// the agent is told to answer in plain text, but strip stray markdown just in case
function plain(text) {
  return text.replace(/\*\*|__/g, "").replace(/^#+\s*/gm, "").replace(/^\s*[*-]\s+/gm, "• ");
}

function addMessage(text, kind) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "error") el.setAttribute("role", "alert");
  el.textContent = kind === "agent" ? plain(text) : text;
  $("log").appendChild(el);
  $("log").scrollTop = $("log").scrollHeight;
  return el;
}

function addSteps(steps) {
  if (!steps || !steps.length) return;
  const details = document.createElement("details");
  details.className = "steps";
  const summary = document.createElement("summary");
  summary.textContent = `How the agent worked (${steps.length} steps)`;
  const list = document.createElement("ol");
  for (const s of steps) {
    const li = document.createElement("li");
    li.textContent = STEP_LABELS[s.tool] || s.tool;
    list.appendChild(li);
  }
  details.append(summary, list);
  $("log").appendChild(details);
}

function showTrust(t) {
  const trusted = t.trust_level === "trusted";
  $("t-trust").textContent = `${t.trust_score}/100 · ${trusted ? "Trusted" : "Needs verification"}`;
  $("t-trust").classList.toggle("warn", !trusted);
  const list = $("trust-reasons");
  list.replaceChildren();
  for (const r of t.trust_reasons || []) {
    const li = document.createElement("li");
    li.textContent = `${r.text} (${r.points > 0 ? "+" : ""}${r.points})`;
    list.appendChild(li);
  }
  $("trust-details").hidden = !(t.trust_reasons || []).length;
}

function showRewards(r) {
  if (!r) return;
  $("r-total").textContent = r.total;
  const badges = $("r-badges");
  badges.replaceChildren();
  for (const name of r.badges) {
    const li = document.createElement("li");
    li.textContent = `🏅 ${name}`;
    badges.appendChild(li);
  }
}

async function loadLeaders() {
  try {
    const r = await fetch("/api/leaderboard");
    if (!r.ok) return;
    const { leaders } = await r.json();
    const list = $("leaders");
    list.replaceChildren();
    if (!leaders.length) {
      const li = document.createElement("li");
      li.textContent = "No points yet. Be the first!";
      list.appendChild(li);
    }
    for (const l of leaders) {
      const li = document.createElement("li");
      li.textContent = `${l.name}: ${l.points} pts`;
      list.appendChild(li);
    }
  } catch { /* leaderboard is optional */ }
}

function showAward(steps) {
  const filed = (steps || []).find((s) => s.tool === "file_complaint" && s.result && s.result.ticket_id);
  if (!filed) return;
  const { points_awarded: pts, points_reason: why } = filed.result;
  $("r-last").textContent = pts > 0 ? `+${pts} points: ${why}` : `No points this time: ${why}`;
}

function showTicket(t) {
  if (!t) return;
  $("ticket").hidden = false;
  $("t-id").textContent = t.id;
  $("t-issue").textContent = `${ISSUE_LABELS[t.category] || t.category} (${t.severity})`;
  $("t-ward").textContent = `${t.ward}, ${t.zone} zone`;
  $("t-dept").textContent = t.department;
  $("t-status").textContent = t.status;
  $("t-expected").textContent = t.expected_by;
  showTrust(t);
  $("t-note").textContent = t.user_note || "";
  $("t-note").hidden = $("t-note-label").hidden = !t.user_note;
  if (t.social) {
    lastPost = t.social.text;
    $("share-x").href = t.social.x_url;
  }
}

function setBusy(on) {
  busy = on;
  $("submit").disabled = on || !selectedFile;
  for (const b of document.querySelectorAll("#chat-form button, .chip")) b.disabled = on;
}

// labels can be a list: the message moves through them so a slow reply
// from the free Gemini tier doesn't look like the page froze
async function run(labels, request) {
  if (busy) return;
  setBusy(true);
  labels = Array.isArray(labels) ? labels : [labels];
  const thinking = addMessage(labels[0], "thinking");
  let step = 0;
  const ticker = setInterval(() => {
    step = Math.min(step + 1, labels.length - 1);
    thinking.textContent = labels[step];
  }, 6000);
  try {
    const res = await request();
    clearInterval(ticker);
    thinking.remove();
    addSteps(res.steps);
    addMessage(res.reply || "Done.", "agent");
    showTicket(res.ticket);
    showRewards(res.rewards);
    showAward(res.steps);
    loadLeaders();
    const status = res.steps && res.steps.find((s) => s.tool === "get_complaint_status");
    if (status && status.result && status.result.status && status.result.id === $("t-id").textContent) {
      $("t-status").textContent = status.result.status;
    }
  } catch (err) {
    clearInterval(ticker);
    thinking.remove();
    addMessage(err.message, "error");
  } finally {
    setBusy(false);
  }
}

function usePhoto(file, live) {
  selectedFile = file;
  capturedLive = live;
  $("preview").src = URL.createObjectURL(file);
  $("preview").alt = live ? "Photo taken with the camera" : `Selected photo: ${file.name}`;
  $("preview").hidden = false;
  $("photo-hint").hidden = true;
  $("submit").disabled = busy;
}

$("pick-photo").addEventListener("click", () => $("photo").click());
$("photo").addEventListener("change", () => {
  if ($("photo").files[0]) usePhoto($("photo").files[0], false);
});
// phones without live camera access fall back to the system camera app
$("camera-input").addEventListener("change", () => {
  if ($("camera-input").files[0]) usePhoto($("camera-input").files[0], false);
});

function closeCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  $("camera").hidden = true;
  $("take-photo").focus();
}

$("take-photo").addEventListener("click", async () => {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.isSecureContext) {
    $("camera-input").click();
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (err) {
    if (err && err.name === "NotAllowedError") {
      addMessage("Camera permission is blocked. Allow it from the 🔒 icon in the address bar, or upload a photo.", "error");
    } else {
      $("camera-input").click();
    }
    return;
  }
  $("camera-view").srcObject = stream;
  $("camera").hidden = false;
  $("snap").focus();
  // a live photo has no GPS of its own, so grab the device location now
  if (!deviceLocation) $("locate").click();
});

$("snap").addEventListener("click", () => {
  const video = $("camera-view");
  const scale = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    closeCamera();
    if (blob) usePhoto(new File([blob], "camera.jpg", { type: "image/jpeg" }), true);
  }, "image/jpeg", 0.9);
});

$("camera-close").addEventListener("click", closeCamera);
$("camera").addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCamera();
});

const LOCATION_ERRORS = {
  1: "Location permission is blocked. Allow it from the 🔒 icon in the address bar, or type the area.",
  2: "Your device couldn't find its location. Turn on location services, or type the area.",
  3: "Getting your location took too long. Try again, or type the area.",
};

function getPosition(options) {
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, options));
}

$("locate").addEventListener("click", async () => {
  if (!navigator.geolocation || !window.isSecureContext) {
    $("loc-status").textContent = "Location needs a secure (https) page. Type the area instead.";
    return;
  }
  $("loc-status").textContent = "Getting your location...";
  let pos;
  try {
    // precise first (phones with GPS), then a quick network-based fix (laptops)
    pos = await getPosition({ enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
  } catch (err) {
    if (err.code === 1) {
      $("loc-status").textContent = LOCATION_ERRORS[1];
      return;
    }
    try {
      pos = await getPosition({ enableHighAccuracy: false, timeout: 15000, maximumAge: 300000 });
    } catch (err2) {
      $("loc-status").textContent = LOCATION_ERRORS[err2.code] || LOCATION_ERRORS[2];
      return;
    }
  }
  deviceLocation = { lat: pos.coords.latitude.toFixed(6), lng: pos.coords.longitude.toFixed(6) };
  const accuracy = Math.round(pos.coords.accuracy);
  $("loc-status").textContent = `✓ Using your current location (about ${accuracy} m accurate) if the photo has none.`;
});

// hosting limits request bodies to ~4.5 MB, so big phone photos are shrunk
// here first (this drops the photo's GPS, so the 📍 button or area is needed)
const MAX_UPLOAD = 4 * 1024 * 1024;

async function shrink(file) {
  if (file.size <= MAX_UPLOAD) return file;
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
  return new File([blob], "photo.jpg", { type: "image/jpeg" });
}

$("report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let file = selectedFile;
  if (!file) return;
  try {
    file = await shrink(file);
  } catch {
    addMessage("Couldn't read that photo. Please try a JPEG or PNG.", "error");
    return;
  }
  const area = $("area").value.trim();
  const note = $("note").value.trim();
  addMessage(area ? `Reporting an issue near ${area}` : "Reporting an issue", "user");
  run([
    "Looking at the photo...",
    "Checking where this is and finding the AMC ward...",
    "Picking the right department...",
    "Checking the photo is genuine and filing the complaint...",
    "Still working. The AI is busy right now, this can take up to a minute...",
  ], () =>
    api("/api/report", (sid) => {
      const fd = new FormData();
      fd.append("session_id", sid);
      fd.append("photo", file);
      fd.append("area_text", area);
      fd.append("description", note);
      if (capturedLive) fd.append("captured_live", "true");
      if (deviceLocation) {
        fd.append("lat", deviceLocation.lat);
        fd.append("lng", deviceLocation.lng);
      }
      return { method: "POST", body: fd };
    }),
  );
});

function sendChat(text) {
  text = text.trim();
  if (!text) return;
  addMessage(text, "user");
  run("Thinking...", () =>
    api("/api/chat", (sid) => ({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid, message: text }),
    })),
  );
}

$("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  sendChat($("message").value);
  $("message").value = "";
});

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => sendChat(chip.dataset.msg));
}

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = "Copied ✓";
    setTimeout(() => { button.textContent = old; }, 1500);
  } catch {
    addMessage(text, "agent");
  }
}

$("copy-id").addEventListener("click", (e) => copy($("t-id").textContent, e.currentTarget));
$("copy-post").addEventListener("click", (e) => copy(lastPost, e.currentTarget));

sessionId = storageGet("civic_session");
loadLeaders();
if (sessionId) {
  fetch("/api/rewards", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).then((r) => (r.ok ? r.json() : null)).then(showRewards).catch(() => {});
}
