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
};

const ISSUE_LABELS = {
  pothole: "Pothole", garbage: "Garbage", streetlight: "Streetlight",
  waterlogging: "Waterlogging", other_civic: "Other civic issue",
};

let sessionId = null;
let deviceLocation = null;
let lastPost = "";
let busy = false;

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

function showTicket(t) {
  if (!t) return;
  $("ticket").hidden = false;
  $("t-id").textContent = t.id;
  $("t-issue").textContent = `${ISSUE_LABELS[t.category] || t.category} (${t.severity})`;
  $("t-ward").textContent = `${t.ward}, ${t.zone} zone`;
  $("t-dept").textContent = t.department;
  $("t-status").textContent = t.status;
  $("t-expected").textContent = t.expected_by;
  if (t.social) {
    lastPost = t.social.text;
    $("share-x").href = t.social.x_url;
  }
}

function setBusy(on) {
  busy = on;
  $("submit").disabled = on || !$("photo").files.length;
  for (const b of document.querySelectorAll("#chat-form button, .chip")) b.disabled = on;
}

async function run(label, request) {
  if (busy) return;
  setBusy(true);
  const thinking = addMessage(label, "thinking");
  try {
    const res = await request();
    thinking.remove();
    addSteps(res.steps);
    addMessage(res.reply || "Done.", "agent");
    showTicket(res.ticket);
    const status = res.steps && res.steps.find((s) => s.tool === "get_complaint_status");
    if (status && status.result && status.result.status && status.result.id === $("t-id").textContent) {
      $("t-status").textContent = status.result.status;
    }
  } catch (err) {
    thinking.remove();
    addMessage(err.message, "error");
  } finally {
    setBusy(false);
  }
}

$("photo").addEventListener("change", () => {
  const file = $("photo").files[0];
  if (!file) return;
  const url = URL.createObjectURL(file);
  $("preview").src = url;
  $("preview").alt = `Selected photo: ${file.name}`;
  $("preview").hidden = false;
  $("photo-hint").hidden = true;
  $("submit").disabled = busy;
});

$("locate").addEventListener("click", () => {
  if (!navigator.geolocation) {
    $("loc-status").textContent = "Location isn't available on this device. Type the area instead.";
    return;
  }
  $("loc-status").textContent = "Getting your location...";
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      deviceLocation = { lat: pos.coords.latitude.toFixed(6), lng: pos.coords.longitude.toFixed(6) };
      $("loc-status").textContent = "✓ Using your current location (if the photo has none).";
    },
    () => { $("loc-status").textContent = "Couldn't get your location. Type the area instead."; },
    { enableHighAccuracy: true, timeout: 10000 },
  );
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
  let file = $("photo").files[0];
  if (!file) return;
  try {
    file = await shrink(file);
  } catch {
    addMessage("Couldn't read that photo. Please try a JPEG or PNG.", "error");
    return;
  }
  const area = $("area").value.trim();
  addMessage(area ? `Reporting an issue near ${area}` : "Reporting an issue", "user");
  run("Checking the photo and finding the right department...", () =>
    api("/api/report", (sid) => {
      const fd = new FormData();
      fd.append("session_id", sid);
      fd.append("photo", file);
      fd.append("area_text", area);
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
