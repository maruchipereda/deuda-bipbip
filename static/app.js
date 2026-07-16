const state = {
  auth: JSON.parse(sessionStorage.getItem("bipbipAuth") || "null"),
  user: null,
  users: [],
  settings: {},
  cases: [],
  summary: null,
  currentBucket: "pendientes",
  currentView: "cases",
  statusFilter: "",
  currentDriver: null,
  lookupCedula: "",
  lookupPhone: "",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 3500);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function authenticatedUrl(url) {
  if (!url || !state.auth?.token) return url || "";
  const joiner = url.includes("?") ? "&" : "?";
  return `${url}${joiner}token=${encodeURIComponent(state.auth.token)}`;
}

function money(value, currency = "VES") {
  const number = Number(value || 0);
  return new Intl.NumberFormat("es-VE", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

function shortDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("es-VE", { dateStyle: "short", timeStyle: "short" });
}

function parseAlerts(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  try {
    return JSON.parse(value);
  } catch {
    return [];
  }
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.auth?.token) headers.Authorization = `Bearer ${state.auth.token}`;
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json();
  if (!response.ok || payload.error) {
    if (response.status === 401) {
      sessionStorage.removeItem("bipbipAuth");
      state.auth = null;
      showPublic();
    }
    throw new Error(payload.details || payload.error || "Request fallido");
  }
  return payload;
}

function readFile(input) {
  const file = input.files?.[0];
  if (!file) return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, type: file.type, data: reader.result });
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function showPublic() {
  $("#publicView").classList.remove("hidden");
  $("#adminLoginView").classList.add("hidden");
  $("#adminApp").classList.add("hidden");
}

function showLogin() {
  $("#publicView").classList.add("hidden");
  $("#adminLoginView").classList.remove("hidden");
  $("#adminApp").classList.add("hidden");
}

function showAdmin() {
  $("#publicView").classList.add("hidden");
  $("#adminLoginView").classList.add("hidden");
  $("#adminApp").classList.remove("hidden");
}

async function loadPublicConfig() {
  const payload = await api("/api/public/config");
  state.settings = payload.settings;
  renderBankSettings();
}

function renderBankSettings() {
  const accounts = paymentAccounts();
  $("#paymentAccountCards").innerHTML = accounts.map((account) => `
    <article class="payment-account-card">
      <div class="account-type">${escapeHtml(account.type || "Cuenta")}</div>
      <dl>
        <div><dt>Banco</dt><dd>${escapeHtml(account.bank_name || "-")}</dd></div>
        <div><dt>Titular</dt><dd>${escapeHtml(account.account_holder || "-")}</dd></div>
        ${account.account_number ? `<div><dt>Cuenta</dt><dd>${escapeHtml(account.account_number)}</dd></div>` : ""}
        ${account.phone ? `<div><dt>Telefono</dt><dd>${escapeHtml(account.phone)}</dd></div>` : ""}
        ${account.document ? `<div><dt>Documento</dt><dd>${escapeHtml(account.document)}</dd></div>` : ""}
        ${account.rif && account.rif !== account.document ? `<div><dt>RIF</dt><dd>${escapeHtml(account.rif)}</dd></div>` : ""}
      </dl>
      ${account.instructions ? `<p>${escapeHtml(account.instructions)}</p>` : ""}
    </article>
  `).join("");
  $("#bankInstructions").textContent = state.settings.instructions || "";
}

function paymentAccounts() {
  if (Array.isArray(state.settings.payment_accounts) && state.settings.payment_accounts.length) {
    return state.settings.payment_accounts;
  }
  return [{
    type: "Transferencia a cuenta indicada",
    bank_name: state.settings.bank_name || "",
    account_holder: state.settings.account_holder || "",
    account_number: state.settings.account_number || "",
    rif: state.settings.rif || "",
    document: state.settings.rif || "",
    phone: "",
    instructions: "Usa esta cuenta solo para transferencia bancaria.",
  }];
}

