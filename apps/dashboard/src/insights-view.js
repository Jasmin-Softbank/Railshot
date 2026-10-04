// Shared rendering for the dashboard and MCP App. All remote strings are text nodes.
export const insightsStyle = `
.rs-insights{font:15px/1.6 system-ui,sans-serif;color:#19352e;background:#f5f8f7;padding:24px;border-radius:16px;max-width:1000px;margin:auto}
.rs-insights h2,.rs-insights h3{margin:0 0 12px}.rs-insights small{color:#526960}.rs-insights header,.rs-insights nav{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center}
.rs-insights button,.rs-insights select{font:inherit;border:1px solid #b7cfc5;background:white;border-radius:8px;padding:6px 12px;cursor:pointer}
.rs-insights .rs-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}.rs-insights article,.rs-insights section{background:white;border:1px solid #dce7e1;border-radius:12px;padding:18px;margin-bottom:12px}
.rs-insights strong{display:block;font-size:24px}.rs-insights svg{width:100%;height:auto;display:block}.rs-insights .rs-muted{color:#62756c}.rs-insights .rs-alert{color:#a12e28}.rs-insights .rs-ok{color:#087d61}
.rs-insights pre{white-space:pre-wrap;overflow-wrap:anywhere}.rs-insights li{overflow-wrap:anywhere}.rs-insights a{color:#087d61}
@media(max-width:480px){.rs-insights{padding:12px}.rs-insights strong{font-size:20px}}`;
const el = (tag, text, className) => {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (className) e.className = className;
  return e;
};
const labels = {
  ready: "수집됨",
  unsupported: "앱 관측 미등록",
  not_configured: "관측 미설정",
  unavailable: "조회 불가",
  no_data: "표본 부족",
  stale: "오래된 관측",
  collection_failed: "수집 실패",
  succeeded: "완료",
  published: "이미지 게시 완료",
  deployed: "적용 완료",
  failed: "실패",
  blocked: "조치 필요",
  running: "진행 중",
  unknown: "확인 필요",
  analyzing: "AI 처리 중",
  verifying: "재검증 중",
  current: "최신",
};
const label = (value) => labels[value] || value || "기록 없음";
const time = (value) =>
  Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString("ko-KR")
    : "기록 없음";
const number = (value, suffix = "") =>
  Number.isFinite(value)
    ? `${value.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}${suffix}`
    : "자료 없음";

// Fetch time must not make an old measurement look current, including in exports.
const observationState = (metric) => {
  if (metric?.state !== "ready") return metric?.state;
  const age = Date.now() - Date.parse(metric.observed_at);
  return Number.isFinite(age) && age >= -5000 && age <= 90000
    ? "ready"
    : "stale";
};
const httpSummary = (http) =>
  observationState(http) === "ready"
    ? http.value === 1
      ? "응답 정상"
      : http.value === 0
        ? "응답 실패"
        : "확인 필요"
    : label(observationState(http));

