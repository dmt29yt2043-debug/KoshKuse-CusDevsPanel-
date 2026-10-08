// Общее для всех страниц: статус трубы в шапке, тост, мелкие форматтеры.

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
const plural = (n, a, b, c) => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c; };
const fmtDate = d => new Date(d).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
const fmtDur = s => s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 2200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.reload(); throw new Error("нет доступа"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

function agoText(iso) {
  const min = Math.round((Date.now() - new Date(iso)) / 60000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} ч назад`;
  return fmtDate(iso);
}

async function refreshPipe() {
  try {
    const q = await api("/api/queue?limit=100");
    $("agentDot").className = "dot" + (q.agent_online ? "" : " off");
    $("agentText").textContent = q.agent_online
      ? `Мак на связи · ${agoText(q.agent_last_seen)}`
      : q.agent_last_seen ? `Мак не на связи · был ${agoText(q.agent_last_seen)}` : "Мак ещё ни разу не выходил на связь";
    const waiting = q.queued + q.in_work;
    $("queueChip").textContent = `В очереди: ${waiting}` + (q.failed ? ` · ошибок: ${q.failed}` : "");
    document.dispatchEvent(new CustomEvent("queue", { detail: q }));
  } catch (e) { /* шапка — не повод ломать страницу */ }
}
refreshPipe();
setInterval(refreshPipe, 30000);
