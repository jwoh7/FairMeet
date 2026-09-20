/* FairMeet 화면. 서버가 준 문자열은 항상 텍스트 노드로만 넣는다 (HTML 문자열을 만들어 넣지 않는다).
   상태 갱신은 폴링이다: Cloudflare Quick Tunnel 은 SSE 를 지원하지 않으므로 이 화면은 SSE 에 의존하지 않는다. */
"use strict";

const S = {
  me: null, csrf: null, demo: false, route: {name: "home"},
  meetings: [], filter: "all", detail: null, draft: null, result: null,
  idem: {text: "", key: ""}, timer: null, matStatus: null, busy: false, confirming: false,
};
const $ = s => document.querySelector(s);
const app = $("#app");

// ── 화면 표시 선호만 localStorage 에 저장한다 (인증 정보는 저장하지 않는다) ──
const PREF_KEY = "fm.pref";
function loadPref() { try { return JSON.parse(localStorage.getItem(PREF_KEY) || "{}") || {}; } catch (e) { return {}; } }
function savePref(p) { try { localStorage.setItem(PREF_KEY, JSON.stringify(Object.assign(loadPref(), p))); } catch (e) { /* 저장 실패는 무시 */ } }
S.filter = ["all", "progress", "waiting", "confirmed", "failed", "cancelled"].includes(loadPref().filter) ? loadPref().filter : "all";

// ── DOM 도우미 ──
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function mark() {
  const s = document.createElementNS(svgNS, "svg");
  s.setAttribute("viewBox", "0 0 32 32"); s.setAttribute("aria-hidden", "true");
  [["12", "#0f766e", ".92"], ["20", "#5eead4", ".85"]].forEach(([cx, fill, op]) => {
    const c = document.createElementNS(svgNS, "circle");
    c.setAttribute("cx", cx); c.setAttribute("cy", "16"); c.setAttribute("r", "9");
    c.setAttribute("fill", fill); c.setAttribute("opacity", op); s.append(c);
  });
  return s;
}
function say(msg) { $("#live").textContent = ""; setTimeout(() => { $("#live").textContent = msg; }, 30); }
function toast(msg) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const t = h("div", {class: "toast", role: "status"}, msg);
  document.body.append(t); say(msg);
  setTimeout(() => t.remove(), 4200);
}
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return "";
  return d.toLocaleString("ko-KR", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
}
const GROUP_CLASS = {progress: "", waiting: "warn", confirmed: "ok", failed: "bad", cancelled: "mute"};
const badge = (group, label) => h("span", {class: "badge " + (GROUP_CLASS[group] || "")}, label);

// ── API ──
async function api(path, opt = {}) {
  const headers = {};
  let body;
  const method = opt.method || (opt.json !== undefined || opt.form ? "POST" : "GET");
  if (method !== "GET" && S.csrf) headers["X-CSRF-Token"] = S.csrf;
  if (opt.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opt.json); }
  if (opt.form) body = opt.form;
  let r;
  try { r = await fetch(path, {method, headers, body, credentials: "same-origin"}); }
  catch (e) { return {ok: false, status: 0, data: {detail: "네트워크 연결을 확인해 주세요."}}; }
  let data = {}; try { data = await r.json(); } catch (e) { /* 본문 없음 */ }
  if (r.status === 401) { S.me = {authenticated: false, loginRequired: true}; stopPoll(); render(); }
  return {ok: r.ok, status: r.status, data};
}

