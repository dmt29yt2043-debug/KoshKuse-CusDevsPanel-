// Дашборд: лента цитат, срезы по СКЮ, панель. Данные — /api/dashboard, подписи — /api/taxonomy.

let T, TODAY, CONVS = [], QUOTES = [], TESTERS = [];
let transcriptHits = null; // id разговоров, где слово нашлось в полном транскрипте
const state = { q: "", sku: null, verdict: null, theme: "", tester: "", period: 0 };

const daysAgo = d => Math.round((TODAY - new Date(String(d).slice(0, 10))) / 864e5);
const vClass = v => "v v-" + (v || "none");
const vLabel = v => (v ? T.verdicts[v] : "без вердикта");
const skuLabel = (s, f) => (s ? T.skus[s] : "не про СКЮ") + (f ? " · " + T.formats[f] : "");
const norm = s => s.toLowerCase().replaceAll("ё", "е");

async function load() {
  const [tax, data] = await Promise.all([api("/api/taxonomy"), api("/api/dashboard")]);
  T = tax;
  TODAY = new Date(data.today);
  CONVS = data.conversations;
  TESTERS = data.testers;
  QUOTES = CONVS.flatMap(c => c.items.flatMap(it => it.quotes.map(q => ({ ...q, conv: c, item: it }))));
  $("nQuotes").textContent = QUOTES.length;
  $("nTesters").textContent = TESTERS.length;
  setupFilters();
  renderFeed(); renderSkus(); renderPanel();
  const open = new URLSearchParams(location.search).get("open"); // ссылка «Разбор →» со страницы загрузки
  if (open) openConv(+open);
}

// ── фильтры
function pills(el, map, key) {
  el.innerHTML = Object.entries(map).map(([k, v]) => `<button class="pill" data-k="${k}" aria-pressed="false">${esc(v)}</button>`).join("");
  el.onclick = e => {
    const b = e.target.closest(".pill"); if (!b) return;
    state[key] = state[key] === b.dataset.k ? null : b.dataset.k; renderFeed();
  };
}
function setupFilters() {
  const used = Object.fromEntries(Object.entries(T.skus).filter(([k]) => QUOTES.some(q => q.item.sku === k)));
  pills($("fSku"), { ...used, none: "не про СКЮ" }, "sku");
  pills($("fVerdict"), T.verdicts, "verdict");
  $("fTheme").innerHTML = `<option value="">Все темы</option>` + Object.entries(T.themes).filter(([k]) => QUOTES.some(q => q.theme === k || q.item.themes.includes(k))).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("fTester").innerHTML = `<option value="">Все тестеры</option>` + [...new Set(CONVS.map(c => c.tester))].sort().map(t => `<option>${esc(t)}</option>`).join("");
}
let searchTimer;
$("q").addEventListener("input", e => {
  state.q = e.target.value.trim();
  renderFeed();
  clearTimeout(searchTimer);
  transcriptHits = null;
  if (state.q.length >= 2) searchTimer = setTimeout(async () => {
    try { transcriptHits = new Set(await api("/api/search?q=" + encodeURIComponent(state.q))); renderFeed(); } catch (e) {}
  }, 300);
});
$("fTheme").addEventListener("change", e => { state.theme = e.target.value; renderFeed(); });
$("fTester").addEventListener("change", e => { state.tester = e.target.value; renderFeed(); });
$("fPeriod").addEventListener("change", e => { state.period = +e.target.value; renderFeed(); });
$("reset").addEventListener("click", () => {
  Object.assign(state, { q: "", sku: null, verdict: null, theme: "", tester: "", period: 0 });
  transcriptHits = null;
  $("q").value = ""; $("fTheme").value = ""; $("fTester").value = ""; $("fPeriod").value = "0"; renderFeed();
});

function highlight(text, needle) {
  if (!needle) return esc(text);
  const i = norm(text).indexOf(norm(needle));
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + needle.length)) + "</mark>" + esc(text.slice(i + needle.length));
}

