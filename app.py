#!/usr/bin/env python3
import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
RAILWAY_DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data"))
DEFAULT_DATA_DIR = RAILWAY_DATA_DIR if RAILWAY_DATA_DIR.exists() else ROOT
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", DEFAULT_DATA_DIR / "uploads"))
DB_PATH = Path(os.environ.get("DB_PATH", DEFAULT_DATA_DIR / "deuda_bipbip.db"))
DEFAULT_SHEET_ID = "1DcX_PW9xfqs9eCpVl6uqng4hG1Q1ewfAYwrtiuNpOFU"
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID)
DEBT_SHEET_NAME = os.environ.get("GOOGLE_DEBT_SHEET", "Deuda")
CONCILIATED_SHEET_NAME = os.environ.get("GOOGLE_CONCILIATED_SHEET", "Conciliados")
PORTAL_CONFIG_SHEET_NAME = os.environ.get("GOOGLE_PORTAL_CONFIG_SHEET", "PortalConfig")
PORTAL_PAYMENTS_SHEET_NAME = os.environ.get("GOOGLE_PORTAL_PAYMENTS_SHEET", "PortalPagos")
SYNC_TIMEZONE = ZoneInfo(os.environ.get("SYNC_TIMEZONE", "America/Caracas"))
SYNC_VERSION = "2026-07-09-b-phone-c-cedula-money"
AUTH_TOKENS = {}
DB_LOCK = threading.Lock()

CASE_STATUSES = {
    "pendiente_pago": "Pendiente de pago",
    "pago_reportado": "Pago reportado",
    "en_validacion": "En validacion",
    "pago_parcial": "Pago parcial",
    "conciliado": "Conciliado",
    "rechazado": "Rechazado",
    "duplicado": "Duplicado / conflicto",
    "fraudulento": "Fraudulento",
    "revision_manual": "Revision manual",
    "desbloqueado": "Desbloqueado",
}

BUCKETS = {
    "pendientes": ["pendiente_pago"],
    "reportados": ["pago_reportado"],
    "validacion": ["en_validacion"],
    "conciliados": ["conciliado"],
    "rechazados": ["rechazado"],
    "duplicados": ["duplicado", "fraudulento"],
    "desbloqueo": ["conciliado", "desbloqueado"],
}

PAID_PAYMENT_STATUSES = ("pago_parcial", "conciliado")

CONCILIATED_HEADERS = [
    "nombre",
    "cedula",
    "telefono",
    "placa",
    "driver_id",
    "monto_conciliado",
    "referencia_validada",
    "fecha_conciliacion",
    "agente_conciliacion",
    "estado_conciliacion",
    "conciliado",
    "estado_desbloqueo",
    "fecha_desbloqueo",
    "observaciones",
]

PORTAL_CONFIG_HEADERS = ["key", "value", "updated_at"]
PORTAL_PAYMENT_HEADERS = [
    "backup_key",
    "driver_cedula",
    "driver_phone",
    "driver_name",
    "driver_plate",
    "driver_external_id",
    "debt_usd",
    "debt_ves",
    "rate",
    "driver_status",
    "cedula_reportada",
    "payment_phone",
    "plate_reportada",
    "amount_ves",
    "reference",
    "reference_norm",
    "bank",
    "payment_date",
    "payment_method",
    "observations",
    "attachment_path",
    "attachment_name",
    "attachment_type",
    "payment_status",
    "alerts_json",
    "internal_notes",
    "reconciliation_agent",
    "validated_reference",
    "validated_by_email",
    "validated_at",
    "created_at",
    "updated_at",
]
TRANSIENT_SETTING_KEYS = {"sync_status", "debt_sync_local_date", "debt_sync_version"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def local_sync_date():
    return datetime.now(SYNC_TIMEZONE).date().isoformat()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def row_to_dict(row):
    return dict(row) if row else None


def clean_text(value):
    return str(value or "").strip()


def normalize_digits(value):
    return "".join(char for char in clean_text(value) if char.isdigit())


def phone_variants(value):
    digits = normalize_digits(value)
    variants = {digits}
    if digits.startswith("58") and len(digits) >= 12:
        variants.add(digits[2:])
    if digits.startswith("0") and len(digits) >= 11:
        variants.add(digits[1:])
    for item in list(variants):
        if item and not item.startswith("0"):
            variants.add(f"0{item}")
        if len(item) == 10 and item.startswith("4"):
            variants.add(f"58{item}")
            variants.add(f"0{item}")
    return {item for item in variants if item}


def normalize_reference(value):
    return "".join(char for char in clean_text(value).upper() if char.isalnum())


def parse_money(value):
    raw = clean_text(value)
    if not raw:
        return 0.0
    negative = "-" in raw or ("(" in raw and ")" in raw)
    raw = re.sub(r"[^\d,.\-]", "", raw).replace("-", "")
    if not raw:
        return 0.0
    last_dot = raw.rfind(".")
    last_comma = raw.rfind(",")
    last_sep = max(last_dot, last_comma)
    if last_sep >= 0:
        integer = re.sub(r"[^\d]", "", raw[:last_sep])
        decimal = re.sub(r"[^\d]", "", raw[last_sep + 1:])
        if decimal:
            raw = f"{integer or '0'}.{decimal}"
        else:
            raw = integer or "0"
    else:
        raw = re.sub(r"[^\d]", "", raw)
    try:
        amount = round(float(raw), 2)
        return -amount if negative else amount
    except ValueError:
        return 0.0


def money(value):
    return round(float(value or 0), 2)


def hash_password(password):
    return hashlib.sha256(str(password).encode("utf-8")).hexdigest()


def parse_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8") or "{}")


def send_json(handler, payload, status=200):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_text(handler, text, content_type, status=200, headers=None):
    data = text.encode("utf-8-sig")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def send_file(handler, path, content_type):
    if not path.exists():
        return send_json(handler, {"error": "No encontrado"}, 404)
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_token(user):
    token = uuid.uuid4().hex
    AUTH_TOKENS[token] = {"user_id": user["id"], "created_at": now_iso()}
    return token


def public_user(row):
    data = row_to_dict(row)
    if data:
        data.pop("password_hash", None)
        data["active"] = bool(data.get("active"))
    return data


def auth_context(handler):
    header = handler.headers.get("Authorization", "")
    token = ""
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()
    else:
        token = parse_qs(urlparse(handler.path).query).get("token", [""])[0]
    session = AUTH_TOKENS.get(token)
    if not session:
        return None
    with db() as con:
        row = con.execute(
            "select * from users where id = ? and active = 1",
            (session["user_id"],),
        ).fetchone()
    return public_user(row)


def require_user(handler):
    user = auth_context(handler)
    if not user:
        send_json(handler, {"error": "No autorizado"}, 401)
        return None
    return user


def can_manage(user):
    return user["role"] in ("master", "admin")


def can_conciliate(user):
    return user["role"] in ("master", "admin", "conciliacion")


def can_unlock(user):
    return user["role"] in ("master", "admin", "operaciones")


def add_event(con, driver_id, user_id, event_type, notes="", payload=None):
    con.execute(
        """
        insert into audit_events (driver_id, user_id, event_type, notes, payload_json, created_at)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            driver_id,
            user_id,
            event_type,
            clean_text(notes),
            json.dumps(payload or {}, ensure_ascii=False),
            now_iso(),
        ),
    )


def save_upload(file_info):
    if not file_info or not file_info.get("data"):
        return None, None, None
    raw = file_info["data"]
    if "," in raw:
        raw = raw.split(",", 1)[1]
    data = base64.b64decode(raw)
    if not data:
        return None, None, None
    original = Path(file_info.get("name") or "comprobante").name
    suffix = Path(original).suffix[:12] or ".bin"
    stored = f"comprobante-{uuid.uuid4().hex}{suffix}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / stored
    target.write_bytes(data)
    content_type = file_info.get("type") or mimetypes.guess_type(original)[0] or "application/octet-stream"
    return str(Path("uploads") / stored), original, content_type


def get_settings(con):
    rows = con.execute("select key, value from settings").fetchall()
    data = {row["key"]: row["value"] for row in rows}
    legacy_account = {
        "type": "Transferencia a cuenta indicada",
        "bank_name": data.get("bank_name", "Banco por configurar"),
        "account_holder": data.get("account_holder", "BipBip"),
        "account_number": data.get("account_number", "0000-0000-00-0000000000"),
        "rif": data.get("rif", "J-00000000-0"),
        "phone": "",
        "document": data.get("rif", "J-00000000-0"),
        "instructions": "Usa esta cuenta solo para transferencia bancaria.",
    }
    defaults = {
        "bank_name": "Banco por configurar",
        "account_number": "0000-0000-00-0000000000",
        "rif": "J-00000000-0",
        "account_holder": "BipBip",
        "instructions": "Paga exactamente el monto indicado, guarda el comprobante y reportalo aqui. No recargues tu billetera.",
        "payment_accounts_json": json.dumps([
            {
                "type": "Pago movil",
                "bank_name": "Banco por configurar",
                "account_holder": "BipBip",
                "account_number": "",
                "rif": "J-00000000-0",
                "phone": "0000000000",
                "document": "J-00000000-0",
                "instructions": "Realiza un pago movil por el monto exacto indicado.",
            },
            legacy_account,
        ], ensure_ascii=False),
        "sync_status": "Sin sincronizacion todavia",
        "debt_sync_local_date": "",
        "debt_sync_version": "",
    }
    defaults.update(data)
    try:
        accounts = json.loads(defaults.get("payment_accounts_json") or "[]")
    except json.JSONDecodeError:
        accounts = []
    if not accounts:
        accounts = [legacy_account]
    defaults["payment_accounts"] = accounts
    return defaults


def set_setting(con, key, value):
    con.execute(
        """
        insert into settings (key, value, updated_at)
        values (?, ?, ?)
        on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, clean_text(value), now_iso()),
    )