// ── 라우팅 ──
function parseRoute() {
  const m = location.hash.match(/^#\/meeting\/([0-9a-f]{32})$/);
  if (m) return {name: "meeting", id: m[1]};
  if (location.hash === "#/meetings") return {name: "list"};
  return {name: "home"};
}
window.addEventListener("hashchange", () => { S.route = parseRoute(); S.detail = null; S.lastJson = null; render(); });

function stopPoll() { clearTimeout(S.timer); S.timer = null; }
function schedulePoll(ms) {
  stopPoll();
  S.timer = setTimeout(async () => { if (!document.hidden) await refreshData(); schedulePoll(ms); }, ms);
}

// ── 부팅 ──
async function restoreDraft() {
  // 새로고침 뒤에도 편집 중이던 초안(과 그 버튼 상태)을 서버 기준으로 되살린다. 실패하면 조용히 넘어간다
  // (사용자는 새로 요청하면 된다) — 화면에 오류를 띄우지 않는다.
  try {
    const r = await api("/api/requests/current");
    if (r.ok && r.data.draft) {
      S.draft = r.data.draft;
      S.result = {kind: "draft", message: "이어서 확인해 주세요.", refusals: [], notes: []};
    }
  } catch (e) { /* 무시 */ }
}

async function boot() {
  const r = await api("/api/me");
  S.me = r.data; S.csrf = r.data.csrfToken || null; S.demo = !!r.data.demoMode;
  if (r.data.authenticated !== false) await restoreDraft();
  S.route = parseRoute();
  render();
}

// ── 공통 틀 ──
function shell(content) {
  const me = S.me || {};
  const nav = h("nav", {class: "nav", "aria-label": "주 메뉴"},
    h("a", {href: "#/", "aria-current": S.route.name === "home" ? "page" : null}, "홈"),
    h("a", {href: "#/meetings", "aria-current": S.route.name === "list" ? "page" : null}, "회의 목록"));
  const right = h("div", {class: "row"});
  if (S.demo) right.append(h("span", {class: "demo-tag", role: "status"}, "시연 모드"));
  if (me.demoAvailable) {
    const cb = h("input", {type: "checkbox", id: "demo-toggle"});
    cb.checked = S.demo;
    cb.addEventListener("change", toggleDemo);
    right.append(h("label", {class: "row note", for: "demo-toggle"}, cb, " 시연 모드"));
  }
  right.append(h("span", {class: "note"}, me.user || ""));
  if (me.mode === "admin") right.append(h("button", {class: "btn btn-sm btn-quiet", onclick: logout}, "로그아웃"));
  const bar = h("header", {class: "topbar"}, h("div", {class: "topbar-in"},
    h("a", {class: "brand", href: "#/"}, mark(), "FairMeet"), nav, h("div", {class: "spacer"}), right));
  return [bar, h("main", {id: "main", class: "page", tabindex: "-1"}, content)];
}

function render() {
  stopPoll();
  if (!S.me) { app.replaceChildren(h("main", {class: "page"}, h("div", {class: "empty"}, "불러오는 중…"))); return; }
  if (!S.me.authenticated) { renderLogin(); return; }
  const r = S.route;
  if (r.name === "meeting") renderMeeting();
  else if (r.name === "list") renderList();
  else renderHome();
  const main = $("#main"); if (main) main.focus({preventScroll: true});
}

// ── 로그인 ──
function renderLogin(msg) {
  const user = h("input", {class: "field", id: "lu", autocomplete: "username", required: true});
  const pw = h("input", {class: "field", id: "lp", type: "password", autocomplete: "current-password", required: true});
  const err = h("div", {class: "alert bad", role: "alert", hidden: !msg}, msg || "");
  const btn = h("button", {class: "btn btn-primary btn-lg", type: "submit"}, "로그인");
  const form = h("form", {class: "card stack login"},
    h("div", {class: "row"}, mark(), h("strong", {}, "FairMeet 관리자 로그인")),
    h("div", {}, h("label", {class: "lbl", for: "lu"}, "아이디"), user),
    h("div", {}, h("label", {class: "lbl", for: "lp"}, "비밀번호"), pw),
    err, btn);
  form.addEventListener("submit", async ev => {
    ev.preventDefault(); btn.disabled = true;
    try {
      const r = await fetch("/api/login", {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-FairMeet": "1"},
        body: JSON.stringify({username: user.value, password: pw.value})});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { pw.value = ""; err.hidden = false; err.textContent = d.detail || "로그인하지 못했어요."; btn.disabled = false; pw.focus(); return; }
      S.me = d; S.csrf = d.csrfToken || null; S.demo = !!d.demoMode; render();
    } catch (e) { err.hidden = false; err.textContent = "네트워크 연결을 확인해 주세요."; btn.disabled = false; }
  });
  app.replaceChildren(h("main", {class: "page", id: "main"}, form));
  user.focus();
}
async function logout() {
  await api("/api/logout", {method: "POST"});
  S.me = {authenticated: false, loginRequired: true}; S.csrf = null; S.demo = false;
  S.meetings = []; S.detail = null; S.result = null; S.draft = null;
  render();
}
async function toggleDemo(ev) {
  const on = ev.target.checked;
  const r = await api("/api/demo-mode", {json: {enabled: on}});
  if (!r.ok) { ev.target.checked = !on; toast(r.data.detail || "바꾸지 못했어요."); return; }
  S.demo = r.data.demoMode; render();
}

// ── 홈 ──
const EXAMPLES = [
  "다음 주 화요일부터 목요일 사이에 재원, 도연, 승현과 90분 회의를 잡아줘. 재원은 필수이고 재원 포함 2명 이상 동의하면 돼. 오후 시간을 우선해줘.",
  "내일 오후 2시 이후에 1시간짜리 주간 점검 회의를 잡아줘.",
  "다음 주 월요일부터 수요일 사이에 2시간 워크숍 시간을 찾아줘. 가능하면 오전으로.",
];

function renderHome() {
  const ta = h("textarea", {class: "field", id: "req", rows: "3", "aria-describedby": "req-hint",
    placeholder: "예) 다음 주 화요일 오후에 재원, 도연과 90분 회의를 잡아줘", maxlength: "1000"});
  ta.value = S.idem.text || "";
  ta.addEventListener("input", () => { S.idem.text = ta.value; });
  ta.addEventListener("keydown", ev => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); submitRequest(); }
  });
  const go = h("button", {class: "btn btn-primary btn-lg", id: "go", onclick: submitRequest}, "회의 요청하기");
  const chips = h("div", {class: "chips", role: "group", "aria-label": "추천 요청 예시"},
    EXAMPLES.map(t => h("button", {class: "chip", type: "button", onclick: () => { ta.value = t; S.idem.text = t; ta.focus(); }},
      t.length > 34 ? t.slice(0, 34) + "…" : t)));
  const hero = h("section", {class: "hero"},
    h("h1", {}, "어떤 회의를 잡을까요?"),
    h("p", {}, "날짜, 참석자, 시간 조건을 편하게 적어 주세요. 조건이 명확하면 바로 참가자에게 물어봐요."),
    h("div", {class: "card composer"}, h("label", {class: "sr-only", for: "req"}, "회의 요청 내용"), ta,
      h("div", {class: "row between"}, h("span", {class: "note", id: "req-hint"}, "Enter로 요청 · Shift+Enter로 줄바꿈"), go),
      chips));
  const result = h("div", {id: "result", class: "stack"});
  const left = h("div", {class: "stack-lg"}, h("section", {class: "card", "aria-labelledby": "t-active"},
    h("h2", {id: "t-active"}, "진행 중인 회의"), h("div", {id: "active"}, skeleton())));
  const right = h("section", {class: "card", "aria-labelledby": "t-recent"},
    h("div", {class: "row between"}, h("h2", {id: "t-recent"}, "최근 회의"),
      h("a", {href: "#/meetings", class: "note"}, "전체 보기")), h("div", {id: "recent"}, skeleton()));
  app.replaceChildren(...shell([hero, result, h("div", {class: "cols"}, left, right)]));
  renderResult();
  refreshData();
  schedulePoll(5000);
}
const skeleton = () => h("div", {class: "stack"}, h("div", {class: "skeleton"}), h("div", {class: "skeleton"}));

