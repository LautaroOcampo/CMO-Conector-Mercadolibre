# Importación de ventas Meli → Odoo: WEBHOOK + POLLING + IDEMPOTENCIA

El conector importa ventas de Mercado Libre a Odoo con esta estrategia:

## 1. WEBHOOK (tiempo real)

- **Endpoint:** `POST /ml/notification`
- Mercado Libre envía una notificación cuando hay un cambio en una orden (`topic`: `orders` o `orders_v2`).
- El controlador extrae `order_id` y `user_id`, busca la cuenta por `meli_user_id` y llama a `ml.sale.update_or_create_from_meli(order_id, account_id=...)`.
- Solo se procesa si en la cuenta está activo **Procesar ventas en tiempo real** (`process_webhook_sales = True`).

## 2. POLLING (respaldo)

- **Cron:** "Sincronizar Órdenes de Venta (pooling)" cada **10 minutos**.
- Método: `ml.account.cron_poll_recent_orders()`.
- Para cada cuenta con `process_webhook_sales = True` y conectada:
  - Consulta a la API `orders/search` (seller=me, sort=date_desc, limit=50).
  - Por cada `order_id` devuelto llama a `update_or_create_from_meli(order_id, account_id=account.id)`.
- Cubre ventas que el webhook no entregó (caídas, retardos, etc.).

## 3. IDEMPOTENCIA

- **Clave única:** `(ml_order_id, company_id)` en `ml.sale` (constraint `ml_order_id_unique`).
- En `update_or_create_from_meli()`:
  - Se busca si ya existe una venta con ese `ml_order_id` en la compañía actual.
  - **Si existe:** se actualiza con los datos frescos de la API (`write`).
  - **Si no existe:** se crea (`create`). Si dos procesos crean a la vez (p. ej. webhook y cron), se captura `UniqueViolation` y se rehace la búsqueda y un `write`.
- Resultado: la misma orden de ML nunca se duplica en Odoo, venga por webhook, por polling o por ambos.

## Resumen

| Canal    | Frecuencia     | Función |
|----------|----------------|--------|
| Webhook  | En tiempo real | Notificación de ML → procesar orden |
| Polling  | Cada 10 min    | Consultar últimas 50 órdenes y sincronizar |
| Idempotencia | Siempre   | Mismo `ml_order_id` → actualizar o crear una sola vez |

Ambos canales usan el mismo método `update_or_create_from_meli()`, así que el comportamiento (crear orden Odoo, factura, cliente, stock) es el mismo; la idempotencia evita duplicados.
