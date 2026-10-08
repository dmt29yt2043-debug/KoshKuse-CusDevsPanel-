// Страница загрузки: файлы/скриншоты или текст → /api/upload, /api/text; очередь из /api/queue.

const AUDIO = /\.(m4a|mp3|wav|ogg|opus|oga|aac|mp4)$/i;
const VOICE = /\.(ogg|opus|oga)$/i; // голосовые из Telegram
const ACCEPT = /\.(m4a|mp3|wav|ogg|opus|oga|aac|mp4|png|jpe?g|webp|heic|txt)$/i;
const STATUS = { queued: "ждёт Мак", transcribing: "распознаётся", parsing: "разбирается", done: "готово", failed: "ошибка" };
const KIND = { audio: "запись", text: "переписка", image: "скриншот" };

let T;
let mode = "files";
let picked = []; // {file, date}
let channelTouched = false;

// datetime-local хочет местное время без пояса
const toLocalInput = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

async function init() {
  T = await api("/api/taxonomy");
  $("sku").insertAdjacentHTML("beforeend", Object.entries(T.skus).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join(""));
  $("channel").innerHTML = Object.entries(T.channels).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("channel").value = T.default_channel;
  const testers = await api("/api/testers");
  $("testerList").innerHTML = testers.map(t => `<option value="${esc(t)}">`).join("");
  renderQueue();
}

// ── режим
function setMode(m) {
  mode = m;
  $("modeFiles").setAttribute("aria-pressed", m === "files");
  $("modeText").setAttribute("aria-pressed", m === "text");
  $("filesPane").hidden = m !== "files";
  $("textPane").hidden = m !== "text";
  $("hint").textContent = m === "files" ? "Дата разговора берётся из даты файла — поправьте, если нужно." : "Переписка идёт в разбор, минуя распознавание: ждёт только Мак, не MacWhisper.";
  if (!channelTouched) $("channel").value = m === "text" ? "chat" : guessChannel();
  updateSend();
}
$("modeFiles").onclick = () => setMode("files");
$("modeText").onclick = () => setMode("text");
$("channel").addEventListener("change", () => (channelTouched = true));

function guessChannel() {
  if (picked.length && picked.every(p => VOICE.test(p.file.name))) return "voice";
  if (picked.length && picked.every(p => !AUDIO.test(p.file.name))) return "chat";
  return T?.default_channel || "call";
}

// ── выбор файлов
function addFiles(list) {
  const bad = [];
  for (const file of list) {
    if (!ACCEPT.test(file.name)) { bad.push(file.name); continue; }
    if (picked.some(p => p.file.name === file.name && p.file.size === file.size)) continue;
    picked.push({ file, date: toLocalInput(new Date(file.lastModified || Date.now())) });
  }
  if (bad.length) toast(`Не принимаем: ${bad.join(", ")}`);
  if (!channelTouched) $("channel").value = guessChannel();
  renderPicked();
}
function renderPicked() {
  $("files").innerHTML = picked.map((p, i) => `
    <div class="file">
      <span class="name" title="${esc(p.file.name)}">${esc(p.file.name)} <span class="note">· ${(p.file.size / 1048576).toFixed(1)} МБ</span></span>
      <input type="datetime-local" value="${p.date}" data-i="${i}" aria-label="Дата разговора для ${esc(p.file.name)}">
      <button class="x" data-rm="${i}" aria-label="Убрать ${esc(p.file.name)}">×</button>
    </div>`).join("");
  updateSend();
}
$("files").addEventListener("input", e => { if (e.target.dataset.i) picked[+e.target.dataset.i].date = e.target.value; });
$("files").addEventListener("click", e => { const b = e.target.closest("[data-rm]"); if (b) { picked.splice(+b.dataset.rm, 1); renderPicked(); } });
$("fileInput").addEventListener("change", e => { addFiles(e.target.files); e.target.value = ""; });
const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => addFiles(e.dataTransfer.files));
document.addEventListener("paste", e => {
  if (mode !== "files") return;
  const imgs = [...e.clipboardData.files].filter(f => f.type.startsWith("image/"));
  if (!imgs.length) return;
  e.preventDefault();
  const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
  addFiles(imgs.map((f, i) => new File([f], `скриншот-${stamp}-${i + 1}.${f.type.split("/")[1] || "png"}`, { type: f.type, lastModified: Date.now() })));
});