function meetingRow(m) {
  return h("a", {class: "listrow", href: "#/meeting/" + m.id},
    h("div", {class: "grow"}, h("div", {class: "lr-title"}, m.title),
      h("div", {class: "lr-sub"}, m.slot ? m.slot : m.headline)),
    h("div", {class: "stack"}, badge(m.group, m.groupLabel), h("div", {class: "lr-sub"}, when(m.createdAt))));
}
function listInto(box, items, emptyText) {
  if (!box) return;
  box.replaceChildren(items.length ? h("div", {}, items.map(meetingRow)) : h("p", {class: "empty"}, emptyText));
}

async function refreshData() {
  const r = S.route;
  if (r.name === "home" || r.name === "list") {
    const g = r.name === "list" ? S.filter : "all";
    const res = await api("/api/meetings?group=" + g);
    if (!res.ok) return;
    S.meetings = res.data.meetings || [];
    if (r.name === "home") {
      listInto($("#active"), S.meetings.filter(m => m.group === "progress" || m.group === "waiting"),
        "진행 중인 회의가 없어요. 위에서 새 회의를 요청해 보세요.");
      listInto($("#recent"), S.meetings.slice(0, 6), "아직 회의가 없어요.");
    } else listInto($("#list"), S.meetings, "이 조건의 회의가 없어요.");
  } else if (r.name === "meeting") {
    const res = await api("/api/meetings/" + r.id);
    if (!res.ok) { S.detail = null; if (res.status === 404) renderMissing(); return; }
    const sig = JSON.stringify(res.data);
    if (sig === S.lastJson && $("#detail .cols-detail")) return;          // 바뀐 게 없으면 다시 그리지 않는다 (입력 중인 것을 지우지 않는다)
    S.lastJson = sig; S.detail = res.data;
    const keep = window.scrollY;
    renderMeetingBody();
    window.scrollTo(0, keep);
  }
}

// ── 회의 요청 ──
function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  return Array.from({length: 32}, () => Math.floor(Math.random() * 16).toString(16)).join("");
}
async function submitRequest() {
  const text = ($("#req").value || "").trim();
  if (!text) { toast("회의 요청 내용을 입력해 주세요."); $("#req").focus(); return; }
  if (S.busy) return;
  S.busy = true; const go = $("#go"); go.disabled = true; go.textContent = "요청하는 중…";
  // 네트워크 재시도는 같은 키를 쓰므로 세션이 두 번 만들어지지 않는다. 내용이 바뀌면 새 키.
  if (S.idem.sentText !== text || !S.idem.key) { S.idem.key = newKey(); S.idem.sentText = text; }
  const r = await api("/api/requests", {json: {text, idempotencyKey: S.idem.key}});
  S.busy = false; go.disabled = false; go.textContent = "회의 요청하기";
  if (r.status === 0) { toast(r.data.detail); return; }          // 연결 오류: 같은 키로 다시 시도할 수 있다
  handleRequestResult(r);
}
function handleRequestResult(r) {
  const d = r.data;
  if (!r.ok && !d.status) { S.result = {kind: "error", message: d.detail || "요청을 처리하지 못했어요."}; renderResult(); return; }
  if (d.status === "started") {
    S.idem = {text: "", key: "", sentText: ""}; S.result = null; S.draft = null;   // 입력 UI 만 초기화한다
    toast(d.message || "회의를 요청했어요.");
    location.hash = "#/meeting/" + d.meetingId;
    return;
  }
  if (d.status === "draft") { S.draft = d.draft; S.result = {kind: "draft", message: d.message, refusals: d.refusals, notes: d.notes}; }
  else if (d.status === "refused" || d.status === "question") S.result = {kind: d.status, message: d.message, alternative: d.alternative, refusals: d.refusals, notes: d.notes};
  else if (d.status === "rate_limited") S.result = {kind: "error", message: d.detail};
  else S.result = {kind: "error", message: d.detail || "요청을 처리하지 못했어요."};
  renderResult();
}
function renderResult() {
  const box = $("#result"); if (!box) return;
  const r = S.result; if (!r) { box.replaceChildren(); return; }
  const parts = [];
  if (r.kind === "refused" || r.kind === "question" || r.kind === "error") {
    parts.push(h("div", {class: "alert " + (r.kind === "question" ? "" : "warn"), role: "alert"},
      h("strong", {}, r.message), r.alternative ? h("div", {}, r.alternative) : null));
  }
  (r.refusals || []).filter(x => !(r.kind === "refused" && x === r.refusals[0])).forEach(x =>
    parts.push(h("div", {class: "alert warn"}, h("strong", {}, x.message), h("div", {}, x.alternative))));
  (r.notes || []).forEach(n => parts.push(h("div", {class: "alert"}, n)));
  if (r.kind === "draft" && S.draft) parts.push(draftCard(S.draft, r.message));
  box.replaceChildren(...parts);
  const first = box.querySelector("[role=alert], .draft-card");
  if (first) say(r.message || "확인이 필요해요");
}

