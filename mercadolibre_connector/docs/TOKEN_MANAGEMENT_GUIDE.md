# Guía Completa: Manejo de Tokens de Mercado Libre

## 📋 Tabla de Contenidos

1. [Dónde Guardar los Tokens](#dónde-guardar-los-tokens)
2. [Cuándo Refrescar los Tokens](#cuándo-refrescar-los-tokens)
3. [Cómo Evitar que Expiren](#cómo-evitar-que-expiren)
4. [Arquitectura Recomendada](#arquitectura-recomendada)
5. [Uso de Webhooks](#uso-de-webhooks)
6. [Manejo de Errores](#manejo-de-errores)
7. [Flujo Completo](#flujo-completo)
8. [Buenas Prácticas](#buenas-prácticas)

---

## 🔐 Dónde Guardar los Tokens

### Ubicación en la Base de Datos

Los tokens se guardan en el modelo `ml.account`:

```python
# mercadolibre_connector/models/ml_account.py

class MlAccount(models.Model):
    _name = "ml.account"
    
    access_token = fields.Char(string="Access Token", copy=False)
    refresh_token = fields.Char(string="Refresh Token", copy=False)
    token_expiration = fields.Datetime(string="Token Expiration")
    is_connected = fields.Boolean(string="Connected", default=False)
```

### Campos de Token

- **`access_token`**: Token de acceso para hacer llamadas a la API (válido por ~6 horas)
- **`refresh_token`**: Token para obtener nuevos access_tokens (no expira, pero puede revocarse)
- **`token_expiration`**: Fecha y hora de expiración del access_token
- **`is_connected`**: Indica si la cuenta está conectada y tiene tokens válidos

### Seguridad

- Los tokens se guardan como `Char` (texto plano) en la base de datos
- **Recomendación**: En producción, considerar encriptar los tokens sensibles
- El campo `copy=False` evita que se copien accidentalmente

---

## ⏰ Cuándo Refrescar los Tokens

### Estrategia Actual Implementada

#### 1. **Verificación Proactiva** (`_ensure_valid_token`)

Se llama antes de cada operación con la API:

```python
def _ensure_valid_token(self):
    """
    Verifica si el token está próximo a expirar (menos de 2 horas) 
    y lo refresca automáticamente.
    """
    # Si expira en menos de 2 horas → refrescar
    if time_until_expiration < timedelta(hours=2):
        self.refresh_access_token()
```

**Ventajas:**
- ✅ Refresca automáticamente antes de usar
- ✅ No requiere intervención del usuario
- ✅ Funciona incluso si no hay usuarios activos

**Desventajas:**
- ⚠️ Puede hacer llamadas innecesarias si no hay actividad
- ⚠️ Depende de que haya actividad para refrescar

#### 2. **Cron Job Programado** (`cron_refresh_expiring_tokens`)

Se ejecuta cada 2 horas automáticamente:

```xml
<!-- mercadolibre_connector/data/cron_data.xml -->
<record id="ir_cron_refresh_ml_tokens" model="ir.cron">
    <field name="name">Refrescar Tokens de MercadoLibre</field>
    <field name="interval_number">2</field>
    <field name="interval_type">hours</field>
    <field name="code">model.cron_refresh_expiring_tokens()</field>
</record>
```

**Ventajas:**
- ✅ Refresca automáticamente sin necesidad de actividad
- ✅ Funciona incluso si no hay usuarios activos
- ✅ Garantiza que los tokens nunca expiren

**Desventajas:**
- ⚠️ Puede refrescar tokens que no se están usando

### Cuándo se Refresca

1. **Automáticamente cada 2 horas** (cron job)
2. **Antes de cada operación con la API** (si expira en < 2 horas)
3. **Manualmente** (botón "Refrescar token" en la interfaz)

---

## 🛡️ Cómo Evitar que Expiren

### Estrategia de Doble Protección

#### 1. **Cron Job Preventivo**

```python
@api.model
def cron_refresh_expiring_tokens(self):
    """
    Cron job que se ejecuta cada 2 horas para refrescar tokens 
    que están próximos a expirar.
    """
    accounts = self.search([
        ('access_token', '!=', False),
        ('refresh_token', '!=', False),
        ('is_connected', '=', True)
    ])
    
    for account in accounts:
        try:
            account._ensure_valid_token()
        except Exception as e:
            _logger.error("Error refrescando token: %s", e)
```

**Frecuencia recomendada:** Cada 2 horas (tokens expiran en ~6 horas)

#### 2. **Verificación Pre-Operación**

Cada método que usa la API llama a `_ensure_valid_token()`:

```python
def action_update_all_publications_price_stock(self):
    # Verificar y refrescar token antes de usar
    self._ensure_valid_token()
    
    # Ahora usar el token con seguridad
    headers = {"Authorization": f"Bearer {self.access_token}"}
    # ... operación con API
```

### Ventana de Seguridad

- **Tokens expiran en:** ~6 horas
- **Refresco automático cuando faltan:** 2 horas
- **Cron job cada:** 2 horas
- **Resultado:** Los tokens nunca expiran si el cron está activo

---

## 🏗️ Arquitectura Recomendada

### Opción 1: Cron Jobs de Odoo (Implementada) ✅

**Ventajas:**
- ✅ Integrado en Odoo
- ✅ No requiere servicios externos
- ✅ Fácil de mantener
- ✅ Funciona automáticamente

**Desventajas:**
- ⚠️ Depende de que Odoo esté corriendo
- ⚠️ Si Odoo se cae, los tokens pueden expirar

**Implementación actual:**
```xml
<record id="ir_cron_refresh_ml_tokens" model="ir.cron">
    <field name="interval_number">2</field>
    <field name="interval_type">hours</field>
    <field name="code">model.cron_refresh_expiring_tokens()</field>
</record>
```

### Opción 2: Worker/Servicio Externo

**Ventajas:**
- ✅ Independiente de Odoo
- ✅ Puede monitorear múltiples instancias
- ✅ Más robusto ante fallos

**Desventajas:**
- ⚠️ Requiere infraestructura adicional
- ⚠️ Más complejo de mantener
- ⚠️ Necesita sincronización con Odoo

**Ejemplo con Python:**
```python
# worker_refresh_tokens.py
import requests
import time
from odoo import api, SUPERUSER_ID

def refresh_tokens_worker():
    env = api.Environment(cr, SUPERUSER_ID, {})
    while True:
        accounts = env['ml.account'].search([
            ('is_connected', '=', True)
        ])
        for account in accounts:
            account._ensure_valid_token()
        time.sleep(7200)  # 2 horas
```

### Recomendación

**Usar Cron Jobs de Odoo** (opción actual) porque:
- Es más simple
- Está integrado
- Funciona bien para la mayoría de casos
- Solo requiere que Odoo esté corriendo

---

## 🔔 Uso de Webhooks

### Configuración del Webhook

1. **Registrar el webhook en Mercado Libre:**
   ```
   POST https://api.mercadolibre.com/apps/{app_id}/webhooks
   {
     "topic": "orders",
     "url": "https://tu-dominio.com/ml/notification"
   }
   ```

2. **Endpoint en Odoo:**
   ```python
   @http.route('/ml/notification', type='http', auth='public', methods=['POST'], csrf=False)
   def ml_notification(self, **kwargs):
       # Procesar notificación
   ```

### Qué Hacer si el Token Está Vencido

#### Estrategia de Retry con Refresh

**✅ Implementado en `ml_account.py`:**

```python
def _ml_request_with_retry(self, method, url, headers=None, json=None, data=None, files=None, params=None, max_retries=3, timeout=30):
    """
    Hace una request a la API de Mercado Libre con retry automático si el token expira.
    """
    # 1. Verificar token antes de la primera request
    self._ensure_valid_token()
    
    # 2. Preparar headers con Authorization
    if headers is None:
        headers = {}
    if 'Authorization' not in headers:
        headers['Authorization'] = f"Bearer {self.access_token}"
    
    # 3. Intentar request con retry
    for attempt in range(1, max_retries + 1):
        response = requests.request(method, url, headers=headers, json=json, timeout=timeout)
        
        # Si 401 → refrescar token y reintentar
        if response.status_code == 401:
            self.refresh_access_token()
            headers['Authorization'] = f"Bearer {self.access_token}"
            continue
        
        # Si 403 → token revocado, requerir nueva autorización
        if response.status_code == 403:
            self.is_connected = False
            raise UserError(_("Token revocado. Autorice nuevamente."))
        
        # Si 429 → rate limit, esperar y reintentar
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 60))
            time.sleep(retry_after)
            continue
        
        return response
```

**Uso recomendado:**
```python
# ❌ ANTES (sin retry automático):
headers = {"Authorization": f"Bearer {self.access_token}"}
response = requests.get(url, headers=headers)
if response.status_code == 401:
    # Manejar manualmente...

# ✅ AHORA (con retry automático):
response = self._ml_request_with_retry('GET', url)
# El método maneja automáticamente:
# - Verificación de token antes de la request
# - Refresh automático si expira (401)
# - Retry con backoff exponencial
# - Manejo de rate limits (429)
# - Manejo de errores retryables

# Ejemplos de uso:
# GET request
response = account._ml_request_with_retry('GET', 'https://api.mercadolibre.com/orders/123')

# POST request con JSON
response = account._ml_request_with_retry(
    'POST', 
    'https://api.mercadolibre.com/items',
    json={'title': 'Producto', 'price': 100}
)

# PUT request con archivos
response = account._ml_request_with_retry(
    'PUT',
    'https://api.mercadolibre.com/items/MLA123',
    json={'price': 200},
    files={'picture': open('image.jpg', 'rb')}
)
```

### Flujo de Webhook con Token Vencido

```
1. Webhook llega → Procesar orden
2. Llamar API de ML → Error 401 (token expirado)
3. Refrescar token automáticamente
4. Reintentar llamada a API
5. Procesar orden exitosamente
```

---

## ⚠️ Manejo de Errores

### Errores Comunes

#### 1. **401 Unauthorized** (Token Expirado)

**Solución:**
```python
if response.status_code == 401:
    # Refrescar token y reintentar
    self._ensure_valid_token()
    headers["Authorization"] = f"Bearer {self.access_token}"
    response = requests.request(method, url, headers=headers, json=payload)
```

#### 2. **403 Forbidden** (Token Revocado)

**Solución:**
```python
if response.status_code == 403:
    # Token revocado, requerir nueva autorización
    self.is_connected = False
    self.access_token = False
    raise UserError(_("Token revocado. Por favor, autorice la cuenta nuevamente."))
```

#### 3. **429 Too Many Requests** (Rate Limit)

**Solución:**
```python
if response.status_code == 429:
    # Esperar y reintentar
    retry_after = int(response.headers.get('Retry-After', 60))
    time.sleep(retry_after)
    # Reintentar
```

### Estrategia de Retry

```python
def _ml_request_with_retry(self, method, url, headers, payload, max_retries=3):
    retryable_status = {401, 408, 409, 423, 429, 500, 502, 503, 504}
    
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, headers=headers, json=payload, timeout=30)
            
            # Si es 401, refrescar token
            if response.status_code == 401:
                self._ensure_valid_token()
                headers["Authorization"] = f"Bearer {self.access_token}"
                continue
            
            # Si es retryable, esperar y reintentar
            if response.status_code in retryable_status and attempt < max_retries:
                wait_time = attempt * 2
                time.sleep(wait_time)
                continue
            
            return response
            
        except Exception as e:
            if attempt < max_retries:
                time.sleep(attempt)
                continue
            raise
```

---

## 📊 Flujo Completo: Venta en ML → Orden en Odoo

### Paso a Paso

```
┌─────────────────────────────────────────────────────────────┐
│ 1. VENTA EN MERCADO LIBRE                                    │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. MERCADO LIBRE ENVÍA WEBHOOK                              │
│    POST /ml/notification                                    │
│    {                                                         │
│      "topic": "orders",                                     │
│      "resource": "/orders/2000014810306306",                │
│      "user_id": 3118328100                                  │
│    }                                                         │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. WEBHOOK CONTROLLER RECIBE NOTIFICACIÓN                   │
│    - Verifica que process_webhook_sales = True              │
│    - Extrae order_id del resource                           │
│    - Busca cuenta ML por meli_user_id                      │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. LLAMAR update_or_create_from_meli()                      │
│    - Verifica token: _ensure_valid_token()                  │
│    - Si expira en < 2h → refresca automáticamente           │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. OBTENER ORDEN DESDE API DE ML                            │
│    GET /orders/{order_id}                                   │
│    - Si 401 → refrescar token y reintentar                  │
│    - Si éxito → parsear datos de la orden                   │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. CREAR/ACTUALIZAR ml.sale                                 │
│    - Crear cliente si no existe                             │
│    - Crear líneas de venta                                  │
│    - Manejar kits (BOM phantom)                             │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 7. CREAR ORDEN DE VENTA EN ODOO                             │
│    - Crear sale.order                                       │
│    - Agregar líneas de productos                            │
│    - Aplicar impuestos si corresponde                       │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 8. CREAR FACTURA (SI auto_create_invoice = True)            │
│    - Crear account.move                                     │
│    - Confirmar factura                                      │
│    - Registrar pago si corresponde                          │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│ 9. ACTUALIZAR STOCK                                          │
│    - Descontar stock de productos                           │
│    - Si es kit → descontar componentes                      │
└─────────────────────────────────────────────────────────────┘
```

### Código del Flujo

```python
# 1. Webhook recibe notificación
@http.route('/ml/notification', type='http', auth='public', methods=['POST'])
def ml_notification(self, **kwargs):
    data = json.loads(request.httprequest.data)
    order_id = data['resource'].split('/')[-1]
    user_id = data['user_id']
    
    # 2. Buscar cuenta
    account = request.env['ml.account'].search([('meli_user_id', '=', str(user_id))])
    
    # 3. Verificar si procesar
    if not account.process_webhook_sales:
        return {"status": "ok"}  # Ignorar
    
    # 4. Procesar orden
    result = request.env['ml.sale'].update_or_create_from_meli(order_id, account_id=account.id)
    
    return {"status": "ok"}

# 5. update_or_create_from_meli
def update_or_create_from_meli(self, order_id, account_id=None):
    # Verificar token
    account._ensure_valid_token()  # Refresca si es necesario
    
    # Obtener orden desde API
    response = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", 
                           headers={"Authorization": f"Bearer {account.access_token}"})
    
    # Si 401, refrescar y reintentar
    if response.status_code == 401:
        account._ensure_valid_token()
        response = requests.get(...)  # Reintentar
    
    # Procesar orden...
    order_data = response.json()
    
    # Crear ml.sale
    ml_sale = self.create({...})
    
    # Crear sale.order
    sale_order = ml_sale.create_odoo_sale_order()
    
    # Crear factura si corresponde
    if account.auto_create_invoice:
        invoice = sale_order._create_invoices()
        invoice.action_post()
    
    return ml_sale
```

---

## ✅ Buenas Prácticas

### 1. No Depender del Login del Usuario

**❌ MAL:**
```python
# Refrescar solo cuando el usuario hace login
def action_login(self):
    self.refresh_access_token()  # Solo se refresca al login
```

**✅ BIEN:**
```python
# Refrescar automáticamente con cron
@api.model
def cron_refresh_expiring_tokens(self):
    # Se ejecuta cada 2 horas, sin necesidad de usuarios
    accounts = self.search([('is_connected', '=', True)])
    for account in accounts:
        account._ensure_valid_token()
```

### 2. Manejo de Errores 401/403

**✅ Implementar retry con refresh:**
```python
def _ml_request_with_retry(self, method, url, headers, payload):
    for attempt in range(3):
        response = requests.request(method, url, headers=headers, json=payload)
        
        if response.status_code == 401:
            # Token expirado → refrescar y reintentar
            self._ensure_valid_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            continue
        
        if response.status_code == 403:
            # Token revocado → requerir nueva autorización
            self.is_connected = False
            raise UserError(_("Token revocado. Autorice nuevamente."))
        
        return response
```

### 3. Logs y Monitoreo

**✅ Logging detallado:**
```python
_logger.info("🔄 Token próximo a expirar. Refrescando...")
_logger.info("✅ Token refrescado exitosamente")
_logger.error("❌ Error al refrescar token: %s", e)
_logger.warning("⚠️ Token expirado, refrescando automáticamente")
```

**✅ Monitoreo recomendado:**
- Alertas cuando falla el refresh
- Alertas cuando el token está próximo a expirar sin refresh
- Métricas de frecuencia de refresh

### 4. Validación de Tokens

**✅ Verificar antes de usar:**
```python
def any_method_that_uses_api(self):
    # Siempre verificar antes de usar
    if not self._ensure_valid_token():
        raise UserError(_("No se pudo validar el token. Por favor, autorice la cuenta nuevamente."))
    
    # Ahora usar el token con seguridad
    headers = {"Authorization": f"Bearer {self.access_token}"}
```

### 5. Manejo de Webhooks con Token Vencido

**✅ Estrategia:**
```python
@http.route('/ml/notification', type='http', auth='public', methods=['POST'])
def ml_notification(self, **kwargs):
    try:
        # Procesar webhook
        result = request.env['ml.sale'].update_or_create_from_meli(order_id)
    except Exception as e:
        # Si falla por token, refrescar y reintentar
        if "401" in str(e) or "Unauthorized" in str(e):
            account._ensure_valid_token()
            result = request.env['ml.sale'].update_or_create_from_meli(order_id)
        
        # Siempre retornar 200 para evitar reenvíos
        return {"status": "ok"}
```

---

## 📝 Resumen de Implementación Actual

### ✅ Lo que ya está implementado:

1. **Tokens guardados en DB** (`ml.account`)
2. **Refresh automático cada 2 horas** (cron job)
3. **Verificación pre-operación** (`_ensure_valid_token`)
4. **Manejo de webhooks** con verificación de token
5. **Logs detallados** para debugging
6. **Método helper con retry automático** (`_ml_request_with_retry`)

### 🔧 Mejoras Recomendadas:

1. **Encriptar tokens en producción**
2. **Alertas cuando falla el refresh**
3. **Métricas de uso de tokens**
4. **Migrar todas las llamadas API a usar `_ml_request_with_retry`**

### 🚀 Migración Gradual

Para migrar código existente a usar el nuevo método helper:

```python
# ANTES:
self._ensure_valid_token()
headers = {"Authorization": f"Bearer {self.access_token}"}
response = requests.get(url, headers=headers)
if response.status_code == 401:
    # Manejo manual...

# DESPUÉS:
response = self._ml_request_with_retry('GET', url)
# Todo el manejo de errores y retry es automático
```

---

## 🎯 Conclusión

La implementación actual usa una **estrategia de doble protección**:

1. **Cron job preventivo** (cada 2 horas)
2. **Verificación pre-operación** (antes de cada llamada API)

Esto garantiza que los tokens **nunca expiren** si:
- ✅ El cron job está activo
- ✅ Odoo está corriendo
- ✅ Hay conexión a internet

**Recomendación:** Mantener esta arquitectura, es robusta y simple.