def latest_payment_select():
    return """
        select payments.*
        from payments
        where payments.driver_id = drivers.id
        order by payments.created_at desc
        limit 1
    """


def public_payment(row):
    data = row_to_dict(row)
    if not data:
        return None
    data["amount_ves"] = money(data.get("amount_ves"))
    try:
        data["alerts"] = json.loads(data.get("alerts_json") or "[]")
    except json.JSONDecodeError:
        data["alerts"] = []
    data["attachment_url"] = "/" + data["attachment_path"] if data.get("attachment_path") else ""
    return data


def public_driver(row, include_events=False):
    data = row_to_dict(row)
    if not data:
        return None
    paid_ves = money(data.get("paid_ves"))
    pending_ves = max(0.0, money(data.get("debt_ves")) - paid_ves)
    rate = money(data.get("rate"))
    paid_usd = paid_ves / rate if rate else 0.0
    pending_usd = max(0.0, money(data.get("debt_usd")) - paid_usd)
    data["debt_usd"] = money(data.get("debt_usd"))
    data["debt_ves"] = money(data.get("debt_ves"))
    data["rate"] = rate
    data["paid_ves"] = paid_ves
    data["paid_usd"] = paid_usd
    data["pending_ves"] = pending_ves
    data["pending_usd"] = pending_usd
    data["successful_call_count"] = int(data.get("successful_call_count") or 0)
    data["missed_call_count"] = int(data.get("missed_call_count") or 0)
    data["followup_count"] = int(data.get("followup_count") or 0)
    data["status_label"] = CASE_STATUSES.get(data.get("status"), data.get("status"))
    if data.get("payment_json"):
        try:
            data["payment"] = public_payment(sqlite3.Row)  # never used, keeps linter quiet
        except TypeError:
            data["payment"] = json.loads(data["payment_json"])
    else:
        data["payment"] = None
    data.pop("payment_json", None)
    if include_events:
        with db() as con:
            data["events"] = [
                row_to_dict(row)
                for row in con.execute(
                    """
                    select audit_events.*, users.name as user_name
                    from audit_events
                    left join users on users.id = audit_events.user_id
                    where audit_events.driver_id = ?
                    order by audit_events.created_at desc
                    """,
                    (data["id"],),
                )
            ]
    return data


def driver_with_latest_payment(con, where="", params=None):
    return con.execute(
        f"""
        select drivers.*,
               (
                   select json_object(
                       'id', payments.id,
                       'amount_ves', payments.amount_ves,
                       'reference', payments.reference,
                       'bank', payments.bank,
                       'payment_phone', payments.payment_phone,
                       'payment_date', payments.payment_date,
                       'payment_method', payments.payment_method,
                       'observations', payments.observations,
                       'status', payments.status,
                       'alerts', payments.alerts_json,
                       'internal_notes', payments.internal_notes,
                       'reconciliation_agent', payments.reconciliation_agent,
                       'attachment_url', case when payments.attachment_path is null then '' else '/' || payments.attachment_path end,
                       'created_at', payments.created_at,
                       'validated_reference', payments.validated_reference,
                       'validated_at', payments.validated_at,
                       'validator_name', validator.name
                       )
                   from payments
                   left join users validator on validator.id = payments.validated_by
                   where payments.driver_id = drivers.id
                   order by payments.created_at desc
                   limit 1
               ) as payment_json,
               (
                   select count(*)
                   from audit_events
                   where audit_events.driver_id = drivers.id and audit_events.event_type = 'llamada_exitosa'
               ) as successful_call_count,
               (
                   select count(*)
                   from audit_events
                   where audit_events.driver_id = drivers.id and audit_events.event_type = 'llamada_perdida'
               ) as missed_call_count,
               (
                   select count(*)
                   from audit_events
                   where audit_events.driver_id = drivers.id and audit_events.event_type in ('nota_seguimiento', 'llamada_exitosa', 'llamada_perdida')
               ) as followup_count,
               coalesce((
                   select sum(payments.amount_ves)
                   from payments
                   where payments.driver_id = drivers.id and payments.status in ('pago_parcial', 'conciliado')
               ), 0) as paid_ves
        from drivers
        {where}
        order by drivers.updated_at desc
        """,
        params or [],
    )