function draftCard(d, message) {
  const s = d.summary;
  const kv = h("dl", {class: "kv"},
    h("dt", {}, "기간"), h("dd", {}, s.period || "정해지지 않았어요"),
    h("dt", {}, "회의 시간"), h("dd", {}, s.durationMin + "분"),
    h("dt", {}, "참가자"), h("dd", {}, (s.participants || []).join(", ")),
    h("dt", {}, "승인 기준"), h("dd", {}, s.approval),
    s.required && s.required.length ? [h("dt", {}, "필수 참석자"), h("dd", {}, s.required.join(", "))] : null,
    h("dt", {}, "후보 시간"), h("dd", {}, "캘린더 확인 전 " + s.candidates + "개"));
  const card = h("section", {class: "card draft-card stack", "aria-labelledby": "dc"},
    h("h2", {id: "dc"}, "이렇게 이해했어요"), message ? h("p", {class: "note"}, message) : null, kv);

  d.issues.forEach(i => card.append(h("div", {class: "alert " + (i.kind === "conflict" ? "bad" : "warn"), role: "alert"}, i.message)));
  d.questions.forEach(q => {
    const sel = h("select", {class: "field", id: "q-" + q.id, "aria-label": q.text},
      h("option", {value: ""}, "선택해 주세요"),
      q.options.map(o => h("option", {value: String(o.value), selected: q.answer != null && String(q.answer) === String(o.value)}, o.label)));
    sel.addEventListener("change", () => { if (sel.value !== "") resolveDraft({answers: {[q.id]: sel.value}}); });
    card.append(h("div", {class: "draft-block stack"}, h("label", {class: "lbl", for: "q-" + q.id}, q.text), sel));
  });
  if (d.constraints.length) {
    const list = h("div", {class: "stack"});
    d.constraints.forEach(c => {
      const row = h("div", {class: "draft-block stack"},
        h("div", {class: "row"}, h("span", {class: "badge " + (c.hard ? "" : "mute")}, c.strength), h("strong", {}, c.label),
          c.supported ? null : h("span", {class: "badge warn"}, "직접 확인 필요")),
        h("div", {}, c.text), c.source ? h("div", {class: "note"}, "입력하신 표현: “" + c.source + "”") : null);
      const acts = h("div", {class: "row"});
      if (c.canToggle) acts.append(h("button", {class: "btn btn-sm", type: "button",
        onclick: () => resolveDraft({strengths: {[c.id]: c.hard ? "soft" : "hard"}})}, c.hard ? "선호로 바꾸기" : "필수로 바꾸기"));
      if (!c.supported) {
        const cb = h("input", {type: "checkbox", id: "ack-" + c.id}); cb.checked = c.acknowledged;
        cb.addEventListener("change", () => resolveDraft(cb.checked ? {acknowledge: [c.id]} : {unacknowledge: [c.id]}));
        acts.append(h("label", {class: "row", for: "ack-" + c.id}, cb, "자동으로 반영되지 않아요. 제가 직접 확인할게요"));
      }
      acts.append(h("button", {class: "btn btn-sm btn-quiet", type: "button", onclick: () => resolveDraft({remove: [c.id]})}, "조건 빼기"));
      row.append(acts); list.append(row);
    });
    card.append(h("div", {}, h("h3", {class: "lbl"}, "적용할 조건"), list));
  }
  // 기본값 확인만 남아 있으면(다른 실제 결정 사항이 없으면) 버튼을 막지 않는다: 누르면 화면에 보이는
  // 기본값을 확인한 것으로 치고 바로 이어서 요청한다. 실제로 답해야 할 질문·조건이 남아 있으면 막는다.
  const onlyDefaultsPending = d.defaults.length > 0 && d.blocking.length === 1;
  const reallyBlocked = !d.ready && !onlyDefaultsPending;
  const go = h("button", {class: "btn btn-primary btn-lg", type: "button",
                         disabled: reallyBlocked || S.confirming,
                         onclick: confirmDraft}, S.confirming ? "요청하는 중…" : "이대로 요청하기");
  const cancel = h("button", {class: "btn", type: "button", onclick: cancelDraft}, "취소");
  card.append(h("div", {class: "row"}, go, cancel));
  if (reallyBlocked && d.blocking.length) {
    card.append(h("p", {class: "note", id: "draft-blocking"},
      "요청하려면 먼저 해결해 주세요: " + d.blocking.map(b => b.message).join(" / ")));
  } else if (onlyDefaultsPending) {
    card.append(h("p", {class: "note"},
      "위 내용대로 진행할게요. 다른 조건이면 위에서 먼저 고쳐 주세요."));
  }
  return card;
}
async function resolveDraft(changes) {
  const r = await api(`/api/requests/${S.draft.draftId}/resolve`, {json: changes});
  if (!r.ok) { toast(r.data.detail || "반영하지 못했어요."); return; }
  S.draft = r.data.draft; renderResult();
}
async function refreshDraft(draftId) {
  const r = await api(`/api/requests/${draftId}`);
  if (r.ok && r.data.status === "draft") { S.draft = r.data.draft; return "draft"; }
  if (r.ok && r.data.status === "started") { return {meetingId: r.data.meetingId}; }
  S.draft = null; S.result = null;                          // 초안이 사라졌거나(취소·만료) 찾을 수 없다
  return "gone";
}