// ── отправка
$("tester").addEventListener("input", updateSend);
$("text").addEventListener("input", updateSend);
function updateSend() {
  const ready = $("tester").value.trim() && (mode === "files" ? picked.length : $("text").value.trim());
  $("send").disabled = !ready;
}
const withOffset = local => (local ? new Date(local).toISOString() : null);

$("send").addEventListener("click", async () => {
  const common = { tester: $("tester").value.trim(), sku_hint: $("sku").value, channel: $("channel").value, note: $("note").value.trim() };
  $("send").disabled = true;
  $("result").innerHTML = "";
  const report = (name, r) => {
    const cls = r.result === "error" ? "msg bad" : "msg";
    const text = r.result === "added" ? "в очереди" : r.result === "duplicate" ? "этот файл уже есть — повторно не обрабатываем" : r.message;
    $("result").insertAdjacentHTML("beforeend", `<div class="${cls}">${esc(name)}: ${esc(text)}</div>`);
  };
  try {
    if (mode === "text") {
      const r = await api("/api/text", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...common, text: $("text").value }) });
      report("Переписка", r);
      if (r.result !== "error") $("text").value = "";
    } else {
      // по одному запросу на файл: у каждого своя дата, и одна битая запись не валит остальные
      for (const p of picked.slice()) {
        const fd = new FormData();
        Object.entries(common).forEach(([k, v]) => v && fd.append(k, v));
        fd.append("talked_at", withOffset(p.date) || "");
        fd.append("files", p.file);
        try {
          const [r] = await api("/api/upload", { method: "POST", body: fd });
          report(p.file.name, r);
          if (r.result !== "error") picked = picked.filter(x => x !== p);
        } catch (e) { report(p.file.name, { result: "error", message: e.message }); }
      }
      renderPicked();
    }
    refreshPipe();
  } catch (e) {
    $("result").innerHTML = `<div class="msg bad">Не отправилось: ${esc(e.message)}</div>`;
  }
  updateSend();
});

// ── очередь
let lastQueue = null;
document.addEventListener("queue", e => { lastQueue = e.detail; renderQueue(); });
function renderQueue() {
  const q = lastQueue;
  if (!q || !T) return;
  $("queueNote").textContent = q.agent_online
    ? "Мак на связи — записи разбираются по одной, в порядке загрузки."
    : "Мак сейчас не на связи. Это нормально: файлы ждут в очереди и разберутся, когда он включится. Заливать заново не нужно.";
  $("queueBody").innerHTML = q.items.length ? q.items.map(it => `
    <tr class="${it.overdue ? "overdue" : ""}">
      <td>${fmtDate(it.created_at)} ${new Date(it.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</td>
      <td>${esc(it.tester)}</td>
      <td>${esc(KIND[it.kind])}${it.filename ? `<br><span class="note">${esc(it.filename)}</span>` : ""}</td>
      <td class="num">${fmtDur(it.duration_sec)}</td>
      <td><span class="st st-${it.status}">${STATUS[it.status] || it.status}</span>${it.overdue ? `<br><span class="note">ждёт больше суток</span>` : ""}${it.error ? `<div class="err">${esc(it.error)}</div>` : ""}</td>
      <td>${it.status === "done" ? `<a href="/?open=${it.id}">Разбор →</a>` : it.status === "failed" ? `<button class="link" data-retry="${it.id}">Повторить</button>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="6">Пока ничего не загружено.</td></tr>`;
}
$("queueBody").addEventListener("click", async e => {
  const b = e.target.closest("[data-retry]"); if (!b) return;
  try { await api(`/api/conversations/${b.dataset.retry}/retry`, { method: "POST" }); toast("Вернул в очередь"); refreshPipe(); }
  catch (err) { toast(err.message); }
});

init().catch(e => toast("Не загрузилось: " + e.message));