async function lookupDebt(event) {
  event.preventDefault();
  const cedula = $("#lookupCedula").value.trim();
  const phone = $("#lookupPhone").value.trim();
  state.lookupCedula = cedula;
  state.lookupPhone = phone;
  try {
    const payload = await api(`/api/public/debt?cedula=${encodeURIComponent(cedula)}&phone=${encodeURIComponent(phone)}`);
    state.currentDriver = payload.driver;
    state.settings = payload.settings;
    renderBankSettings();
    renderDebt();
    $("#debtPanel").classList.remove("hidden");
    $("#debtPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message);
  }
}

function renderDebt() {
  const driver = state.currentDriver;
  if (!driver) return;
  $("#debtVes").textContent = money(driver.debt_ves, "VES");
  $("#debtUsd").textContent = money(driver.debt_usd, "USD");
  $("#debtRate").textContent = `Tasa: ${Number(driver.rate || 0).toLocaleString("es-VE", { minimumFractionDigits: 2 })}`;
  $("#paidVes").textContent = money(driver.paid_ves, "VES");
  $("#paidUsd").textContent = money(driver.paid_usd, "USD");
  $("#pendingVes").textContent = money(driver.pending_ves, "VES");
  $("#pendingUsd").textContent = money(driver.pending_usd, "USD");
  $("#caseStatus").textContent = driver.status_label || driver.status;
  $("#payCedula").value = driver.cedula || "";
  $("#payPhone").value = state.lookupPhone || driver.phone || "";
  $("#payPlate").value = driver.plate || "";
  $("#payAmount").value = Number(driver.pending_ves || driver.debt_ves || 0).toFixed(2);
  $("#payDate").value = new Date().toISOString().slice(0, 10);
}

async function submitPayment(event) {
  event.preventDefault();
  const file = await readFile($("#payFile"));
  const payload = {
    cedula: $("#payCedula").value.trim(),
    lookup_cedula: state.currentDriver?.cedula || state.lookupCedula || "",
    registered_phone: state.currentDriver?.phone || "",
    lookup_phone: state.lookupPhone || "",
    payment_phone: $("#payPhone").value.trim(),
    plate: $("#payPlate").value.trim(),
    amount_ves: $("#payAmount").value.trim(),
    reference: $("#payReference").value.trim(),
    bank: $("#payBank").value.trim(),
    payment_date: $("#payDate").value,
    payment_method: $("#payMethod").value,
    observations: $("#payObservations").value.trim(),
    attachment_file: file,
  };
  try {
    await api("/api/public/payments", { method: "POST", body: JSON.stringify(payload) });
    toast("Pago reportado. Lo revisaremos para desbloquear tu wallet.");
    $("#paymentForm").reset();
    $("#debtPanel").classList.add("hidden");
  } catch (error) {
    toast(error.message);
  }
}

async function login(event) {
  event.preventDefault();
  try {
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: $("#loginEmail").value, password: $("#loginPassword").value }),
    });
    state.auth = { token: payload.token };
    sessionStorage.setItem("bipbipAuth", JSON.stringify(state.auth));
    await bootstrap();
  } catch (error) {
    toast(error.message);
  }
}

async function bootstrap() {
  const payload = await api("/api/bootstrap");
  state.user = payload.user;
  state.users = payload.users;
  state.settings = payload.settings;
  $("#sessionLabel").textContent = `${state.user.name} · ${state.user.role}`;
  document.body.dataset.role = state.user.role;
  applyPermissions();
  hydrateSettings();
  renderUsers();
  showAdmin();
  await loadCases();
}

function applyPermissions() {
  const manage = ["master", "admin"].includes(state.user?.role);
  $$(".manage-only").forEach((node) => node.classList.toggle("hidden", !manage));
  $$(".master-only").forEach((node) => node.classList.toggle("hidden", state.user?.role !== "master"));
  if (state.user?.role === "operaciones") {
    setBucket("desbloqueo");
  }
}

function hydrateSettings() {
  $("#setBank").value = state.settings.bank_name || "";
  $("#setHolder").value = state.settings.account_holder || "";
  $("#setAccount").value = state.settings.account_number || "";
  $("#setRif").value = state.settings.rif || "";
  $("#setInstructions").value = state.settings.instructions || "";
  renderPaymentAccountsEditor();
}

function setBucket(bucket) {
  state.currentBucket = bucket;
  state.currentView = "cases";
  $$(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.bucket === bucket));
  $$(".admin-view").forEach((view) => view.classList.remove("active"));
  $("#casesView").classList.add("active");
  $("#adminTitle").textContent = $(`[data-bucket="${bucket}"]`)?.textContent || "Casos";
  loadCases();
}