export function insightsMarkdown(data) {
  const d = data.deployment,
    t = data.traffic,
    http = data.observation?.metrics?.http,
    fresh = observationState(t) === "ready";
  return [
    `# ${data.app} 운영 확인`,
    `조회 시각: ${data.checked_at}`,
    `배포 ID: ${data.deployment_id}`,
    `대상: ${data.target_id}`,
    `배포 결과: ${d.status} / ${d.stage}`,
    `소스: ${d.source_commit || "미제공"}`,
    `GitOps: ${d.revision || "미제공"}`,
    `현재 HTTP: ${httpSummary(http)} · 표본 시각: ${http?.observed_at || "없음"}`,
    `트래픽 구간: ${t.window.start} ~ ${t.window.end}`,
    `관측 상태: ${observationState(t)}`,
    `실제 표본 시각: ${t.observed_at || "없음"}`,
    `앱 요청 추정 집계: ${number(fresh ? t.requests : null)}`,
    `5xx 오류율: ${number(fresh ? t.error_percent : null, "%")}`,
    `p95: ${number(fresh ? t.p95_ms : null, " ms")}`,
    `AI 활동 조회: ${data.activity_state}`,
    ...(data.agent_activity?.changes || []).map(
      (c) => `- 적용: ${c.path} — AI 설명: ${c.summary}`,
    ),
    ...(data.agent_activity?.verification || []).map(
      (v) => `- 검사: ${v.label || v.key} — ${v.state}`,
    ),
    "",
    ...data.interpretation,
  ].join("\n\n");
}
function chart(traffic, deployedAt) {
  const ns = "http://www.w3.org/2000/svg";
  const make = (name, attrs, text) => {
    const n = document.createElementNS(ns, name);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text) n.textContent = text;
    return n;
  };
  const svg = make("svg", {
    viewBox: "0 0 800 220",
    role: "img",
    "aria-label": "앱 요청 추이와 배포 검증 시점",
  });
  const start = Date.parse(traffic.window.start),
    end = Date.parse(traffic.window.end);
  const points = traffic.series || [],
    max = Math.max(1, ...points.map((p) => p.requests_per_second || 0));
  const x = (at) => 48 + ((Date.parse(at) - start) / (end - start)) * 720;
  svg.append(
    make("line", { x1: 48, x2: 768, y1: 175, y2: 175, stroke: "#b7cfc5" }),
    make("text", { x: 48, y: 18, fill: "#526960" }, `${number(max)} req/s`),
  );
  let path = "",
    previous = null;
  for (const point of points) {
    if (point.requests_per_second === null) {
      previous = null;
      continue;
    }
    const contiguous =
      previous &&
      Date.parse(point.at) - Date.parse(previous) <=
        traffic.window.step_seconds * 1500;
    path += `${contiguous ? "L" : "M"}${x(point.at)},${175 - (point.requests_per_second / max) * 140} `;
    previous = point.at;
  }
  svg.append(
    make("path", {
      d: path,
      fill: "none",
      stroke: "#008267",
      "stroke-width": 3,
    }),
  );
  for (const point of points)
    if (point.requests_per_second !== null)
      svg.append(
        make("circle", {
          cx: x(point.at),
          cy: 175 - (point.requests_per_second / max) * 140,
          r: 2,
          fill: "#008267",
        }),
      );
  if (Date.parse(deployedAt) >= start && Date.parse(deployedAt) <= end) {
    const at = x(deployedAt);
    svg.append(
      make("line", {
        x1: at,
        x2: at,
        y1: 32,
        y2: 175,
        stroke: "#aa7a18",
        "stroke-dasharray": "5 4",
      }),
      make(
        "text",
        { x: Math.min(at + 4, 660), y: 30, fill: "#906511" },
        "배포 HTTP 검증",
      ),
    );
  }
  svg.append(
    make(
      "text",
      { x: 48, y: 207, fill: "#526960" },
      new Date(start).toLocaleTimeString("ko-KR"),
    ),
    make(
      "text",
      { x: 768, y: 207, "text-anchor": "end", fill: "#526960" },
      new Date(end).toLocaleTimeString("ko-KR"),
    ),
  );
  return svg;
}
export function renderInsights(host, data, { refresh, minutes = 15 } = {}) {
  host.replaceChildren();
  host.className = "rs-insights";
  const header = el("header"),
    heading = el("div");
  heading.append(
    el("h2", `${data.app} · 운영 확인`),
    el("small", `${data.target_id} · 조회 ${time(data.checked_at)}`),
  );
  const controls = el("nav"),
    select = el("select");
  select.setAttribute("aria-label", "조회 기간");
  for (const value of [15, 60]) {
    const option = el("option", `최근 ${value}분`);
    option.value = value;
    select.append(option);
  }
  select.value = minutes;
  select.onchange = () => refresh?.(Number(select.value));
  const reload = el("button", "새로고침");
  reload.onclick = () => refresh?.(Number(select.value));
  const download = el("button", "보고서 저장");
  download.onclick = () => {
    const url = URL.createObjectURL(
      new Blob([insightsMarkdown(data)], {
        type: "text/markdown;charset=utf-8",
      }),
    );
    const a = el("a");
    a.href = url;
    a.download = "railshot-insights.md";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  controls.append(select, reload, download);
  header.append(heading, controls);
  host.append(header);
  const t = data.traffic,
    d = data.deployment,
    cards = el("div", null, "rs-cards"),
    http = data.observation?.metrics?.http;
  const freshTraffic = observationState(t) === "ready";
  for (const [title, value] of [
    ["현재 HTTP", httpSummary(http)],
    [
      "앱 요청 집계",
      freshTraffic
        ? number(t.requests)
        : label(t.state === "ready" ? "stale" : t.state),
    ],
    ["5xx 오류율", freshTraffic ? number(t.error_percent, "%") : "자료 없음"],
    ["응답 시간 p95", freshTraffic ? number(t.p95_ms, " ms") : "자료 없음"],
  ]) {
    const card = el("article");
    card.append(el("small", title), el("strong", value));
    cards.append(card);
  }
  host.append(cards);
  const traffic = el("section");
  traffic.append(
    el("h3", "요청 추이"),
    el("small", "앱에 도달한 요청 · 순 방문자 수 아님 · 30초 수집 주기"),
  );
  if (freshTraffic && t.history_state === "ready")
    traffic.append(chart(t, d.public_http?.verified_at));
  else
    traffic.append(
      el(
        "p",
        `차트: ${label(freshTraffic ? t.history_state : t.state === "ready" ? "stale" : t.state)}`,
        "rs-muted",
      ),
    );
  traffic.append(
    el(
      "small",
      `실제 표본 ${time(t.observed_at)} · 구간 집계는 Prometheus 추정값입니다.`,
    ),
  );
  host.append(traffic);
  const deployment = el("section");
  deployment.append(
    el("h3", "배포 결과"),
    el(
      "p",
      `CI: ${label(d.ci_state)} → CD: ${label(d.cd_state)} · 전체: ${label(d.status)}`,
    ),
    el(
      "small",
      `소스 ${d.source_commit?.slice(0, 12) || "미제공"} · GitOps ${d.revision?.slice(0, 12) || "미제공"}`,
    ),
  );
  host.append(deployment);
  const activity = el("section"),
    agent = data.agent_activity;
  activity.append(el("h3", "AI 처리 내역"));
  if (agent) {
    activity.append(
      el(
        "p",
        `${agent.attempt}차 시도 · ${label(agent.state)} · 관측 ${label(agent.observation?.state)}`,
      ),
    );
    if (agent.current_action) activity.append(el("p", agent.current_action));
    const list = el("ul");
    for (const c of agent.changes || [])
      list.append(el("li", `${c.path} — 적용됨 / AI 설명: ${c.summary}`));
    for (const v of agent.verification || [])
      list.append(el("li", `${v.label || v.key}: ${label(v.state)}`));
    activity.append(list);
  } else
    activity.append(
      el(
        "p",
        data.activity_state === "unavailable"
          ? "AI 활동을 조회하지 못했습니다."
          : "확인된 AI 처리 기록이 없습니다.",
      ),
    );
  host.append(activity);
  for (const note of data.interpretation)
    host.append(el("small", note), el("br"));
}