async function confirmDraft() {
  // 중복 클릭·재시도로 세션·메일·Google 이벤트가 두 번 생기지 않게 한다. 사용자에게는 버튼 한 번 클릭으로 보인다.
  if (S.confirming || !S.draft) return;
  S.confirming = true; renderResult();
  const draftId = S.draft.draftId;
  let navigateTo = null;                    // 성공(또는 이미 시작됨)하면 이동할 곳, 그 외엔 화면을 다시 그린다
  try {
    if (S.draft.defaults && S.draft.defaults.length) {
      // 화면에 표시된 기본값(예: 참가자 전체)을 사용자가 이 버튼으로 확인한 것으로 처리한다.
      const ack = await api(`/api/requests/${draftId}/resolve`,
        {json: {ack_defaults: S.draft.defaults.map(x => x.key)}});
      if (!ack.ok) {
        if (ack.status === 404 || ack.status === 409 || ack.status === 410) {
          const out = await refreshDraft(draftId);
          if (out && out.meetingId) navigateTo = out.meetingId;
        } else {
          toast(ack.data.detail || "확인하지 못했어요. 다시 시도해 주세요.");
        }
        return;
      }
      S.draft = ack.data.draft;
      if (!S.draft.ready) return;            // 확인해도 남아 있는 실제 결정 사항이 있다 (다시 그려서 보여준다)
    }
    const r = await api(`/api/requests/${draftId}/confirm`, {method: "POST"});
    if (!r.ok) {
      if (r.status === 404 || r.status === 409 || r.status === 410) {
        const out = await refreshDraft(draftId);
        if (out && out.meetingId) navigateTo = out.meetingId;
        else if (out === "gone") toast("초안이 더 이상 유효하지 않아요. 다시 요청해 주세요.");
      } else {
        toast((r.data.detail || "요청하지 못했어요.") +
              (r.data.reasons ? " — " + r.data.reasons.map(x => x.message).join(" / ") : ""));
      }
      return;
    }
    navigateTo = r.data.meetingId;
  } catch (e) {
    toast("네트워크 연결을 확인해 주세요. 다시 시도할 수 있어요.");
  } finally {
    S.confirming = false;
    if (navigateTo) {
      S.idem = {text: "", key: "", sentText: ""}; S.result = null; S.draft = null;
      location.hash = "#/meeting/" + navigateTo;
    } else {
      renderResult();                        // 실패·중단 모든 경우에 버튼을 다시 누를 수 있는 상태로 되돌린다
    }
  }
}
async function cancelDraft() {
  await api(`/api/requests/${S.draft.draftId}/cancel`, {method: "POST"});
  S.draft = null; S.result = null; renderResult(); toast("요청을 취소했어요.");
}

// ── 회의 목록 ──
const FILTERS = [["all", "전체"], ["progress", "진행 중"], ["waiting", "동의 대기"], ["confirmed", "확정"], ["failed", "실패"], ["cancelled", "취소"]];
function renderList() {
  const chips = h("div", {class: "tabs", role: "group", "aria-label": "상태 필터"},
    FILTERS.map(([k, label]) => h("button", {class: "chip", type: "button", "aria-pressed": String(S.filter === k),
      onclick: () => { S.filter = k; savePref({filter: k}); renderList(); }}, label)));
  const card = h("section", {class: "card", "aria-labelledby": "t-all"}, h("h2", {id: "t-all"}, "회의 목록"), chips,
    h("div", {id: "list"}, skeleton()));
  app.replaceChildren(...shell([card]));
  refreshData(); schedulePoll(5000);
}

// ── 회의 상세 ──
function renderMissing() {
  app.replaceChildren(...shell([h("div", {class: "card empty"}, "회의를 찾을 수 없어요. ", h("a", {href: "#/meetings"}, "회의 목록으로 돌아가기"))]));
}
function renderMeeting() {
  app.replaceChildren(...shell([h("div", {id: "detail"}, h("div", {class: "card"}, skeleton()))]));
  refreshData(); schedulePoll(2500);
}
const STEP_MARK = {done: "✓", current: "●", failed: "!", todo: "○"};