// ── лента
function renderFeed() {
  document.querySelectorAll("#fSku .pill").forEach(b => b.setAttribute("aria-pressed", b.dataset.k === state.sku));
  document.querySelectorAll("#fVerdict .pill").forEach(b => b.setAttribute("aria-pressed", b.dataset.k === state.verdict));
  const needle = norm(state.q);
  const list = QUOTES.filter(q =>
    (!state.sku || (state.sku === "none" ? q.item.sku === null : q.item.sku === state.sku)) &&
    (!state.verdict || q.item.verdict === state.verdict) &&
    (!state.theme || q.theme === state.theme || q.item.themes.includes(state.theme)) &&
    (!state.tester || q.conv.tester === state.tester) &&
    (!state.period || daysAgo(q.conv.date) <= state.period) &&
    (!needle || norm(q.text).includes(needle))
  ); // свежие сверху — сервер уже отдал по дате
  const convN = new Set(list.map(q => q.conv.id)).size;
  $("feedCount").textContent = `${list.length} ${plural(list.length, "цитата", "цитаты", "цитат")} из ${convN} ${plural(convN, "разговора", "разговоров", "разговоров")}, свежие сверху`;

  // разговоры, где слово есть только в транскрипте, а не в цитатах — тоже показываем
  const shown = new Set(list.map(q => q.conv.id));
  const extra = needle ? CONVS.filter(c => !shown.has(c.id) &&
    (transcriptHits?.has(c.id) || norm(c.summary).includes(needle))) : [];
  $("hits").hidden = !extra.length;
  $("hits").innerHTML = extra.length ? `«${esc(state.q)}» есть в разговорах, но не в цитатах — ещё ${extra.length} ${plural(extra.length, "разговора", "разговоров", "разговоров")}: ` +
    extra.map(c => `<button class="link" data-open="${c.id}">${esc(c.tester)}, ${fmtDate(c.date)}</button>`).join(", ") : "";

  if (!QUOTES.length) {
    $("feedList").innerHTML = `<div class="empty">Цитат пока нет. Загрузите первые записи разговоров — <a href="/upload">на странице загрузки</a>. Разбор появится здесь, когда Мак их обработает.</div>`;
    return;
  }
  $("feedList").innerHTML = list.length ? list.map(q => `
    <article class="q">
      <blockquote>${highlight(q.text, state.q)}</blockquote>
      <div class="meta"><span class="who">${esc(q.conv.tester)}</span><span>· ${fmtDate(q.conv.date)} · ${esc(T.channels[q.conv.channel] || "")}</span></div>
      <div class="meta">
        <span class="tag">${esc(skuLabel(q.item.sku, q.item.format))}</span>
        <span class="${vClass(q.item.verdict)}">${esc(vLabel(q.item.verdict))}</span>
        ${q.theme ? `<span class="tag">${esc(T.themes[q.theme] || q.theme)}</span>` : ""}
      </div>
      <div class="actions">
        <button class="link" data-copy="${QUOTES.indexOf(q)}">Скопировать с подписью</button>
        <button class="link" data-open="${q.conv.id}" data-quote="${QUOTES.indexOf(q)}">Весь разговор →</button>
      </div>
    </article>`).join("") : `<div class="empty">Под эти фильтры цитат нет. Снимите один из фильтров или нажмите «Сбросить фильтры».</div>`;
}

// ── копирование, разговор, переход из среза
document.addEventListener("click", e => {
  const c = e.target.closest("[data-copy]");
  if (c) {
    const q = QUOTES[+c.dataset.copy];
    const text = `«${q.text}» — ${q.conv.tester}, ${skuLabel(q.item.sku)}, ${fmtDate(q.conv.date)}`;
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
      .then(() => toast("Цитата скопирована"), () => toast("Не получилось скопировать — выделите текст цитаты"));
  }
  const o = e.target.closest("[data-open]");
  if (o) openConv(+o.dataset.open, o.dataset.quote ? QUOTES[+o.dataset.quote].text : state.q);
  const s = e.target.closest("[data-sku]");
  if (s) { Object.assign(state, { sku: s.dataset.sku, verdict: null }); show("feed"); renderFeed(); }
});

async function openConv(id, mark) {
  $("dlgBody").innerHTML = `<p>Загружаю разговор…</p>`;
  $("dlg").showModal();
  let c;
  try { c = await api(`/api/conversations/${id}`); }
  catch (e) { $("dlgBody").innerHTML = `<p class="err">Не удалось открыть разговор: ${esc(e.message)}</p>`; return; }
  $("dlgBody").innerHTML = `
    <span class="eyebrow">${fmtDate(c.date)} · ${esc(T.channels[c.channel] || "")}${c.duration_sec ? " · " + fmtDur(c.duration_sec) : ""}</span>
    <h2>${esc(c.tester)}</h2>
    <p>${esc(c.summary)}</p>
    ${c.items.map(it => `<div class="item">
      <div class="meta"><span class="tag">${esc(skuLabel(it.sku, it.format))}</span><span class="${vClass(it.verdict)}">${esc(vLabel(it.verdict))}</span>${it.themes.map(t => `<span class="tag">${esc(T.themes[t] || t)}</span>`).join("")}</div>
      ${it.quotes.map(q => `<blockquote>«${esc(q.text)}»</blockquote>`).join("")}
    </div>`).join("")}
    ${c.doc ? `<a href="${esc(c.doc)}" target="_blank" rel="noopener">Документ в Google Drive →</a>` : ""}
    <details ${mark ? "open" : ""}><summary>Полный транскрипт${c.speakers === "several" ? " (говорят двое — разметки спикеров нет)" : ""}</summary>
      <div class="transcript" id="tr">${highlight(c.transcript, mark)}</div></details>
    <button class="pill close" id="dlgClose">Закрыть</button>`;
  $("dlgClose").onclick = () => $("dlg").close();
  $("tr")?.querySelector("mark")?.scrollIntoView({ block: "center" });
}
$("dlg").addEventListener("click", e => { if (e.target === $("dlg")) $("dlg").close(); });