def list_cases(user, query):
    bucket = clean_text(query.get("bucket", [""])[0])
    search = clean_text(query.get("q", [""])[0])
    statuses = BUCKETS.get(bucket, [])
    if user["role"] == "operaciones":
        statuses = ["conciliado", "desbloqueado"]
    params = []
    where = []
    if statuses:
        where.append("drivers.status in ({})".format(",".join("?" for _ in statuses)))
        params.extend(statuses)
    if search:
        where.append(
            "(drivers.cedula like ? or drivers.phone like ? or drivers.plate like ? or drivers.name like ? or drivers.driver_external_id like ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like])
    clause = "where " + " and ".join(where) if where else ""
    with db() as con:
        rows = driver_with_latest_payment(con, clause, params).fetchall()
    return [public_driver(row) for row in rows]


def summary_by_status(user):
    statuses = None
    if user["role"] == "operaciones":
        statuses = ["conciliado", "desbloqueado"]
    params = []
    where = ""
    if statuses:
        where = "where drivers.status in ({})".format(",".join("?" for _ in statuses))
        params.extend(statuses)
    with db() as con:
        rows = con.execute(
            f"""
            select drivers.status,
                   count(*) as case_count,
                   coalesce(sum(drivers.debt_usd), 0) as debt_usd,
                   coalesce(sum(
                       case
                           when drivers.rate > 0 then paid.paid_ves / drivers.rate
                           else 0
                       end
                   ), 0) as paid_usd
            from drivers
            left join (
                select driver_id, sum(amount_ves) as paid_ves
                from payments
                where status in ('pago_parcial', 'conciliado')
                group by driver_id
            ) paid on paid.driver_id = drivers.id
            {where}
            group by drivers.status
            order by drivers.status
            """,
            params,
        ).fetchall()
    summary = []
    totals = {"case_count": 0, "debt_usd": 0.0, "paid_usd": 0.0, "pending_usd": 0.0}
    for row in rows:
        debt_usd = money(row["debt_usd"])
        paid_usd = money(row["paid_usd"])
        pending_usd = max(0.0, debt_usd - paid_usd)
        item = {
            "status": row["status"],
            "status_label": CASE_STATUSES.get(row["status"], row["status"]),
            "case_count": int(row["case_count"] or 0),
            "debt_usd": debt_usd,
            "paid_usd": paid_usd,
            "pending_usd": pending_usd,
        }
        summary.append(item)
        totals["case_count"] += item["case_count"]
        totals["debt_usd"] += debt_usd
        totals["paid_usd"] += paid_usd
        totals["pending_usd"] += pending_usd
    return {"rows": summary, "totals": totals}


def find_driver(con, cedula, phone):
    cedula_norm = normalize_digits(cedula)
    phones = sorted(phone_variants(phone))
    if not cedula_norm or not phones:
        return None
    placeholders = ",".join("?" for _ in phones)
    return driver_with_latest_payment(
        con,
        f"""
        where cedula_norm = ? and phone_norm in ({placeholders})
        """,
        [cedula_norm, *phones],
    ).fetchone()


def upsert_driver(con, payload, source="manual"):
    cedula = clean_text(payload.get("cedula"))
    phone = clean_text(payload.get("phone"))
    cedula_norm = normalize_digits(cedula)
    phone_norm = normalize_digits(phone)
    if not cedula_norm or not phone_norm:
        raise ValueError("Cedula y telefono son obligatorios")
    timestamp = now_iso()
    existing = con.execute("select id from drivers where cedula_norm = ?", (cedula_norm,)).fetchone()
    values = (
        clean_text(payload.get("name")),
        cedula,
        cedula_norm,
        phone,
        phone_norm,
        clean_text(payload.get("plate")).upper(),
        clean_text(payload.get("driver_external_id")),
        money(payload.get("debt_usd")),
        money(payload.get("rate")),
        money(payload.get("debt_ves")),
        source,
        timestamp,
    )
    if existing:
        con.execute(
            """
            update drivers set
                name = coalesce(nullif(?, ''), name),
                cedula = ?, cedula_norm = ?, phone = ?, phone_norm = ?,
                plate = coalesce(nullif(?, ''), plate),
                driver_external_id = coalesce(nullif(?, ''), driver_external_id),
                debt_usd = ?, rate = ?, debt_ves = ?, source = ?, updated_at = ?
            where id = ?
            """,
            (*values, existing["id"]),
        )
        return existing["id"]
    driver_id = con.execute(
        """
        insert into drivers (
            name, cedula, cedula_norm, phone, phone_norm, plate, driver_external_id,
            debt_usd, rate, debt_ves, status, source, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente_pago', ?, ?, ?)
        """,
        (*values[:11], timestamp, timestamp),
    ).lastrowid
    add_event(con, driver_id, None, "creacion_caso", f"Caso creado desde {source}")
    return driver_id


def evaluate_payment(con, driver, body):
    reference = normalize_reference(body.get("reference"))
    amount = money(body.get("amount_ves"))
    paid = con.execute(
        "select coalesce(sum(amount_ves), 0) as paid from payments where driver_id = ? and status in ('pago_parcial', 'conciliado')",
        (driver["id"],),
    ).fetchone()["paid"]
    expected = max(0.0, money(driver["debt_ves"]) - money(paid))
    phone_ok = driver["phone_norm"] in phone_variants(body.get("payment_phone"))
    amount_ok = abs(amount - expected) <= max(1.0, expected * 0.01)
    duplicate = con.execute(
        "select payments.id, drivers.cedula, drivers.phone from payments join drivers on drivers.id = payments.driver_id where payments.reference_norm = ?",
        (reference,),
    ).fetchone()
    alerts = []
    if duplicate:
        alerts.append("referencia_duplicada")
    if not amount_ok:
        alerts.append("monto_no_coincide")
    if not phone_ok:
        alerts.append("pago_desde_tercero")
    if not reference:
        alerts.append("falta_referencia")
    return alerts, duplicate


def google_service():
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    credentials_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials_json and not credentials_file:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        return None
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if credentials_json:
        info = json.loads(credentials_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def sheet_range(sheet_name, cells):
    return f"'{sheet_name}'!{cells}"


def ensure_sheet_with_headers(service, sheet_name, headers):
    spreadsheet = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    sheets = spreadsheet.get("sheets", [])
    if not any(item.get("properties", {}).get("title") == sheet_name for item in sheets):
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
    last_column = column_letter(len(headers) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=sheet_range(sheet_name, f"A1:{last_column}1"),
        valueInputOption="USER_ENTERED",
        body={"values": [headers]},
    ).execute()


def read_portal_config_from_sheets():
    service = google_service()
    if not service:
        return {}
    try:
        ensure_sheet_with_headers(service, PORTAL_CONFIG_SHEET_NAME, PORTAL_CONFIG_HEADERS)
        rows = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=sheet_range(PORTAL_CONFIG_SHEET_NAME, "A2:C500"),
        ).execute().get("values", [])
    except Exception as exc:
        print(f"No se pudo leer {PORTAL_CONFIG_SHEET_NAME}: {exc}")
        return {}
    config = {}
    for row in rows:
        key = clean_text(row[0] if row else "")
        if key and key not in TRANSIENT_SETTING_KEYS:
            config[key] = row[1] if len(row) > 1 else ""
    return config


def save_settings_snapshot_to_sheets(con):
    service = google_service()
    if not service:
        return {"ok": False, "error": "Google Sheets no configurado"}
    try:
        ensure_sheet_with_headers(service, PORTAL_CONFIG_SHEET_NAME, PORTAL_CONFIG_HEADERS)
        rows = [
            [row["key"], row["value"], row["updated_at"]]
            for row in con.execute("select key, value, updated_at from settings order by key")
            if row["key"] not in TRANSIENT_SETTING_KEYS
        ]
        values_api = service.spreadsheets().values()
        values_api.clear(spreadsheetId=SHEET_ID, range=sheet_range(PORTAL_CONFIG_SHEET_NAME, "A2:C500")).execute()
        if rows:
            values_api.update(
                spreadsheetId=SHEET_ID,
                range=sheet_range(PORTAL_CONFIG_SHEET_NAME, "A2:C500"),
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()
        return {"ok": True}
    except Exception as exc:
        print(f"No se pudo guardar {PORTAL_CONFIG_SHEET_NAME}: {exc}")
        return {"ok": False, "error": str(exc)}


def load_settings_from_sheets(con):
    config = read_portal_config_from_sheets()
    if not config:
        return False
    for key, value in config.items():
        set_setting(con, key, value)
    return True


def ensure_portal_config_snapshot(con):
    if google_service():
        if not read_portal_config_from_sheets():
            save_settings_snapshot_to_sheets(con)


def payment_backup_row(driver, payment, user=None):
    return [
        payment.get("backup_key") or "",
        driver.get("cedula") or "",
        driver.get("phone") or "",
        driver.get("name") or "",
        driver.get("plate") or "",
        driver.get("driver_external_id") or "",
        driver.get("debt_usd") or "",
        driver.get("debt_ves") or "",
        driver.get("rate") or "",
        driver.get("status") or "",
        payment.get("cedula") or "",
        payment.get("payment_phone") or "",
        payment.get("plate") or "",
        payment.get("amount_ves") or "",
        payment.get("reference") or "",
        payment.get("reference_norm") or "",
        payment.get("bank") or "",
        payment.get("payment_date") or "",
        payment.get("payment_method") or "",
        payment.get("observations") or "",
        payment.get("attachment_path") or "",
        payment.get("attachment_name") or "",
        payment.get("attachment_type") or "",
        payment.get("status") or "",
        payment.get("alerts_json") or "[]",
        payment.get("internal_notes") or "",
        payment.get("reconciliation_agent") or "",
        payment.get("validated_reference") or "",
        (user or {}).get("email") or "",
        payment.get("validated_at") or "",
        payment.get("created_at") or "",
        payment.get("updated_at") or "",
    ]


def backup_payment_to_sheets(driver, payment, user=None):
    service = google_service()
    if not service or not payment or not payment.get("backup_key"):
        return {"ok": False, "error": "Google Sheets no configurado"}
    try:
        ensure_sheet_with_headers(service, PORTAL_PAYMENTS_SHEET_NAME, PORTAL_PAYMENT_HEADERS)
        values_api = service.spreadsheets().values()
        rows = values_api.get(
            spreadsheetId=SHEET_ID,
            range=sheet_range(PORTAL_PAYMENTS_SHEET_NAME, "A1:AF5000"),
        ).execute().get("values", [])
        target_row = None
        for row_number, row in enumerate(rows[1:], start=2):
            if clean_text(row[0] if row else "") == payment.get("backup_key"):
                target_row = row_number
                break
        row_values = payment_backup_row(driver, payment, user)
        if target_row:
            values_api.update(
                spreadsheetId=SHEET_ID,
                range=sheet_range(PORTAL_PAYMENTS_SHEET_NAME, f"A{target_row}:AF{target_row}"),
                valueInputOption="USER_ENTERED",
                body={"values": [row_values]},
            ).execute()
        else:
            values_api.append(
                spreadsheetId=SHEET_ID,
                range=sheet_range(PORTAL_PAYMENTS_SHEET_NAME, "A:AF"),
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row_values]},
            ).execute()
        return {"ok": True}
    except Exception as exc:
        print(f"No se pudo guardar {PORTAL_PAYMENTS_SHEET_NAME}: {exc}")
        return {"ok": False, "error": str(exc)}