function renderMeetingBody() {
  const d = S.detail, box = $("#detail"); if (!d || !box) return;
  const status = h("section", {class: "card status-hero stack", "aria-labelledby": "st"},
    h("div", {class: "row between"}, h("a", {href: "#/meetings", class: "note"}, "← 회의 목록"), badge(d.group, d.groupLabel)),
    h("div", {}, h("p", {class: "note"}, d.title), h("h1", {id: "st"}, d.headline),
      d.detail ? h("p", {class: "note"}, d.detail) : null,
      d.slot ? h("div", {class: "slot-big mono"}, d.slot) : null),
    h("ol", {class: "stepper", "aria-label": "진행 단계"}, d.steps.map(s =>
      h("li", {"data-s": s.state}, h("span", {class: "mk", "aria-hidden": "true"}, STEP_MARK[s.state] + " "), s.label,
        h("span", {class: "sr-only"}, s.state === "done" ? " (완료)" : s.state === "current" ? " (진행 중)" : s.state === "failed" ? " (멈춤)" : " (대기)")))));
  if (d.alert) status.append(h("div", {class: "alert warn", role: "alert"}, d.alert.text, " ",
    h("button", {class: "btn btn-sm", onclick: retryMail}, d.alert.actionLabel)));
  if (d.actions.length) status.append(h("div", {class: "row"}, d.actions.map(a =>
    h("button", {class: "btn " + (a.action === "close" ? "btn-quiet" : ""), onclick: () => followup(a.action)}, a.label))));
  if (!d.active) status.append(h("div", {}, h("a", {class: "btn btn-primary", href: "#/"}, "새 회의 요청")));

  const people = h("section", {class: "card", "aria-labelledby": "pp"}, h("h2", {id: "pp"}, "참가자"));
  if (d.progress.show) {
    const pct = d.progress.total ? Math.round(100 * d.progress.responded / d.progress.total) : 0;
    const m = h("div", {class: "meter", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(d.progress.total),
      "aria-valuenow": String(d.progress.responded), "aria-label": "응답 진행률"}, h("i", {}));
    m.firstChild.style.setProperty("--pct", pct);
    people.append(h("p", {class: "note"}, `${d.progress.total}명 중 ${d.progress.responded}명이 응답했어요. 누가 어떻게 답했는지는 보이지 않아요.`), m);
  }
  people.append(h("div", {}, d.progress.people.map(p => h("div", {class: "listrow"},
    h("div", {class: "grow lr-title"}, p.name),
    p.label ? h("span", {class: "badge " + (p.state === "responded" ? "ok" : p.state === "unsent" ? "warn" : "mute")}, p.label) : null))));

  const u = d.understood;
  const und = h("section", {class: "card stack", "aria-labelledby": "un"}, h("h2", {id: "un"}, "이해한 요청 조건"),
    h("dl", {class: "kv"},
      h("dt", {}, "회의 시간"), h("dd", {}, u.durationMin + "분"),
      h("dt", {}, "찾은 기간"), h("dd", {}, u.period || "-"),
      h("dt", {}, "확정 기준"), h("dd", {}, u.approval)));
  if (u.conditions.length) und.append(h("div", {}, u.conditions.map(c => h("div", {class: "listrow"},
    h("span", {class: "badge " + (c.strength === "필수" ? "" : "mute")}, c.strength), h("div", {class: "grow"}, c.text),
    c.manual ? h("span", {class: "note"}, c.manualAck ? "직접 확인하기로 함" : "자동 확인 불가") : null))));
  if (u.manualChecks.length) und.append(h("div", {class: "alert warn"}, "직접 확인이 필요한 조건이 있어요: " + u.manualChecks.join(" / ")));

  const left = h("div", {class: "stack-lg"}, status, people, und);
  const right = h("div", {class: "stack-lg"});
  if (d.confirmed || d.change) right.append(changeCard(d));
  right.append(materialsCard(d));
  if (S.demo) right.append(h("section", {class: "card demo-panel stack", id: "demo", "aria-labelledby": "dm"},
    h("h2", {id: "dm"}, "시연 패널 (시연 모드)"), h("div", {class: "skeleton"})));
  const openIds = new Set(Array.from(box.querySelectorAll("details[open]")).map(x => x.dataset.k));
  box.replaceChildren(h("div", {class: "cols-detail"}, left, right));
  box.querySelectorAll("details").forEach(x => { if (openIds.has(x.dataset.k)) x.open = true; });
  if (S.demo) loadDemo(d.effectiveId || d.id);
}
async function retryMail() {
  const d = S.detail; const r = await api(`/session/${d.effectiveId}/notifications/retry`, {method: "POST"});
  toast(r.ok ? (r.data.failed ? "일부 참가자에게 아직 보내지 못했어요." : "동의 요청을 다시 보냈어요.") : "다시 보내지 못했어요. 잠시 후 시도해 주세요.");
  refreshData();
}
async function followup(action) {
  const d = S.detail; const r = await api(`/session/${d.effectiveId}/followup`, {json: {action}});
  if (!r.ok) { toast(r.data.detail || "요청하지 못했어요."); return; }
  if (action === "close") { toast("이번 요청을 닫았어요."); refreshData(); return; }
  location.hash = "#/meeting/" + r.data.sessionId;
}