// ── срезы по СКЮ
function renderSkus() {
  const items = CONVS.flatMap(c => c.items.map(it => ({ ...it, conv: c })));
  const order = ["liked", "mixed", "disliked", null];
  $("skuList").innerHTML = Object.entries(T.skus).filter(([k]) => k !== "other").map(([k, label]) => {
    const its = items.filter(i => i.sku === k);
    if (!its.length) return `<article class="sku"><h3>${esc(label)} <small>пока ни одного разговора</small></h3></article>`;
    const people = new Set(its.map(i => i.conv.tester)).size;
    const counts = order.map(v => its.filter(i => i.verdict === v).length);
    const themes = {};
    its.forEach(i => i.themes.forEach(t => (themes[t] = (themes[t] || 0) + 1)));
    const top = Object.entries(themes).sort((a, b) => b[1] - a[1]).slice(0, 4);
    const max = top.length ? top[0][1] : 1;
    const latest = its.find(i => i.quotes.length);
    return `<article class="sku">
      <h3>${esc(label)} <small>${its.length} ${plural(its.length, "отзыв", "отзыва", "отзывов")} · ${people} ${plural(people, "человек", "человека", "человек")}</small></h3>
      <div class="bar" role="img" aria-label="${order.map((v, i) => `${vLabel(v)}: ${counts[i]}`).join(", ")}">
        ${order.map((v, i) => (counts[i] ? `<div class="b-${v || "none"}" style="flex:${counts[i]}">${counts[i]}</div>` : "")).join("")}
      </div>
      <div class="legend">${order.map((v, i) => (counts[i] ? `<span><i class="b-${v || "none"}"></i>${esc(vLabel(v))} ${counts[i]}</span>` : "")).join("")}</div>
      <div class="themes">${top.map(([t, n]) => `<div class="th-row"><span>${esc(T.themes[t] || t)}</span><div class="track"><div style="width:${(n / max) * 100}%"></div></div><b>${n}</b></div>`).join("")}</div>
      ${latest ? `<div class="last">«${esc(latest.quotes[0].text)}» <span class="meta">— ${esc(latest.conv.tester)}, ${fmtDate(latest.conv.date)}</span></div>` : ""}
      <button class="link" data-sku="${k}">Все цитаты про ${esc(label)} →</button>
    </article>`;
  }).join("");
}

// ── панель
let sortKey = "last";
function renderPanel() {
  const panel = TESTERS.map(name => {
    const cs = CONVS.filter(c => c.tester === name);
    const last = cs.length ? cs[0].date : null; // CONVS отсортированы от свежих
    const skus = [...new Set(cs.flatMap(c => c.items.map(i => i.sku)).filter(Boolean))];
    return { name, n: cs.length, last, ago: last ? daysAgo(last) : Infinity, skus };
  });
  const active30 = panel.filter(p => p.ago <= 30).length;
  const silent = panel.filter(p => p.ago > 14).length;
  $("stats").innerHTML = `
    <div class="stat"><b>${panel.length}</b><span>человек в панели</span></div>
    <div class="stat"><b>${active30}</b><span>дали обратную связь за 30 дней</span></div>
    <div class="stat alert"><b>${silent}</b><span>молчат больше двух недель — пора набрать</span></div>`;
  const rows = panel.slice().sort((a, b) =>
    sortKey === "name" ? a.name.localeCompare(b.name) : sortKey === "n" ? b.n - a.n : b.ago - a.ago);
  $("panelBody").innerHTML = rows.length ? rows.map(p => `<tr>
    <td><b>${esc(p.name)}</b></td>
    <td class="num">${p.n || "—"}</td>
    <td>${p.last === null ? `<span class="ago never">ни разу</span> <span class="call">набрать</span>`
      : `<span class="ago ${p.ago > 14 ? "stale" : ""}">${fmtDate(p.last)} · ${p.ago} ${plural(p.ago, "день", "дня", "дней")} назад</span>${p.ago > 14 ? ` <span class="call">набрать</span>` : ""}`}</td>
    <td>${p.skus.map(s => `<span class="tag">${esc(T.skus[s] || s)}</span>`).join(" ") || "—"}</td>
  </tr>`).join("") : `<tr><td colspan="4">Тестеры появятся после первой загрузки.</td></tr>`;
}
document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => { sortKey = th.dataset.sort; renderPanel(); }));

// ── вкладки
function show(tab) {
  ["feed", "sku", "panel"].forEach(t => { $(t).hidden = t !== tab; $("tab-" + t).setAttribute("aria-selected", t === tab); });
  try { localStorage.setItem("tab", tab); } catch (e) {}
}
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => show(b.dataset.tab)));
let saved = null; try { saved = localStorage.getItem("tab"); } catch (e) {}
show(["feed", "sku", "panel"].includes(saved) ? saved : "feed");

load().catch(e => { $("feedList").innerHTML = `<div class="empty">Не удалось загрузить данные: ${esc(e.message)}</div>`; });
