// Kansas City News Radar — minimal, dependency-free dashboard.
// All external text is escaped before insertion; do not use innerHTML with untrusted content.

const $ = (id) => document.getElementById(id);

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtBeat(b) {
  if (!b) return "";
  return b.replaceAll("_", " ").toLowerCase().replace(/(^|\s)\S/g, (c) => c.toUpperCase());
}

function fmtDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      timeZone: "America/Chicago",
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      timeZoneName: "short",
    });
  } catch { return iso; }
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function postJSON(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${url} -> ${r.status}`);
  return body;
}

function scoreBar(score, label) {
  const pct = Math.max(0, Math.min(100, score || 0));
  return `<div class="score">
    <div class="score__label">${esc(label)}</div>
    <div class="score__bar"><span style="width:${pct}%"></span></div>
    <div class="score__num">${pct} / 100</div>
  </div>`;
}

function priorityCard(f) {
  const reasons = (f.explanation && f.explanation.likelihood && f.explanation.likelihood.components) || [];
  const relevanceReasons = (f.explanation && f.explanation.editorial_relevance && f.explanation.editorial_relevance.components) || [];
  const bullets = reasons.map(c => `<li>+${c.weight} — ${esc(c.reason)}</li>`).join("");
  const relBullets = relevanceReasons.map(c => `<li>+${c.weight} — ${esc(c.reason)}</li>`).join("");
  const versionInfo = f.version_count > 1
    ? `<span class="chip">v${f.version} · Δlikelihood ${f.delta_likelihood >= 0 ? "+" : ""}${f.delta_likelihood}</span>`
    : `<span class="chip chip--new">v${f.version} · new</span>`;
  return `
  <article class="card card--priority">
    <header class="card__head">
      <div class="card__title">${esc(f.claim)}</div>
      <div class="card__chips">${versionInfo}<span class="chip">${esc(fmtBeat(f.beat))}</span></div>
    </header>
    ${scoreBar(f.likelihood_score, "Experimental likelihood")}
    ${scoreBar(f.editorial_relevance_score, "Editorial relevance")}
    ${scoreBar(f.priority_score, "Priority (newsroom rank)")}
    <div class="card__row">
      <div><strong>Why elevated:</strong><ul class="reasons">${bullets || "<li>—</li>"}</ul></div>
      <div><strong>Editorial-relevance factors:</strong><ul class="reasons">${relBullets || "<li>—</li>"}</ul></div>
    </div>
    <footer class="card__foot">
      <div>Window ends: ${esc(fmtDate(f.horizon_end))}</div>
      <div>Model: ${esc(f.model_version)}</div>
      <div class="scientific-note">Experimental score — not a calibrated probability.</div>
    </footer>
  </article>`;
}

function miniCard(f, extraChip) {
  return `<article class="card card--mini">
    <div class="card__title">${esc(f.claim || f.title || "")}</div>
    <div class="card__chips">
      ${extraChip ? `<span class="chip">${esc(extraChip)}</span>` : ""}
      <span class="chip">${esc(fmtBeat(f.beat))}</span>
      ${f.likelihood_score !== undefined ? `<span class="chip chip--score">L ${f.likelihood_score}</span>` : ""}
      ${f.editorial_relevance_score !== undefined ? `<span class="chip chip--score">R ${f.editorial_relevance_score}</span>` : ""}
    </div>
  </article>`;
}

function watchCard(sig) {
  return `<article class="card card--mini card--watch">
    <div class="card__title">${esc(sig.title)}</div>
    <div class="card__body">${esc(sig.summary)}</div>
    <div class="card__chips">
      <span class="chip">${esc(sig.signal_type)}</span>
      <span class="chip">${esc(fmtBeat(sig.beat))}</span>
      <span class="chip chip--score">N ${sig.novelty_score}</span>
      <span class="chip chip--score">I ${sig.local_impact_score}</span>
    </div>
  </article>`;
}

function questionRow(q) {
  return `<li class="question">
    <div class="question__prompt">${esc(q.prompt)}</div>
    <div class="question__why">Why: ${esc(q.why)}</div>
    <div class="chip">${esc(fmtBeat(q.beat))}</div>
  </li>`;
}

function beatMomentumRow(r) {
  const catalyst = r.next_catalyst
    ? `${esc(r.next_catalyst)} (~${r.next_catalyst_hours}h)`
    : "—";
  return `<tr>
    <td>${esc(fmtBeat(r.beat))}</td>
    <td class="momentum">${esc(r.momentum)}</td>
    <td>${esc(r.editorial_relevance)}</td>
    <td>${catalyst}</td>
  </tr>`;
}

function next72Row(e) {
  return `<tr>
    <td>${esc(fmtDate(e.event_at))} <span class="dim">(~${e.hours_out}h)</span></td>
    <td>${esc(e.title)}</td>
    <td>${esc(fmtBeat(e.beat))}</td>
    <td>${esc(e.geography || "")}</td>
    <td>${e.canonical_url ? `<a href="${esc(e.canonical_url)}" target="_blank" rel="noopener">${esc(e.source_name)}</a>` : esc(e.source_name)}</td>
  </tr>`;
}

function sourceRow(s) {
  const cls = s.status === "HEALTHY" ? "ok" : s.status === "DEGRADED" ? "warn" : s.status === "FAILED" ? "bad" : "dim";
  return `<tr>
    <td>${esc(s.source_name)}</td>
    <td><span class="badge badge--${cls}">${esc(s.status)}</span></td>
    <td>${esc(fmtDate(s.last_attempt))}</td>
    <td>${esc(fmtDate(s.last_success))}</td>
    <td>${s.item_count}</td>
    <td>${s.latency_ms} ms</td>
    <td class="dim">${esc(s.error_message || "")}</td>
  </tr>`;
}

function ledgerRow(f) {
  const outcome = f.resolution ? f.resolution.outcome : "";
  return `<tr>
    <td>${esc(fmtDate(f.issued_at))}</td>
    <td><button class="link-button forecast-open" data-forecast-id="${esc(f.forecast_id)}">${esc(f.claim)}</button></td>
    <td>${f.likelihood_score}</td>
    <td>${f.editorial_relevance_score}</td>
    <td>${f.priority_score}</td>
    <td>${esc(fmtDate(f.horizon_end))}</td>
    <td><span class="badge badge--${f.display_status === 'OPEN' ? 'ok' : f.display_status === 'EXPIRED' ? 'warn' : 'dim'}">${esc(f.display_status)}</span></td>
    <td>${esc(outcome)}</td>
    <td>v${f.version}</td>
  </tr>`;
}

function evidenceSignal(signal) {
  const records = signal.source_records.map(item => `<article class="evidence-record">
    <div><strong>${esc(item.source_name)}</strong> · observed ${esc(fmtDate(item.retrieved_at))}</div>
    <div class="card__title">${esc(item.title)}</div>
    <div class="card__body">${esc(item.excerpt || "No excerpt supplied by source.")}</div>
    <div class="evidence-meta">First seen ${esc(fmtDate(item.first_seen_at))} · last seen ${esc(fmtDate(item.last_seen_at))} · content hash ${esc(item.content_hash)}</div>
    ${item.canonical_url ? `<a href="${esc(item.canonical_url)}" target="_blank" rel="noopener">Open public source record</a>` : `<span class="dim">No canonical URL supplied.</span>`}
  </article>`).join("");
  return `<section class="evidence-signal">
    <h3>${esc(signal.signal_type)} — ${esc(signal.title)}</h3>
    <p>${esc(signal.summary)}</p>
    <div class="card__chips"><span class="chip">Novelty ${signal.novelty_score}</span><span class="chip">Impact ${signal.local_impact_score}</span></div>
    ${records}
  </section>`;
}

async function openForecast(forecastId) {
  const root = $("forecastDetail");
  root.hidden = false;
  root.innerHTML = "<div class='empty'>Loading forecast evidence…</div>";
  root.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const detail = await fetchJSON(`/api/forecasts/${encodeURIComponent(forecastId)}`);
    const f = detail.latest;
    const limitations = detail.evidence_limitations.map(x => `<li>${esc(x)}</li>`).join("");
    const evidence = detail.supporting_evidence.length
      ? detail.supporting_evidence.map(evidenceSignal).join("")
      : `<div class="callout callout--warn">Historical evidence unavailable. Current source content has not been substituted.</div>`;
    const feedbackHistory = detail.editorial_feedback.length
      ? detail.editorial_feedback.map(x => `<li>${esc(x.label)} · ${esc(fmtDate(x.created_at))}${x.note ? ` — ${esc(x.note)}` : ""}</li>`).join("")
      : "<li>No feedback recorded.</li>";
    const resolution = detail.resolution;
    const resolutionBlock = resolution
      ? `<div class="callout"><strong>Outcome: ${esc(resolution.outcome)}</strong> for forecast v${esc(resolution.forecast_version)}<br>Recorded ${esc(fmtDate(resolution.resolved_at))}<br>Evidence: ${esc(resolution.evidence)}${resolution.notes ? `<br>Notes: ${esc(resolution.notes)}` : ""}<br><span class="dim">The original forecast remains immutable.</span></div>`
      : `<form id="resolutionForm" class="compact-form">
          <h3>Record outcome for forecast v${f.version}</h3>
          <label>Outcome<select name="outcome" required>
            <option value="">Choose…</option><option>CONFIRMED</option><option>NOT_OCCURRED</option><option>AMBIGUOUS</option><option>EXPIRED_UNRESOLVED</option>
          </select></label>
          <label>Supporting evidence<textarea name="evidence" maxlength="2000" required placeholder="Public record, published report, or concise explanation"></textarea></label>
          <label>Notes (optional)<textarea name="notes" maxlength="1000"></textarea></label>
          <button type="submit">Record immutable outcome</button><span class="form-status" id="resolutionStatus"></span>
        </form>`;
    root.innerHTML = `<div class="detail-head"><div><h2>${esc(f.claim)}</h2><div class="card__chips"><span class="chip">Forecast v${f.version}</span><span class="chip">${esc(detail.evidence_status)}</span><span class="chip">Model ${esc(f.model_version)}</span></div></div><button id="closeDetail" class="secondary-button">Close</button></div>
      <p class="scientific-note">Experimental score — not a calibrated probability.</p>
      <h2>Supporting public evidence captured at issuance</h2>${evidence}
      <h3>Evidence boundaries</h3><ul>${limitations}</ul>
      <div class="workflow-grid"><form id="feedbackForm" class="compact-form">
        <h3>Editorial feedback</h3>
        <label>Structured label<select name="label" required><option value="">Choose…</option><option>USEFUL</option><option>NOT_USEFUL</option><option>ALREADY_KNEW</option><option>WATCH</option><option>ASSIGNED_REPORTER</option><option>NOT_NEWSWORTHY</option><option>INCORRECT</option></select></label>
        <label>Note (optional)<textarea name="note" maxlength="400"></textarea></label>
        <button type="submit">Save feedback</button><span class="form-status" id="feedbackStatus"></span>
        <h4>Recorded feedback</h4><ul>${feedbackHistory}</ul>
      </form>${resolutionBlock}</div>`;
    $("closeDetail").addEventListener("click", () => { root.hidden = true; });
    $("feedbackForm").addEventListener("submit", async event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const status = $("feedbackStatus");
      try {
        await postJSON("/api/feedback", { subject_type: "forecast", subject_id: forecastId, label: form.get("label"), note: form.get("note") || null });
        status.textContent = "Saved.";
        await openForecast(forecastId);
      } catch (e) { status.textContent = e.message; }
    });
    if ($("resolutionForm")) $("resolutionForm").addEventListener("submit", async event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const status = $("resolutionStatus");
      try {
        await postJSON(`/api/forecasts/${encodeURIComponent(forecastId)}/resolution`, {
          forecast_version: f.version, outcome: form.get("outcome"), evidence: form.get("evidence"), notes: form.get("notes") || null,
        });
        status.textContent = "Outcome recorded.";
        await loadAll();
        await openForecast(forecastId);
      } catch (e) { status.textContent = e.message; }
    });
  } catch (e) { root.innerHTML = `<div class="callout callout--warn">${esc(e.message)}</div>`; }
}

function renderPerformance(performance) {
  const outcomeRows = performance.by_outcome.map(r => `<tr><td>${esc(r.outcome)}</td><td>${r.resolved_count}</td><td>${r.denominator}</td></tr>`).join("");
  const modelRows = performance.by_model_version.map(r => `<tr><td>${esc(r.model_version)}</td><td>${r.resolved_count}</td><td>${r.overall_resolved_denominator}</td><td>${esc(JSON.stringify(r.outcomes))}</td></tr>`).join("");
  const signalRows = performance.by_signal_type.map(r => `<tr><td>${esc(r.signal_type)}</td><td>${r.resolved_count}</td><td>${r.overall_resolved_denominator}</td><td>${esc(JSON.stringify(r.outcomes))}</td></tr>`).join("");
  return `<div class="callout ${performance.sufficient_resolved_evidence ? "" : "callout--warn"}">${esc(performance.evidence_note)}</div>
    <p>Resolved denominator: <strong>${performance.denominator}</strong>. Legacy outcomes lacking an explicit forecast version and excluded: <strong>${performance.excluded_legacy_resolutions_without_version}</strong>.</p>
    <div class="grid-3"><div><h2>By outcome</h2><table class="table"><thead><tr><th>Outcome</th><th>Count</th><th>Denominator</th></tr></thead><tbody>${outcomeRows || "<tr><td colspan='3'>No resolved forecasts.</td></tr>"}</tbody></table></div>
    <div><h2>By model version</h2><table class="table"><thead><tr><th>Model</th><th>Count</th><th>Overall denominator</th><th>Outcomes</th></tr></thead><tbody>${modelRows || "<tr><td colspan='4'>No resolved forecasts.</td></tr>"}</tbody></table></div>
    <div><h2>By signal type</h2><table class="table"><thead><tr><th>Signal</th><th>Count</th><th>Overall denominator</th><th>Outcomes</th></tr></thead><tbody>${signalRows || "<tr><td colspan='4'>No resolved forecasts.</td></tr>"}</tbody></table></div></div>
    <p class="scientific-note">${esc(performance.scientific_note)}</p>`;
}

async function loadAll() {
  try {
    const [brief, sources, forecasts, signals, health, performance] = await Promise.all([
      fetchJSON("/api/brief"),
      fetchJSON("/api/sources"),
      fetchJSON("/api/forecasts"),
      fetchJSON("/api/signals?limit=200"),
      fetchJSON("/api/health"),
      fetchJSON("/api/performance"),
    ]);

    // Top bar
    const meta = `${esc(brief.generated_at_local)} · ${brief.sources_summary.healthy} healthy · ${brief.sources_summary.degraded} degraded · ${brief.sources_summary.failed} failed · scoring model ${esc(brief.scoring_model_version)}`;
    $("topMeta").innerHTML = meta;
    if (health.demo_mode) {
      $("demoBanner").hidden = false;
    }
    if (health.database.warnings.length) {
      $("databaseBanner").hidden = false;
      $("databaseBanner").textContent = `Database: ${health.database.path} — ${health.database.warnings.join(" ")}`;
    } else {
      $("databaseBanner").hidden = true;
    }

    // Brief header
    $("briefMeta").textContent = brief.generated_at_local;
    $("briefNote").textContent = brief.scientific_note;
    $("briefDisclaimer").textContent = brief.disclaimer;

    // Top priorities
    $("topPriorities").innerHTML = brief.top_priorities.length
      ? brief.top_priorities.map(priorityCard).join("")
      : "<div class='empty'>No open priority forecasts at this time.</div>";

    $("briefNew").innerHTML = brief.new.length ? brief.new.map(f => miniCard(f, "NEW")).join("") : "<div class='empty'>—</div>";
    $("briefChanged").innerHTML = brief.changed.length ? brief.changed.map(f => miniCard(f, `Δ${f.delta_likelihood >= 0 ? "+" : ""}${f.delta_likelihood}`)).join("") : "<div class='empty'>—</div>";
    $("briefResolved").innerHTML = brief.resolved.length
      ? brief.resolved.map(r => `<article class='card card--mini'><div class='card__title'>${esc(r.claim)}</div><div class='card__chips'><span class='chip chip--resolved'>${esc(r.outcome)}</span><span class='chip'>${esc(fmtDate(r.resolved_at))}</span></div></article>`).join("")
      : "<div class='empty'>None in the last 7 days.</div>";
    $("briefWatch").innerHTML = brief.watch.length ? brief.watch.map(watchCard).join("") : "<div class='empty'>No watch-list signals.</div>";
    $("briefBeatMomentum").innerHTML = brief.beat_momentum.length ? brief.beat_momentum.map(beatMomentumRow).join("") : "<tr><td colspan='4' class='empty'>—</td></tr>";
    $("briefQuestions").innerHTML = brief.questions.length ? brief.questions.map(questionRow).join("") : "<li class='empty'>No suggested discussion questions.</li>";

    // Emerging signals
    const emerging = signals.signals.filter(s => ["UNUSUAL_AGENDA_ITEM","MULTI_SOURCE_CONVERGENCE","REPEATED_ENTITY_ACTIVITY","NEW_ITEM","ITEM_UPDATED"].includes(s.signal_type));
    $("emergingList").innerHTML = emerging.length ? emerging.map(watchCard).join("") : "<div class='empty'>No emerging signals.</div>";

    // Next 72h
    $("next72Body").innerHTML = brief.next_72h.length ? brief.next_72h.map(next72Row).join("") : "<tr><td colspan='5' class='empty'>No scheduled catalysts detected in the next 72h.</td></tr>";

    // Ledger
    $("ledgerBody").innerHTML = forecasts.forecasts.length ? forecasts.forecasts.map(ledgerRow).join("") : "<tr><td colspan='9' class='empty'>No forecasts issued yet.</td></tr>";
    document.querySelectorAll(".forecast-open").forEach(button => button.addEventListener("click", () => openForecast(button.dataset.forecastId)));

    $("performanceSummary").innerHTML = renderPerformance(performance);

    // Source health
    $("sourcesBody").innerHTML = sources.sources.map(sourceRow).join("");
  } catch (e) {
    console.error(e);
    $("topMeta").textContent = "error loading data: " + e.message;
  }
}

// Tab switching
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("tab--active"));
    btn.classList.add("tab--active");
    const id = btn.dataset.tab;
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("panel--active"));
    document.getElementById(id).classList.add("panel--active");
  });
});

loadAll();
