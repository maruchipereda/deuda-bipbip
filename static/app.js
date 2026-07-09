const state = {
  auth: JSON.parse(sessionStorage.getItem("bipbipAuth") || "null"),
  user: null,
  users: [],
  settings: {},
  cases: [],
  currentBucket: "pendientes",
  currentView: "cases",
  currentDriver: null,
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
    throw new Error(payload.error || payload.details || "Request fallido");
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
  $("#bankName").textContent = state.settings.bank_name || "-";
  $("#accountHolder").textContent = state.settings.account_holder || "-";
  $("#accountNumber").textContent = state.settings.account_number || "-";
  $("#rif").textContent = state.settings.rif || "-";
  $("#bankInstructions").textContent = state.settings.instructions || "";
}

async function lookupDebt(event) {
  event.preventDefault();
  const cedula = $("#lookupCedula").value.trim();
  const phone = $("#lookupPhone").value.trim();
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
  $("#caseStatus").textContent = driver.status_label || driver.status;
  $("#payCedula").value = driver.cedula || "";
  $("#payPhone").value = state.lookupPhone || driver.phone || "";
  $("#payPlate").value = driver.plate || "";
  $("#payAmount").value = Number(driver.debt_ves || 0).toFixed(2);
  $("#payDate").value = new Date().toISOString().slice(0, 10);
}