function setAdminView(view) {
  state.currentView = view;
  $$(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".admin-view").forEach((node) => node.classList.remove("active"));
  $(`#${view}View`).classList.add("active");
  $("#adminTitle").textContent = view === "settings" ? "Cuenta bancaria" : view === "summary" ? "Resumen" : "Usuarios";
  if (view === "summary") loadSummary();
}

async function loadCases() {
  if (state.currentView !== "cases") return;
  const q = $("#caseSearch").value.trim();
  const payload = await api(`/api/cases?bucket=${encodeURIComponent(state.currentBucket)}&q=${encodeURIComponent(q)}`);
  state.cases = payload.cases;
  renderMetrics();
  renderCases();
}

function filteredCases() {
  if (!state.statusFilter) return state.cases;
  return state.cases.filter((item) => item.status === state.statusFilter);
}

function renderMetrics() {
  const counts = state.cases.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  $("#metrics").innerHTML = [
    ["Casos", state.cases.length],
    ["Reportados", counts.pago_reportado || 0],
    ["Billetera Bs.", money(state.cases.filter((item) => item.status === "billetera_bipbip").reduce((sum, item) => sum + Number(item.payment?.amount_ves || 0), 0), "VES")],
    ["Conciliados", counts.conciliado || 0],
    ["Desbloqueados", counts.desbloqueado || 0],
  ].map(([label, value]) => `<div><strong>${value}</strong><span>${label}</span></div>`).join("");
}

function renderCases() {
  const cases = filteredCases();
  if (!cases.length) {
    $("#caseTable").innerHTML = `<div class="empty">No hay casos en esta bandeja.</div>`;
    return;
  }
  $("#caseTable").innerHTML = `
    <div class="table-head">
      <span>Conductor</span><span>Deuda</span><span>Pago</span><span>Estado</span><span></span>
    </div>
    ${cases.map(caseRow).join("")}
  `;
}

async function loadSummary() {
  const payload = await api("/api/summary");
  state.summary = payload;
  renderSummary();
}

function renderSummary() {
  const summary = state.summary || { rows: [], totals: {} };
  const totals = summary.totals || {};
  $("#summaryMetrics").innerHTML = [
    ["Casos", totals.case_count || 0],
    ["Deuda USD", money(totals.debt_usd, "USD")],
    ["Monto Bs.", money(totals.amount_ves, "VES")],
    ["Abonado USD", money(totals.paid_usd, "USD")],
    ["Pendiente USD", money(totals.pending_usd, "USD")],
  ].map(([label, value]) => `<div><strong>${escapeHtml(value)}</strong><span>${label}</span></div>`).join("");
  if (!summary.rows.length) {
    $("#summaryTable").innerHTML = `<div class="empty">No hay casos para resumir.</div>`;
    return;
  }
  $("#summaryTable").innerHTML = `
    <div class="summary-head">
      <span>Status</span><span>Casos</span><span>Deuda USD</span><span>Monto Bs.</span><span>Abonado USD</span><span>Pendiente USD</span>
    </div>
    ${summary.rows.map((row) => `
      <article class="summary-row">
        <span><span class="status ${escapeHtml(row.status)}">${escapeHtml(row.status_label || row.status)}</span></span>
        <strong>${Number(row.case_count || 0)}</strong>
        <strong>${money(row.debt_usd, "USD")}</strong>
        <strong>${money(row.amount_ves, "VES")}</strong>
        <strong>${money(row.paid_usd, "USD")}</strong>
        <strong>${money(row.pending_usd, "USD")}</strong>
      </article>
    `).join("")}
  `;
}

function caseRow(item) {
  const payment = item.payment || {};
  const alerts = parseAlerts(payment.alerts);
  const signal = signalPill(item);
  return `
    <article class="case-row">
      <div>
        <strong>${escapeHtml(item.name || "Sin nombre")}</strong>
        <small>${escapeHtml(item.cedula)} · ${escapeHtml(item.phone)} · ${escapeHtml(item.plate || "sin placa")}</small>
      </div>
      <div>
        <strong>${money(item.debt_ves, "VES")}</strong>
        <small>${money(item.debt_usd, "USD")} · pendiente ${money(item.pending_usd, "USD")} / ${money(item.pending_ves, "VES")}</small>
        ${signal}
      </div>
      <div>
        <strong>${escapeHtml(payment.reference || "-")}</strong>
        <small>${payment.amount_ves ? money(payment.amount_ves, "VES") : "sin reporte"} ${alerts.length ? `· ${alerts.length} alerta(s)` : ""}</small>
        <div class="call-pills">
          <span class="call-pill success">Exitosas ${Number(item.successful_call_count || 0)}</span>
          <span class="call-pill missed">Perdidas ${Number(item.missed_call_count || 0)}</span>
        </div>
      </div>
      <div><span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status_label || item.status)}</span></div>
      <button class="icon-action" type="button" data-open-case="${item.id}" title="Abrir detalle"><svg><use href="#i-eye"></use></svg></button>
    </article>
  `;
}