function changeCard(d) {
  const c = d.change || {requests: [], open: false, linksSent: 0, linksMissing: 0};
  const card = h("section", {class: "card stack", "aria-labelledby": "ch"}, h("h2", {id: "ch"}, "일정 변경"));
  if (d.confirmed) {
    card.append(h("p", {class: "note"}, "참가자는 확정 안내 메일의 링크로 변경을 요청할 수 있어요. 요청이 들어와도 다른 참가자의 투표가 통과되기 전에는 일정이 바뀌지 않고, 요청한 사람과 이유는 공개되지 않아요."));
    card.append(h("div", {}, `확정 안내 메일: ${c.linksSent}명에게 발송` + (c.linksMissing ? ` · ${c.linksMissing}명 미발송` : "")));
    if (c.linksMissing || !c.linksSent) card.append(h("button", {class: "btn btn-sm", onclick: sendConfirmLinks}, "확정 안내 메일 다시 보내기"));
  }
  if (!c.requests.length) card.append(h("p", {class: "empty"}, "변경 요청이 없어요."));
  c.requests.forEach(r => card.append(h("div", {class: "alert " + (r.open ? "warn" : "")},
    h("strong", {}, r.kind), " · ", r.stateLabel, r.state === "voting" ? ` · 투표 ${r.voted}/${r.voters}` : "")));
  return card;
}
async function sendConfirmLinks() {
  const r = await api(`/session/${S.detail.effectiveId}/change-links/send`, {method: "POST"});
  toast(r.ok ? `확정 안내 메일을 ${r.data.sent}명에게 보냈어요.` : (r.data.detail || "보내지 못했어요."));
  refreshData();
}

// ── 회의 자료 ──
function materialsCard(d) {
  const card = h("section", {class: "card stack", "aria-labelledby": "mt"}, h("h2", {id: "mt"}, "회의 자료"));
  card.append(h("p", {class: "note"}, "PDF를 올리면 핵심 요약, 쟁점, 결정할 안건을 정리해요. 정리된 결과는 주최자가 확인하고 승인한 뒤에만, 참석이 확정된 분께 전달돼요. 원본 PDF는 저장하거나 메일에 첨부하지 않아요."));
  if (S.matStatus) card.append(h("div", {class: "alert" + (S.matStatus.aiAvailable ? "" : " warn")}, S.matStatus.modeText));
  const file = h("input", {type: "file", id: "pdf", accept: "application/pdf,.pdf", class: "field", "aria-label": "회의 자료 PDF 선택"});
  const go = h("button", {class: "btn", onclick: () => uploadPdf(file, go)}, "분석하기");
  if (S.matStatus && !S.matStatus.enabled) { file.disabled = true; go.disabled = true; }
  card.append(h("div", {class: "row"}, file, go));
  const inflight = h("div", {id: "mat-note", class: "note", role: "status"});
  card.append(inflight);
  (d.materials || []).forEach(m => card.append(materialItem(d, m)));
  return card;
}
async function uploadPdf(file, btn) {
  if (!file.files.length) { toast("PDF 파일을 선택해 주세요."); return; }
  const fd = new FormData(); fd.append("file", file.files[0]);
  btn.disabled = true; $("#mat-note").textContent = "분석하는 중이에요. 잠시만 기다려 주세요…";
  const r = await api(`/api/meetings/${S.detail.id}/materials`, {form: fd});
  btn.disabled = false; $("#mat-note").textContent = "";
  if (!r.ok) { toast(r.data.detail || "분석하지 못했어요."); return; }
  toast(r.data.mode === "rules" ? "규칙 기반의 제한적인 정리를 만들었어요." : "분석을 마쳤어요.");
  refreshData();
}
function claimList(title, items) {
  if (!items || !items.length) return null;
  return h("div", {}, h("h3", {class: "lbl"}, title), items.map(c => h("div", {class: "claim" + (c.verified ? "" : " unv")}, c.text,
    (c.evidence || []).map(e => h("span", {class: "pg"}, "p." + (e.page || "?") + (e.verified ? "" : " · 문서에서 확인되지 않음"))),
    c.verified ? null : h("span", {class: "pg"}, "⚠ 근거 미확인"))));
}
function strList(title, items) {
  if (!items || !items.length) return null;
  return h("div", {}, h("h3", {class: "lbl"}, title), items.map(t => h("div", {class: "claim"}, t)));
}
function materialItem(d, m) {
  const isRules = m.mode === "rules";
  const box = h("div", {class: "draft-block stack"},
    h("div", {class: "row"}, h("span", {class: "badge " + (isRules ? "warn" : "ok")}, isRules ? "규칙 기반 (제한적)" : "AI 분석"),
      h("span", {class: "note"}, when(m.createdAt))),
    m.summary ? h("p", {}, m.summary) : null);
  const det = h("details", {"data-k": m.prepId}, h("summary", {}, "정리 결과 전체 보기"));
  det.addEventListener("toggle", async () => {
    if (!det.open || det.dataset.loaded) return;
    det.dataset.loaded = "1";
    const r = await api("/api/materials/" + m.prepId);
    if (!r.ok) { det.append(h("p", {class: "note"}, "불러오지 못했어요.")); return; }
    const res = r.data.result, meta = res.meta || {};
    det.append(h("div", {class: "stack"},
      isRules ? h("div", {class: "alert warn"}, meta.modeLabel) : null,
      h("div", {}, h("h3", {class: "lbl"}, "핵심 요약"), h("div", {class: "claim"}, res.summary || "요약을 만들지 못했어요.")),
      claimList("주요 쟁점", res.key_claims), claimList("찬성 근거", res.pros), claimList("반대 근거", res.cons),
      claimList("위험과 부족한 정보", res.risks_and_gaps), strList("결정할 안건", res.decision_questions),
      strList("추가로 확인할 점", (res.unverified_claims || []).map(t => "근거를 확인하지 못한 주장: " + t)),
      (meta.warnings || []).length ? h("p", {class: "note"}, meta.warnings.join(" · ")) : null));
  });
  box.append(det);
  box.append(shareBlock(m));
  return box;
}
function shareBlock(m) {
  const sh = m.share || m;                                   // 목록에서는 share 뷰가 그대로 들어 있다
  const wrap = h("div", {class: "stack"});
  if (!sh.canSend) { wrap.append(h("p", {class: "note"}, sh.cannotSendReason || "지금은 보낼 수 없어요.")); return wrap; }
  const names = sh.recipients.map(p => p.name).join(", ");
  const status = sh.recipients.map(p => h("span", {class: "badge " + (p.status === "sent" ? "ok" : p.status === "failed" ? "bad" : "mute")}, p.name + " · " + p.label));
  wrap.append(h("div", {class: "row"}, status));
  const pending = sh.recipients.filter(p => p.status !== "sent").length;
  if (pending) {
    const panel = h("div", {class: "alert", hidden: true, role: "group", "aria-label": "발송 확인"},
      h("p", {}, h("strong", {}, `참석이 확정된 ${sh.recipientCount}명`), `에게 보낼까요? (${names})`),
      h("p", {class: "note"}, "참석하지 않기로 했거나 빠진 분은 포함되지 않아요. 이메일에는 짧은 요약과 개인 링크만 들어가요."),
      h("div", {class: "row"}, h("button", {class: "btn btn-primary", onclick: () => sendMaterial(m.prepId || sh.prepId, sh.recipientCount)}, `${sh.recipientCount}명에게 보내기`),
        h("button", {class: "btn btn-quiet", onclick: () => { panel.hidden = true; }}, "취소")));
    wrap.append(h("button", {class: "btn btn-sm", onclick: () => { panel.hidden = false; panel.querySelector("button").focus(); }},
      sh.counts.sent ? "남은 분께 다시 보내기" : "수신자 확인하고 보내기"), panel);
  }
  return wrap;
}
async function sendMaterial(prepId, expected) {
  const r = await api(`/api/materials/${prepId}/send`, {json: {expectedCount: expected}});
  toast(r.ok ? `${r.data.sent}명에게 보냈어요.` + (r.data.failed ? ` ${r.data.failed}명은 보내지 못했어요.` : "") : (r.data.detail || "보내지 못했어요."));
  refreshData();
}