async function submitPayment(event) {
  event.preventDefault();
  const file = await readFile($("#payFile"));
  const payload = {
    cedula: $("#payCedula").value.trim(),
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
  $("#adminTitle").textContent = view === "settings" ? "Cuenta bancaria" : "Usuarios";
}

async function loadCases() {
  if (state.currentView !== "cases") return;
  const q = $("#caseSearch").value.trim();
  const payload = await api(`/api/cases?bucket=${encodeURIComponent(state.currentBucket)}&q=${encodeURIComponent(q)}`);
  state.cases = payload.cases;
  renderMetrics();
  renderCases();
}

function renderMetrics() {
  const counts = state.cases.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  $("#metrics").innerHTML = [
    ["Casos", state.cases.length],
    ["Reportados", counts.pago_reportado || 0],
    ["Conciliados", counts.conciliado || 0],
    ["Desbloqueados", counts.desbloqueado || 0],
  ].map(([label, value]) => `<div><strong>${value}</strong><span>${label}</span></div>`).join("");
}

function renderCases() {
  if (!state.cases.length) {
    $("#caseTable").innerHTML = `<div class="empty">No hay casos en esta bandeja.</div>`;
    return;
  }
  $("#caseTable").innerHTML = `
    <div class="table-head">
      <span>Conductor</span><span>Deuda</span><span>Pago</span><span>Estado</span><span></span>
    </div>
    ${state.cases.map(caseRow).join("")}
  `;
}

function caseRow(item) {
  const payment = item.payment || {};
  const alerts = parseAlerts(payment.alerts);
  return `
    <article class="case-row">
      <div>
        <strong>${escapeHtml(item.name || "Sin nombre")}</strong>
        <small>${escapeHtml(item.cedula)} · ${escapeHtml(item.phone)} · ${escapeHtml(item.plate || "sin placa")}</small>
      </div>
      <div>
        <strong>${money(item.debt_ves, "VES")}</strong>
        <small>${money(item.debt_usd, "USD")} · tasa ${Number(item.rate || 0).toFixed(2)}</small>
      </div>
      <div>
        <strong>${escapeHtml(payment.reference || "-")}</strong>
        <small>${payment.amount_ves ? money(payment.amount_ves, "VES") : "sin reporte"} ${alerts.length ? `· ${alerts.length} alerta(s)` : ""}</small>
      </div>
      <div><span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status_label || item.status)}</span></div>
      <button class="icon-action" type="button" data-open-case="${item.id}" title="Abrir detalle"><svg><use href="#i-eye"></use></svg></button>
    </article>
  `;
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
  const alerts = parseAlerts(payment.alerts);
  const canConciliate = ["master", "admin", "conciliacion"].includes(state.user.role);
  const canUnlock = ["master", "admin", "operaciones"].includes(state.user.role);
  return `
    <section class="detail-grid">
      <div><span>Nombre</span><strong>${escapeHtml(item.name || "-")}</strong></div>
      <div><span>Cedula</span><strong>${escapeHtml(item.cedula)}</strong></div>
      <div><span>Telefono</span><strong>${escapeHtml(item.phone)}</strong></div>
      <div><span>Placa</span><strong>${escapeHtml(item.plate || "-")}</strong></div>
      <div><span>Driver ID</span><strong>${escapeHtml(item.driver_external_id || "-")}</strong></div>
      <div><span>Deuda</span><strong>${money(item.debt_ves, "VES")}</strong></div>
    </section>
    <section class="detail-block">
      <h3>Pago reportado</h3>
      ${payment.id ? `
        <div class="detail-grid">
          <div><span>Referencia</span><strong>${escapeHtml(payment.reference || "-")}</strong></div>
          <div><span>Monto</span><strong>${money(payment.amount_ves, "VES")}</strong></div>
          <div><span>Banco emisor</span><strong>${escapeHtml(payment.bank || "-")}</strong></div>
          <div><span>Telefono de pago</span><strong>${escapeHtml(payment.payment_phone || "-")}</strong></div>
          <div><span>Fecha de pago</span><strong>${escapeHtml(payment.payment_date || "-")}</strong></div>
          <div><span>Confianza</span><strong>${escapeHtml(payment.match_confidence || "bajo")}</strong></div>
        </div>
        <p class="notes">${escapeHtml(payment.observations || "")}</p>
        ${alerts.length ? `<div class="alert-list">${alerts.map((alert) => `<span>${labelAlert(alert)}</span>`).join("")}</div>` : ""}
        ${payment.attachment_url ? `<a class="file-link" href="${payment.attachment_url}" target="_blank">Ver comprobante</a>` : ""}
      ` : `<p class="notes">El conductor aun no ha reportado pago.</p>`}
    </section>
    ${canConciliate && payment.id ? `
      <form class="detail-block action-form" data-status-form="${item.id}">
        <h3>Conciliacion</h3>
        <label>Referencia conciliada
          <input name="validated_reference" value="${escapeHtml(payment.validated_reference || payment.reference || "")}" required />
        </label>
        <label>Notas internas
          <textarea name="notes" rows="3">${escapeHtml(payment.internal_notes || "")}</textarea>
        </label>
        <div class="action-row">
          <button type="button" data-status-action="en_validacion">En validacion</button>
          <button type="button" data-status-action="conciliado"><svg><use href="#i-check"></use></svg>Conciliado</button>
          <button class="secondary" type="button" data-status-action="revision_manual">Revision manual</button>
          <button class="secondary" type="button" data-status-action="rechazado"><svg><use href="#i-x"></use></svg>Rechazar</button>
          <button class="secondary" type="button" data-status-action="fraudulento">Fraude</button>
        </div>
      </form>
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

async function updateCaseStatus(form, status) {
  const id = form.dataset.statusForm;
  const payload = {
    status,
    validated_reference: form.elements.validated_reference.value.trim(),
    notes: form.elements.notes.value.trim(),
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
    const payload = await api("/api/settings/save", {
      method: "POST",
      body: JSON.stringify({
        bank_name: $("#setBank").value,
        account_holder: $("#setHolder").value,
        account_number: $("#setAccount").value,
        rif: $("#setRif").value,
        instructions: $("#setInstructions").value,
      }),
    });
    state.settings = payload.settings;
    toast("Datos bancarios guardados.");
  } catch (error) {
    toast(error.message);
  }
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
    if (button.dataset.bucket) setBucket(button.dataset.bucket);
    if (button.dataset.view) setAdminView(button.dataset.view);
  });
  $("#caseSearch").addEventListener("input", () => {
    window.clearTimeout(window.searchTimer);
    window.searchTimer = window.setTimeout(loadCases, 250);
  });
  $("#caseTable").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-case]");
    if (button) openCase(button.dataset.openCase);
  });
  $("#caseDetail").addEventListener("click", (event) => {
    const statusButton = event.target.closest("[data-status-action]");
    if (statusButton) updateCaseStatus(statusButton.closest("form"), statusButton.dataset.statusAction);
    const unlockButton = event.target.closest("[data-unlock-case]");
    if (unlockButton) unlockCase(unlockButton.dataset.unlockCase);
  });
  $$("[data-close-modal]").forEach((node) => node.addEventListener("click", () => $("#caseModal").classList.add("hidden")));
  $$("[data-close-user]").forEach((node) => node.addEventListener("click", () => $("#userModal").classList.add("hidden")));
  $("#settingsForm").addEventListener("submit", saveSettings);
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