function signalPill(item) {
  if (item.is_fully_paid) return `<span class="ready-pill">Pagado completo</span>`;
  if (item.is_partial_paid) return `<span class="ready-pill partial">Pago parcial</span>`;
  if (item.ready_to_conciliate) return `<span class="ready-pill">Listo para conciliar</span>`;
  return "";
}

async function openCase(id) {
  const payload = await api(`/api/cases/${id}`);
  const item = payload.case;
  $("#modalTitle").textContent = `${item.cedula} · ${item.status_label}`;
  $("#caseDetail").innerHTML = renderCaseDetail(item);
  $("#caseModal").classList.remove("hidden");
}

function renderCaseDetail(item) {
  const payment = item.payment || {};
  const payments = item.payments || (payment.id ? [payment] : []);
  const alerts = parseAlerts(payment.alerts);
  const signal = signalPill(item);
  const canConciliate = ["master", "admin", "conciliacion"].includes(state.user.role);
  const canUnlock = ["master", "admin", "operaciones"].includes(state.user.role);
  const canDelete = state.user.role === "master";
  const statusOptions = [
    ["en_validacion", "En validacion"],
    ["pago_parcial", "Pago parcial"],
    ["billetera_bipbip", "Billetera BipBip"],
    ["conciliado", "Conciliado"],
    ["rechazado", "Rechazado"],
    ["fraudulento", "Fraude"],
    ["duplicado", "Duplicado"],
  ];
  return `
    <section class="detail-grid">
      <div><span>Cedula</span><strong>${escapeHtml(item.cedula)}</strong></div>
      <div><span>Telefono</span><strong>${escapeHtml(item.phone)}</strong></div>
      <div><span>Placa</span><strong>${escapeHtml(item.plate || "-")}</strong></div>
      <div><span>Driver ID</span><strong>${escapeHtml(item.driver_external_id || "-")}</strong></div>
      <div><span>Deuda</span><strong>${money(item.debt_ves, "VES")}</strong></div>
      <div><span>Abonado conciliado</span><strong>${money(item.paid_usd, "USD")} / ${money(item.paid_ves, "VES")}</strong></div>
      <div><span>Reportado por revisar</span><strong>${money(item.review_usd, "USD")} / ${money(item.review_ves, "VES")}</strong></div>
      <div><span>Falta por cubrir</span><strong>${money(item.pending_usd, "USD")} / ${money(item.pending_ves, "VES")}</strong></div>
      <div><span>Senal</span><strong>${signal || "Aun falta pago"}</strong></div>
    </section>
    <section class="detail-block">
      <h3>${payments.length > 1 ? "Pagos registrados" : "Pago reportado"}</h3>
      ${payment.id ? `
        ${payments.length > 1 ? `<p class="notes">Falta por cubrir usa la suma de estos pagos, no solo el ultimo.</p>` : ""}
        ${payments.length > 1 ? `<div class="payment-list">${payments.map(paymentHistoryRow).join("")}</div>` : ""}
        <div class="detail-grid">
          <div><span>Referencia</span><strong>${escapeHtml(payment.reference || "-")}</strong></div>
          <div><span>Monto reportado / validado</span><strong>${money(payment.amount_ves, "VES")}</strong></div>
          <div><span>Equivalente USD</span><strong>${money(payment.amount_usd_at_payment, "USD")}</strong></div>
          <div><span>Tasa del pago</span><strong>${Number(payment.rate_at_payment || 0).toLocaleString("es-VE", { minimumFractionDigits: 2 })}</strong></div>
          <div><span>Banco emisor</span><strong>${escapeHtml(payment.bank || "-")}</strong></div>
          <div><span>Telefono de pago</span><strong>${escapeHtml(payment.payment_phone || "-")}</strong></div>
          <div><span>Fecha de pago</span><strong>${escapeHtml(payment.payment_date || "-")}</strong></div>
        </div>
        <p class="notes">${escapeHtml(payment.observations || "")}</p>
        ${alerts.length ? `<div class="alert-list">${alerts.map((alert) => `<span>${labelAlert(alert)}</span>`).join("")}</div>` : ""}
        ${payment.attachment_url ? `<button class="file-link" type="button" data-preview-receipt="${escapeHtml(payment.attachment_url)}">Ver comprobante</button>` : ""}
      ` : `<p class="notes">El conductor aun no ha reportado pago.</p>`}
    </section>
    <section class="detail-block followup-block">
      <h3>Seguimiento</h3>
      <div class="call-pills">
        <span class="call-pill success">Llamadas exitosas ${Number(item.successful_call_count || 0)}</span>
        <span class="call-pill missed">Llamadas perdidas ${Number(item.missed_call_count || 0)}</span>
        <span class="call-pill neutral">Notas ${Number(item.followup_count || 0)}</span>
      </div>
      <label>Nota de contacto
        <textarea id="followupNotes" rows="3" placeholder="Ej. Se llamo al conductor, indico que pagara hoy en la tarde."></textarea>
      </label>
      <div class="action-row">
        <button type="button" data-followup-action="llamada_exitosa" data-followup-case="${item.id}">Llamada exitosa</button>
        <button class="secondary" type="button" data-followup-action="llamada_perdida" data-followup-case="${item.id}">Llamada perdida</button>
        <button class="secondary" type="button" data-followup-action="nota" data-followup-case="${item.id}">Guardar nota</button>
      </div>
    </section>
    ${canConciliate && payment.id ? `
      <form class="detail-block action-form" data-status-form="${item.id}">
        <h3>Conciliacion</h3>
        <div class="status-control-head">
          <span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status_label || item.status)}</span>
        </div>
        <label>Agente
          <input name="reconciliation_agent" value="${escapeHtml(payment.reconciliation_agent || state.user.name || "")}" required />
        </label>
        <label>Referencia conciliada
          <input name="validated_reference" value="${escapeHtml(payment.validated_reference || payment.reference || "")}" required />
        </label>
        <label>Monto validado Bs.
          <input name="validated_amount_ves" inputmode="decimal" value="${Number(payment.amount_ves || 0).toFixed(2)}" required />
        </label>
        <label>Adjuntar / reemplazar comprobante
          <input name="attachment_file" type="file" accept="image/*,application/pdf" />
        </label>
        <label>Cambiar estado
          <select name="status" required>
            ${statusOptions.map(([value, label]) => `<option value="${value}" ${item.status === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </label>
        <label>Notas internas
          <textarea name="notes" rows="3">${escapeHtml(payment.internal_notes || "")}</textarea>
        </label>
        <div class="action-row compact-actions">
          <button type="button" data-status-save><svg><use href="#i-check"></use></svg>Guardar estado</button>
        </div>
      </form>
    ` : ""}
    ${canDelete ? `
      <section class="detail-block danger-zone">
        <h3>Administracion</h3>
        <p class="notes">Para limpiar pruebas, borra solo el ultimo pago. Borrar caso elimina la deuda completa del conductor.</p>
        ${payment.id ? `<button class="secondary danger-action" type="button" data-delete-payment="${item.id}">Borrar ultimo pago</button>` : ""}
        <button class="secondary danger-action" type="button" data-delete-case="${item.id}">Borrar caso</button>
      </section>
    ` : ""}
    ${canUnlock && ["conciliado", "desbloqueado"].includes(item.status) ? `
      <section class="detail-block">
        <h3>Operaciones</h3>
        <button ${item.status === "desbloqueado" ? "disabled" : ""} type="button" data-unlock-case="${item.id}"><svg><use href="#i-lock"></use></svg>Marcar desbloqueado</button>
        <p class="notes">${item.unlocked_at ? `Desbloqueado: ${shortDate(item.unlocked_at)}` : "Pendiente por desbloquear."}</p>
      </section>
    ` : ""}
    <section class="detail-block">
      <h3>Historial</h3>
      <div class="timeline">
        ${(item.events || []).map((event) => `
          <div>
            <strong>${escapeHtml(event.event_type)}</strong>
            <span>${shortDate(event.created_at)} · ${escapeHtml(event.user_name || "sistema")}</span>
            <p>${escapeHtml(event.notes || "")}</p>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function paymentHistoryRow(payment) {
  return `
    <article class="payment-history-row">
      <div>
        <strong>${money(payment.amount_ves, "VES")}</strong>
        <span>${money(payment.amount_usd_at_payment, "USD")} · ${escapeHtml(payment.payment_date || shortDate(payment.created_at) || "-")}</span>
      </div>
      <div>
        <strong>${escapeHtml(payment.reference || "-")}</strong>
        <span>${escapeHtml(payment.reconciliation_agent || "Sin agente")}</span>
      </div>
      <span class="status ${escapeHtml(payment.status || "")}">${escapeHtml(state.statuses?.[payment.status] || payment.status || "-")}</span>
    </article>
  `;
}

function labelAlert(alert) {
  const labels = {
    referencia_duplicada: "Referencia duplicada",
    monto_no_coincide: "Monto no coincide",
    pago_desde_tercero: "Pago desde tercero",
    posible_recarga_wallet: "Posible recarga wallet",
    falta_referencia: "Falta referencia",
  };
  return labels[alert] || alert;
}

async function updateCaseStatus(form) {
  const id = form.dataset.statusForm;
  const attachmentFile = await readFile(form.elements.attachment_file);
  const payload = {
    status: form.elements.status.value,
    validated_reference: form.elements.validated_reference.value.trim(),
    validated_amount_ves: form.elements.validated_amount_ves?.value.trim() || "",
    reconciliation_agent: form.elements.reconciliation_agent?.value.trim() || "",
    notes: form.elements.notes.value.trim(),
    attachment_file: attachmentFile,
  };
  try {
    await api(`/api/cases/${id}/status`, { method: "POST", body: JSON.stringify(payload) });
    toast("Estado actualizado.");
    $("#caseModal").classList.add("hidden");
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

async function deleteCase(id) {
  const confirmed = window.confirm("¿Borrar este caso completo? Esto elimina la deuda del conductor en el portal.");
  if (!confirmed) return;
  try {
    await api(`/api/cases/${id}/delete`, { method: "POST", body: "{}" });
    toast("Caso borrado.");
    $("#caseModal").classList.add("hidden");
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

async function deleteLastPayment(id) {
  const confirmed = window.confirm("¿Borrar solo el ultimo pago reportado? La deuda del conductor se mantiene.");
  if (!confirmed) return;
  try {
    await api(`/api/cases/${id}/delete-payment`, { method: "POST", body: "{}" });
    toast("Pago borrado. La deuda sigue activa.");
    $("#caseModal").classList.add("hidden");
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

function openReceiptPreview(url) {
  const signedUrl = authenticatedUrl(url);
  const cleanUrl = url.split("?")[0].toLowerCase();
  const image = $("#receiptPreviewImage");
  const frame = $("#receiptPreviewFrame");
  const fallback = $("#receiptPreviewFallback");
  const link = $("#receiptPreviewLink");
  image.classList.add("hidden");
  frame.classList.add("hidden");
  fallback.classList.add("hidden");
  image.src = "";
  frame.src = "";
  link.href = signedUrl;
  if (cleanUrl.endsWith(".pdf")) {
    frame.src = signedUrl;
    frame.classList.remove("hidden");
  } else {
    image.src = signedUrl;
    image.classList.remove("hidden");
  }
  $("#receiptModal").classList.remove("hidden");
}

async function addFollowup(id, type) {
  const notes = $("#followupNotes")?.value.trim() || "";
  if (type === "nota" && !notes) {
    toast("Escribe una nota para guardarla.");
    return;
  }
  try {
    await api(`/api/cases/${id}/followup`, {
      method: "POST",
      body: JSON.stringify({ type, notes }),
    });
    toast("Seguimiento guardado.");
    await openCase(id);
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

async function unlockCase(id) {
  try {
    await api(`/api/cases/${id}/unlock`, { method: "POST", body: "{}" });
    toast("Caso marcado como desbloqueado.");
    $("#caseModal").classList.add("hidden");
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const accounts = readPaymentAccountsEditor();
    if (!accounts.length) {
      toast("Agrega al menos una cuenta de pago.");
      return;
    }
    const payload = await api("/api/settings/save", {
      method: "POST",
      body: JSON.stringify({
        bank_name: $("#setBank").value,
        account_holder: $("#setHolder").value,
        account_number: $("#setAccount").value,
        rif: $("#setRif").value,
        instructions: $("#setInstructions").value,
        payment_accounts: accounts,
      }),
    });
    state.settings = payload.settings;
    hydrateSettings();
    toast("Datos bancarios guardados.");
  } catch (error) {
    toast(error.message);
  }
}

function blankPaymentAccount(type = "Pago movil") {
  return {
    type,
    bank_name: "",
    account_holder: "",
    account_number: "",
    rif: "",
    phone: "",
    document: "",
    instructions: "",
  };
}

function renderPaymentAccountsEditor() {
  const accounts = paymentAccounts();
  $("#paymentAccountsEditor").innerHTML = accounts.map((account, index) => `
    <article class="payment-account-editor" data-account-index="${index}">
      <div class="section-head compact">
        <h3>${escapeHtml(account.type || `Cuenta ${index + 1}`)}</h3>
        <button class="secondary" type="button" data-remove-payment-account="${index}">Eliminar</button>
      </div>
      <div class="form-grid">
        <label>Tipo de pago
          <select data-account-field="type" required>
            <option value="Pago movil" ${account.type === "Pago movil" ? "selected" : ""}>Pago movil</option>
            <option value="Transferencia a cuenta indicada" ${account.type === "Transferencia a cuenta indicada" ? "selected" : ""}>Transferencia a cuenta indicada</option>
          </select>
        </label>
        <label>Banco<input data-account-field="bank_name" value="${escapeHtml(account.bank_name || "")}" required /></label>
        <label>Titular<input data-account-field="account_holder" value="${escapeHtml(account.account_holder || "")}" required /></label>
        <label>Numero de cuenta<input data-account-field="account_number" value="${escapeHtml(account.account_number || "")}" /></label>
        <label>RIF<input data-account-field="rif" value="${escapeHtml(account.rif || "")}" /></label>
        <label>Telefono pago movil<input data-account-field="phone" value="${escapeHtml(account.phone || "")}" placeholder="4141234567" /></label>
        <label>Documento pago movil<input data-account-field="document" value="${escapeHtml(account.document || "")}" placeholder="J-00000000-0" /></label>
        <label class="span-2">Instrucciones<textarea data-account-field="instructions" rows="3">${escapeHtml(account.instructions || "")}</textarea></label>
      </div>
    </article>
  `).join("");
}

function readPaymentAccountsEditor() {
  return Array.from(document.querySelectorAll(".payment-account-editor")).map((card) => {
    const account = {};
    card.querySelectorAll("[data-account-field]").forEach((field) => {
      account[field.dataset.accountField] = field.value.trim();
    });
    return account;
  }).filter((account) => account.type && account.bank_name && account.account_holder);
}

function renderUsers() {
  $("#usersTable").innerHTML = state.users.map((user) => `
    <article class="user-row">
      <div><strong>${escapeHtml(user.name)}</strong><small>${escapeHtml(user.email)}</small></div>
      <span class="status">${escapeHtml(user.role)}</span>
      <span>${user.active ? "Activo" : "Inactivo"}</span>
      <button class="secondary" type="button" data-edit-user="${user.id}">Editar</button>
    </article>
  `).join("");
}

function openUser(user = {}) {
  $("#userId").value = user.id || "";
  $("#userName").value = user.name || "";
  $("#userEmail").value = user.email || "";
  $("#userRole").value = user.role || "conciliacion";
  $("#userPassword").value = "";
  $("#userActive").checked = user.active !== false;
  $("#userModal").classList.remove("hidden");
}

async function saveUser(event) {
  event.preventDefault();
  try {
    await api("/api/users/save", {
      method: "POST",
      body: JSON.stringify({
        id: $("#userId").value,
        name: $("#userName").value,
        email: $("#userEmail").value,
        role: $("#userRole").value,
        password: $("#userPassword").value,
        active: $("#userActive").checked,
      }),
    });
    $("#userModal").classList.add("hidden");
    const payload = await api("/api/bootstrap");
    state.users = payload.users;
    renderUsers();
    toast("Usuario guardado.");
  } catch (error) {
    toast(error.message);
  }
}

async function syncDebts() {
  try {
    const payload = await api("/api/sync/debts", { method: "POST", body: "{}" });
    toast(payload.imported ? `Sincronizados ${payload.imported} deudores.` : "Sincronizacion ejecutada.");
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

function exportCsv() {
  const q = $("#caseSearch").value.trim();
  const url = `/api/export?bucket=${encodeURIComponent(state.currentBucket)}&q=${encodeURIComponent(q)}&token=${encodeURIComponent(state.auth.token)}`;
  window.open(url, "_blank");
}

function bindEvents() {
  $("#openAdminBtn").addEventListener("click", showLogin);
  $("#backPublicBtn").addEventListener("click", showPublic);
  $("#lookupForm").addEventListener("submit", lookupDebt);
  $("#paymentForm").addEventListener("submit", submitPayment);
  $("#loginForm").addEventListener("submit", login);
  $("#logoutBtn").addEventListener("click", () => {
    sessionStorage.removeItem("bipbipAuth");
    state.auth = null;
    showPublic();
  });
  $("#adminNav").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.hasAttribute("data-bucket")) setBucket(button.dataset.bucket);
    if (button.dataset.view) setAdminView(button.dataset.view);
  });
  $("#caseSearch").addEventListener("input", () => {
    window.clearTimeout(window.searchTimer);
    window.searchTimer = window.setTimeout(loadCases, 250);
  });
  $("#statusFilter").addEventListener("change", (event) => {
    state.statusFilter = event.target.value;
    renderCases();
  });
  $("#caseTable").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-case]");
    if (button) openCase(button.dataset.openCase);
  });
  $("#caseDetail").addEventListener("click", (event) => {
    const previewButton = event.target.closest("[data-preview-receipt]");
    if (previewButton) openReceiptPreview(previewButton.dataset.previewReceipt);
    const statusButton = event.target.closest("[data-status-save]");
    if (statusButton) updateCaseStatus(statusButton.closest("form"));
    const followupButton = event.target.closest("[data-followup-action]");
    if (followupButton) addFollowup(followupButton.dataset.followupCase, followupButton.dataset.followupAction);
    const unlockButton = event.target.closest("[data-unlock-case]");
    if (unlockButton) unlockCase(unlockButton.dataset.unlockCase);
    const deletePaymentButton = event.target.closest("[data-delete-payment]");
    if (deletePaymentButton) deleteLastPayment(deletePaymentButton.dataset.deletePayment);
    const deleteButton = event.target.closest("[data-delete-case]");
    if (deleteButton) deleteCase(deleteButton.dataset.deleteCase);
  });
  $("#caseDetail").addEventListener("submit", (event) => {
    const form = event.target.closest("[data-status-form]");
    if (!form) return;
    event.preventDefault();
    updateCaseStatus(form);
  });
  $$("[data-close-modal]").forEach((node) => node.addEventListener("click", () => $("#caseModal").classList.add("hidden")));
  $$("[data-close-user]").forEach((node) => node.addEventListener("click", () => $("#userModal").classList.add("hidden")));
  $$("[data-close-receipt]").forEach((node) => node.addEventListener("click", () => {
    $("#receiptModal").classList.add("hidden");
    $("#receiptPreviewImage").src = "";
    $("#receiptPreviewFrame").src = "";
  }));
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#receiptPreviewImage").addEventListener("error", () => {
    $("#receiptPreviewImage").classList.add("hidden");
    $("#receiptPreviewFallback").classList.remove("hidden");
  });
  $("#addPaymentAccountBtn").addEventListener("click", () => {
    const accounts = readPaymentAccountsEditor();
    accounts.push(blankPaymentAccount(accounts.some((account) => account.type === "Pago movil") ? "Transferencia a cuenta indicada" : "Pago movil"));
    state.settings.payment_accounts = accounts;
    renderPaymentAccountsEditor();
  });
  $("#paymentAccountsEditor").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-payment-account]");
    if (!button) return;
    const removeIndex = Number(button.dataset.removePaymentAccount);
    const accounts = readPaymentAccountsEditor().filter((_, index) => index !== removeIndex);
    state.settings.payment_accounts = accounts.length ? accounts : [blankPaymentAccount()];
    renderPaymentAccountsEditor();
  });
  $("#newUserBtn").addEventListener("click", () => openUser());
  $("#usersTable").addEventListener("click", (event) => {
    const button = event.target.closest("[data-edit-user]");
    if (!button) return;
    const user = state.users.find((item) => String(item.id) === String(button.dataset.editUser));
    openUser(user);
  });
  $("#userForm").addEventListener("submit", saveUser);
  $("#syncBtn").addEventListener("click", syncDebts);
  $("#exportBtn").addEventListener("click", exportCsv);
}

bindEvents();
loadPublicConfig().catch(() => {});
if (state.auth?.token) {
  bootstrap().catch(() => showPublic());
}
