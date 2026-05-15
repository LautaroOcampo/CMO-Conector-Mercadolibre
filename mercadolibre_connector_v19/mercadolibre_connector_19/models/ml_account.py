from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import requests
from datetime import datetime, timedelta
import base64
import hashlib
import os
import json
import secrets
import time


_logger = logging.getLogger(__name__)

# OAuth: dominio de autorización según país (ISO 3166-1 alpha-2).
# Fallback: https://auth.mercadolibre.com/authorization
ML_AUTH_AUTHORIZATION_URL_BY_COUNTRY = {
    'AR': 'https://auth.mercadolibre.com.ar/authorization',
    'BR': 'https://auth.mercadolivre.com.br/authorization',
    'MX': 'https://auth.mercadolibre.com.mx/authorization',
    'CL': 'https://auth.mercadolibre.cl/authorization',
    'CO': 'https://auth.mercadolibre.com.co/authorization',
    'UY': 'https://auth.mercadolibre.com.uy/authorization',
    'PE': 'https://auth.mercadolibre.com.pe/authorization',
    'CR': 'https://auth.mercadolibre.co.cr/authorization',
    'EC': 'https://auth.mercadolibre.com.ec/authorization',
    'VE': 'https://auth.mercadolibre.com.ve/authorization',
    'PA': 'https://auth.mercadolibre.com.pa/authorization',
    'BO': 'https://auth.mercadolibre.com.bo/authorization',
    'PY': 'https://auth.mercadolibre.com.py/authorization',
    'DO': 'https://auth.mercadolibre.com.do/authorization',
    'GT': 'https://auth.mercadolibre.com.gt/authorization',
    'HN': 'https://auth.mercadolibre.com.hn/authorization',
    'NI': 'https://auth.mercadolibre.com.ni/authorization',
    'SV': 'https://auth.mercadolibre.com.sv/authorization',
}

# Site Mercado Libre por país (usuarios test). País sin entrada → MLA.
ML_SITE_ID_BY_COUNTRY = {
    'AR': 'MLA', 'BR': 'MLB', 'MX': 'MLM', 'CL': 'MLC', 'CO': 'MCO',
    'UY': 'MLU', 'PE': 'MPE', 'VE': 'MLV', 'EC': 'MEC', 'PA': 'MPA',
    'BO': 'MBO', 'PY': 'MPY', 'CR': 'MCR',
}