def restore_payment_backups_from_sheets():
    service = google_service()
    if not service:
        return {"ok": False, "restored": 0, "error": "Google Sheets no configurado"}
    try:
        ensure_sheet_with_headers(service, PORTAL_PAYMENTS_SHEET_NAME, PORTAL_PAYMENT_HEADERS)
        rows = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=sheet_range(PORTAL_PAYMENTS_SHEET_NAME, "A2:AF5000"),
        ).execute().get("values", [])
    except Exception as exc:
        print(f"No se pudo leer {PORTAL_PAYMENTS_SHEET_NAME}: {exc}")
        return {"ok": False, "restored": 0, "error": str(exc)}
    restored = 0
    with DB_LOCK:
        with db() as con:
            for raw in rows:
                row = dict(zip(PORTAL_PAYMENT_HEADERS, list(raw) + [""] * (len(PORTAL_PAYMENT_HEADERS) - len(raw))))
                backup_key = clean_text(row.get("backup_key"))
                cedula = clean_text(row.get("driver_cedula") or row.get("cedula_reportada"))
                if not backup_key or not cedula:
                    continue
                driver = con.execute("select * from drivers where cedula_norm = ?", (normalize_digits(cedula),)).fetchone()
                if not driver:
                    driver_id = upsert_driver(
                        con,
                        {
                            "name": row.get("driver_name"),
                            "cedula": cedula,
                            "phone": row.get("driver_phone") or row.get("payment_phone"),
                            "plate": row.get("driver_plate") or row.get("plate_reportada"),
                            "driver_external_id": row.get("driver_external_id"),
                            "debt_usd": row.get("debt_usd"),
                            "rate": row.get("rate"),
                            "debt_ves": row.get("debt_ves"),
                        },
                        "portal_pagos_backup",
                    )
                    driver = con.execute("select * from drivers where id = ?", (driver_id,)).fetchone()
                existing = con.execute("select id from payments where backup_key = ?", (backup_key,)).fetchone()
                timestamp = now_iso()
                values = (
                    driver["id"],
                    clean_text(row.get("cedula_reportada") or cedula),
                    clean_text(row.get("payment_phone")),
                    clean_text(row.get("plate_reportada") or row.get("driver_plate")).upper(),
                    money(row.get("amount_ves")),
                    clean_text(row.get("reference")),
                    clean_text(row.get("reference_norm") or normalize_reference(row.get("reference"))),
                    clean_text(row.get("bank")),
                    clean_text(row.get("payment_date")),
                    clean_text(row.get("payment_method")) or "transferencia",
                    clean_text(row.get("observations")),
                    clean_text(row.get("attachment_path")),
                    clean_text(row.get("attachment_name")),
                    clean_text(row.get("attachment_type")),
                    clean_text(row.get("payment_status")) or "pago_reportado",
                    clean_text(row.get("alerts_json")) or "[]",
                    clean_text(row.get("internal_notes")),
                    clean_text(row.get("reconciliation_agent")),
                    clean_text(row.get("validated_reference")),
                    clean_text(row.get("validated_at")),
                    clean_text(row.get("created_at")) or timestamp,
                    clean_text(row.get("updated_at")) or timestamp,
                    backup_key,
                )
                if existing:
                    con.execute(
                        """
                        update payments set
                            driver_id = ?, cedula = ?, payment_phone = ?, plate = ?, amount_ves = ?,
                            reference = ?, reference_norm = ?, bank = ?, payment_date = ?, payment_method = ?,
                            observations = ?, attachment_path = ?, attachment_name = ?, attachment_type = ?,
                            status = ?, alerts_json = ?, internal_notes = ?, reconciliation_agent = ?,
                            validated_reference = ?, validated_at = ?, created_at = ?, updated_at = ?
                        where backup_key = ?
                        """,
                        values,
                    )
                else:
                    con.execute(
                        """
                        insert into payments (
                            driver_id, cedula, payment_phone, plate, amount_ves, reference, reference_norm,
                            bank, payment_date, payment_method, observations, attachment_path, attachment_name,
                            attachment_type, status, alerts_json, internal_notes, reconciliation_agent,
                            validated_reference, validated_at, created_at, updated_at, backup_key
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    restored += 1
                status = clean_text(row.get("driver_status") or row.get("payment_status"))
                if status in CASE_STATUSES:
                    con.execute("update drivers set status = ?, updated_at = ? where id = ?", (status, now_iso(), driver["id"]))
            if restored:
                set_setting(con, "sync_status", f"Restaurados {restored} pagos desde {PORTAL_PAYMENTS_SHEET_NAME} en {now_iso()}")
    return {"ok": True, "restored": restored}


def snapshot_local_payment_backups_to_sheets():
    with db() as con:
        rows = con.execute(
            """
            select payments.*, drivers.name as driver_name, drivers.cedula as driver_cedula,
                   drivers.phone as driver_phone, drivers.plate as driver_plate,
                   drivers.driver_external_id, drivers.debt_usd, drivers.debt_ves,
                   drivers.rate, drivers.status as driver_status
            from payments
            join drivers on drivers.id = payments.driver_id
            order by payments.created_at
            """
        ).fetchall()
    backed_up = 0
    for row in rows:
        item = row_to_dict(row)
        driver = {
            "name": item.get("driver_name"),
            "cedula": item.get("driver_cedula"),
            "phone": item.get("driver_phone"),
            "plate": item.get("driver_plate"),
            "driver_external_id": item.get("driver_external_id"),
            "debt_usd": item.get("debt_usd"),
            "debt_ves": item.get("debt_ves"),
            "rate": item.get("rate"),
            "status": item.get("driver_status"),
        }
        result = backup_payment_to_sheets(driver, item)
        if result.get("ok"):
            backed_up += 1
    if backed_up:
        with db() as con:
            set_setting(con, "sync_status", f"Respaldados {backed_up} pagos en {PORTAL_PAYMENTS_SHEET_NAME} en {now_iso()}")
    return {"ok": True, "backed_up": backed_up}


def header_index(headers, candidates, fallback):
    normalized = [clean_text(item).lower().replace("é", "e").replace("á", "a").replace("ó", "o") for item in headers]
    for candidate in candidates:
        for index, header in enumerate(normalized):
            if candidate in header:
                return index
    return fallback


def sync_debts_from_sheets(force=False):
    today = local_sync_date()
    if not force:
        with db() as con:
            settings = get_settings(con)
            imported_drivers = con.execute(
                "select count(*) as count from drivers where source = 'google_sheets'"
            ).fetchone()["count"]
            if imported_drivers and settings.get("debt_sync_local_date") == today and settings.get("debt_sync_version") == SYNC_VERSION:
                return {"ok": True, "skipped": True, "date": today}
    service = google_service()
    if not service:
        return {"ok": False, "error": "Configura GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_APPLICATION_CREDENTIALS para sincronizar Google Sheets."}
    value_api = service.spreadsheets().values()
    values = value_api.get(spreadsheetId=SHEET_ID, range=f"{DEBT_SHEET_NAME}!A1:H5000").execute().get("values", [])
    if not values:
        return {"ok": False, "error": "La hoja Deuda esta vacia o no se pudo leer."}
    rate_values = value_api.get(spreadsheetId=SHEET_ID, range=f"{DEBT_SHEET_NAME}!H2").execute().get("values", [])
    sheet_rate = parse_money(rate_values[0][0]) if rate_values and rate_values[0] else 0.0
    headers = values[0]
    idx_name = header_index(headers, ["nombre", "driver"], 0)
    idx_phone = 1
    idx_cedula = 2
    idx_usd = 3
    idx_ves = 4
    idx_plate = header_index(headers, ["placa"], 5)
    idx_driver = header_index(headers, ["driver id", "rider id", "id"], 6)
    imported = 0
    with DB_LOCK:
        with db() as con:
            for raw in values[1:]:
                if len(raw) <= max(idx_cedula, idx_phone, idx_usd, idx_ves):
                    continue
                cedula = clean_text(raw[idx_cedula] if idx_cedula < len(raw) else "")
                phone = clean_text(raw[idx_phone] if idx_phone < len(raw) else "")
                if not cedula or not phone:
                    continue
                debt_usd = parse_money(raw[idx_usd] if idx_usd < len(raw) else 0)
                debt_ves = parse_money(raw[idx_ves] if idx_ves < len(raw) else 0)
                rate = sheet_rate or (debt_ves / debt_usd if debt_usd else 0)
                upsert_driver(
                    con,
                    {
                        "name": raw[idx_name] if idx_name < len(raw) else "",
                        "cedula": cedula,
                        "phone": phone,
                        "plate": raw[idx_plate] if idx_plate < len(raw) else "",
                        "driver_external_id": raw[idx_driver] if idx_driver < len(raw) else "",
                        "debt_usd": debt_usd,
                        "rate": rate,
                        "debt_ves": debt_ves,
                    },
                    "google_sheets",
                )
                imported += 1
            set_setting(con, "sync_status", f"Sincronizado {imported} deudores desde Google Sheets en {now_iso()}")
            set_setting(con, "debt_sync_local_date", today)
            set_setting(con, "debt_sync_version", SYNC_VERSION)
    return {"ok": True, "imported": imported, "date": today}


def append_conciliated_to_sheets(driver, payment, user):
    service = google_service()
    if not service:
        return {"ok": False, "error": "Google Sheets no configurado"}
    ensure_conciliated_sheet_headers(service)
    updated = update_conciliated_status_in_sheets(driver, payment, user, "conciliado", service=service)
    if updated.get("ok") and updated.get("updated"):
        return updated
    row = [
        driver.get("name") or "",
        driver.get("cedula") or "",
        driver.get("phone") or "",
        driver.get("plate") or "",
        driver.get("driver_external_id") or "",
        payment.get("amount_ves") or "",
        payment.get("validated_reference") or payment.get("reference") or "",
        payment.get("validated_at") or now_iso(),
        payment.get("reconciliation_agent") or user.get("name") or "",
        CASE_STATUSES.get(driver.get("status"), driver.get("status") or ""),
        "Si",
        "pendiente",
        "",
        payment.get("observations") or "",
    ]
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{CONCILIATED_SHEET_NAME}!A:N",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {"ok": True}


def ensure_conciliated_sheet_headers(service):
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{CONCILIATED_SHEET_NAME}!A1:N1",
        valueInputOption="USER_ENTERED",
        body={"values": [CONCILIATED_HEADERS]},
    ).execute()


def column_letter(index):
    value = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def find_header_index(headers, candidates, fallback=None):
    normalized = [clean_text(item).lower().replace("é", "e").replace("á", "a").replace("ó", "o") for item in headers]
    for candidate in candidates:
        for index, header in enumerate(normalized):
            if candidate in header:
                return index
    return fallback


def update_conciliated_status_in_sheets(driver, payment, user, status, service=None):
    service = service or google_service()
    if not service:
        return {"ok": False, "error": "Google Sheets no configurado"}
    ensure_conciliated_sheet_headers(service)
    values_api = service.spreadsheets().values()
    rows = values_api.get(spreadsheetId=SHEET_ID, range=f"{CONCILIATED_SHEET_NAME}!A1:Z5000").execute().get("values", [])
    if not rows:
        return {"ok": True, "updated": False}
    headers = rows[0]
    cedula_idx = find_header_index(headers, ["cedula", "cédula"], 1)
    ref_idx = find_header_index(headers, ["referencia"], 6)
    status_idx = find_header_index(headers, ["estado", "status"], 9)
    conciliado_idx = find_header_index(headers, ["conciliado"], None)
    unlock_idx = find_header_index(headers, ["desbloqueo"], 10)
    agent_idx = find_header_index(headers, ["responsable", "agente"], 8)
    date_idx = find_header_index(headers, ["fecha", "conciliacion"], 7)
    target_cedula = clean_text(driver.get("cedula"))
    target_ref = clean_text(payment.get("validated_reference") or payment.get("reference"))
    target_row = None
    for row_number, row in enumerate(rows[1:], start=2):
        row_cedula = clean_text(row[cedula_idx] if cedula_idx is not None and cedula_idx < len(row) else "")
        row_ref = clean_text(row[ref_idx] if ref_idx is not None and ref_idx < len(row) else "")
        if row_cedula == target_cedula and (not target_ref or row_ref == target_ref):
            target_row = row_number
            break
    if not target_row:
        return {"ok": True, "updated": False}
    updates = []
    status_label = CASE_STATUSES.get(status, status)
    if status_idx is not None:
        updates.append((status_idx, status_label))
    if conciliado_idx is not None:
        updates.append((conciliado_idx, "Si" if status == "conciliado" else "No"))
    if unlock_idx is not None and status == "desbloqueado":
        updates.append((unlock_idx, "desbloqueado"))
    if agent_idx is not None:
        updates.append((agent_idx, payment.get("reconciliation_agent") or user.get("name") or ""))
    if date_idx is not None and status == "conciliado":
        updates.append((date_idx, payment.get("validated_at") or now_iso()))
    for col_idx, value in updates:
        values_api.update(
            spreadsheetId=SHEET_ID,
            range=f"{CONCILIATED_SHEET_NAME}!{column_letter(col_idx)}{target_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [[value]]},
        ).execute()
    return {"ok": True, "updated": True}


def ensure_payment_columns(con):
    existing = {row["name"] for row in con.execute("pragma table_info(payments)")}
    if "reconciliation_agent" not in existing:
        con.execute("alter table payments add column reconciliation_agent text")
    if "backup_key" not in existing:
        con.execute("alter table payments add column backup_key text")
    con.execute("update payments set backup_key = lower(hex(randomblob(16))) where backup_key is null or backup_key = ''")
    con.execute("create unique index if not exists idx_payments_backup_key on payments(backup_key)")


def maybe_sync_debts():
    result = sync_debts_from_sheets(force=False)
    if not result.get("ok"):
        with db() as con:
            set_setting(con, "sync_status", result.get("error", "Google Sheets no configurado"))


def ensure_initial_debt_sync():
    with db() as con:
        imported_drivers = con.execute(
            "select count(*) as count from drivers where source = 'google_sheets'"
        ).fetchone()["count"]
    if imported_drivers:
        return
    result = sync_debts_from_sheets(force=True)
    if not result.get("ok"):
        with db() as con:
            set_setting(con, "sync_status", result.get("error", "Google Sheets no configurado"))


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute(
            """
            create table if not exists users (
                id integer primary key autoincrement,
                name text not null,
                email text not null unique,
                role text not null check (role in ('master', 'admin', 'conciliacion', 'operaciones')),
                password_hash text not null,
                active integer not null default 1,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists drivers (
                id integer primary key autoincrement,
                name text,
                cedula text not null,
                cedula_norm text not null unique,
                phone text not null,
                phone_norm text not null,
                plate text,
                driver_external_id text,
                debt_usd real not null default 0,
                rate real not null default 0,
                debt_ves real not null default 0,
                status text not null default 'pendiente_pago',
                source text not null default 'manual',
                unlocked_by integer references users(id),
                unlocked_at text,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists payments (
                id integer primary key autoincrement,
                driver_id integer not null references drivers(id) on delete cascade,
                cedula text not null,
                payment_phone text not null,
                plate text not null,
                amount_ves real not null,
                reference text not null,
                reference_norm text not null,
                bank text not null,
                payment_date text not null,
                payment_method text not null default 'transferencia',
                observations text not null,
                attachment_path text,
                attachment_name text,
                attachment_type text,
                status text not null default 'pago_reportado',
                match_confidence text not null default 'bajo',
                alerts_json text not null default '[]',
                internal_notes text,
                reconciliation_agent text,
                validated_reference text,
                validated_by integer references users(id),
                validated_at text,
                backup_key text unique,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        ensure_payment_columns(con)
        con.execute("create index if not exists idx_payments_reference on payments(reference_norm)")
        con.execute(
            """
            create table if not exists audit_events (
                id integer primary key autoincrement,
                driver_id integer references drivers(id) on delete cascade,
                user_id integer references users(id),
                event_type text not null,
                notes text,
                payload_json text not null default '{}',
                created_at text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists settings (
                key text primary key,
                value text not null,
                updated_at text not null
            )
            """
        )
        seed_data(con)
        load_settings_from_sheets(con)
        ensure_portal_config_snapshot(con)


def seed_data(con):
    if con.execute("select count(*) as count from users").fetchone()["count"]:
        return
    timestamp = now_iso()
    users = [
        ("Master BipBip", "master@bipbip.local", "master", "master123"),
        ("Admin BipBip", "admin@bipbip.local", "admin", "admin123"),
        ("Conciliacion", "conciliacion@bipbip.local", "conciliacion", "conciliacion123"),
        ("Operaciones", "operaciones@bipbip.local", "operaciones", "operaciones123"),
    ]
    for name, email, role, password in users:
        con.execute(
            """
            insert into users (name, email, role, password_hash, active, created_at, updated_at)
            values (?, ?, ?, ?, 1, ?, ?)
            """,
            (name, email, role, hash_password(password), timestamp, timestamp),
        )
    defaults = {
        "bank_name": "Banco Nacional de Credito",
        "account_number": "0191-0000-00-0000000000",
        "rif": "J-00000000-0",
        "account_holder": "BipBip",
        "instructions": "Realiza una transferencia a esta cuenta por el monto exacto indicado. Guarda el comprobante y reportalo aqui. No recargues el dinero en tu billetera para evitar el cobro de IVA correspondiente a recargas de comision.",
        "payment_accounts_json": json.dumps([
            {
                "type": "Pago movil",
                "bank_name": "Banco Nacional de Credito",
                "account_holder": "BipBip",
                "account_number": "",
                "rif": "J-00000000-0",
                "phone": "0000000000",
                "document": "J-00000000-0",
                "instructions": "Realiza un pago movil por el monto exacto indicado.",
            },
            {
                "type": "Transferencia a cuenta indicada",
                "bank_name": "Banco Nacional de Credito",
                "account_holder": "BipBip",
                "account_number": "0191-0000-00-0000000000",
                "rif": "J-00000000-0",
                "phone": "",
                "document": "J-00000000-0",
                "instructions": "Realiza una transferencia bancaria por el monto exacto indicado.",
            },
        ], ensure_ascii=False),
        "sync_status": "Pendiente por configurar Google Sheets",
        "debt_sync_local_date": "",
        "debt_sync_version": "",
    }
    for key, value in defaults.items():
        set_setting(con, key, value)
    samples = [
        {
            "name": "Conductor Demo",
            "cedula": "V12345678",
            "phone": "4141234567",
            "plate": "ABC123",
            "driver_external_id": "DRV-001",
            "debt_usd": 12.5,
            "rate": 36.5,
            "debt_ves": 456.25,
        }
    ]
    for sample in samples:
        upsert_driver(con, sample, "demo")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/admin"):
            return send_file(self, STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if parsed.path.startswith("/static/"):
            target = (STATIC_DIR / parsed.path.removeprefix("/static/")).resolve()
            if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                return send_json(self, {"error": "No encontrado"}, 404)
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return send_file(self, target, content_type)
        if parsed.path.startswith("/uploads/"):
            user = require_user(self)
            if user is None:
                return
            target = (UPLOAD_DIR / parsed.path.removeprefix("/uploads/")).resolve()
            if UPLOAD_DIR.resolve() not in target.parents and target.parent != UPLOAD_DIR.resolve():
                return send_json(self, {"error": "No encontrado"}, 404)
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return send_file(self, target, content_type)
        if parsed.path == "/api/public/config":
            with db() as con:
                return send_json(self, {"settings": get_settings(con)})
        if parsed.path == "/api/public/debt":
            maybe_sync_debts()
            query = parse_qs(parsed.query)
            cedula = query.get("cedula", [""])[0]
            phone = query.get("phone", [""])[0]
            with db() as con:
                row = find_driver(con, cedula, phone)
                settings = get_settings(con)
                if row:
                    add_event(con, row["id"], None, "consulta_conductor", "Consulta publica de deuda")
            if not row:
                return send_json(self, {"error": "No encontramos una deuda con esa cedula y telefono. Usa cedula tipo V12345678 y telefono tipo 4141234567, o contacta soporte."}, 404)
            return send_json(self, {"driver": public_driver(row), "settings": settings})

        user = require_user(self)
        if user is None:
            return
        if parsed.path == "/api/bootstrap":
            with db() as con:
                users = [public_user(row) for row in con.execute("select * from users order by role, name")] if user["role"] == "master" else []
                settings = get_settings(con)
            return send_json(self, {"user": user, "users": users, "settings": settings, "statuses": CASE_STATUSES})
        if parsed.path == "/api/summary":
            return send_json(self, summary_by_status(user))
        if parsed.path == "/api/cases":
            return send_json(self, {"cases": list_cases(user, parse_qs(parsed.query))})
        if parsed.path.startswith("/api/cases/"):
            driver_id = int(parsed.path.split("/")[-1])
            with db() as con:
                row = driver_with_latest_payment(con, "where drivers.id = ?", [driver_id]).fetchone()
            if not row:
                return send_json(self, {"error": "Caso no encontrado"}, 404)
            return send_json(self, {"case": public_driver(row, include_events=True)})
        if parsed.path == "/api/export":
            cases = list_cases(user, parse_qs(parsed.query))
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["nombre", "cedula", "telefono", "placa", "driver_id", "deuda_usd", "tasa", "deuda_ves", "estado", "referencia", "monto_reportado", "llamadas_exitosas", "llamadas_perdidas", "seguimientos", "actualizado"])
            for item in cases:
                payment = item.get("payment") or {}
                writer.writerow([
                    item.get("name") or "",
                    item.get("cedula") or "",
                    item.get("phone") or "",
                    item.get("plate") or "",
                    item.get("driver_external_id") or "",
                    item.get("debt_usd") or "",
                    item.get("rate") or "",
                    item.get("debt_ves") or "",
                    item.get("status_label") or item.get("status"),
                    payment.get("reference", ""),
                    payment.get("amount_ves", ""),
                    item.get("successful_call_count", 0),
                    item.get("missed_call_count", 0),
                    item.get("followup_count", 0),
                    item.get("updated_at", ""),
                ])
            return send_text(self, output.getvalue(), "text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="deuda-bipbip.csv"'})
        return send_json(self, {"error": "No encontrado"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = parse_json_body(self)
            if parsed.path == "/api/auth/login":
                email = clean_text(body.get("email")).lower()
                password = body.get("password", "")
                with db() as con:
                    row = con.execute("select * from users where lower(email) = ? and active = 1", (email,)).fetchone()
                if not row or row["password_hash"] != hash_password(password):
                    return send_json(self, {"error": "Email o clave incorrectos"}, 401)
                user = public_user(row)
                return send_json(self, {"token": make_token(user), "user": user})

            if parsed.path == "/api/public/payments":
                required = ["cedula", "payment_phone", "plate", "amount_ves", "reference", "bank", "payment_date", "observations", "attachment_file"]
                missing = [field for field in required if not body.get(field)]
                if missing:
                    return send_json(self, {"error": "Todos los campos son obligatorios.", "missing": missing}, 400)
                with DB_LOCK:
                    with db() as con:
                        lookup_cedula = body.get("lookup_cedula") or body.get("cedula")
                        lookup_phone = body.get("registered_phone") or body.get("lookup_phone") or body.get("payment_phone")
                        driver = find_driver(con, lookup_cedula, lookup_phone)
                        if not driver:
                            driver = con.execute("select * from drivers where cedula_norm = ?", (normalize_digits(lookup_cedula),)).fetchone()
                        if not driver:
                            return send_json(self, {"error": "No encontramos el caso de deuda consultado. Vuelve a consultar la deuda antes de reportar el pago."}, 404)
                        alerts, duplicate = evaluate_payment(con, driver, body)
                        if duplicate:
                            return send_json(
                                self,
                                {
                                    "error": "Esa referencia bancaria ya fue reportada o validada. No puedes repetirla; cambia la referencia para continuar.",
                                    "duplicate": row_to_dict(duplicate),
                                },
                                409,
                            )
                        attachment_path, attachment_name, attachment_type = save_upload(body.get("attachment_file"))
                        timestamp = now_iso()
                        backup_key = uuid.uuid4().hex
                        payment_id = con.execute(
                            """
                            insert into payments (
                                driver_id, cedula, payment_phone, plate, amount_ves, reference, reference_norm,
                                bank, payment_date, payment_method, observations, attachment_path, attachment_name,
                                attachment_type, status, alerts_json, backup_key, created_at, updated_at
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pago_reportado', ?, ?, ?, ?)
                            """,
                            (
                                driver["id"],
                                clean_text(body.get("cedula")),
                                clean_text(body.get("payment_phone")),
                                clean_text(body.get("plate")).upper(),
                                money(body.get("amount_ves")),
                                clean_text(body.get("reference")),
                                normalize_reference(body.get("reference")),
                                clean_text(body.get("bank")),
                                clean_text(body.get("payment_date")),
                                clean_text(body.get("payment_method")) or "transferencia",
                                clean_text(body.get("observations")),
                                attachment_path,
                                attachment_name,
                                attachment_type,
                                json.dumps(alerts, ensure_ascii=False),
                                backup_key,
                                timestamp,
                                timestamp,
                            ),
                        ).lastrowid
                        con.execute("update drivers set status = 'pago_reportado', plate = coalesce(nullif(?, ''), plate), updated_at = ? where id = ?", (clean_text(body.get("plate")).upper(), timestamp, driver["id"]))
                        add_event(con, driver["id"], None, "submission_formulario", "Pago reportado por conductor", {"payment_id": payment_id, "alerts": alerts})
                        updated_driver = row_to_dict(con.execute("select * from drivers where id = ?", (driver["id"],)).fetchone())
                        updated_payment = row_to_dict(con.execute("select * from payments where id = ?", (payment_id,)).fetchone())
                        backup_payment_to_sheets(updated_driver, updated_payment)
                return send_json(self, {"ok": True, "message": "Pago reportado. El equipo de conciliacion lo revisara."}, 201)

            user = require_user(self)
            if user is None:
                return

            if parsed.path == "/api/sync/debts":
                if not can_manage(user):
                    return send_json(self, {"error": "Solo master o admin pueden sincronizar deudas"}, 403)
                result = sync_debts_from_sheets(force=True)
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/import/debts":
                if not can_manage(user):
                    return send_json(self, {"error": "Solo master o admin pueden importar deudas"}, 403)
                rows = body.get("rows") or []
                imported = 0
                with DB_LOCK:
                    with db() as con:
                        for row in rows:
                            upsert_driver(con, row, "csv")
                            imported += 1
                        set_setting(con, "sync_status", f"Importados {imported} registros CSV en {now_iso()}")
                return send_json(self, {"ok": True, "imported": imported})

            if parsed.path == "/api/settings/save":
                if not can_manage(user):
                    return send_json(self, {"error": "Solo master o admin pueden editar datos bancarios"}, 403)
                with db() as con:
                    for key in ["bank_name", "account_number", "rif", "account_holder", "instructions"]:
                        set_setting(con, key, body.get(key))
                    payment_accounts = body.get("payment_accounts")
                    if isinstance(payment_accounts, list):
                        cleaned_accounts = []
                        for account in payment_accounts[:6]:
                            if not isinstance(account, dict):
                                continue
                            if not clean_text(account.get("type")):
                                continue
                            cleaned_accounts.append({
                                "type": clean_text(account.get("type")),
                                "bank_name": clean_text(account.get("bank_name")),
                                "account_holder": clean_text(account.get("account_holder")),
                                "account_number": clean_text(account.get("account_number")),
                                "rif": clean_text(account.get("rif")),
                                "phone": clean_text(account.get("phone")),
                                "document": clean_text(account.get("document")),
                                "instructions": clean_text(account.get("instructions")),
                            })
                        if cleaned_accounts:
                            set_setting(con, "payment_accounts_json", json.dumps(cleaned_accounts, ensure_ascii=False))
                    add_event(con, None, user["id"], "edicion_datos_bancarios", "Datos bancarios actualizados")
                    settings = get_settings(con)
                    save_settings_snapshot_to_sheets(con)
                return send_json(self, {"settings": settings})

            if parsed.path == "/api/users/save":
                if user["role"] != "master":
                    return send_json(self, {"error": "Solo master puede configurar usuarios"}, 403)
                user_id = int(body.get("id") or 0)
                name = clean_text(body.get("name"))
                email = clean_text(body.get("email")).lower()
                role = clean_text(body.get("role"))
                active = 1 if body.get("active", True) else 0
                if role not in ("master", "admin", "conciliacion", "operaciones") or not name or not email:
                    return send_json(self, {"error": "Nombre, email y rol son obligatorios"}, 400)
                timestamp = now_iso()
                with db() as con:
                    if user_id:
                        params = [name, email, role, active, timestamp]
                        sql = "update users set name = ?, email = ?, role = ?, active = ?, updated_at = ?"
                        if clean_text(body.get("password")):
                            sql += ", password_hash = ?"
                            params.append(hash_password(body.get("password")))
                        sql += " where id = ?"
                        params.append(user_id)
                        con.execute(sql, params)
                    else:
                        password = clean_text(body.get("password")) or "bipbip123"
                        user_id = con.execute(
                            """
                            insert into users (name, email, role, password_hash, active, created_at, updated_at)
                            values (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (name, email, role, hash_password(password), active, timestamp, timestamp),
                        ).lastrowid
                    row = con.execute("select * from users where id = ?", (user_id,)).fetchone()
                return send_json(self, {"user": public_user(row)})

            if parsed.path.startswith("/api/cases/") and parsed.path.endswith("/followup"):
                driver_id = int(parsed.path.split("/")[3])
                followup_type = clean_text(body.get("type"))
                notes = clean_text(body.get("notes"))
                allowed = {
                    "nota": "nota_seguimiento",
                    "llamada_exitosa": "llamada_exitosa",
                    "llamada_perdida": "llamada_perdida",
                }
                event_type = allowed.get(followup_type)
                if not event_type:
                    return send_json(self, {"error": "Tipo de seguimiento invalido"}, 400)
                if not notes:
                    notes = {
                        "nota_seguimiento": "Nota agregada",
                        "llamada_exitosa": "Llamada exitosa registrada",
                        "llamada_perdida": "Llamada perdida registrada",
                    }[event_type]
                with db() as con:
                    driver = con.execute("select * from drivers where id = ?", (driver_id,)).fetchone()
                    if not driver:
                        return send_json(self, {"error": "Caso no encontrado"}, 404)
                    add_event(con, driver_id, user["id"], event_type, notes)
                return send_json(self, {"ok": True})

            if parsed.path.startswith("/api/cases/") and parsed.path.endswith("/status"):
                if not can_conciliate(user):
                    return send_json(self, {"error": "No autorizado para conciliar"}, 403)
                driver_id = int(parsed.path.split("/")[3])
                status = clean_text(body.get("status"))
                notes = clean_text(body.get("notes"))
                validated_reference = clean_text(body.get("validated_reference"))
                reconciliation_agent = clean_text(body.get("reconciliation_agent")) or user["name"]
                if status not in CASE_STATUSES:
                    return send_json(self, {"error": "Estado invalido"}, 400)
                if status == "duplicado":
                    ref_norm = normalize_reference(validated_reference or body.get("reference"))
                    if ref_norm:
                        with db() as con:
                            count = con.execute("select count(*) as count from payments where reference_norm = ?", (ref_norm,)).fetchone()["count"]
                        if count < 2:
                            return send_json(self, {"error": "Solo se puede marcar duplicado si la referencia aparece en mas de un reporte."}, 400)
                with DB_LOCK:
                    with db() as con:
                        driver = con.execute("select * from drivers where id = ?", (driver_id,)).fetchone()
                        if not driver:
                            return send_json(self, {"error": "Caso no encontrado"}, 404)
                        payment = con.execute("select * from payments where driver_id = ? order by created_at desc limit 1", (driver_id,)).fetchone()
                        timestamp = now_iso()
                        if payment:
                            con.execute(
                                """
                                update payments set status = ?, internal_notes = ?, validated_reference = coalesce(nullif(?, ''), reference),
                                    reconciliation_agent = ?, validated_by = ?, validated_at = ?, updated_at = ?
                                where id = ?
                                """,
                                (status, notes, validated_reference, reconciliation_agent, user["id"], timestamp, timestamp, payment["id"]),
                            )
                        con.execute("update drivers set status = ?, updated_at = ? where id = ?", (status, timestamp, driver_id))
                        add_event(con, driver_id, user["id"], "cambio_estado", f"Estado cambiado a {CASE_STATUSES[status]}. {notes}", {"status": status})
                        updated_driver = row_to_dict(con.execute("select * from drivers where id = ?", (driver_id,)).fetchone())
                        updated_payment = row_to_dict(con.execute("select * from payments where driver_id = ? order by created_at desc limit 1", (driver_id,)).fetchone())
                        if updated_payment:
                            backup_payment_to_sheets(updated_driver, updated_payment, user)
                            try:
                                if status == "conciliado":
                                    sync_result = append_conciliated_to_sheets(updated_driver, updated_payment, user)
                                else:
                                    sync_result = update_conciliated_status_in_sheets(updated_driver, updated_payment, user, status)
                                if sync_result.get("ok"):
                                    add_event(con, driver_id, user["id"], "sync_conciliados", f"Tab Conciliados actualizado: {CASE_STATUSES[status]}")
                                else:
                                    add_event(con, driver_id, user["id"], "sync_conciliados_error", sync_result.get("error", "Google Sheets no configurado"))
                            except Exception as exc:
                                add_event(con, driver_id, user["id"], "sync_conciliados_error", str(exc))
                return send_json(self, {"ok": True})

            if parsed.path.startswith("/api/cases/") and parsed.path.endswith("/delete"):
                if user["role"] != "master":
                    return send_json(self, {"error": "Solo master puede borrar casos"}, 403)
                driver_id = int(parsed.path.split("/")[3])
                with DB_LOCK:
                    with db() as con:
                        driver = con.execute("select * from drivers where id = ?", (driver_id,)).fetchone()
                        if not driver:
                            return send_json(self, {"error": "Caso no encontrado"}, 404)
                        add_event(con, driver_id, user["id"], "borrado_caso", "Caso borrado por master")
                        con.execute("delete from payments where driver_id = ?", (driver_id,))
                        con.execute("delete from audit_events where driver_id = ?", (driver_id,))
                        con.execute("delete from drivers where id = ?", (driver_id,))
                return send_json(self, {"ok": True})

            if parsed.path.startswith("/api/cases/") and parsed.path.endswith("/unlock"):
                if not can_unlock(user):
                    return send_json(self, {"error": "No autorizado para desbloquear"}, 403)
                driver_id = int(parsed.path.split("/")[3])
                timestamp = now_iso()
                with db() as con:
                    driver = con.execute("select * from drivers where id = ?", (driver_id,)).fetchone()
                    if not driver:
                        return send_json(self, {"error": "Caso no encontrado"}, 404)
                    if driver["status"] not in ("conciliado", "desbloqueado"):
                        return send_json(self, {"error": "Solo puedes desbloquear casos conciliados"}, 400)
                    con.execute("update drivers set status = 'desbloqueado', unlocked_by = ?, unlocked_at = ?, updated_at = ? where id = ?", (user["id"], timestamp, timestamp, driver_id))
                    add_event(con, driver_id, user["id"], "desbloqueo_wallet", "Wallet marcada como desbloqueada")
                    payment = row_to_dict(con.execute("select * from payments where driver_id = ? order by created_at desc limit 1", (driver_id,)).fetchone())
                    updated_driver = row_to_dict(con.execute("select * from drivers where id = ?", (driver_id,)).fetchone())
                    if payment:
                        backup_payment_to_sheets(updated_driver, payment, user)
                        try:
                            update_conciliated_status_in_sheets(updated_driver, payment, user, "desbloqueado")
                        except Exception as exc:
                            add_event(con, driver_id, user["id"], "sync_conciliados_error", str(exc))
                return send_json(self, {"ok": True})

            return send_json(self, {"error": "No encontrado"}, 404)
        except Exception as exc:
            return send_json(self, {"error": "Error interno", "details": str(exc)}, 500)


def main():
    init_db()
    ensure_initial_debt_sync()
    restore_payment_backups_from_sheets()
    snapshot_local_payment_backups_to_sheets()
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Deuda BipBip corriendo en http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
