# Deuda BipBip

Portal interno simple para consultar deuda de conductores, recibir comprobantes, conciliar pagos y publicar la lista de casos listos para desbloqueo.

## Vistas

- Portal del conductor: consulta por cedula + telefono, monto exacto a pagar, datos bancarios y formulario obligatorio de reporte.
- Panel interno: bandejas de pendientes, reportados, validacion, conciliados, rechazados, duplicados/fraude y lista para desbloqueo.
- Roles: `master`, `admin`, `conciliacion`, `operaciones`.

## Credenciales demo

```txt
master@bipbip.local / master123
admin@bipbip.local / admin123
conciliacion@bipbip.local / conciliacion123
operaciones@bipbip.local / operaciones123
```

## Correr local

```bash
python3 app.py
```

Abre `http://127.0.0.1:8787/`.

## Google Sheets

La app puede leer el tab `Deuda` y escribir los conciliados en `Conciliados` del spreadsheet:

```txt
1DcX_PW9xfqs9eCpVl6uqng4hG1Q1ewfAYwrtiuNpOFU
```

Variables de entorno:

```txt
GOOGLE_SHEET_ID=1DcX_PW9xfqs9eCpVl6uqng4hG1Q1ewfAYwrtiuNpOFU
GOOGLE_DEBT_SHEET=Deuda
GOOGLE_CONCILIATED_SHEET=Conciliados
GOOGLE_SERVICE_ACCOUNT_JSON={...json de service account...}
SYNC_TIMEZONE=America/Caracas
```

Tambien puedes usar `GOOGLE_APPLICATION_CREDENTIALS=/ruta/service-account.json`.

Importante: comparte el Google Sheet con el email del service account con permisos de editor. Sin eso, Google devolvera `403 Forbidden`.

La sincronizacion de deudas es diaria y liviana: despues de la medianoche en `SYNC_TIMEZONE`, la primera consulta del portal o el boton manual de sincronizar lee `Deuda`, toma la tasa de `H2`, actualiza SQLite y no vuelve a leer Google Sheets hasta el dia siguiente.

La configuracion de cuentas destino se guarda tambien en el tab `PortalConfig`, creado automaticamente por la app. Asi no se pierden las cuentas cuando Railway hace redeploy.

## Persistencia en Railway

Para que no se borren usuarios, casos, comprobantes ni sesiones de trabajo entre deploys, agrega un volumen persistente en Railway montado en `/data`. La app lo detecta automaticamente y usa:

```txt
/data/deuda_bipbip.db
/data/uploads
```

Si no existe el volumen, Railway puede recrear el filesystem del deploy y volver a la base inicial.

## Columnas esperadas en Deuda

La app detecta encabezados, pero usa este fallback si no los encuentra:

```txt
B telefono
C cedula
D deuda_usd
E deuda_ves
F placa
G driver_id
H2 tasa del dia
```

Formato esperado en el portal:

```txt
Cedula: V12345678
Telefono: 4141234567
```

## Salida Conciliados

Cuando un pago se marca como `conciliado`, la app intenta agregar una fila al tab `Conciliados` con:

```txt
nombre, cedula, telefono, placa, driver_id, monto_conciliado,
referencia_validada, fecha_conciliacion, responsable, estado, desbloqueo
```

Si Google Sheets no esta configurado, el caso queda conciliado en SQLite y aparece en la vista `Lista para desbloqueo`.