class MlAccount(models.Model):
    _name = "ml.account"
    _inherit = ["mail.activity.mixin"]
    _description = "Mercado Libre Account"
    _rec_name = "name"

    name = fields.Char(string="Account Name", required=True)
    client_id = fields.Char(string="Client ID", required=True)
    client_secret = fields.Char(
        string="Client Secret",
        required=True,
        groups="mercadolibre_connector.group_ml_admin",
    )
    redirect_uri = fields.Char(string="Redirect URI", required=True, default="/mercadolibre/oauth/callback")
    country_id = fields.Many2one(
        'res.country',
        string='País de la cuenta',
        required=True,
        default=lambda self: self._default_ml_account_country_id(),
        help=(
            'País del sitio Mercado Libre de esta cuenta. Define la URL de autorización OAuth '
            'y el site_id usado en operaciones por país (p. ej. usuario test).'
        ),
    )
    access_token = fields.Char(string="Access Token", copy=False, groups="mercadolibre_connector.group_ml_admin")
    refresh_token = fields.Char(string="Refresh Token", copy=False, groups="mercadolibre_connector.group_ml_admin")
    token_expiration = fields.Datetime(string="Token Expiration")
    is_connected = fields.Boolean(string="Connected", default=False)
    code_verifier = fields.Char(string="PKCE Verifier", copy=False, groups="mercadolibre_connector.group_ml_admin")
    oauth_state_token = fields.Char(
        string='OAuth state (CSRF)',
        copy=False,
        help='Token de un solo uso para validar el parámetro state en el callback OAuth (anti-CSRF).',
    )
    meli_user_id = fields.Char(
        string="ML User ID",
        copy=False,
        help="ID del usuario de MercadoLibre asociado a este access_token (se completa automáticamente)."
    )
    meli_nickname = fields.Char(
        string="ML Nickname",
        copy=False,
        help="Nickname del usuario de MercadoLibre (se completa automáticamente)."
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string="Warehouse for Stock Sync",
        help="El stock de Mercado Libre se sincronizará con este almacén."
    )
    stock_type = fields.Selection(
        [
            ('available', 'Stock Disponible'),
            ('expected', 'Stock Esperado'),
        ],
        string="Tipo de Stock a Sincronizar",
        default='available',
        required=True,
        help="Stock Disponible: Stock físico disponible. Stock Esperado: Stock disponible menos reservado más entradas esperadas."
    )
    product_brand_id = fields.Char(string="Marca")
    ml_publication_ids = fields.One2many(
    "ml.publication",
    "ml_account_id",
    string="Publicaciones"
    )
    invoice_journal_id = fields.Many2one(
        'account.journal',
        string='Diario para Facturas',
        domain=[('type', '=', 'sale')],
        help='Diario que se usará al crear facturas automáticamente desde ventas de MercadoLibre.'
    )

    default_income_account_id = fields.Many2one(
        'account.account',
        string='Cuenta de Ingresos Predeterminada',
        domain=[('deprecated', '=', False)],
        help='Cuenta contable de ingresos para líneas de factura de ventas ML. Si se configura, reemplaza la cuenta por defecto del producto/categoría.'
    )

    fallback_product_id = fields.Many2one(
        'product.product',
        string='Producto por Defecto (Errores SKU)',
        domain=[('active', '=', True)],
        help='Producto a usar cuando no se pueda mapear una línea de venta de MercadoLibre por SKU/publicación.'
    )
    default_order_partner_id = fields.Many2one(
        'res.partner',
        string='Contacto por Defecto (Órdenes sin cliente)',
        domain="[('is_company', '=', False)]",
        help=(
            'Contacto que se usará al crear la orden de venta cuando el comprador de ML '
            'no tenga datos mínimos para crear/matchear cliente.'
        ),
    )
    
    payment_journal_id = fields.Many2one(
        'account.journal',
        string='Diario para Pagos',
        domain=[('type', 'in', ['bank', 'cash'])],
        help='Opcional. El conector ya no registra pagos automáticos al facturar ventas ML; puede usarlo como referencia o registrar cobros a mano.',
    )
    
    default_tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto por Defecto',
        domain=[('type_tax_use', '=', 'sale')],
        help='Impuesto que se aplicará a las facturas de MercadoLibre. Si no se configura, se detectará automáticamente según el país.'
    )
    
    auto_create_invoice = fields.Boolean(
        string='Crear Factura Automáticamente',
        default=True,
        help=(
            'Si está activado, se creará y publicará la factura al importar la venta (sin cobro automático). '
            'Si está desactivado, tampoco se creará ni actualizará el contacto del cliente en Odoo al importar ventas.'
        ),
    )
    
    process_webhook_sales = fields.Boolean(
        string='Procesar Ventas en Tiempo Real',
        default=True,
        help='Si está activado: (1) Se procesan las notificaciones del webhook de ML en tiempo real. (2) Un cron cada 10 min hace polling de las últimas órdenes como respaldo. Ambas vías son idempotentes (no se duplican ventas).'
    )

    polling_hours_back = fields.Integer(
        string='Horas hacia atrás en polling',
        default=24,
        help='Solo se consideran órdenes creadas en las últimas N horas al hacer el polling (respaldo del webhook). Es el único parámetro configurable del polling.'
    )
    
    auto_sync_stock_on_odoo_change = fields.Boolean(
        string='Sincronizar Stock Automáticamente',
        default=False,
        help='Si está activado, cualquier cambio de stock en Odoo actualizará automáticamente el stock en MercadoLibre. El stock se copia directamente (no se suma ni resta).'
    )

    sync_price_pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Lista de precios (Mercado Libre)',
        help='Precio que se enviará a Mercado Libre al sincronizar precios: según esta lista y el producto vinculado a cada publicación.',
    )
    auto_sync_price_on_odoo_change = fields.Boolean(
        string='Sincronizar precio automáticamente',
        default=False,
        help=(
            'Si está activado, al cambiar precios en Odoo (producto o ítems de la lista de precios elegida) '
            'se actualiza el precio en Mercado Libre usando el precio de esa lista para la variante vinculada a cada publicación.'
        ),
    )

    timezone = fields.Selection(
        [
            ('America/Argentina/Buenos_Aires', 'Argentina (Buenos Aires)'),
            ('America/Sao_Paulo', 'Brasil (São Paulo)'),
            ('America/Mexico_City', 'México (Ciudad de México)'),
            ('America/Santiago', 'Chile (Santiago)'),
            ('America/Bogota', 'Colombia (Bogotá)'),
            ('America/Caracas', 'Venezuela (Caracas)'),
            ('America/Lima', 'Perú (Lima)'),
            ('America/Montevideo', 'Uruguay (Montevideo)'),
            ('UTC', 'UTC'),
        ],
        string='Zona Horaria',
        default='America/Argentina/Buenos_Aires',
        required=True,
        help=(
            'Zona horaria del mercado para las ventas importadas: interpreta fechas ISO sin zona '
            'como hora local en este huso; las fechas con offset de ML se guardan en UTC de forma '
            'coherente con Odoo.'
        ),
    )
    
    auto_pause_when_max_stock = fields.Boolean(
        string='Pausar cuando alcanza Stock Máximo',
        default=False,
        help='Si está activado, las publicaciones se pausarán automáticamente cuando el stock sea menor o igual al valor máximo configurado (inclusive).'
    )
    
    max_stock_to_pause = fields.Integer(
        string='Máximo de Stock para Pausar',
        default=0,
        help='Cuando el stock de una publicación sea menor o igual a este valor (inclusive) y esté activa, se pausará automáticamente. Si una publicación tiene reglas personalizadas, se usarán esas en su lugar.'
    )
    
    auto_activate_when_min_stock = fields.Boolean(
        string='Activar cuando alcanza Stock Mínimo',
        default=False,
        help='Si está activado, las publicaciones se activarán automáticamente cuando el stock sea mayor o igual al valor mínimo configurado (inclusive).'
    )
    
    min_stock_to_activate = fields.Integer(
        string='Mínimo de Stock para Activar',
        default=0,
        help='Cuando el stock de una publicación sea mayor o igual a este valor (inclusive) y esté pausada, se activará automáticamente. Si una publicación tiene reglas personalizadas, se usarán esas en su lugar.'
    )
    
    # ⚠️ TEMPORAL: Campo para usuarios test
    # TODO: REMOVER ESTE CAMPO CUANDO YA NO SE NECESITE
    test_user_ids = fields.One2many(
        'ml.test.user',
        'ml_account_id',
        string='Usuarios Test',
        help='Usuarios test de MercadoLibre creados para esta cuenta'
    )
    test_users_count = fields.Integer(
        string='Cantidad de Usuarios Test',
        compute='_compute_test_users_count',
        help='Cantidad de usuarios test guardados'
    )

    sync_log_error_recent_count = fields.Integer(
        string='Errores sync (30 días)',
        compute='_compute_sync_log_error_recent_count',
        help='Cantidad de registros ml.sync.log con error en los últimos 30 días.',
    )
    
    @api.depends('test_user_ids')
    def _compute_test_users_count(self):
        for record in self:
            record.test_users_count = len(record.test_user_ids)

    @api.constrains('auto_sync_price_on_odoo_change', 'sync_price_pricelist_id')
    def _check_ml_auto_sync_price_pricelist(self):
        for rec in self:
            if rec.auto_sync_price_on_odoo_change and not rec.sync_price_pricelist_id:
                raise ValidationError(
                    _('Debe elegir una lista de precios para activar la sincronización automática de precios a Mercado Libre.')
                )

    # No usar @api.depends('id'): Odoo 19+ lo rechaza. El valor se invalida al crear ml.sync.log.
    @api.depends('name')
    def _compute_sync_log_error_recent_count(self):
        Log = self.env['ml.sync.log'].sudo()
        threshold = fields.Datetime.now() - timedelta(days=30)
        for rec in self:
            rec.sync_log_error_recent_count = Log.search_count([
                ('ml_account_id', '=', rec.id),
                ('result', '=', 'error'),
                ('create_date', '>=', threshold),
            ])
    # =====================================================
    # FIN SECCIÓN TEMPORAL
    # =====================================================

    @api.model
    def _default_ml_account_country_id(self):
        return self.env.ref('base.ar', raise_if_not_found=False) or self.env['res.country'].search(
            [('code', '=', 'AR')], limit=1
        )

    def _mercadolibre_auth_authorization_base_url(self):
        """URL base de OAuth (…/authorization) según `country_id`."""
        self.ensure_one()
        code = (self.country_id.code or 'AR').upper()
        return ML_AUTH_AUTHORIZATION_URL_BY_COUNTRY.get(
            code, 'https://auth.mercadolibre.com/authorization'
        )

    def _mercadolibre_site_id(self):
        """Site_id de Mercado Libre (MLA, MLB, …) según `country_id`."""
        self.ensure_one()
        code = (self.country_id.code or 'AR').upper()
        return ML_SITE_ID_BY_COUNTRY.get(code, 'MLA')

    def _ml_build_oauth_authorization_url(self):
        """Genera PKCE + state OAuth, los persiste en la cuenta y devuelve (url_ml, verifier, oauth_state)."""
        self.ensure_one()
        verifier = base64.urlsafe_b64encode(os.urandom(40)).decode('utf-8').rstrip('=')
        oauth_state = secrets.token_urlsafe(32)
        self.sudo().write({
            'code_verifier': verifier,
            'oauth_state_token': oauth_state,
        })
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('utf-8')).digest()
        ).decode('utf-8').rstrip('=')
        base = self._mercadolibre_auth_authorization_base_url()
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'state': oauth_state,
        }
        qs = '&'.join(
            f'{k}={requests.utils.requote_uri(str(v))}' for k, v in params.items()
        )
        full_url = f'{base}?{qs}'
        return full_url, verifier, oauth_state

    def get_authorize_url(self):
        """Return Mercado Libre authorize URL with PKCE (OAuth 2.0)."""
        url, _verifier, _state = self._ml_build_oauth_authorization_url()
        return url



    def _save_tokens(self, token_data):
        """Guardar tokens y calcular expiración."""
        self.access_token = token_data.get('access_token')
        self.refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in')  # segundos
        if expires_in:
            self.token_expiration = fields.Datetime.to_datetime(datetime.utcnow() + timedelta(seconds=int(expires_in)))
        self.is_connected = bool(self.access_token)
        self.sudo().write({
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_expiration': self.token_expiration,
            'is_connected': self.is_connected,
        })
        # Guardar user_id/nickname para poder enrutar webhooks por cuenta
        try:
            self._sync_meli_user_info()
        except Exception as e:
            _logger.warning("⚠️ No se pudo sincronizar users/me para la cuenta %s: %s", self.name, str(e))

    def _sync_meli_user_info(self):
        """Obtiene /users/me y guarda meli_user_id + meli_nickname en la cuenta."""
        self.ensure_one()
        if not self.access_token:
            return False
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()

        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.get("https://api.mercadolibre.com/users/me", headers=headers, timeout=15)
        if not resp.ok:
            raise UserError(_("Error al obtener users/me: %s") % resp.text)

        data = resp.json() or {}
        user_id = data.get("id")
        nickname = data.get("nickname")
        self.sudo().write({
            "meli_user_id": str(user_id) if user_id else False,
            "meli_nickname": nickname or False,
        })
        return True

    def exchange_code_for_token(self, code):
        """Intercambia 'code' por tokens usando PKCE."""
        url = "https://api.mercadolibre.com/oauth/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,  # ✅ agregar esto
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": self.code_verifier,
        }
    
        try:
            resp = requests.post(url, data=data, timeout=15)
            resp.raise_for_status()
            token_data = resp.json()
            self._save_tokens(token_data)
            return token_data
        except Exception as e:
            _logger.exception("Error exchanging code for token: %s", e)
            raise





    def refresh_access_token(self):
        """Refrescar token usando refresh_token."""
        if not self.refresh_token:
            raise ValueError(_("No refresh_token available"))
        url = "https://api.mercadolibre.com/oauth/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token
        }
        try:
            resp = requests.post(url, data=data, timeout=15)
            resp.raise_for_status()
            token_data = resp.json()
            self._save_tokens(token_data)
            _logger.info("✅ Token refrescado exitosamente para cuenta %s", self.name)
            return token_data
        except Exception as e:
            _logger.exception("Error refreshing token: %s", e)
            raise

    def _ensure_valid_token(self):
        """
        Verifica si el token está próximo a expirar (menos de 1 hora) y lo refresca automáticamente.
        Este método debe llamarse antes de usar el access_token en cualquier operación con la API.
        """
        self.ensure_one()
        
        if not self.access_token:
            _logger.warning("⚠️ No hay access_token para la cuenta %s", self.name)
            return False
        
        if not self.token_expiration:
            _logger.warning("⚠️ No hay fecha de expiración para el token de la cuenta %s. Intentando refrescar...", self.name)
            try:
                self.refresh_access_token()
                return True
            except Exception as e:
                _logger.error("❌ Error al refrescar token sin fecha de expiración: %s", e)
                return False
        
        # Calcular tiempo restante hasta la expiración
        now = fields.Datetime.now()
        expiration = fields.Datetime.to_datetime(self.token_expiration)
        time_until_expiration = expiration - now
        
        # Si el token expira en menos de 1 hora, refrescarlo automáticamente
        if time_until_expiration < timedelta(hours=1):
            _logger.info("🔄 Token próximo a expirar en %s (menos de 1 hora). Refrescando automáticamente...", time_until_expiration)
            try:
                self.refresh_access_token()
                return True
            except Exception as e:
                _logger.error("❌ Error al refrescar token automáticamente: %s", e)
                return False
        
        _logger.debug("✅ Token válido para cuenta %s. Tiempo restante: %s", self.name, time_until_expiration)
        return True

    def _ml_request_with_retry(self, method, url, headers=None, json=None, data=None, files=None, params=None, max_retries=3, timeout=30):
        """
        Hace una request a la API de Mercado Libre con retry automático si el token expira.
        
        Args:
            method: 'GET', 'POST', 'PUT', 'DELETE'
            url: URL completa de la API
            headers: Headers HTTP (si no se proporciona, se agrega Authorization automáticamente)
            json: Payload JSON para POST/PUT
            data: Payload form-data para POST/PUT
            files: Archivos para upload
            params: Query parameters para GET
            max_retries: Número máximo de reintentos (default: 3)
            timeout: Timeout en segundos (default: 30)
        
        Returns:
            Response object de requests
            
        Raises:
            requests.exceptions.RequestException: Si falla después de todos los reintentos
        """
        self.ensure_one()
        
        # Asegurar que el token sea válido antes de la primera request
        self._ensure_valid_token()
        
        # Preparar headers si no se proporcionaron
        if headers is None:
            headers = {}
        
        # Agregar Authorization si no está presente
        if 'Authorization' not in headers:
            headers['Authorization'] = f"Bearer {self.access_token}"
        
        # Códigos de estado que requieren retry
        retryable_status = {401, 408, 409, 423, 429, 500, 502, 503, 504}
        
        for attempt in range(1, max_retries + 1):
            try:
                # Preparar kwargs para requests
                kwargs = {
                    'headers': headers,
                    'timeout': timeout
                }
                
                if json is not None:
                    kwargs['json'] = json
                if data is not None:
                    kwargs['data'] = data
                if files is not None:
                    kwargs['files'] = files
                if params is not None:
                    kwargs['params'] = params
                
                # Hacer la request
                response = requests.request(method, url, **kwargs)
                
                # Si el token expiró (401), refrescar y reintentar
                if response.status_code == 401:
                    _logger.warning("🔄 Token expirado durante request (intento %d/%d). Refrescando...", attempt, max_retries)
                    try:
                        self.refresh_access_token()
                        headers['Authorization'] = f"Bearer {self.access_token}"
                        continue  # Reintentar con nuevo token
                    except Exception as refresh_error:
                        _logger.error("❌ Error al refrescar token: %s", refresh_error)
                        if attempt < max_retries:
                            continue
                        raise
                
                # Si es 403, el token fue revocado
                if response.status_code == 403:
                    _logger.error("❌ Token revocado para cuenta %s. Se requiere nueva autorización.", self.name)
                    self.sudo().write({
                        'is_connected': False,
                        'access_token': False
                    })
                    raise UserError(_("Token revocado. Por favor, autorice la cuenta nuevamente."))
                
                # Si es rate limit (429), esperar y reintentar
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    _logger.warning("⏳ Rate limit alcanzado. Esperando %d segundos...", retry_after)
                    if attempt < max_retries:
                        time.sleep(retry_after)
                        continue
                
                # Si es otro error retryable, esperar y reintentar
                if response.status_code in retryable_status and attempt < max_retries:
                    wait_time = attempt * 2  # Backoff exponencial: 2s, 4s, 6s
                    _logger.warning("⚠️ Error %d en request (intento %d/%d). Reintentando en %d segundos...", 
                                   response.status_code, attempt, max_retries, wait_time)
                    time.sleep(wait_time)
                    continue
                
                # Si llegamos aquí, la request fue exitosa o es un error no retryable
                return response
                
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    _logger.warning("⏱️ Timeout en request (intento %d/%d). Reintentando...", attempt, max_retries)
                    time.sleep(attempt)
                    continue
                raise
                
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    _logger.warning("⚠️ Error en request (intento %d/%d): %s. Reintentando...", attempt, max_retries, e)
                    time.sleep(attempt)
                    continue
                raise
        
        # Si llegamos aquí, se agotaron los reintentos
        raise Exception(f"Request falló después de {max_retries} intentos")

    @api.model
    def cron_refresh_expiring_tokens(self):
        """
        Cron job para refrescar tokens de todas las cuentas de MercadoLibre.
        Se ejecuta cada 2 horas automáticamente y refresca TODAS las cuentas conectadas,
        sin importar si están próximas a expirar o no.
        """
        accounts = self.search([
            ('is_connected', '=', True),
            ('refresh_token', '!=', False)
        ])
        
        if not accounts:
            _logger.info("ℹ️ No hay cuentas de MercadoLibre conectadas para refrescar tokens.")
            return True
        
        _logger.info("🔄 Iniciando refresh automático de tokens para %d cuenta(s) de MercadoLibre...", len(accounts))
        
        refreshed_count = 0
        error_count = 0
        
        for account in accounts:
            try:
                account.refresh_access_token()
                refreshed_count += 1
                _logger.info("✅ Token refrescado exitosamente para cuenta: %s (ID: %d)", account.name, account.id)
            except Exception as e:
                error_count += 1
                _logger.error("❌ Error refrescando token para cuenta %s (ID: %d): %s", account.name, account.id, e)
        
        _logger.info("🔄 Cron job de refresh de tokens completado. ✅ Exitosas: %d | ❌ Errores: %d", refreshed_count, error_count)
        return True

    @api.model
    def cron_poll_recent_orders(self):
        """
        Polling periódico de ventas ML → Odoo (complemento al webhook).

        Estrategia: WEBHOOK + POLLING + IDEMPOTENCIA
        - Webhook: notificaciones en tiempo real (ya implementado en /ml/notification).
        - Polling: este cron consulta las últimas órdenes cada X minutos para capturar
          cualquier venta que el webhook no haya entregado (caídas, retardos, etc.).
        - Idempotencia: update_or_create_from_meli() busca por ml_order_id + company_id;
          si existe actualiza, si no crea. Sin duplicados.

        Solo se ejecuta para cuentas con process_webhook_sales = True (misma bandera que el webhook).
        """
        accounts = self.sudo().search([
            ('process_webhook_sales', '=', True),
            ('is_connected', '=', True),
            ('access_token', '!=', False),
        ])
        if not accounts:
            _logger.debug("Meli polling: no hay cuentas con procesamiento de ventas activo.")
            return True

        for account in accounts:
            try:
                account._ensure_valid_token()
            except Exception as e:
                _logger.warning("Meli polling: no se pudo refrescar token cuenta %s (ID=%s): %s", account.name, account.id, e)
                continue

            user_id = account.meli_user_id
            if not user_id:
                _logger.debug("Meli polling: cuenta %s sin meli_user_id, omitiendo.", account.name)
                continue

            headers = {
                "Authorization": f"Bearer {account.access_token}",
                "Content-Type": "application/json",
            }
            # Límite fijo de órdenes por request (solo configurable: horas hacia atrás)
            POLLING_ORDER_LIMIT = 100
            hours_back = max(1, min(720, int(account.polling_hours_back or 24)))
            from_dt = datetime.utcnow() - timedelta(hours=hours_back)
            order_created_from = from_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

            params = {
                'seller': str(user_id),
                'sort': 'date_desc',
                'offset': 0,
                'limit': POLLING_ORDER_LIMIT,
                'order_created_from': order_created_from,
            }
            try:
                resp = requests.get(
                    "https://api.mercadolibre.com/orders/search",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                if not resp.ok and 'order_created_from' in params:
                    params_no_date = {k: v for k, v in params.items() if k != 'order_created_from'}
                    resp = requests.get(
                        "https://api.mercadolibre.com/orders/search",
                        headers=headers,
                        params=params_no_date,
                        timeout=30,
                    )
                if not resp.ok:
                    _logger.warning("Meli polling: API orders/search falló para cuenta %s: %s", account.name, resp.status_code)
                    continue
                data = resp.json()
                order_ids = [str(o.get('id')) for o in data.get('results', []) if o.get('id')]
            except Exception as e:
                _logger.warning("Meli polling: error obteniendo órdenes cuenta %s: %s", account.name, e)
                continue

            if not order_ids:
                continue

            sale_model = self.env['ml.sale'].sudo()
            for order_id in order_ids:
                try:
                    sale_model.update_or_create_from_meli(
                        order_id,
                        account_id=account.id,
                        create_odoo_order=True,
                        update_stock=True,
                        create_customer=True,
                    )
                except Exception as e:
                    _logger.warning("Meli polling: error procesando orden %s (cuenta %s): %s", order_id, account.name, e)

        return True






    def action_open_authorize(self):
        self.ensure_one()
        web_base = (
            self.env['ir.config_parameter']
            .sudo()
            .get_param('web.base.url', '')
            .rstrip('/')
        )
        if not web_base:
            raise UserError(
                _(
                    'Configure el parámetro del sistema "web.base.url" con la URL pública '
                    'de este Odoo (necesario para abrir el flujo OAuth de Mercado Libre).'
                )
            )
        path = f'/mercadolibre/oauth/start?account_id={self.id}'
        return {
            'type': 'ir.actions.act_url',
            'url': f'{web_base}{path}',
            'target': 'new',
        }

    # =====================================================
    # ⚠️ TEMPORAL: CREACIÓN DE USUARIOS TEST
    # TODO: REMOVER ESTA SECCIÓN CUANDO YA NO SE NECESITE
    # =====================================================
    def action_create_test_user(self):
        """
        ⚠️ TEMPORAL: Crea un usuario test de MercadoLibre.
        Muestra todos los datos en logs para copiarlos.
        TODO: REMOVER ESTE MÉTODO CUANDO YA NO SE NECESITE
        """
        self.ensure_one()
        
        if not self.access_token:
            raise UserError(_('⚠️ La cuenta debe estar autorizada primero. Use "Autorizar cuenta" antes de crear usuarios test.'))
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()
        
        _logger.info("=" * 80)
        _logger.info("🧪 INICIO: Creación de usuario test de MercadoLibre")
        _logger.info("=" * 80)
        
        try:
            # Endpoint para crear usuarios test según documentación de ML
            # POST https://api.mercadolibre.com/users/test_user
            url = "https://api.mercadolibre.com/users/test_user"
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            site_id = self._mercadolibre_site_id()
            
            payload = {
                "site_id": site_id
            }
            
            _logger.info("📤 Enviando request a: %s", url)
            _logger.info("📤 Payload: %s", payload)
            _logger.info("📤 Headers: Authorization=Bearer %s...", self.access_token[:20] if self.access_token else 'None')
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            _logger.info("📥 Response status: %s", response.status_code)
            _logger.info("📥 Response headers: %s", dict(response.headers))
            
            if not response.ok:
                error_text = response.text
                _logger.error("❌ Error creando usuario test: %s", error_text)
                try:
                    error_json = response.json()
                    error_message = error_json.get('message', error_text)
                    error_cause = error_json.get('cause', [])
                    if error_cause:
                        cause_messages = [c.get('message', '') for c in error_cause if isinstance(c, dict)]
                        if cause_messages:
                            error_message += "\n\nDetalles:\n" + "\n".join(f"• {msg}" for msg in cause_messages)
                except Exception:
                    error_message = error_text

                raise UserError(_("Error al crear usuario test:\n\n%s") % error_message)
            
            test_user_data = response.json()
            
            # Guardar el usuario test en la base de datos
            test_user_record = self.env['ml.test.user'].create({
                'ml_account_id': self.id,
                'user_id': test_user_data.get('id', ''),
                'nickname': test_user_data.get('nickname', ''),
                'password': test_user_data.get('password', ''),
                'site_id': test_user_data.get('site_id', ''),
                'email': test_user_data.get('email', ''),
                'first_name': test_user_data.get('first_name', ''),
                'last_name': test_user_data.get('last_name', ''),
                'full_data': json.dumps(test_user_data, indent=2, ensure_ascii=False),
            })
            
            _logger.info("Usuario test ML guardado en BD (ID: %s, nickname: %s)", test_user_record.id, test_user_data.get('nickname'))

            # Notificación al usuario (incluye password para que pueda copiarla; no se loguea)
            message = _(
                "✅ Usuario test creado exitosamente\n\n"
                "📋 Datos del usuario:\n"
                "   • ID: %s\n"
                "   • Nickname: %s\n"
                "   • Password: %s\n"
                "   • Email: %s\n\n"
                "💾 El usuario ha sido guardado. Puedes ver todos los usuarios test guardados haciendo clic en 'Ver Usuarios Test Guardados'."
            ) % (
                test_user_data.get('id', 'N/A'),
                test_user_data.get('nickname', 'N/A'),
                test_user_data.get('password', 'N/A'),
                test_user_data.get('email', 'N/A')
            )
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Usuario Test Creado'),
                    'message': message,
                    'type': 'success',
                    'sticky': True,
                }
            }
            
        except UserError:
            raise
        except requests.exceptions.RequestException as e:
            _logger.error("❌ Error de conexión creando usuario test: %s", str(e), exc_info=True)
            raise UserError(_("Error de conexión con MercadoLibre:\n\n%s") % str(e))
        except Exception as e:
            _logger.error("❌ Error inesperado creando usuario test: %s", str(e), exc_info=True)
            raise UserError(_("Error inesperado al crear usuario test:\n\n%s") % str(e))
    def action_view_test_users(self):
        """
        ⚠️ TEMPORAL: Muestra todos los usuarios test guardados para esta cuenta.
        TODO: REMOVER ESTE MÉTODO CUANDO YA NO SE NECESITE
        """
        self.ensure_one()
        
        action = {
            'name': _('Usuarios Test Guardados'),
            'type': 'ir.actions.act_window',
            'res_model': 'ml.test.user',
            'view_mode': 'list,form',
            'domain': [('ml_account_id', '=', self.id)],
            'context': {
                'default_ml_account_id': self.id,
                'search_default_ml_account_id': self.id,
            },
            'target': 'current',
        }
        
        return action
    

    def action_refresh_token(self):
        """Llamar al método de refresco y mostrar mensaje."""
        for rec in self:
            try:
                rec.refresh_access_token()
            except Exception as e:
                raise
        return True



    def get_meli_client(self):
        """Devuelve un cliente básico para interactuar con la API de Mercado Libre."""
        self.ensure_one()

        if not self.access_token:
            _logger.error("❌ La cuenta %s no tiene access_token configurado", self.name)
            return None
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()

        class MeliClient:
            def __init__(self, access_token):
                self.access_token = access_token
                self.base_url = "https://api.mercadolibre.com"

            def post(self, endpoint, data=None, files=None):
                """Realiza un POST en la API de ML."""
                import base64, io

                url = f"{self.base_url}{endpoint}?access_token={self.access_token}"
                headers = {"Accept": "application/json"}

                # Manejo de archivos binarios (para subir imágenes)
                files_payload = None
                json_payload = None
                if isinstance(data, dict) and "file" in data:
                    # `data["file"]` puede venir en base64
                    img_data = base64.b64decode(data["file"])
                    files_payload = {"file": ("image.jpg", io.BytesIO(img_data), "image/jpeg")}
                elif files:
                    files_payload = files
                else:
                    json_payload = data

                resp = requests.post(url, headers=headers, files=files_payload, json=json_payload)
                if not resp.ok:
                    _logger.error("❌ Error POST %s: %s", endpoint, resp.text)
                    return {}
                return resp.json()

            def put(self, endpoint, data):
                """Realiza un PUT en la API de ML."""
                url = f"{self.base_url}{endpoint}?access_token={self.access_token}"
                headers = {"Content-Type": "application/json"}
                resp = requests.put(url, headers=headers, json=data)
                if not resp.ok:
                    _logger.error("❌ Error PUT %s: %s", endpoint, resp.text)
                    return {}
                return resp.json()

            def get(self, endpoint):
                """Realiza un GET en la API de ML."""
                url = f"{self.base_url}{endpoint}?access_token={self.access_token}"
                resp = requests.get(url)
                if not resp.ok:
                    _logger.error("❌ Error GET %s: %s", endpoint, resp.text)
                    return {}
                return resp.json()

        return MeliClient(self.access_token)

    def action_match_all_publications_by_sku(self):
        """
        Matchea masivamente todas las publicaciones de esta cuenta con productos de Odoo usando SKU.
        """
        self.ensure_one()
        
        # Buscar publicaciones sin producto relacionado
        publications = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
            ('product_tmpl_id', '=', False)
        ])
        
        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _('No hay publicaciones sin producto relacionado para matchear.'),
                    'type': 'info',
                    'sticky': False,
                }
            }
        
        matched = 0
        unmatched = 0
        errors = []
        
        for idx, publication in enumerate(publications, 1):
            try:
                _logger.info("🔍 [%d/%d] Procesando publicación ID=%d, ML Item ID=%s, Nombre=%s", 
                           idx, len(publications), publication.id, publication.ml_item_id or 'N/A', publication.name)
                
                # Obtener SKU
                sku = None
                sku_source = None
                
                _logger.info("   🔍 1/3: Buscando SKU en seller_sku...")
                if publication.seller_sku:
                    sku = str(publication.seller_sku).strip()
                    sku_source = "seller_sku"
                    _logger.info("      ✅ SKU encontrado: '%s' (longitud: %d)", sku, len(sku))
                else:
                    _logger.info("      ❌ seller_sku está vacío")
                
                if not sku:
                    _logger.info("   🔍 2/3: Buscando SKU en atributo SELLER_SKU...")
                    seller_sku_attr = publication.ml_attribute_ids.filtered(
                        lambda a: a.ml_attribute_id == 'SELLER_SKU'
                    )
                    _logger.info("      📊 Atributos SELLER_SKU encontrados: %d", len(seller_sku_attr))
                    if seller_sku_attr and seller_sku_attr[0].value_name:
                        sku = str(seller_sku_attr[0].value_name).strip()
                        sku_source = "atributo SELLER_SKU"
                        _logger.info("      ✅ SKU encontrado: '%s'", sku)
                    else:
                        _logger.info("      ❌ No se encontró SKU en atributo SELLER_SKU")
                
                if not sku and publication.ml_variant_ids:
                    _logger.info("   🔍 3/3: Buscando SKU en variantes (total: %d)...", len(publication.ml_variant_ids))
                    for v_idx, variant in enumerate(publication.ml_variant_ids, 1):
                        if variant.seller_sku:
                            sku = str(variant.seller_sku).strip()
                            sku_source = f"variante {v_idx} seller_sku"
                            _logger.info("      ✅ SKU encontrado en variante %d: '%s'", v_idx, sku)
                            break
                    if not sku:
                        _logger.info("      ❌ Ninguna variante tiene seller_sku")
                elif not sku:
                    _logger.info("   🔍 3/3: No hay variantes")
                
                if not sku:
                    _logger.warning("   ⚠️ Publicación %d sin SKU, omitiendo", publication.id)
                    unmatched += 1
                    continue
                
                _logger.info("   📌 SKU final: '%s' (fuente: %s)", sku, sku_source)
                
                # Buscar producto SOLO por default_code. Si no hay match, no relacionar (no usar barcode ni otros campos).
                _logger.info("   🔍 Buscando producto con default_code='%s' (company_id=%d o None)...", sku, self.env.company.id)
                product_variant = self.env['product.product'].search([
                    ('default_code', '=', sku),
                    '|',
                    ('company_id', '=', self.env.company.id),
                    ('company_id', '=', False)
                ], limit=1)
                
                if product_variant:
                    product = product_variant.product_tmpl_id
                    company_info = product_variant.company_id.name if product_variant.company_id else 'Compartido'
                    _logger.info("      ✅ Encontrado en product.product: ID=%d, Nombre='%s', company_id=%s", 
                               product.id, product.name, company_info)
                else:
                    _logger.info("      ❌ No encontrado en product.product, buscando en product.template...")
                    product = self.env['product.template'].search([
                        ('default_code', '=', sku),
                        '|',
                        ('company_id', '=', self.env.company.id),
                        ('company_id', '=', False)
                    ], limit=1)
                    
                    if product:
                        company_info = product.company_id.name if product.company_id else 'Compartido'
                        _logger.info("      ✅ Encontrado en product.template: ID=%d, Nombre='%s', company_id=%s", 
                                   product.id, product.name, company_info)
                    else:
                        _logger.warning("      ❌ No encontrado en product.template")
                        # Log adicional para debug
                        all_products = self.env['product.template'].search([('default_code', '=', sku)], limit=3)
                        if all_products:
                            _logger.warning("      ⚠️ Existen productos con default_code='%s' pero company_id diferente:", sku)
                            for p in all_products:
                                _logger.warning("         - ID=%d, Nombre='%s', company_id=%s", 
                                              p.id, p.name, p.company_id.name if p.company_id else 'Sin compañía')
                
                if product:
                    publication.product_tmpl_id = product.id
                    if product_variant:
                        publication.product_variant_id = product_variant.id
                    elif len(product.product_variant_ids) == 1:
                        publication.product_variant_id = product.product_variant_id.id
                    else:
                        publication.product_variant_id = False
                    matched += 1
                    _logger.info("   ✅ MATCH EXITOSO: Publicación ID=%d → Producto ID=%d (%s)", 
                               publication.id, product.id, product.name)
                else:
                    unmatched += 1
                    _logger.warning("   ❌ MATCH FALLIDO: No se encontró producto para SKU='%s'", sku)
                    
            except Exception as e:
                error_msg = _('Publicación %s: %s') % (publication.ml_item_id or publication.name, str(e))
                errors.append(error_msg)
                unmatched += 1
                _logger.error("   ❌ ERROR procesando publicación ID=%d: %s", publication.id, str(e), exc_info=True)
        
        message = _(
            'Matcheo masivo completado:\n\n'
            '✅ Publicaciones matcheadas: %d\n'
            '⚠️ Publicaciones sin matchear: %d'
        ) % (matched, unmatched)
        
        if errors:
            message += _('\n\nErrores:\n') + '\n'.join(f'• {e}' for e in errors[:5])
            if len(errors) > 5:
                message += _('\n... y %d más') % (len(errors) - 5)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Matcheo Masivo Completado'),
                'message': message,
                'type': 'success' if matched > 0 else 'warning',
                'sticky': True,
            }
        }
    
    def action_import_all_publications_from_ml(self):
        """
        Importa todas las publicaciones de MercadoLibre para esta cuenta.
        Obtiene todos los items del usuario y los importa a Odoo.
        """
        self.ensure_one()
        
        if not self.access_token:
            raise UserError(_('La cuenta de MercadoLibre no tiene token de acceso configurado. Por favor, autorice la cuenta primero.'))
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        
        imported_count = 0
        updated_count = 0
        error_count = 0
        errors = []
        
        try:
            # Log detallado de la cuenta que se está usando
            _logger.info("=" * 80)
            _logger.info("📥 INICIO: Importación de publicaciones desde MercadoLibre")
            _logger.info("=" * 80)
            _logger.info("🔑 Cuenta ID: %d", self.id)
            _logger.info("🔑 Nombre de cuenta: %s", self.name)
            _logger.info("🔑 ML User ID guardado: %s", self.meli_user_id or 'N/A')
            _logger.info("🔑 ML Nickname guardado: %s", self.meli_nickname or 'N/A')
            _logger.info("🔑 Access Token (primeros 20 chars): %s...", self.access_token[:20] if self.access_token else 'N/A')
            _logger.info("=" * 80)
            
            # 1. Obtener user_id desde el token
            user_info_url = "https://api.mercadolibre.com/users/me"
            user_response = requests.get(user_info_url, headers=headers, timeout=10)
            
            if not user_response.ok:
                raise UserError(_('Error al obtener información del usuario: %s') % user_response.text)
            
            user_data = user_response.json()
            user_id = user_data.get('id')
            user_nickname = user_data.get('nickname', 'N/A')
            
            if not user_id:
                raise UserError(_('No se pudo obtener el ID del usuario desde MercadoLibre.'))
            
            _logger.info("📥 Usuario obtenido desde API: ID=%s, Nickname=%s", user_id, user_nickname)
            
            # Verificar que el user_id coincida con el guardado
            if self.meli_user_id and str(self.meli_user_id) != str(user_id):
                _logger.warning("⚠️ ADVERTENCIA: El user_id obtenido (%s) no coincide con el guardado (%s)", 
                              user_id, self.meli_user_id)
            else:
                _logger.info("✅ El user_id coincide con el guardado en la cuenta")
            
            # 2. Obtener todos los items del usuario (con paginación)
            # La API de ML retorna máximo 50 items por página, necesitamos paginar
            item_ids = []
            offset = 0
            limit = 50  # Máximo permitido por ML
            page_num = 1
            
            while True:
                items_url = f"https://api.mercadolibre.com/users/{user_id}/items/search"
                params = {
                    'offset': offset,
                    'limit': limit,
                }
                _logger.info("📡 Consultando página %d (offset: %d, limit: %d)", page_num, offset, limit)
                items_response = requests.get(items_url, headers=headers, params=params, timeout=30)
                
                if not items_response.ok:
                    raise UserError(_('Error al obtener items de MercadoLibre: %s') % items_response.text)
                
                items_data = items_response.json()
                page_item_ids = items_data.get('results', [])
                
                # Log información de paginación
                paging = items_data.get('paging', {})
                total = paging.get('total', 0)
                _logger.info("📊 Respuesta API - Items en página: %d, Total según API: %s, Offset: %d", 
                           len(page_item_ids), total if total > 0 else 'N/A', offset)
                
                if not page_item_ids:
                    _logger.info("📦 No hay más items, terminando paginación")
                    break
                
                item_ids.extend(page_item_ids)
                _logger.info("📦 Página %d: %d items obtenidos (total acumulado: %d)", 
                           page_num, len(page_item_ids), len(item_ids))
                
                # Si la página tiene menos items que el límite, es la última página
                if len(page_item_ids) < limit:
                    _logger.info("📦 Última página detectada (%d < %d), terminando paginación", 
                               len(page_item_ids), limit)
                    break
                
                # Si tenemos el total y ya lo alcanzamos, salir
                if total > 0 and len(item_ids) >= total:
                    _logger.info("📦 Total alcanzado (%d >= %d), terminando paginación", len(item_ids), total)
                    break
                
                # Continuar con la siguiente página
                offset += limit
                page_num += 1
                
                # Protección contra loops infinitos (máximo 1000 páginas = 50,000 items)
                if page_num > 1000:
                    _logger.warning("⚠️ Límite de páginas alcanzado (1000), deteniendo paginación")
                    break
            
            if not item_ids:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Sin publicaciones'),
                        'message': _('No se encontraron publicaciones en MercadoLibre para esta cuenta.'),
                        'type': 'info',
                        'sticky': False,
                    }
                }
            
            _logger.info("📦 Total de items encontrados en MercadoLibre: %d", len(item_ids))
            
            # 3. Importar cada item
            for item_id in item_ids:
                try:
                    # Obtener datos del item desde ML (una sola vez por item)
                    item_url = f"https://api.mercadolibre.com/items/{item_id}"
                    item_response = requests.get(item_url, headers=headers, timeout=30)
                    
                    if not item_response.ok:
                        error_count += 1
                        error_msg = _('Error obteniendo datos del item %s: %s') % (item_id, item_response.text)
                        errors.append(error_msg)
                        _logger.error("❌ %s", error_msg)
                        continue
                    
                    item_data = item_response.json()
                    variations = item_data.get('variations') or []
                    item_title = item_data.get('title', 'Item')
                    
                    # Si hay variaciones, obtener datos completos del endpoint /items/:id/variations (ahí suele venir el SKU)
                    if variations:
                        # [SKU_VARIANT] Log para diagnosticar: qué trae GET /items en variations
                        _logger.warning("[SKU_VARIANT] item_id=%s GET/items variations: count=%s", item_id, len(variations))
                        for i, v in enumerate(variations[:3]):
                            _logger.warning("[SKU_VARIANT]   var[%s] id=%s keys=%s seller_custom_field=%s seller_sku=%s inventory_id=%s attributes=%s",
                                            i, v.get('id'), list(v.keys()), v.get('seller_custom_field'), v.get('seller_sku'),
                                            v.get('inventory_id'), v.get('attributes'))
                        
                        variations_by_id = {}
                        try:
                            var_url = f"https://api.mercadolibre.com/items/{item_id}/variations"
                            var_response = requests.get(var_url, headers=headers, timeout=30)
                            _logger.warning("[SKU_VARIANT] GET %s status=%s", var_url, var_response.status_code)
                            if var_response.ok:
                                var_data = var_response.json()
                                raw_list = var_data.get('variations') if isinstance(var_data, dict) else (var_data if isinstance(var_data, list) else [])
                                for v in (raw_list or []):
                                    vid = str(v.get('id') or '')
                                    if vid:
                                        variations_by_id[vid] = dict(v)
                                # Para cada variación, GET /items/:id/variations/:variation_id trae atributos (incl. SELLER_SKU)
                                for vid in list(variations_by_id.keys()):
                                    try:
                                        one_url = f"https://api.mercadolibre.com/items/{item_id}/variations/{vid}"
                                        one_resp = requests.get(one_url, headers=headers, timeout=15)
                                        if one_resp.ok:
                                            one_var = one_resp.json()
                                            if isinstance(one_var, dict):
                                                variations_by_id[vid].update(one_var)
                                            _logger.info("📦 Variación %s: attributes=%s", vid, (one_var.get('attributes') if isinstance(one_var, dict) else None))
                                    except Exception as e:
                                        _logger.debug("GET /items/%s/variations/%s: %s", item_id, vid, e)
                                for i, (vid, v) in enumerate(list(variations_by_id.items())[:3]):
                                    _logger.warning("[SKU_VARIANT]   variations_by_id[%s] keys=%s seller_custom_field=%s seller_sku=%s inventory_id=%s attributes=%s",
                                                    vid, list(v.keys()), v.get('seller_custom_field'), v.get('seller_sku'), v.get('inventory_id'), v.get('attributes'))
                                _logger.info("📦 Obtenidas %d variaciones desde /items/%s/variations", len(variations_by_id), item_id)
                            else:
                                _logger.warning("⚠️ GET /items/%s/variations falló (%s), usando solo datos del item", item_id, var_response.status_code)
                        except Exception as e:
                            _logger.warning("⚠️ Error obteniendo /items/%s/variations: %s", item_id, e)
                        
                        # Una publicación por cada variación: cada una se puede matchear por SKU con un producto/variante de Odoo
                        existing_by_var = {
                            p.ml_variation_id: p
                            for p in self.env['ml.publication'].search([
                                ('ml_item_id', '=', item_id),
                                ('ml_account_id', '=', self.id),
                            ])
                            if p.ml_variation_id
                        }
                        for var in variations:
                            var_id = str(var.get('id') or '')
                            # Usar datos del endpoint /variations si están (tienen SKU); si no, usar var del item
                            full_var = variations_by_id.get(var_id) or var
                            combo = full_var.get('attribute_combinations') or var.get('attribute_combinations') or []
                            parts = [str(ac.get('value_name') or ac.get('value_id', '')).strip() for ac in combo if ac.get('value_name') or ac.get('value_id')]
                            variant_label = ', '.join(parts) if parts else (full_var.get('seller_custom_field') or '').strip() or _('Variante %s') % var_id or str(len(existing_by_var) + 1)
                            # SKU: 1) seller_custom_field 2) attributes SELLER_SKU 3) seller_sku 4) inventory_id (código interno ML)
                            sku_var = (full_var.get('seller_custom_field') or full_var.get('seller_sku') or '').strip()
                            _logger.warning("[SKU_VARIANT] var_id=%s full_var keys=%s -> sku from seller_custom_field/seller_sku=%s", var_id, list(full_var.keys()), sku_var or '(vacío)')
                            if not sku_var and full_var.get('attributes'):
                                for a in full_var.get('attributes', []):
                                    if (a.get('id') or '').upper() == 'SELLER_SKU' and (a.get('value_name') or a.get('value_id')):
                                        sku_var = str(a.get('value_name') or a.get('value_id', '')).strip()
                                        _logger.warning("[SKU_VARIANT] var_id=%s SKU desde attributes SELLER_SKU: %s", var_id, sku_var)
                                        break
                            if not sku_var and combo:
                                for ac in combo:
                                    if (ac.get('id') or '').upper() == 'SELLER_SKU' and (ac.get('value_name') or ac.get('value_id')):
                                        sku_var = str(ac.get('value_name') or ac.get('value_id', '')).strip()
                                        _logger.warning("[SKU_VARIANT] var_id=%s SKU desde attribute_combinations SELLER_SKU: %s", var_id, sku_var)
                                        break
                            if not sku_var and full_var.get('inventory_id'):
                                sku_var = str(full_var.get('inventory_id', '')).strip()
                                _logger.info("📝 Usando inventory_id como SKU para variación %s: %s", var_id, sku_var)
                            if not sku_var and full_var.get('user_product_id'):
                                sku_var = str(full_var.get('user_product_id', '')).strip()
                                _logger.info("📝 Usando user_product_id como SKU para variación %s (ML no devolvió SELLER_SKU): %s", var_id, sku_var)
                            if not sku_var:
                                sku_var = var_id
                                _logger.info("📝 Usando id de variación ML como SKU para matcheo (puede usar este valor en Referencia interna en Odoo): %s", sku_var)
                            _logger.warning("[SKU_VARIANT] var_id=%s sku_var FINAL guardado en publicación: %s", var_id, sku_var or '(VACÍO - no matcheará por SKU)')
                            if not sku_var:
                                _logger.warning("⚠️ Variación %s (item %s) sin SKU en ML: seller_custom_field=%s, attributes=%s, inventory_id=%s",
                                              var_id, item_id, full_var.get('seller_custom_field'), [a.get('id') for a in (full_var.get('attributes') or [])], full_var.get('inventory_id'))
                            name_title = f"{item_title} | {variant_label}"
                            pub_vals = {
                                'name': name_title,  # Solo para diferenciar variantes; no incluir cuenta
                                'title': name_title,
                                'ml_item_id': item_id,
                                'ml_variation_id': var_id,
                                'ml_account_id': self.id,
                                'ml_status': item_data.get('status', 'inactive'),
                                'permalink': item_data.get('permalink', ''),
                                'current_price_ml': float(full_var.get('price') or var.get('price') or 0),
                                'current_stock_ml': int(full_var.get('available_quantity') or var.get('available_quantity') or 0),
                                'seller_sku': sku_var or '',
                                'category_id': item_data.get('category_id') or '',
                            }
                            existing = existing_by_var.get(var_id)
                            if existing:
                                try:
                                    existing.write(pub_vals)
                                    existing.action_import_from_ml()
                                    updated_count += 1
                                except Exception as e:
                                    error_count += 1
                                    errors.append(_('Error actualizando variación %s del item %s: %s') % (variant_label, item_id, str(e)))
                            else:
                                try:
                                    pub = self.env['ml.publication'].create(pub_vals)
                                    pub.action_import_from_ml()
                                    imported_count += 1
                                except Exception as e:
                                    error_count += 1
                                    errors.append(_('Error importando variación %s del item %s: %s') % (variant_label, item_id, str(e)))
                    else:
                        # Ítem sin variaciones: una sola publicación (comportamiento clásico)
                        existing_publication = self.env['ml.publication'].search([
                            ('ml_item_id', '=', item_id),
                            ('ml_account_id', '=', self.id),
                        ], limit=1)
                        
                        if existing_publication:
                            if existing_publication.ml_account_id.id != self.id:
                                existing_publication.ml_account_id = self.id
                            try:
                                existing_publication.action_import_from_ml()
                                updated_count += 1
                            except Exception as e:
                                error_count += 1
                                errors.append(_('Error actualizando publicación %s: %s') % (item_id, str(e)))
                        else:
                            seller_sku = (item_data.get('seller_custom_field') or '')
                            if seller_sku and str(seller_sku).strip() and str(seller_sku).strip() not in ('False', 'None'):
                                seller_sku = str(seller_sku).strip()
                            else:
                                seller_sku = ''
                            publication_vals = {
                                'name': item_title,  # Solo para diferenciar variantes; no incluir cuenta
                                'title': item_title,
                                'ml_item_id': item_id,
                                'ml_account_id': self.id,
                                'ml_status': item_data.get('status', 'inactive'),
                                'permalink': item_data.get('permalink', ''),
                                'current_price_ml': item_data.get('price', 0.0),
                                'current_stock_ml': item_data.get('available_quantity', 0),
                                'seller_sku': seller_sku,
                            }
                            if item_data.get('category_id'):
                                publication_vals['category_id'] = item_data['category_id']
                            try:
                                publication = self.env['ml.publication'].create(publication_vals)
                                publication.action_import_from_ml()
                                imported_count += 1
                            except Exception as e:
                                error_count += 1
                                errors.append(_('Error importando publicación %s: %s') % (item_id, str(e)))
                
                except Exception as e:
                    error_count += 1
                    error_msg = _('Error procesando item %s: %s') % (item_id, str(e))
                    errors.append(error_msg)
                    _logger.error("❌ %s", error_msg)
            
            # 4. Preparar mensaje de resultado
            message = _(
                'Importación completada:\n\n'
                '✅ Publicaciones importadas: %d\n'
                '🔄 Publicaciones actualizadas: %d\n'
                '❌ Errores: %d'
            ) % (imported_count, updated_count, error_count)
            
            if errors:
                message += _('\n\nErrores:\n') + '\n'.join(f'• {e}' for e in errors[:10])
                if len(errors) > 10:
                    message += _('\n... y %d más') % (len(errors) - 10)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Importación de Publicaciones Completada'),
                    'message': message,
                    'type': 'success' if error_count == 0 else 'warning',
                    'sticky': True,
                }
            }
            
        except UserError:
            raise
        except Exception as e:
            _logger.error("❌ Error inesperado importando publicaciones: %s", str(e), exc_info=True)
            raise UserError(_('Error inesperado al importar publicaciones:\n\n%s') % str(e))

    def action_view_sync_log_errors(self):
        """Abre lista de errores de sync ML recientes (30 días) para esta cuenta."""
        self.ensure_one()
        threshold = fields.Datetime.now() - timedelta(days=30)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Errores de sincronización ML (30 días)'),
            'res_model': 'ml.sync.log',
            'view_mode': 'list,form',
            'domain': [
                ('ml_account_id', '=', self.id),
                ('result', '=', 'error'),
                ('create_date', '>=', threshold),
            ],
            'context': {'default_ml_account_id': self.id},
        }

    def action_open_import_orders_wizard(self):
        """Abre el wizard para importar órdenes con opciones"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Importar Órdenes de MercadoLibre'),
            'res_model': 'ml.import.orders.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ml_account_id': self.id,
            }
        }
    
    def action_update_all_publications_stock(self):
        """
        Actualiza SOLO el stock de todas las publicaciones de esta cuenta en Mercado Libre.
        Solo actualiza publicaciones que tienen producto relacionado y están publicadas en ML.
        """
        self.ensure_one()
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()
        
        if not self.access_token:
            raise UserError(_('La cuenta de MercadoLibre no tiene token de acceso configurado. Por favor, autorice la cuenta primero.'))
        
        # Buscar todas las publicaciones de esta cuenta que:
        # - Tengan ml_item_id (estén publicadas en ML)
        # - Tengan product_tmpl_id (tengan producto relacionado)
        publications = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
            ('product_tmpl_id', '!=', False),
        ])
        
        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _('No se encontraron publicaciones con producto relacionado y publicadas en MercadoLibre.'),
                    'type': 'info',
                    'sticky': False,
                }
            }
        
        _logger.info("=" * 80)
        _logger.info("🔄 INICIO: Actualización masiva de stock")
        _logger.info("   Cuenta: %s (ID: %d)", self.name, self.id)
        _logger.info("   Publicaciones a actualizar: %d", len(publications))
        _logger.info("=" * 80)
        
        updated_count = 0
        error_count = 0
        errors = []
        total = len(publications)

        for idx, publication in enumerate(publications, start=1):
            pct = (100.0 * idx / total) if total else 100.0
            try:
                _logger.info(
                    "🔄 Progreso stock ML: %d/%d (%.1f%%) — %s (item %s)",
                    idx,
                    total,
                    pct,
                    publication.name,
                    publication.ml_item_id or "",
                )

                # Usar la misma lógica que _compute_stock: variante seleccionada, almacén, tipo disponible/esperado, kits
                publication.invalidate_recordset(['stock'])
                publication._compute_stock()
                stock = max(0, int(publication.stock or 0))

                # Actualizar solo stock en Mercado Libre
                publication._force_update_stock_in_ml(stock_value=stock)

                updated_count += 1
                _logger.info(
                    "✅ [%d/%d] (%.1f%%) Stock replicado en ML: %s (stock=%d)",
                    idx,
                    total,
                    pct,
                    publication.name,
                    stock,
                )

            except Exception as e:
                error_count += 1
                error_msg = _('Error actualizando stock de publicación %s: %s') % (publication.ml_item_id or publication.name, str(e))
                errors.append(error_msg)
                _logger.exception(
                    "❌ [%d/%d] (%.1f%%) %s",
                    idx,
                    total,
                    pct,
                    error_msg,
                )
            finally:
                if idx < total:
                    time.sleep(0.1)
        
        _logger.info("=" * 80)
        _logger.info("✅ FIN: Actualización masiva de stock completada")
        _logger.info("   Actualizadas: %d", updated_count)
        _logger.info("   Errores: %d", error_count)
        _logger.info("=" * 80)
        
        # Preparar mensaje de resultado
        if error_count == 0:
            message = _('Stock actualizado correctamente para %d publicación(es).') % updated_count
            message_type = 'success'
        else:
            message = _('Stock actualizado para %d publicación(es).\n\n') % updated_count
            message += _('Errores: %d\n\n') % error_count
            message += '\n'.join(f"• {err}" for err in errors[:5])  # Mostrar máximo 5 errores
            if len(errors) > 5:
                message += _('\n\n... y %d error(es) más.') % (len(errors) - 5)
            message_type = 'warning'
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Actualización de Stock'),
                'message': message,
                'type': message_type,
                'sticky': False,
            }
        }
    
    def action_update_all_publications_price_stock(self):
        """
        Actualiza precio y stock de todas las publicaciones de esta cuenta en Mercado Libre.
        Solo actualiza publicaciones que tienen producto relacionado y están publicadas en ML.
        """
        self.ensure_one()
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()
        
        if not self.access_token:
            raise UserError(_('La cuenta de MercadoLibre no tiene token de acceso configurado. Por favor, autorice la cuenta primero.'))
        
        # Buscar todas las publicaciones de esta cuenta que:
        # - Tengan ml_item_id (estén publicadas en ML)
        # - Tengan product_tmpl_id (tengan producto relacionado)
        publications = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
            ('product_tmpl_id', '!=', False),
        ])
        
        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _('No se encontraron publicaciones con producto relacionado y publicadas en MercadoLibre.'),
                    'type': 'info',
                    'sticky': False,
                }
            }
        
        _logger.info("=" * 80)
        _logger.info("🔄 INICIO: Actualización masiva de precio y stock")
        _logger.info("   Cuenta: %s (ID: %d)", self.name, self.id)
        _logger.info("   Publicaciones a actualizar: %d", len(publications))
        _logger.info("=" * 80)
        
        updated_count = 0
        error_count = 0
        errors = []
        
        for publication in publications:
            try:
                _logger.info("🔄 Actualizando publicación: %s (ML Item ID: %s)", publication.name, publication.ml_item_id)
                
                # Calcular precio y stock desde el producto relacionado
                variant = publication.product_variant_id or publication.product_tmpl_id.product_variant_id
                product = variant or publication.product_tmpl_id
                if self.sync_price_pricelist_id:
                    if not variant:
                        _logger.warning(
                            '⚠️ Publicación %s sin variante de producto; no se puede leer lista de precios, omitiendo',
                            publication.name,
                        )
                        continue
                    try:
                        price = self.sync_price_pricelist_id._get_product_price(variant, 1.0)
                    except TypeError:
                        price = self.sync_price_pricelist_id._get_product_price(variant.product_tmpl_id, 1.0)
                    price = float(price or 0)
                else:
                    price = getattr(product, 'list_price', None) or publication.product_tmpl_id.list_price
                
                # Calcular stock (considerando kits)
                # Usar el método _compute_stock de la publicación que ya maneja kits
                publication._compute_stock()
                # Leer el stock calculado
                stock = publication.stock
                
                # Si el stock es 0 pero el producto existe, verificar si es kit o si realmente no hay stock
                if stock == 0 and publication.product_tmpl_id:
                    # Verificar si es kit para asegurar que se calculó correctamente
                    product_variant = publication.product_variant_id or publication.product_tmpl_id.product_variant_id
                    if product_variant:
                        bom_components = publication._get_kit_components_from_bom(product_variant)
                        if bom_components:
                            # Es un kit, el stock ya debería estar calculado correctamente
                            _logger.info("📦 Kit detectado en actualización masiva: stock calculado=%d", stock)
                
                # Verificar que tenemos valores válidos
                if not price or price <= 0:
                    _logger.warning("⚠️ Publicación %s tiene precio inválido (%.2f), omitiendo", publication.name, price or 0)
                    continue
                
                # Actualizar los campos computados directamente
                publication.sudo().write({
                    'price': price,
                    'stock': stock,
                })
                
                # Actualizar en Mercado Libre
                publication._force_update_price_stock_in_ml()
                
                updated_count += 1
                _logger.info("✅ Publicación actualizada: %s (precio=%.2f, stock=%d)", publication.name, price, stock)
                
            except Exception as e:
                error_count += 1
                error_msg = _('Error actualizando publicación %s: %s') % (publication.ml_item_id or publication.name, str(e))
                errors.append(error_msg)
                _logger.error("❌ %s", error_msg, exc_info=True)
        
        _logger.info("=" * 80)
        _logger.info("✅ FIN: Actualización masiva completada")
        _logger.info("   Actualizadas: %d", updated_count)
        _logger.info("   Errores: %d", error_count)
        _logger.info("=" * 80)
        
        # Contar publicaciones con variantes para aviso
        with_variants = publications.filtered(
            lambda p: p.product_tmpl_id and len(p.product_tmpl_id.product_variant_ids) > 1
        )
        
        # Preparar mensaje de resultado
        message = _(
            'Actualización completada:\n\n'
            '✅ Publicaciones actualizadas: %d\n'
            '❌ Errores: %d'
        ) % (updated_count, error_count)
        
        if errors:
            message += _('\n\nErrores:\n') + '\n'.join(f'• {e}' for e in errors[:10])
            if len(errors) > 10:
                message += _('\n... y %d más') % (len(errors) - 10)
        
        if with_variants:
            message += _('\n\n⚠️ Revise el precio de las publicaciones con variantes (%d publicación(es)). '
                        'Si cada variante tiene un precio distinto, actualice el precio desde cada publicación.') % len(with_variants)
        
        # Si hay publicaciones con variantes, mostrar aviso en un diálogo grande en pantalla
        if with_variants:
            wizard = self.env['ml.variant.warning.wizard'].create({
                'title': _('Actualización de Precio y Stock Completada'),
                'message': message,
            })
            return {
                'type': 'ir.actions.act_window',
                'name': _('Actualización de Precio y Stock Completada'),
                'res_model': 'ml.variant.warning.wizard',
                'res_id': wizard.id,
                'view_mode': 'form',
                'target': 'new',
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Actualización de Precio y Stock Completada'),
                'message': message,
                'type': 'success' if error_count == 0 else 'warning',
                'sticky': True,
            }
        }

    def action_import_all_orders_from_ml(self, create_odoo_orders=True, update_stock=False, create_customers=True, date_from=None, date_to=None):
        """
        Importa órdenes de venta de MercadoLibre para esta cuenta.
        Opcionalmente filtra por rango de fechas (date_from, date_to).
        
        Args:
            create_odoo_orders: Si True, crea automáticamente las órdenes de venta en Odoo y facturas (default: True)
            update_stock: Si True, se descontará el stock al confirmar las órdenes. Para importaciones manuales desde el wizard se usa siempre False (no tocar stock).
            create_customers: Si True, crea/actualiza el contacto del cliente (default: True)
            date_from: fecha desde (date o None). Solo órdenes creadas >= esta fecha.
            date_to: fecha hasta (date o None). Solo órdenes creadas <= esta fecha.
        """
        self.ensure_one()
        
        # Verificar y refrescar token si es necesario
        self._ensure_valid_token()
        
        if not self.access_token:
            raise UserError(_('La cuenta de MercadoLibre no tiene token de acceso configurado. Por favor, autorice la cuenta primero.'))
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        
        imported_count = 0
        updated_count = 0
        error_count = 0
        errors = []
        
        try:
            # Log detallado de la cuenta que se está usando
            _logger.info("=" * 80)
            _logger.info("📦 INICIO: Importación de órdenes de venta desde MercadoLibre")
            _logger.info("=" * 80)
            _logger.info("🔑 Cuenta ID: %d", self.id)
            _logger.info("🔑 Nombre de cuenta: %s", self.name)
            _logger.info("🔑 ML User ID guardado: %s", self.meli_user_id or 'N/A')
            _logger.info("🔑 ML Nickname guardado: %s", self.meli_nickname or 'N/A')
            _logger.info("🔑 Access Token (primeros 20 chars): %s...", self.access_token[:20] if self.access_token else 'N/A')
            _logger.info("=" * 80)
            
            # 1. Obtener user_id primero para validar
            user_info_url = "https://api.mercadolibre.com/users/me"
            user_response = requests.get(user_info_url, headers=headers, timeout=10)
            
            if not user_response.ok:
                raise UserError(_('Error al obtener información del usuario: %s') % user_response.text)
            
            user_data = user_response.json()
            user_id = user_data.get('id')
            user_nickname = user_data.get('nickname', 'N/A')
            
            if not user_id:
                raise UserError(_('No se pudo obtener el ID del usuario desde MercadoLibre.'))
            
            _logger.info("📥 Usuario obtenido desde API: ID=%s, Nickname=%s", user_id, user_nickname)
            
            # Verificar que el user_id coincida con el guardado
            if self.meli_user_id and str(self.meli_user_id) != str(user_id):
                _logger.warning("⚠️ ADVERTENCIA: El user_id obtenido (%s) no coincide con el guardado (%s)", 
                              user_id, self.meli_user_id)
            else:
                _logger.info("✅ El user_id coincide con el guardado en la cuenta")
            
            _logger.info("📥 Importando órdenes de VENTA (seller) para usuario: %s (ID: %s)", 
                        user_nickname, user_id)
            
            # 2. Obtener todas las órdenes donde el usuario es SELLER (vendedor)
            order_ids = []
            offset = 0
            limit = 50  # Máximo permitido por ML
            page_num = 1
            # Filtro por fechas: ISO para API y/o filtro en resultados
            order_created_from = None
            order_created_to = None
            if date_from:
                order_created_from = date_from.isoformat() + 'T00:00:00.000-00:00'
            if date_to:
                order_created_to = date_to.isoformat() + 'T23:59:59.999-00:00'
            stop_before_date = date_from  # Para parar paginación cuando orden.date < date_from (sort date_desc)
            
            while True:
                orders_url = "https://api.mercadolibre.com/orders/search"
                params = {
                    'seller': str(user_id),  # Solo buscar como vendedor
                    'sort': 'date_desc',
                    'offset': offset,
                    'limit': limit,
                }
                if order_created_from:
                    params['order_created_from'] = order_created_from
                if order_created_to:
                    params['order_created_to'] = order_created_to
                _logger.info("📡 Consultando página %d de órdenes de VENTA (offset: %d, limit: %d)%s", 
                           page_num, offset, limit,
                           ' [filtro fechas: %s - %s]' % (date_from, date_to) if (date_from or date_to) else '')
                
                orders_response = requests.get(orders_url, headers=headers, params=params, timeout=30)
                
                # Si la API rechaza los parámetros de fecha, reintentar sin ellos y filtrar en código
                if not orders_response.ok and (order_created_from or order_created_to):
                    _logger.info("📡 API rechazó filtro de fechas, reintentando sin parámetros de fecha")
                    params_no_date = {k: v for k, v in params.items() if k not in ('order_created_from', 'order_created_to')}
                    orders_response = requests.get(orders_url, headers=headers, params=params_no_date, timeout=30)
                    order_created_from = None
                    order_created_to = None
                
                # Log detallado de la respuesta para debugging
                _logger.info("📡 Status Code: %d", orders_response.status_code)
                
                if not orders_response.ok:
                    # Si hay error, puede ser que no sea vendedor o no tenga órdenes como vendedor
                    error_text = orders_response.text
                    _logger.warning("⚠️ Error en respuesta de API: Status %d, Body: %s", 
                                  orders_response.status_code, error_text[:500])
                    
                    if 'caller.id' in error_text or '403' in str(orders_response.status_code):
                        _logger.warning("⚠️ El usuario %s no tiene órdenes como vendedor o no tiene permisos.", user_id)
                        # Verificar si es usuario test
                        if 'TESTUSER' in str(user_id) or 'test' in str(user_data.get('nickname', '')).lower():
                            _logger.info("ℹ️ Usuario TEST detectado. Los usuarios test pueden no tener órdenes de venta.")
                        break
                    raise UserError(_('Error al obtener órdenes de MercadoLibre: %s') % error_text)
                
                orders_data = orders_response.json()
                page_orders = orders_data.get('results', [])
                
                # Log información de paginación
                paging = orders_data.get('paging', {})
                total = paging.get('total', 0)
                offset_response = paging.get('offset', 0)
                limit_response = paging.get('limit', 0)
                
                _logger.info("📊 Respuesta API - Órdenes en página: %d, Total según API: %s, Offset: %d, Limit: %d", 
                           len(page_orders), total if total > 0 else 'N/A', offset_response, limit_response)
                
                # Log adicional si es usuario test
                if 'TESTUSER' in str(user_id) or 'test' in str(user_data.get('nickname', '')).lower():
                    _logger.info("ℹ️ Usuario TEST detectado. Si no hay órdenes, puede ser normal para usuarios test.")
                    _logger.info("   Parámetros de búsqueda: seller=%s, sort=%s, offset=%d, limit=%d", 
                               params.get('seller'), params.get('sort'), offset, limit)
                
                # Log de la estructura completa de la respuesta si está vacía (para debugging)
                if not page_orders and page_num == 1:
                    _logger.info("🔍 Respuesta completa de API (primer intento, sin órdenes):")
                    _logger.info("   Keys en respuesta: %s", list(orders_data.keys()))
                    if 'paging' in orders_data:
                        _logger.info("   Paging completo: %s", orders_data['paging'])
                    if 'results' in orders_data:
                        _logger.info("   Results (tipo): %s, longitud: %d", type(orders_data['results']), len(orders_data.get('results', [])))
                
                if not page_orders:
                    _logger.info("📦 No hay más órdenes de venta, terminando paginación")
                    break
                
                # Filtrar por rango de fechas si aplica (por si la API no filtró o devuelve más)
                def order_in_date_range(order):
                    if not date_from and not date_to:
                        return True
                    created = order.get('date_created') or order.get('date_last_updated') or order.get('last_updated')
                    if not created:
                        return True  # Sin fecha, incluir
                    try:
                        # ML suele devolver ISO: 2025-02-24T18:00:00.000Z o con offset
                        if isinstance(created, str) and 'T' in created:
                            created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                        else:
                            created_dt = created
                        order_date = created_dt.date() if hasattr(created_dt, 'date') else created_dt
                        if date_from and order_date < date_from:
                            return False
                        if date_to and order_date > date_to:
                            return False
                        return True
                    except Exception:
                        return True
                
                for order in page_orders:
                    oid = order.get('id')
                    if not oid:
                        continue
                    if not order_in_date_range(order):
                        if stop_before_date:
                            try:
                                created = order.get('date_created') or order.get('date_last_updated') or order.get('last_updated')
                                if created and isinstance(created, str) and 'T' in created:
                                    created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                                    if hasattr(created_dt, 'date') and created_dt.date() < stop_before_date:
                                        _logger.info("📦 Orden %s fuera de rango (antes de desde), terminando paginación", oid)
                                        page_orders = []  # forzar break del while
                                        break
                            except Exception:
                                pass
                        continue
                    order_ids.append(str(oid))
                
                if not page_orders:
                    break
                
                _logger.info("📦 Página %d: %d órdenes de venta obtenidas (total acumulado: %d)", 
                           page_num, len(page_orders), len(order_ids))
                
                # Si la página tiene menos órdenes que el límite, es la última página
                if len(page_orders) < limit:
                    _logger.info("📦 Última página detectada (%d < %d), terminando paginación", 
                               len(page_orders), limit)
                    break
                
                # Si tenemos el total y ya lo alcanzamos, salir
                if total > 0 and len(order_ids) >= total:
                    _logger.info("📦 Total alcanzado (%d >= %d), terminando paginación", len(order_ids), total)
                    break
                
                # Continuar con la siguiente página
                offset += limit
                page_num += 1
                
                # Protección contra loops infinitos (máximo 200 páginas = 10,000 órdenes)
                if page_num > 200:
                    _logger.warning("⚠️ Límite de páginas alcanzado (200), deteniendo paginación")
                    break
            
            if not order_ids:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Sin órdenes'),
                        'message': _('No se encontraron órdenes en MercadoLibre para esta cuenta.'),
                        'type': 'info',
                        'sticky': False,
                    }
                }
            
            _logger.info("📦 Total de órdenes a importar: %d", len(order_ids))
            
            # 2. Procesar cada orden
            _logger.info("📦 Configuración de importación: create_odoo_orders=%s, update_stock=%s, create_customers=%s", create_odoo_orders, update_stock, create_customers)
            for idx, order_id in enumerate(order_ids, 1):
                try:
                    _logger.info("📦 [%d/%d] Procesando orden: %s (Cuenta ID: %d)", idx, len(order_ids), order_id, self.id)
                    
                    # Verificar si ya existe
                    existing = self.env['ml.sale'].search([
                        ('ml_order_id', '=', str(order_id)),
                        ('company_id', '=', self.env.company.id)
                    ], limit=1)
                    
                    if existing:
                        # Verificar que la orden tenga la cuenta correcta
                        if existing.ml_account_id.id != self.id:
                            _logger.warning("⚠️ Orden %s tiene cuenta diferente (ID: %d vs %d), actualizando cuenta", 
                                          order_id, existing.ml_account_id.id, self.id)
                            existing.ml_account_id = self.id
                        
                        # Actualizar orden existente
                        try:
                            _logger.info("🔄 Actualizando orden existente: %s (Cuenta ID: %d)", order_id, self.id)
                            self.env['ml.sale'].update_or_create_from_meli(
                                str(order_id), 
                                self.id, 
                                create_odoo_order=create_odoo_orders,
                                update_stock=update_stock,
                                create_customer=create_customers
                            )
                            updated_count += 1
                            _logger.info("🔄 Orden actualizada: %s", order_id)
                        except Exception as e:
                            error_count += 1
                            error_msg = _('Error actualizando orden %s: %s') % (order_id, str(e))
                            errors.append(error_msg)
                            _logger.error("❌ %s", error_msg)
                    else:
                        # Crear nueva orden
                        try:
                            result = self.env['ml.sale'].update_or_create_from_meli(
                                str(order_id), 
                                self.id, 
                                create_odoo_order=create_odoo_orders,
                                update_stock=update_stock,
                                create_customer=create_customers
                            )
                            if result:
                                imported_count += 1
                                _logger.info("✅ Orden importada: %s", order_id)
                            else:
                                error_count += 1
                                error_msg = _('No se pudo importar orden %s') % order_id
                                errors.append(error_msg)
                                _logger.warning("⚠️ %s", error_msg)
                        except Exception as e:
                            error_count += 1
                            error_msg = _('Error importando orden %s: %s') % (order_id, str(e))
                            errors.append(error_msg)
                            _logger.error("❌ %s", error_msg)
                    
                except Exception as e:
                    error_count += 1
                    error_msg = _('Error procesando orden %s: %s') % (order_id, str(e))
                    errors.append(error_msg)
                    _logger.error("❌ %s", error_msg)
            
            _logger.info("=" * 80)
            _logger.info("📦 FIN: Importación de órdenes completada")
            _logger.info("   ✅ Importadas: %d | 🔄 Actualizadas: %d | ❌ Errores: %d", 
                        imported_count, updated_count, error_count)
            _logger.info("=" * 80)
            
            # 3. Preparar mensaje de resultado
            message = _(
                'Importación de órdenes completada:\n\n'
                '✅ Órdenes importadas: %d\n'
                '🔄 Órdenes actualizadas: %d\n'
                '❌ Errores: %d'
            ) % (imported_count, updated_count, error_count)
            
            if errors:
                message += _('\n\nErrores:\n') + '\n'.join(f'• {e}' for e in errors[:10])
                if len(errors) > 10:
                    message += _('\n... y %d más') % (len(errors) - 10)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Importación de Órdenes Completada'),
                    'message': message,
                    'type': 'success' if error_count == 0 else 'warning',
                    'sticky': True,
                }
            }
            
        except UserError:
            raise
        except Exception as e:
            _logger.error("❌ Error inesperado importando órdenes: %s", str(e), exc_info=True)
            raise UserError(_('Error inesperado al importar órdenes:\n\n%s') % str(e))
        
        
            