// ── 시연 패널 (시연 모드에서만) ──
async function loadDemo(id) {
  const box = $("#demo"); if (!box) return;
  const r = await api("/api/demo/" + id);
  if (!r.ok) { box.replaceChildren(h("h2", {}, "시연 패널 (시연 모드)"), h("p", {class: "note"}, r.data.detail || "불러오지 못했어요.")); return; }
  const d = r.data, lg = d.ledger;
  const max = Math.max(10, ...lg.entries.map(e => Math.abs(e.balance)));
  const bars = h("div", {class: "bars"}, lg.entries.map(e => {
    const fill = h("span", {class: "fill" + (e.balance < 0 ? " neg" : "")});
    fill.style.width = (Math.abs(e.balance) / max * 50) + "%";
    if (e.balance >= 0) fill.style.left = "50%"; else fill.style.right = "50%";
    return h("div", {class: "bar"}, h("span", {}, e.name), h("span", {class: "track"}, h("span", {class: "mid"}), fill),
      h("span", {class: "mono"}, (e.balance > 0 ? "+" : "") + e.balance.toFixed(1)));
  }));
  box.replaceChildren(h("h2", {}, "시연 패널 (시연 모드)"),
    h("p", {class: "note"}, "이 패널은 화면 표시만 바꿔요. 협상 알고리즘과 결과에는 영향이 없어요."),
    h("h3", {class: "lbl"}, "양보 잔액"), bars,
    h("p", {}, "공정성: ", h("strong", {}, lg.verdict), ` (격차 ${lg.spread}, 합계 ${lg.total})`),
    h("p", {class: "note"}, lg.note),
    h("h3", {class: "lbl"}, "협상 과정 (익명)"),
    h("ol", {}, d.phases.map(p => h("li", {}, p.text))),
    d.proposals.length ? h("div", {}, h("h3", {class: "lbl"}, "제안한 시간"), d.proposals.map(p =>
      h("div", {class: "listrow"}, h("div", {class: "grow"}, `${p.rank}번째 · ${p.slot}`), h("span", {class: "badge mute"}, p.label)))) : null,
    h("p", {class: "note"}, `${d.participants}명의 에이전트 · ${d.exchange}`));
}

// ── 시작 ──
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.me && S.me.authenticated) refreshData(); });
(async function init() {
  await boot();
  if (S.me && S.me.authenticated) { const r = await api("/api/materials/status"); if (r.ok) { S.matStatus = r.data; render(); } }
})();
