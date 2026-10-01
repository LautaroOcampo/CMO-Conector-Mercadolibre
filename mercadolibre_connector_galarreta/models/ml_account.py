from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import requests
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import os
import json
import secrets
import time


_logger = logging.getLogger(__name__)

# Margen antes del vencimiento para refrescar en cada llamada API (lazy refresh).
ML_TOKEN_REFRESH_BUFFER_MINUTES = 5
# Respaldo del cron: refrescar si expira dentro de estas horas.
ML_TOKEN_CRON_BACKUP_HOURS = 3

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
    client_id = fields.Char(
        string="Client ID",
        help='App ID de developers.mercadolibre.com. En modo hub (cliente) se completa al autorizar.',
    )
    client_secret = fields.Char(
        string="Client Secret",
        groups="mercadolibre_connector_galarreta.group_ml_admin",
        help='Secret Key de la app. En modo hub (cliente) se completa al autorizar.',
    )
    redirect_uri = fields.Char(
        string="Redirect URI",
        default=lambda self: (
            self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
            + '/mercadolibre/oauth/callback'
        ),
        help=(
            'En modo directo: debe coincidir con la app en developers.mercadolibre.com. '
            'En modo hub: solo el Odoo hub registra el callback; este campo se ignora al autorizar.'
        ),
    )
    oauth_via_hub = fields.Boolean(compute='_compute_oauth_mode')
    oauth_is_hub = fields.Boolean(compute='_compute_oauth_mode')
    oauth_callback_uri = fields.Char(
        string='Redirect URI (app Mercado Libre)',
        compute='_compute_oauth_callback_uri',
        help='URI a registrar en developers.mercadolibre.com cuando esta base es el hub o autoriza directo.',
    )
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
    access_token = fields.Char(string="Access Token", copy=False, groups="mercadolibre_connector_galarreta.group_ml_admin")
    refresh_token = fields.Char(string="Refresh Token", copy=False, groups="mercadolibre_connector_galarreta.group_ml_admin")
    token_expiration = fields.Datetime(string="Token Expiration")
    is_connected = fields.Boolean(string="Connected", default=False)
    code_verifier = fields.Char(string="PKCE Verifier", copy=False, groups="mercadolibre_connector_galarreta.group_ml_admin")
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
        string="Almacén depósito (Flex / multi-origen)",
        help=(
            'Stock de este almacén Odoo se sincroniza al depósito propio en Mercado Libre '
            '(Flex: selling_address, o multi-origen: seller_warehouse). '
            'No se mezcla con el almacén Full.'
        ),
    )
    has_meli_full = fields.Boolean(
        string='Usa Mercado Libre Full',
        default=False,
        help=(
            'Active si la empresa vende con logística Full (fulfillment). '
            'Las ventas Full descontarán stock del almacén Full configurado abajo.'
        ),
    )
    full_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Almacén Full',
        help=(
            'Stock de este almacén Odoo corresponde al inventario Full en Mercado Libre (meli_facility). '
            'Mercado Libre no permite modificar stock Full vía API; Odoo lo compara y registra diferencias. '
            'También se usa para descontar ventas Full importadas.'
        ),
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
    import_publications_active = fields.Boolean(
        string='Importación de publicaciones en curso',
        default=False,
        copy=False,
        help='La importación masiva corre en segundo plano (cron). Cada ítem confirma en base de datos.',
    )
    import_publications_offset = fields.Integer(
        string='Offset de importación',
        default=0,
        copy=False,
        help='Posición en la cola de ítems ya listados vía search_type=scan.',
    )
    import_publications_total = fields.Integer(
        string='Total ítems ML (última lectura)',
        default=0,
        copy=False,
        readonly=True,
    )
    import_publications_item_ids = fields.Text(
        string='Cola de ítems a importar (JSON)',
        copy=False,
        help='Lista completa de ml_item_id obtenida con search_type=scan (permite >1000 publicaciones).',
    )
    import_publications_batch_size = fields.Integer(
        string='Ítems por lote de importación',
        default=25,
        help='Ítems ML procesados por ejecución del cron (5–50). Valores bajos evitan timeouts en Odoo.sh.',
    )
    import_download_images = fields.Boolean(
        string='Descargar imágenes al importar',
        default=False,
        help='Desactivado acelera la importación masiva. Solo registra IDs de imagen ML; el binario se puede traer después.',
    )
    scan_new_publications_enabled = fields.Boolean(
        string='Detectar publicaciones nuevas (cron diario)',
        default=False,
        help=(
            'Una vez al día lista el catálogo ML (search_type=scan) y importa solo ítems '
            'cuyo MLA aún no existe en Odoo. '
            'No corre mientras haya una importación masiva activa.'
        ),
    )
    scan_new_publications_import_limit = fields.Integer(
        string='Máx. ítems nuevos por día',
        default=50,
        help='Tope de ítems ML nuevos a importar por ejecución del cron diario (1–200). '
             'Si hay más pendientes, se procesan en días siguientes.',
    )
    scan_new_publications_last_run = fields.Datetime(
        string='Último scan de novedades',
        readonly=True,
        copy=False,
    )
    scan_new_publications_last_imported = fields.Integer(
        string='Importadas en último scan',
        readonly=True,
        copy=False,
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
        domain=[('active', '=', True)],
        help='Cuenta contable de ingresos para líneas de factura de ventas ML. Si se configura, reemplaza la cuenta por defecto del producto/categoría.'
    )

    ml_commission_account_id = fields.Many2one(
        'account.account',
        string='Cuenta de Comisiones ML',
        domain=[
            ('active', '=', True),
            ('account_type', 'in', ('expense', 'expense_direct_cost', 'expense_depreciation')),
        ],
        help=(
            'Cuenta de gasto (débito) del asiento contable de comisión ML, separado de la factura al cliente. '
            'El importe se obtiene del payload de la orden (suma de sale_fee por ítem). '
            'La contrapartida (crédito) usa la cuenta por defecto del diario configurado en «Diario para comisión ML».'
        ),
    )

    fallback_product_id = fields.Many2one(
        'product.product',
        string='Producto por Defecto (Errores SKU)',
        domain=[('active', '=', True)],
        help='Producto a usar cuando no se pueda mapear una línea de venta de MercadoLibre por SKU/publicación.'
    )
    auto_invoice_with_fallback_product = fields.Boolean(
        string='Facturar automáticamente con producto fallback',
        default=False,
        help=(
            'Si está activo: con líneas fallback se confirma la orden y se factura '
            '(requiere «Crear Factura Automáticamente»). '
            'Si está desactivado: la orden queda en borrador/cotización para revisión manual. '
            'El IVA de cada línea fallback siempre se toma de la tasa que informa ML en la venta '
            '(independiente de este checkbox). En el PDF se muestra el título ML.'
        ),
    )
    default_order_partner_id = fields.Many2one(
        'res.partner',
        string='Contacto Consumidor Final',
        domain="[('is_company', '=', False)]",
        help=(
            'Contacto usado en pedidos y facturas de ventas ML que NO son B2B '
            '(siempre Consumidor Final). En ventas B2B se usa el comprador con datos fiscales de ML '
            'si ya existe en Odoo; si no hay match y «No crear contactos» está activo, se usa este CF.'
        ),
    )
    never_create_order_contacts = fields.Boolean(
        string='No crear contactos nuevos',
        default=False,
        help=(
            'Si está activo, no se crean contactos nuevos en Odoo por ventas ML. '
            'Las ventas no B2B siguen usando Consumidor Final. '
            'Las B2B reutilizan un contacto existente (meli_buyer_id o CUIT/DNI) y aplican billing-info; '
            'si no hay match, se usa Consumidor Final. Los datos del comprador siguen en la venta ML.'
        ),
    )
    
    payment_journal_id = fields.Many2one(
        'account.journal',
        string='Diario para comisión ML',
        domain=[('type', 'in', ['bank', 'cash', 'general'])],
        help=(
            'Diario del asiento de comisión ML (similar a una comisión bancaria). '
            'Débito: cuenta de comisiones ML. Crédito: cuenta por defecto de este diario '
            '(ej. banco donde acredita Mercado Libre). Si no se configura, se usa un diario misceláneo.'
        ),
    )
    
    default_tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto por Defecto',
        domain=[('type_tax_use', '=', 'sale')],
        help=(
            'Impuesto por defecto de la cuenta. Se usa como fallback cuando una línea fallback '
            'no encuentra un account.tax con la tasa IVA que informó Mercado Libre.'
        )
    )
    
    auto_create_sale_order = fields.Boolean(
        string='Crear Orden de Venta Automáticamente',
        default=True,
        help=(
            'Si está activado, al importar la venta ML se crea y confirma la orden de venta en Odoo. '
            'Si está desactivado, solo se guarda el registro ml.sale y podés usar el botón '
            '«Crear Orden de Venta en Odoo» en cada venta.'
        ),
    )
    auto_create_invoice = fields.Boolean(
        string='Crear Factura Automáticamente',
        default=True,
        help=(
            'Requiere «Crear Orden de Venta Automáticamente». Si está activado, se creará y publicará '
            'la factura al importar la venta (sin cobro automático).'
        ),
    )
    
    process_webhook_sales = fields.Boolean(
        string='Activar webhook de ventas',
        default=True,
        help=(
            'Recibe ventas nuevas de Mercado Libre en tiempo real vía notificaciones HTTP. '
            'Independiente del polling. Recomendado activo en Go-Live. Al activarlo se guarda '
            '«Webhook activo desde» y no se importan por webhook órdenes ML anteriores a ese momento.'
        ),
    )
    webhook_active_since = fields.Datetime(
        string='Webhook activo desde',
        readonly=True,
        copy=False,
        help=(
            'Fecha/hora en que se activó por última vez el webhook de ventas. '
            'Solo se importan por webhook órdenes creadas en ML después de este instante.'
        ),
    )
    webhook_last_received_at = fields.Datetime(
        string='Último webhook recibido',
        readonly=True,
        copy=False,
        help='Actualizado cada vez que ML llama a /ml/notification (o al terminar de procesar la venta).',
    )
    webhook_last_status = fields.Selection(
        [
            ('ok', 'OK — venta importada'),
            ('received', 'Recibido — en cola'),
            ('disabled', 'Recibido — procesamiento off'),
            ('rejected', 'Rechazado'),
            ('error', 'Error al procesar'),
            ('ignored', 'Topic ignorado'),
            ('ping', 'Ping GET'),
        ],
        string='Estado último webhook',
        readonly=True,
        copy=False,
    )
    webhook_last_summary = fields.Char(
        string='Resumen último webhook',
        readonly=True,
        copy=False,
    )

    enable_order_polling = fields.Boolean(
        string='Activar polling de ventas',
        default=False,
        help=(
            'Cron cada 10 min que consulta órdenes en ML como respaldo del webhook. '
            'Independiente del webhook. En Go-Live dejelo apagado; actívelo después si necesita '
            'recuperar ventas que el webhook no notificó. Al activarlo se guarda la fecha de inicio '
            'y no se importan órdenes anteriores a ese momento.'
        ),
    )

    polling_active_since = fields.Datetime(
        string='Polling activo desde',
        readonly=True,
        copy=False,
        help='Fecha/hora en que se activó el polling por última vez. Solo se importan órdenes ML posteriores.',
    )

    polling_hours_back = fields.Integer(
        string='Horas hacia atrás en polling',
        default=24,
        help=(
            'Ventana máxima de búsqueda cuando el polling está activo (mín. 1 h, máx. 720 h). '
            'Se combina con «Polling activo desde»: nunca se traen órdenes anteriores al momento '
            'en que activó el polling, aunque este valor sea mayor.'
        ),
    )
    
    auto_sync_stock_on_odoo_change = fields.Boolean(
        string='Sincronizar Stock Automáticamente',
        default=False,
        help='Si está activado, cualquier cambio de stock en Odoo actualizará automáticamente el stock en MercadoLibre. El stock se copia directamente (no se suma ni resta).'
    )
    sync_stock_reactivate_out_of_stock = fields.Boolean(
        string='Reactivar out_of_stock al sincronizar stock',
        default=False,
        help=(
            'Desactivado (recomendado): no se envía stock a publicaciones pausadas en Mercado Libre. '
            'Activado: solo las pausadas por falta de stock (sub_status out_of_stock) reciben stock '
            'desde Odoo y pueden reactivarse automáticamente en ML. '
            'Las pausadas manualmente (paused_by_seller) nunca reciben stock desde Odoo.'
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

    @api.constrains('has_meli_full', 'full_warehouse_id')
    def _check_meli_full_warehouse(self):
        for rec in self:
            if rec.has_meli_full and not rec.full_warehouse_id:
                raise ValidationError(
                    _('Debe elegir un almacén para ventas Full cuando la cuenta usa Mercado Libre Full.')
                )

    @api.constrains('auto_create_invoice', 'auto_create_sale_order')
    def _check_auto_invoice_requires_sale_order(self):
        for rec in self:
            if rec.auto_create_invoice and not rec.auto_create_sale_order:
                raise ValidationError(
                    _('No puede activar facturación automática sin crear orden de venta automáticamente.')
                )

    @api.onchange('auto_create_sale_order')
    def _onchange_auto_create_sale_order(self):
        if not self.auto_create_sale_order:
            self.auto_create_invoice = False

    def write(self, vals):
        vals = dict(vals)
        if vals.get('auto_create_sale_order') is False:
            vals['auto_create_invoice'] = False
        if vals.get('enable_order_polling') and any(not rec.enable_order_polling for rec in self):
            vals.setdefault('polling_active_since', fields.Datetime.now())
        if vals.get('process_webhook_sales') and any(not rec.process_webhook_sales for rec in self):
            vals.setdefault('webhook_active_since', fields.Datetime.now())
        return super().write(vals)

    def _ml_sale_import_options(self):
        """Opciones por defecto al importar ventas (webhook, polling)."""
        self.ensure_one()
        create_so = bool(self.auto_create_sale_order)
        return {
            'create_odoo_order': create_so,
            'create_customer': create_so,
            'update_stock': True,
        }

    def _webhook_order_is_too_old(self, order_data):
        """True si la orden ML es anterior al corte de go-live del webhook."""
        self.ensure_one()
        if not self.webhook_active_since:
            return False
        created_dt = self._polling_order_created_dt(order_data)
        if not created_dt:
            return False
        cutoff = fields.Datetime.to_datetime(self.webhook_active_since)
        return created_dt < cutoff

    @api.model
    def record_webhook_health(self, status, summary):
        """Registra en la cuenta y en logs si el webhook está llegando y cómo se procesó."""
        self.ensure_one()
        summary = (summary or '')[:255]
        self.sudo().write({
            'webhook_last_received_at': fields.Datetime.now(),
            'webhook_last_status': status,
            'webhook_last_summary': summary,
        })
        _logger.info(
            '[ML WEBHOOK][HEALTH] cuenta=%s (id=%s) status=%s %s',
            self.name,
            self.id,
            status,
            summary,
        )

    def _ml_normalize_iva_rate(self, rate):
        try:
            return round(float(rate or 0), 4)
        except (TypeError, ValueError):
            return 0.0

    def _ml_get_sale_tax_for_rate(self, rate):
        """
        Convierte la tasa IVA de ML (21.0, 10.5, …) en account.tax de venta de la compañía.

        Busca por porcentaje exacto; si no hay match usa default_tax_id.
        """
        self.ensure_one()
        default_tax = self.default_tax_id
        rate_f = self._ml_normalize_iva_rate(rate)

        if rate_f <= 0:
            return default_tax

        company = self.env.company

        candidates = self.env['account.tax'].sudo().search([
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', company.id),
            ('amount_type', '=', 'percent'),
            ('active', '=', True),
        ], order='sequence, id')
        for tax in candidates:
            if abs(tax.amount - rate_f) < 0.0001:
                _logger.info(
                    'Tasa IVA ML %.4f%% → impuesto Odoo %s (ID %s)',
                    rate_f, tax.name, tax.id,
                )
                return tax

        if default_tax:
            _logger.warning(
                'Sin impuesto de venta con tasa %.4f%% en %s; usando default_tax_id %s',
                rate_f, company.display_name, default_tax.name,
            )
            return default_tax

        _logger.warning(
            'Sin impuesto de venta con tasa %.4f%% y sin default_tax_id en cuenta %s',
            rate_f, self.name,
        )
        return self.env['account.tax']

    def _meli_logistic_type_is_full(self, logistic_type):
        """True si logistic_type ML corresponde a envío Full (fulfillment)."""
        return (logistic_type or '').lower().strip() == 'fulfillment'

    def _meli_item_data_is_full(self, item_data):
        """
        True si el ítem en ML tiene logística Full a nivel catálogo (GET /items).

        No indica que una venta concreta sea Full (convivencia Full+Flex); para ventas
        usar ``_meli_order_data_is_full`` (logistic_type del envío).
        """
        if not item_data or not isinstance(item_data, dict):
            return False
        shipping = item_data.get('shipping') or {}
        if isinstance(shipping, dict):
            if self._meli_logistic_type_is_full(shipping.get('logistic_type')):
                return True
        tags = item_data.get('tags') or []
        if any(str(tag).lower().strip() == 'fulfillment' for tag in tags):
            return True
        return False

    def _meli_order_data_is_full(self, order_data):
        """
        True solo si ESTA venta se despacha por Full (fulfillment).

        Usa ``logistic_type`` del envío (orden o GET /shipments). No infiere Full desde
        store_id/node_id (Flex/multi-origen) ni desde la configuración del ítem.
        """
        self.ensure_one()
        if not order_data or not isinstance(order_data, dict):
            return False

        order_id = order_data.get('id')
        shipping = order_data.get('shipping') or {}
        if not isinstance(shipping, dict):
            shipping = {}

        lt = (shipping.get('logistic_type') or '').lower().strip()
        if lt:
            is_full = self._meli_logistic_type_is_full(lt)
            _logger.info(
                'Orden ML %s: shipping.logistic_type=%s → is_full=%s',
                order_id, lt, is_full,
            )
            return is_full

        shipment_id = shipping.get('id')
        if shipment_id:
            shipment_data = self._fetch_meli_shipment_data(shipment_id)
            lt = (shipment_data.get('logistic_type') or '').lower().strip()
            if lt:
                is_full = self._meli_logistic_type_is_full(lt)
                _logger.info(
                    'Orden ML %s: shipment %s logistic_type=%s → is_full=%s',
                    order_id, shipment_id, lt, is_full,
                )
                return is_full

        _logger.info(
            'Orden ML %s: sin logistic_type en orden/envío → is_full=False',
            order_id,
        )
        return False

    def _fetch_meli_shipment_data(self, shipment_id):
        """GET /shipments/{id} — dirección y receptor cuando la orden solo trae shipping.id."""
        self.ensure_one()
        if not shipment_id or not self.access_token:
            return {}
        try:
            self._ensure_valid_token()
            url = f'https://api.mercadolibre.com/shipments/{shipment_id}'
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json',
            }
            resp = self._ml_request_with_retry('GET', url, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json() or {}
                _logger.info(
                    '✅ Shipment %s obtenido desde API (logistic_type=%s)',
                    shipment_id,
                    data.get('logistic_type'),
                )
                return data
            _logger.warning(
                '⚠️ GET shipments/%s HTTP %s: %s',
                shipment_id,
                resp.status_code,
                (resp.text or '')[:500],
            )
        except Exception as e:
            _logger.warning('⚠️ No se pudo obtener shipment %s: %s', shipment_id, e)
        return {}

    def _fetch_meli_billing_info_data(self, order_data, billing_info_id=None):
        """
        GET billing-info de ML para datos fiscales (CUIT, situación IVA).
        Flujo nuevo: /orders/billing-info/{site_id}/{billing_info_id}
        Fallback: /orders/{order_id}/billing_info con x-version: 2 (MLA).
        """
        self.ensure_one()
        if not self.access_token:
            return {}

        buyer = (order_data or {}).get('buyer') or {}
        billing_ref = buyer.get('billing_info') or {}
        billing_id = billing_info_id or billing_ref.get('id')
        if not billing_id:
            return {}

        site_id = (order_data.get('context') or {}).get('site')
        if not site_id:
            payments = order_data.get('payments') or []
            if payments and isinstance(payments[0], dict):
                site_id = payments[0].get('site_id')
        if not site_id:
            site_id = self._mercadolibre_site_id()

        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }
        try:
            self._ensure_valid_token()
            url = f'https://api.mercadolibre.com/orders/billing-info/{site_id}/{billing_id}'
            resp = self._ml_request_with_retry('GET', url, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json() or {}
                _logger.info(
                    '✅ billing-info %s/%s obtenido para orden ML %s',
                    site_id,
                    billing_id,
                    order_data.get('id'),
                )
                return data
            _logger.warning(
                '⚠️ GET billing-info/%s/%s HTTP %s: %s',
                site_id,
                billing_id,
                resp.status_code,
                (resp.text or '')[:500],
            )

            order_id = order_data.get('id')
            if order_id and str(site_id).upper() in ('MLA', 'MLM', 'MLB', 'MLC', 'MCO', 'MEC'):
                headers_legacy = {**headers, 'x-version': '2'}
                legacy_url = f'https://api.mercadolibre.com/orders/{order_id}/billing_info'
                resp_legacy = self._ml_request_with_retry(
                    'GET', legacy_url, headers=headers_legacy,
                )
                if resp_legacy.status_code in (200, 201):
                    data = resp_legacy.json() or {}
                    _logger.info(
                        '✅ billing_info legacy obtenido para orden ML %s',
                        order_id,
                    )
                    return data
                _logger.warning(
                    '⚠️ GET orders/%s/billing_info HTTP %s: %s',
                    order_id,
                    resp_legacy.status_code,
                    (resp_legacy.text or '')[:500],
                )
        except Exception as e:
            _logger.warning(
                '⚠️ No se pudo obtener billing-info %s para orden %s: %s',
                billing_id,
                order_data.get('id'),
                e,
            )
        return {}

    def _get_warehouse_for_ml_sale(self, is_full=False):
        """Almacén Odoo para reservar stock de una venta importada."""
        self.ensure_one()
        if is_full and self.has_meli_full and self.full_warehouse_id:
            return self.full_warehouse_id
        if self.warehouse_id:
            return self.warehouse_id
        return self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)

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

    OAUTH_HUB_URL_KEY = 'mercadolibre_connector.oauth_hub_url'
    OAUTH_HUB_SECRET_KEY = 'mercadolibre_connector.oauth_shared_secret'

    @api.depends()
    def _compute_oauth_mode(self):
        via = self._oauth_via_hub_enabled()
        is_hub = self._oauth_this_is_hub()
        for rec in self:
            rec.oauth_via_hub = via
            rec.oauth_is_hub = is_hub

    @api.depends()
    def _compute_oauth_callback_uri(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for rec in self:
            rec.oauth_callback_uri = (
                f'{base}/mercadolibre/oauth/callback' if base else '/mercadolibre/oauth/callback'
            )

    @api.model
    def _oauth_hub_url(self):
        return (self.env['ir.config_parameter'].sudo().get_param(self.OAUTH_HUB_URL_KEY) or '').strip().rstrip('/')

    @api.model
    def _oauth_hub_secret(self):
        return (self.env['ir.config_parameter'].sudo().get_param(self.OAUTH_HUB_SECRET_KEY) or '').strip()

    @api.model
    def _oauth_same_host(self, url_a, url_b):
        if not url_a or not url_b:
            return False
        from urllib.parse import urlparse
        a, b = urlparse(url_a), urlparse(url_b)
        return (a.netloc or '').lower() == (b.netloc or '').lower()

    @api.model
    def _oauth_via_hub_enabled(self):
        return bool(self._oauth_hub_url() and self._oauth_hub_secret())

    @api.model
    def _oauth_this_is_hub(self):
        if not self._oauth_via_hub_enabled():
            return False
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        return self._oauth_same_host(self._oauth_hub_url(), base)

    def _oauth_app_credentials(self):
        """Client ID/Secret de la app ML. En el hub usa la app central (redirect URI única)."""
        self.ensure_one()
        if self._oauth_this_is_hub() and 'ml.oauth.hub.config' in self.env:
            cfg = self.env['ml.oauth.hub.config'].sudo().get_config()
            if cfg:
                hcid = (cfg.client_id or '').strip()
                hsec = (cfg.client_secret or '').strip()
                if hcid and hsec:
                    return hcid, hsec
        return (self.client_id or '').strip(), (self.client_secret or '').strip()

    def _ensure_hub_app_credentials_on_account(self):
        """Copia Client ID/Secret del hub a la cuenta local (refresh y webhooks)."""
        self.ensure_one()
        if not self._oauth_this_is_hub():
            return
        cid, csec = self._oauth_app_credentials()
        vals = {}
        if cid and (self.client_id or '').strip() != cid:
            vals['client_id'] = cid
        if csec and (self.client_secret or '').strip() != csec:
            vals['client_secret'] = csec
        if vals:
            self.sudo().write(vals)

    def _verify_oauth_install_signature(self, raw_body, signature):
        """HMAC-SHA256 del body JSON con el secreto del hub."""
        secret = (self._oauth_hub_secret() or '').encode('utf-8')
        if not secret or not raw_body or not signature:
            return False
        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(expected, str(signature))
        except (TypeError, ValueError):
            return False

    def _apply_tokens_from_hub(self, token_data):
        """Aplica tokens (y credenciales de app) recibidos del hub OAuth."""
        self.ensure_one()
        vals = {}
        if token_data.get('client_id'):
            vals['client_id'] = token_data.get('client_id')
        if token_data.get('client_secret'):
            vals['client_secret'] = token_data.get('client_secret')
        if vals:
            self.sudo().write(vals)
        # Respuesta rápida al hub: sin users/me ni register_seller en esta request
        # (el hub ya registró el seller antes del POST /install).
        self.with_context(ml_oauth_install_fast=True)._save_tokens(token_data)
        self._schedule_meli_user_sync_postcommit()

    def _schedule_meli_user_sync_postcommit(self):
        """users/me + register_seller después de responder HTTP 200 al hub."""
        self.ensure_one()
        account_id = self.id
        dbname = self.env.cr.dbname

        def _run_after_commit():
            try:
                from odoo.modules.registry import Registry
                reg = Registry(dbname)
                with reg.cursor() as cr:
                    env = api.Environment(cr, api.SUPERUSER_ID, {})
                    account = env['ml.account'].browse(account_id)
                    if not account.exists():
                        return
                    try:
                        account._sync_meli_user_info()
                    except Exception as e:
                        _logger.warning(
                            '[ML OAUTH] post-install users/me falló account_id=%s: %s',
                            account_id, e,
                        )
                    cr.commit()
            except Exception:
                _logger.exception(
                    '[ML OAUTH] post-install sync error account_id=%s db=%s',
                    account_id, dbname,
                )

        try:
            postcommit = self.env.cr.postcommit
            if hasattr(postcommit, 'add'):
                postcommit.add(_run_after_commit)
        except Exception:
            _logger.exception('[ML OAUTH] no se pudo programar post-install sync')

    def _register_seller_on_hub(self):
        """Informa al hub el meli_user_id para enrutar webhooks a esta base."""
        self.ensure_one()
        if not self._oauth_via_hub_enabled() or self._oauth_this_is_hub():
            _logger.info(
                '[ML OAUTH] register_seller skip: via_hub=%s is_hub=%s',
                self._oauth_via_hub_enabled(), self._oauth_this_is_hub(),
            )
            return False
        if not (self.meli_user_id or '').strip():
            _logger.warning(
                '[ML OAUTH] register_seller skip: account_id=%s sin meli_user_id',
                self.id,
            )
            return False
        hub = self._oauth_hub_url()
        secret = self._oauth_hub_secret()
        if not hub or not secret:
            _logger.error(
                '[ML OAUTH] register_seller ABORT: hub_url=%s secret_set=%s',
                bool(hub), bool(secret),
            )
            return False
        payload = json.dumps({
            'tenant': self.env.cr.dbname,
            'return_url': self._get_web_base_url(),
            'meli_user_id': (self.meli_user_id or '').strip(),
            'nickname': self.meli_nickname or '',
            'account_id': self.id,
            'ts': int(time.time()),
        }, separators=(',', ':'), sort_keys=True)
        sig = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = '%s/mercadolibre/oauth/hub/register_seller' % hub
        _logger.info(
            '[ML OAUTH] register_seller POST %s meli_user_id=%s account_id=%s',
            url, self.meli_user_id, self.id,
        )
        try:
            resp = requests.post(
                url,
                data=payload.encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'X-ML-OAuth-Signature': sig,
                },
                timeout=20,
            )
        except requests.RequestException as e:
            _logger.warning('[ML OAUTH] register_seller RequestException: %s', e)
            return False
        if resp.status_code != 200:
            _logger.warning(
                '[ML OAUTH] register_seller HTTP %s: %s',
                resp.status_code, (resp.text or '')[:200],
            )
            return False
        _logger.info(
            '[ML OAUTH] register_seller OK seller=%s db=%s',
            self.meli_user_id, self.env.cr.dbname,
        )
        return True

    def _get_web_base_url(self):
        """URL pública de Odoo (parámetro web.base.url)."""
        return (
            self.env['ir.config_parameter']
            .sudo()
            .get_param('web.base.url', '')
            .rstrip('/')
        )

    def _get_oauth_redirect_uri(self):
        """
        URI de callback OAuth canónica. Debe coincidir EXACTAMENTE con la registrada
        en https://developers.mercadolibre.com (mismo host que web.base.url).
        """
        self.ensure_one()
        web_base = self._get_web_base_url()
        if not web_base:
            raise UserError(
                _(
                    'Configure el parámetro del sistema "web.base.url" con la URL pública '
                    'de este Odoo (ej. https://su-dominio.odoo.com).'
                )
            )
        canonical = f'{web_base}/mercadolibre/oauth/callback'
        stored = (self.redirect_uri or '').strip().rstrip('/')
        if not stored:
            return canonical
        if stored.startswith('/'):
            return f'{web_base}{stored}'
        if stored.startswith('http'):
            return stored
        return canonical

    def _validate_oauth_configuration(self):
        """Comprueba datos mínimos antes de redirigir a Mercado Libre."""
        self.ensure_one()
        cid, csec = self._oauth_app_credentials()
        if not cid:
            raise UserError(_('Indique el Client ID de su aplicación en Mercado Libre.'))
        if not csec:
            raise UserError(_('Indique el Client Secret de su aplicación en Mercado Libre.'))
        if not self.country_id:
            raise UserError(
                _('Seleccione el país de la cuenta (debe coincidir con el sitio de Mercado Libre).')
            )
        redirect = self._get_oauth_redirect_uri()
        web_base = self._get_web_base_url()
        if web_base and not redirect.startswith(web_base):
            raise UserError(
                _(
                    'La Redirect URI (%(redirect)s) no usa el mismo dominio que web.base.url (%(base)s). '
                    'En developers.mercadolibre.com registre exactamente:\n%(redirect)s'
                )
                % {'redirect': redirect, 'base': web_base}
            )
        stored = (self.redirect_uri or '').strip().rstrip('/')
        if stored and stored != redirect and stored != redirect.rstrip('/'):
            _logger.warning(
                'OAuth ML cuenta %s: redirect_uri en ficha (%s) difiere de la usada (%s); '
                'se usará la canónica. Actualice la app en developers.mercadolibre.com.',
                self.id,
                stored,
                redirect,
            )
        return redirect

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
        redirect_uri = self._validate_oauth_configuration()

        self._ensure_hub_app_credentials_on_account()
        cid, _csec = self._oauth_app_credentials()
        params = {
            'response_type': 'code',
            'client_id': cid,
            'redirect_uri': redirect_uri,
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
        if self.env.context.get('ml_oauth_install_fast'):
            return
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
        
        self._ensure_valid_token()

        resp = self._ml_request_with_retry(
            'GET',
            'https://api.mercadolibre.com/users/me',
            timeout=15,
        )
        if not resp.ok:
            raise UserError(_("Error al obtener users/me: %s") % resp.text)

        data = resp.json() or {}
        user_id = data.get("id")
        nickname = data.get("nickname")
        self.sudo().write({
            "meli_user_id": str(user_id) if user_id else False,
            "meli_nickname": nickname or False,
        })
        if self._oauth_via_hub_enabled() and not self._oauth_this_is_hub():
            try:
                self._register_seller_on_hub()
            except Exception as e:
                _logger.warning(
                    'OAuth ML: register_seller tras users/me falló (account=%s): %s',
                    self.id, e,
                )
        return True

    def exchange_code_for_token(self, code):
        """Intercambia 'code' por tokens usando PKCE."""
        redirect_uri = self._get_oauth_redirect_uri()
        cid, csec = self._oauth_app_credentials()
        url = "https://api.mercadolibre.com/oauth/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": cid,
            "client_secret": csec,
            "code": code,
            "redirect_uri": redirect_uri,
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
        cid, csec = self._oauth_app_credentials()
        url = "https://api.mercadolibre.com/oauth/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": cid,
            "client_secret": csec,
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

    def _token_needs_refresh(self, buffer_minutes=None):
        """
        True si el access_token no existe, ya venció o vence dentro del margen indicado.
        Usado por el refresh lazy antes de cada llamada a la API.
        """
        self.ensure_one()
        if not self.refresh_token:
            return not bool(self.access_token)
        if not self.access_token or not self.token_expiration:
            return True
        buffer = timedelta(minutes=buffer_minutes if buffer_minutes is not None else ML_TOKEN_REFRESH_BUFFER_MINUTES)
        expiration = fields.Datetime.to_datetime(self.token_expiration)
        return fields.Datetime.now() >= (expiration - buffer)

    def _ensure_valid_token(self, buffer_minutes=None):
        """
        Refresh lazy: renueva el token solo si está vencido o por vencer (margen corto).
        Debe llamarse antes de cada uso del access_token en la API de Mercado Libre.
        """
        self.ensure_one()

        if not self.access_token and not self.refresh_token:
            _logger.warning("⚠️ No hay access_token ni refresh_token para la cuenta %s", self.name)
            return False

        if not self._token_needs_refresh(buffer_minutes=buffer_minutes):
            _logger.debug("✅ Token vigente para cuenta %s (expira %s)", self.name, self.token_expiration)
            return True

        if not self.refresh_token:
            _logger.warning(
                "⚠️ Token expirado o por vencer en cuenta %s y no hay refresh_token. "
                "Reautorice la cuenta.",
                self.name,
            )
            return bool(self.access_token)

        _logger.info(
            "🔄 Refresh lazy del token para cuenta %s (expira %s)",
            self.name,
            self.token_expiration or 'desconocido',
        )
        try:
            self.refresh_access_token()
            return True
        except Exception as e:
            _logger.error("❌ Error al refrescar token (lazy) para cuenta %s: %s", self.name, e)
            return bool(self.access_token)

    def _ml_request_with_retry(self, method, url, headers=None, json=None, data=None, files=None, params=None, max_retries=3, timeout=30):
        """
        Request a la API de Mercado Libre con refresh lazy previo y retry automático en 401.
        """
        self.ensure_one()

        if headers is None:
            headers = {}

        for attempt in range(1, max_retries + 1):
            if not self._ensure_valid_token():
                raise UserError(
                    _('La cuenta «%s» no tiene un token de acceso válido. Reautorice Mercado Libre.')
                    % self.name
                )

            req_headers = dict(headers)
            if 'Authorization' not in req_headers:
                req_headers['Authorization'] = f'Bearer {self.access_token}'

            try:
                kwargs = {'headers': req_headers, 'timeout': timeout}
                if json is not None:
                    kwargs['json'] = json
                if data is not None:
                    kwargs['data'] = data
                if files is not None:
                    kwargs['files'] = files
                if params is not None:
                    kwargs['params'] = params

                response = requests.request(method, url, **kwargs)

                if response.status_code == 401:
                    _logger.warning(
                        "🔄 401 Unauthorized en %s %s (intento %d/%d). Refrescando token...",
                        method, url, attempt, max_retries,
                    )
                    try:
                        self.refresh_access_token()
                    except Exception as refresh_error:
                        _logger.error("❌ No se pudo refrescar token tras 401: %s", refresh_error)
                        if attempt >= max_retries:
                            raise UserError(
                                _('Token de Mercado Libre expirado para «%s». Reautorice la cuenta.')
                                % self.name
                            ) from refresh_error
                    continue

                if response.status_code == 403:
                    _logger.error(
                        "❌ 403 Forbidden para cuenta %s. Token revocado o sin permisos.",
                        self.name,
                    )
                    self.sudo().write({
                        'is_connected': False,
                        'access_token': False,
                    })
                    raise UserError(_('Token revocado. Por favor, autorice la cuenta nuevamente.'))

                if response.status_code == 429 and attempt < max_retries:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    _logger.warning("⏳ Rate limit ML. Esperando %d s...", retry_after)
                    time.sleep(retry_after)
                    continue

                if response.status_code in (408, 409, 423, 500, 502, 503, 504) and attempt < max_retries:
                    wait_time = attempt * 2
                    _logger.warning(
                        "⚠️ HTTP %s en %s (intento %d/%d). Reintento en %ds...",
                        response.status_code, url, attempt, max_retries, wait_time,
                    )
                    time.sleep(wait_time)
                    continue

                return response

            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    _logger.warning("⏱️ Timeout en %s %s (intento %d/%d)", method, url, attempt, max_retries)
                    time.sleep(attempt)
                    continue
                raise

            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    _logger.warning(
                        "⚠️ Error de red en %s %s (intento %d/%d): %s",
                        method, url, attempt, max_retries, e,
                    )
                    time.sleep(attempt)
                    continue
                raise

        raise UserError(
            _('La solicitud a Mercado Libre falló tras %d intentos: %s %s')
            % (max_retries, method, url)
        )

    @api.model
    def cron_refresh_expiring_tokens(self):
        """
        Respaldo programado: refresca tokens que vencen pronto.
        La renovación principal es lazy (_ensure_valid_token) en cada llamada API.
        Nunca propaga excepciones para evitar que Odoo desactive el cron.
        """
        try:
            accounts = self.search([
                ('is_connected', '=', True),
                ('refresh_token', '!=', False),
            ])

            if not accounts:
                _logger.info("ℹ️ Cron ML tokens: no hay cuentas conectadas.")
                return True

            backup_buffer_minutes = ML_TOKEN_CRON_BACKUP_HOURS * 60
            _logger.info(
                "🔄 Cron respaldo tokens ML: %d cuenta(s), umbral %dh",
                len(accounts),
                ML_TOKEN_CRON_BACKUP_HOURS,
            )

            refreshed_count = 0
            skipped_count = 0
            error_count = 0

            for account in accounts:
                try:
                    if not account._token_needs_refresh(buffer_minutes=backup_buffer_minutes):
                        skipped_count += 1
                        continue
                    account.refresh_access_token()
                    refreshed_count += 1
                    _logger.info("✅ Cron: token refrescado para cuenta %s", account.name)
                except Exception as e:
                    error_count += 1
                    _logger.exception(
                        "❌ Cron: error refrescando token cuenta %s (id=%s): %s",
                        account.name,
                        account.id,
                        e,
                    )

            _logger.info(
                "🔄 Cron respaldo tokens ML: refrescados=%d omitidos=%d errores=%d",
                refreshed_count,
                skipped_count,
                error_count,
            )
        except Exception as e:
            _logger.exception("❌ Cron respaldo tokens ML — error global (cron sigue activo): %s", e)

        return True

    def _polling_parse_order_dt_utc(self, iso_string):
        """Convierte fecha ISO de ML a datetime naive UTC."""
        if not iso_string:
            return None
        try:
            dt = datetime.fromisoformat(str(iso_string).replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            return None

    def _polling_resolve_seller_id(self):
        """ID vendedor ML (meli_user_id); si falta, lo obtiene de users/me."""
        self.ensure_one()
        if self.meli_user_id:
            return str(self.meli_user_id)
        try:
            self._sync_meli_user_info()
        except Exception as e:
            _logger.warning(
                "Meli polling: no se pudo obtener users/me para cuenta %s: %s",
                self.name,
                e,
            )
        return str(self.meli_user_id) if self.meli_user_id else None

    def _polling_order_created_dt(self, order):
        """Fecha de creación de la orden en UTC (naive)."""
        if not order or not isinstance(order, dict):
            return None
        return self._polling_parse_order_dt_utc(
            order.get('date_created') or order.get('date_closed')
        )

    def _polling_date_filter_strategies(self, from_dt, to_dt):
        """Variantes de query params de fecha aceptadas (o no) por orders/search."""
        from_s = from_dt.strftime('%Y-%m-%dT%H:%M:%S.000-00:00')
        to_s = to_dt.strftime('%Y-%m-%dT%H:%M:%S.000-00:00')
        return [
            (
                'order.date_created',
                {
                    'order.date_created.from': from_s,
                    'order.date_created.to': to_s,
                },
            ),
            (
                'order_created_from',
                {
                    'order_created_from': from_s,
                    'order_created_to': to_s,
                },
            ),
            ('python_only', {}),
        ]

    def _polling_seller_param_candidates(self):
        """seller=me (token) o ID numérico del vendedor."""
        candidates = ['me']
        seller_id = self._polling_resolve_seller_id()
        if seller_id and str(seller_id) not in candidates:
            candidates.append(str(seller_id))
        return candidates

    def _polling_fetch_order_ids(self):
        """
        Lista IDs de órdenes del vendedor en la ventana polling_hours_back.
        Reintenta orders/search con distintos parámetros de fecha y seller si la API falla.
        """
        self.ensure_one()
        log_prefix = f"Meli polling [{self.name} id={self.id}]"

        try:
            self._ensure_valid_token()
        except Exception as e:
            _logger.error("%s: token inválido antes de listar órdenes: %s", log_prefix, e)
            return []

        hours_back = max(1, min(720, int(self.polling_hours_back or 24)))
        to_dt = datetime.utcnow()
        from_dt = to_dt - timedelta(hours=hours_back)
        polling_since_dt = None
        if self.polling_active_since:
            polling_since_dt = fields.Datetime.to_datetime(self.polling_active_since)
            if polling_since_dt and polling_since_dt > from_dt:
                from_dt = polling_since_dt
        date_strategies = self._polling_date_filter_strategies(from_dt, to_dt)
        seller_candidates = self._polling_seller_param_candidates()

        _logger.info(
            "%s: inicio ventana=%sh (%s → %s UTC), polling_desde=%s, sellers=%s, estrategias_fecha=%s",
            log_prefix,
            hours_back,
            from_dt.strftime('%Y-%m-%d %H:%M:%S'),
            to_dt.strftime('%Y-%m-%d %H:%M:%S'),
            polling_since_dt.strftime('%Y-%m-%d %H:%M:%S') if polling_since_dt else 'N/A',
            seller_candidates,
            [s[0] for s in date_strategies],
        )

        url = 'https://api.mercadolibre.com/orders/search'
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }
        limit = 50

        order_ids = []
        seen = set()
        skipped_old = 0
        skipped_no_date = 0
        skipped_future = 0
        skipped_duplicate = 0

        date_strategy_idx = 0
        seller_idx = 0
        date_extra = date_strategies[date_strategy_idx][1]
        date_strategy_name = date_strategies[date_strategy_idx][0]
        api_date_filter_active = bool(date_extra)
        seller_param = seller_candidates[seller_idx]

        offset = 0
        page_num = 0
        last_http_error = None

        while page_num < 200:
            page_num += 1
            params = {
                'seller': seller_param,
                'sort': 'date_desc',
                'offset': offset,
                'limit': limit,
            }
            params.update(date_extra)

            _logger.info(
                "%s: GET orders/search página=%s offset=%s seller=%s fecha=%s",
                log_prefix,
                page_num,
                offset,
                seller_param,
                date_strategy_name,
            )

            try:
                resp = self._ml_request_with_retry('GET', url, headers=headers, params=params)
            except Exception as e:
                _logger.exception(
                    "%s: error de red orders/search (seller=%s, fecha=%s): %s",
                    log_prefix,
                    seller_param,
                    date_strategy_name,
                    e,
                )
                last_http_error = str(e)
                break

            if not resp.ok:
                body = (resp.text or '')[:500]
                last_http_error = f"HTTP {resp.status_code}: {body}"
                _logger.warning(
                    "%s: orders/search HTTP %s seller=%s fecha=%s body=%s",
                    log_prefix,
                    resp.status_code,
                    seller_param,
                    date_strategy_name,
                    body,
                )
                # Cambiar estrategia de fecha
                if date_extra and date_strategy_idx + 1 < len(date_strategies):
                    date_strategy_idx += 1
                    date_strategy_name, date_extra = date_strategies[date_strategy_idx]
                    api_date_filter_active = bool(date_extra)
                    offset = 0
                    page_num = 0
                    seen.clear()
                    order_ids.clear()
                    skipped_old = skipped_no_date = skipped_future = skipped_duplicate = 0
                    _logger.info(
                        "%s: reintento con estrategia de fecha «%s»",
                        log_prefix,
                        date_strategy_name,
                    )
                    continue
                # Cambiar seller (me ↔ id)
                if seller_idx + 1 < len(seller_candidates):
                    seller_idx += 1
                    seller_param = seller_candidates[seller_idx]
                    offset = 0
                    page_num = 0
                    seen.clear()
                    order_ids.clear()
                    skipped_old = skipped_no_date = skipped_future = skipped_duplicate = 0
                    _logger.info(
                        "%s: reintento con seller=%s (fecha=%s)",
                        log_prefix,
                        seller_param,
                        date_strategy_name,
                    )
                    continue
                _logger.error(
                    "%s: orders/search agotó reintentos (seller=%s, fecha=%s)",
                    log_prefix,
                    seller_param,
                    date_strategy_name,
                )
                break

            data = resp.json() or {}
            page_orders = data.get('results') or []
            paging = data.get('paging') or {}
            total = int(paging.get('total') or 0)

            _logger.info(
                "%s: respuesta OK página=%s resultados=%s total_api=%s",
                log_prefix,
                page_num,
                len(page_orders),
                total or 'N/A',
            )

            if not page_orders:
                if page_num == 1:
                    _logger.info(
                        "%s: primera página vacía (keys=%s paging=%s)",
                        log_prefix,
                        list(data.keys()),
                        paging,
                    )
                break

            if page_num == 1 and page_orders:
                sample = page_orders[0]
                _logger.debug(
                    "%s: muestra orden id=%s keys=%s date_created=%s",
                    log_prefix,
                    sample.get('id'),
                    list(sample.keys()) if isinstance(sample, dict) else type(sample),
                    sample.get('date_created') if isinstance(sample, dict) else None,
                )

            stop_pagination = False
            added_this_page = 0
            for order in page_orders:
                oid = order.get('id')
                if not oid:
                    continue
                oid = str(oid)
                if oid in seen:
                    skipped_duplicate += 1
                    continue

                created_dt = self._polling_order_created_dt(order)
                if not created_dt:
                    if api_date_filter_active:
                        seen.add(oid)
                        order_ids.append(oid)
                        added_this_page += 1
                    else:
                        skipped_no_date += 1
                    continue
                if created_dt < from_dt:
                    skipped_old += 1
                    stop_pagination = True
                    break
                if polling_since_dt and created_dt < polling_since_dt:
                    skipped_old += 1
                    continue
                if created_dt > to_dt:
                    skipped_future += 1
                    continue

                seen.add(oid)
                order_ids.append(oid)
                added_this_page += 1

            _logger.info(
                "%s: página=%s agregadas=%s omit_antiguas=%s sin_fecha=%s futuras=%s dup=%s",
                log_prefix,
                page_num,
                added_this_page,
                skipped_old,
                skipped_no_date,
                skipped_future,
                skipped_duplicate,
            )

            if stop_pagination:
                _logger.info("%s: fin paginación (orden anterior a ventana %sh)", log_prefix, hours_back)
                break

            if len(page_orders) < limit:
                break
            if total and (offset + len(page_orders)) >= total:
                break
            if added_this_page == 0 and (skipped_old or skipped_no_date):
                _logger.info(
                    "%s: fin paginación (página sin órdenes útiles en ventana)",
                    log_prefix,
                )
                break
            offset += limit

        if not order_ids and last_http_error:
            _logger.error(
                "%s: 0 órdenes — último error API: %s (seller=%s, fecha=%s)",
                log_prefix,
                last_http_error,
                seller_param,
                date_strategy_name,
            )
        elif not order_ids:
            _logger.info(
                "%s: 0 órdenes en ventana %sh (sin error HTTP; revisar ventana o actividad ML)",
                log_prefix,
                hours_back,
            )
        else:
            _logger.info(
                "%s: %d órdenes en ventana %sh (seller=%s, fecha=%s, páginas=%s, "
                "omit_antiguas=%s, sin_fecha=%s, futuras=%s)",
                log_prefix,
                len(order_ids),
                hours_back,
                seller_param,
                date_strategy_name,
                page_num,
                skipped_old,
                skipped_no_date,
                skipped_future,
            )
        return order_ids

    @api.model
    def cron_poll_recent_orders(self):
        """
        Polling periódico de ventas ML → Odoo (complemento al webhook).

        Consulta orders/search con paginación en la ventana polling_hours_back.
        Idempotencia vía update_or_create_from_meli (ml_order_id + company_id).
        """
        accounts = self.sudo().search([
            ('enable_order_polling', '=', True),
            ('is_connected', '=', True),
            ('access_token', '!=', False),
        ])
        if not accounts:
            _logger.info(
                "Meli polling: sin cuentas elegibles "
                "(activar «Activar polling de órdenes», cuenta conectada y token)."
            )
            return True

        _logger.info(
            "Meli polling: iniciando para %d cuenta(s): %s",
            len(accounts),
            ', '.join(f"{a.name}(id={a.id})" for a in accounts),
        )
        sale_model = self.env['ml.sale'].sudo()
        total_orders = 0
        total_ok = 0
        total_errors = 0
        accounts_with_orders = 0
        accounts_empty = 0

        for account in accounts:
            _logger.info(
                "Meli polling: procesando cuenta «%s» (id=%s, polling_hours_back=%s, meli_user_id=%s)",
                account.name,
                account.id,
                account.polling_hours_back,
                account.meli_user_id or 'N/A',
            )
            try:
                order_ids = account._polling_fetch_order_ids()
            except Exception as e:
                _logger.exception(
                    "Meli polling: excepción listando órdenes cuenta %s (id=%s): %s",
                    account.name,
                    account.id,
                    e,
                )
                total_errors += 1
                continue

            if not order_ids:
                accounts_empty += 1
                _logger.info(
                    "Meli polling: cuenta %s → 0 órdenes (ver logs «Meli polling [%s]» arriba)",
                    account.name,
                    account.name,
                )
                continue

            accounts_with_orders += 1
            total_orders += len(order_ids)
            _logger.info(
                "Meli polling: cuenta %s → %d orden(es) a sincronizar: %s%s",
                account.name,
                len(order_ids),
                ', '.join(order_ids[:10]),
                '...' if len(order_ids) > 10 else '',
            )

            for order_id in order_ids:
                try:
                    result = sale_model.update_or_create_from_meli(
                        order_id,
                        account_id=account.id,
                        **account._ml_sale_import_options(),
                    )
                    if result:
                        total_ok += 1
                        _logger.info(
                            "Meli polling: orden %s OK → ml.sale id=%s",
                            order_id,
                            result.id,
                        )
                    else:
                        total_errors += 1
                        _logger.warning(
                            "Meli polling: orden %s devolvió None (cuenta %s)",
                            order_id,
                            account.name,
                        )
                except Exception as e:
                    total_errors += 1
                    _logger.exception(
                        "Meli polling: error procesando orden %s (cuenta %s): %s",
                        order_id,
                        account.name,
                        e,
                    )

        _logger.info(
            "Meli polling finalizado: cuentas=%d, con_órdenes=%d, vacías=%d, "
            "órdenes_listadas=%d, sincronizadas_OK=%d, errores=%d",
            len(accounts),
            accounts_with_orders,
            accounts_empty,
            total_orders,
            total_ok,
            total_errors,
        )
        return True






    def action_open_authorize(self):
        self.ensure_one()
        if not self.country_id:
            raise UserError(
                _('Seleccione el país de la cuenta (debe coincidir con el sitio de Mercado Libre).')
            )
        via = self._oauth_via_hub_enabled()
        is_hub = self._oauth_this_is_hub()
        use_hub = via and not is_hub
        if use_hub and self._oauth_same_host(self._oauth_hub_url(), self._get_web_base_url()):
            _logger.warning(
                '[ML OAUTH] action_open_authorize: hub_url=%s coincide con web.base.url → modo directo',
                self._oauth_hub_url(),
            )
            use_hub = False
        _logger.info(
            '[ML OAUTH] action_open_authorize account_id=%s via_hub=%s is_hub=%s use_hub=%s '
            'hub_url=%s web.base.url=%s',
            self.id, via, is_hub, use_hub,
            self._oauth_hub_url() or '(vacío)',
            self._get_web_base_url() or '(vacío)',
        )
        if not use_hub:
            redirect = self._validate_oauth_configuration()
            if (self.redirect_uri or '').strip().rstrip('/') != redirect:
                self.sudo().write({'redirect_uri': redirect})
        web_base = self._get_web_base_url()
        path = f'/mercadolibre/oauth/start?account_id={self.id}'
        url = f'{web_base}{path}'
        _logger.info('[ML OAUTH] action_open_authorize abriendo URL: %s', url)
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    def action_create_test_user(self):
        """Crea un usuario test de MercadoLibre (sandbox de la app)."""
        self.ensure_one()
        if not self.access_token:
            raise UserError(_(
                'La cuenta debe estar autorizada primero. Use "Autorizar cuenta" '
                'antes de crear usuarios test.'
            ))
        self._ensure_valid_token()
        _logger.info("INICIO: Creación de usuario test de MercadoLibre")
        try:
            url = "https://api.mercadolibre.com/users/test_user"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            payload = {"site_id": self._mercadolibre_site_id()}
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if not response.ok:
                error_text = response.text
                try:
                    error_json = response.json()
                    error_message = error_json.get('message', error_text)
                    error_cause = error_json.get('cause', [])
                    if error_cause:
                        cause_messages = [
                            c.get('message', '') for c in error_cause if isinstance(c, dict)
                        ]
                        if cause_messages:
                            error_message += "\n\nDetalles:\n" + "\n".join(
                                f"• {msg}" for msg in cause_messages
                            )
                except Exception:
                    error_message = error_text
                raise UserError(_("Error al crear usuario test:\n\n%s") % error_message)
            test_user_data = response.json()
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
            _logger.info(
                "Usuario test ML guardado en BD (ID: %s, nickname: %s)",
                test_user_record.id,
                test_user_data.get('nickname'),
            )
            message = _(
                "Usuario test creado exitosamente\n\n"
                "ID: %s\nNickname: %s\nPassword: %s\nEmail: %s"
            ) % (
                test_user_data.get('id', 'N/A'),
                test_user_data.get('nickname', 'N/A'),
                test_user_data.get('password', 'N/A'),
                test_user_data.get('email', 'N/A'),
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Usuario Test Creado'),
                    'message': message,
                    'type': 'success',
                    'sticky': True,
                },
            }
        except UserError:
            raise
        except requests.exceptions.RequestException as e:
            _logger.error("Error de conexión creando usuario test: %s", e, exc_info=True)
            raise UserError(_("Error de conexión con MercadoLibre:\n\n%s") % e)
        except Exception as e:
            _logger.error("Error inesperado creando usuario test: %s", e, exc_info=True)
            raise UserError(_("Error inesperado al crear usuario test:\n\n%s") % e)

    def action_view_test_users(self):
        """Muestra usuarios test guardados para esta cuenta."""
        self.ensure_one()
        return {
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

    def action_test_connection(self):
        """Prueba la conexión con Mercado Libre (GET /users/me)."""
        self.ensure_one()
        if not self.access_token:
            raise UserError(_(
                'La cuenta debe estar autorizada primero. Use «Autorizar cuenta».'
            ))
        try:
            ok = self._sync_meli_user_info()
        except Exception as e:
            _logger.exception("Error al probar conexión ML cuenta %s: %s", self.id, e)
            self.sudo().write({'is_connected': False})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error en la conexión'),
                    'message': _('No se pudo conectar con Mercado Libre: %s') % e,
                    'type': 'danger',
                    'sticky': True,
                },
            }
        if not ok:
            self.sudo().write({'is_connected': False})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error en la conexión'),
                    'message': _('No se pudo validar el token con Mercado Libre. Volvé a autorizar la cuenta.'),
                    'type': 'danger',
                    'sticky': True,
                },
            }
        self.sudo().write({'is_connected': True})
        nickname = self.meli_nickname or _('sin nickname')
        user_id = self.meli_user_id or '—'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Conexión exitosa'),
                'message': _(
                    'La conexión con Mercado Libre se estableció correctamente. '
                    'Usuario: %s (ID %s).'
                ) % (nickname, user_id),
                'type': 'success',
                'sticky': False,
            },
        }

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

    @api.model
    def _ml_seller_sku_from_variation_payload(self, var):
        """SKU de una variación ML (seller_custom_field, SELLER_SKU, etc.)."""
        full_var = var or {}
        sku_var = (full_var.get('seller_custom_field') or full_var.get('seller_sku') or '').strip()
        if not sku_var and full_var.get('attributes'):
            for a in full_var.get('attributes', []):
                if (a.get('id') or '').upper() == 'SELLER_SKU' and (a.get('value_name') or a.get('value_id')):
                    sku_var = str(a.get('value_name') or a.get('value_id', '')).strip()
                    break
        if not sku_var:
            for ac in full_var.get('attribute_combinations') or []:
                if (ac.get('id') or '').upper() == 'SELLER_SKU' and (ac.get('value_name') or ac.get('value_id')):
                    sku_var = str(ac.get('value_name') or ac.get('value_id', '')).strip()
                    break
        if not sku_var and full_var.get('inventory_id'):
            sku_var = str(full_var.get('inventory_id', '')).strip()
        if not sku_var and full_var.get('user_product_id'):
            sku_var = str(full_var.get('user_product_id', '')).strip()
        if not sku_var:
            sku_var = str(full_var.get('id') or '').strip()
        return sku_var

    @api.model
    def _ml_seller_sku_from_item_payload(self, item_data):
        """SKU de un ítem ML sin variaciones (misma lógica que action_import_from_ml)."""
        if not item_data:
            return ''
        seller_custom_field = item_data.get('seller_custom_field')
        if seller_custom_field:
            seller_sku_str = str(seller_custom_field).strip()
            if seller_sku_str and seller_sku_str not in ('False', 'None'):
                return seller_sku_str
        for attr in item_data.get('attributes') or []:
            if (attr.get('id') or '').upper() == 'SELLER_SKU' and attr.get('value_name'):
                sku_value = str(attr.get('value_name')).strip()
                if sku_value and sku_value not in ('False', 'None'):
                    return sku_value
        return ''

    def action_match_all_publications_by_sku(self):
        """
        Matchea todas las publicaciones de la cuenta por SKU (default_code en Odoo).
        Incluye las que ya tenían producto: revalida el SKU actual y quita el vínculo
        si en Odoo ya no existe un producto con ese código.
        """
        self.ensure_one()

        publications = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
        ])

        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _('No hay publicaciones con ID de ítem ML para matchear.'),
                    'type': 'info',
                    'sticky': False,
                },
            }

        matched = 0
        updated = 0
        unlinked = 0
        unchanged = 0
        no_sku = 0
        no_product = 0
        errors = []

        for idx, publication in enumerate(publications, 1):
            try:
                _logger.info(
                    "[%d/%d] Matcheo SKU pub_id=%s item=%s",
                    idx,
                    len(publications),
                    publication.id,
                    publication.ml_item_id,
                )
                result = publication._sync_product_link_by_sku()
                if result == 'matched':
                    matched += 1
                elif result == 'updated':
                    updated += 1
                elif result == 'unlinked':
                    unlinked += 1
                elif result == 'unchanged':
                    unchanged += 1
                elif result == 'no_sku':
                    no_sku += 1
                elif result == 'no_product':
                    no_product += 1
            except Exception as e:
                errors.append(
                    _('Publicación %s: %s')
                    % (publication.ml_item_id or publication.name, str(e))
                )
                _logger.error(
                    "Error matcheando publicación %s: %s",
                    publication.id,
                    e,
                    exc_info=True,
                )

        message = _(
            'Matcheo masivo completado:\n\n'
            '✅ Nuevas vinculaciones: %d\n'
            '🔄 Vínculos actualizados (SKU distinto): %d\n'
            '🔗 Sin cambios (ya correctas): %d\n'
            '⛔ Producto quitado (SKU sin match en Odoo): %d\n'
            '⚠️ Sin SKU en la publicación: %d\n'
            '⚠️ Con SKU pero sin producto en Odoo (sin vínculo previo): %d'
        ) % (matched, updated, unchanged, unlinked, no_sku, no_product)

        if errors:
            message += _('\n\nErrores:\n') + '\n'.join(f'• {e}' for e in errors[:5])
            if len(errors) > 5:
                message += _('\n... y %d más') % (len(errors) - 5)

        notif_type = 'success' if (matched + updated + unlinked) else 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Matcheo masivo completado'),
                'message': message,
                'type': notif_type,
                'sticky': True,
            },
        }
    
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

    def _ml_get_stock_sync_publications(self):
        """Publicaciones que recibirían stock desde Odoo (mismo criterio que el sync real)."""
        self.ensure_one()
        return self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
            ('product_tmpl_id', '!=', False),
        ])

    def action_view_stock_sync_publications(self):
        """
        Vista previa (solo lectura) de las publicaciones que recibirían stock.

        Usa exactamente el mismo criterio que el sync automático y el botón
        «Actualizar stock»: publicada en ML y con producto relacionado.
        No envía nada a Mercado Libre.
        """
        self.ensure_one()
        publications = self._ml_get_stock_sync_publications()
        name = _('Sincronizarían stock: %d publicaciones') % len(publications)
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'ml.publication',
            'view_mode': 'list,form',
            'domain': [('id', 'in', publications.ids)],
            'context': {'create': False},
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
        
        # Publicadas en ML + con producto relacionado.
        # Mismo criterio que la vista previa (action_view_stock_sync_publications).
        publications = self._ml_get_stock_sync_publications()

        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _(
                        'No se encontraron publicaciones con producto relacionado '
                        'y publicadas en MercadoLibre.'
                    ),
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

                if publication._ml_should_skip_stock_sync():
                    _logger.info(
                        "⏭️ [%d/%d] Omitida %s: pausada en ML",
                        idx,
                        total,
                        publication.ml_item_id or publication.name,
                    )
                    continue

                stocks = publication._resolve_ml_sync_stocks()
                publication._force_update_stock_in_ml()

                updated_count += 1
                _logger.info(
                    "✅ [%d/%d] (%.1f%%) Stock replicado en ML: %s (depósito=%d, full_odoo=%s)",
                    idx,
                    total,
                    pct,
                    publication.name,
                    stocks['flex'],
                    stocks['full'] if stocks['full'] is not None else 'N/A',
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
        Actualiza solo stock de todas las publicaciones en Mercado Libre.
        El precio no se sincroniza desde Odoo; use el wizard «Actualizar valores» por publicación.
        """
        return self.action_update_all_publications_stock()

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
            user_response = self._ml_request_with_retry(
                'GET',
                'https://api.mercadolibre.com/users/me',
                timeout=10,
            )
            
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
                
                orders_response = self._ml_request_with_retry(
                    'GET',
                    orders_url,
                    params=params,
                    timeout=30,
                )
                
                # Si la API rechaza los parámetros de fecha, reintentar sin ellos y filtrar en código
                if not orders_response.ok and (order_created_from or order_created_to):
                    _logger.info("📡 API rechazó filtro de fechas, reintentando sin parámetros de fecha")
                    params_no_date = {k: v for k, v in params.items() if k not in ('order_created_from', 'order_created_to')}
                    orders_response = self._ml_request_with_retry(
                        'GET',
                        orders_url,
                        params=params_no_date,
                        timeout=30,
                    )
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

    @api.model
    def _retire_manager_group(self):
        """Quita el grupo Gestor. Quien lo tenía pasa a Admin."""
        self._retire_connector_manager_group(
            'mercadolibre_connector_galarreta.group_ml_manager',
            'mercadolibre_connector_galarreta.group_ml_admin',
            'mercadolibre_connector_galarreta.group_ml_user',
            (
                'mercadolibre_connector_galarreta.access_ml_webhook_user',
            ),
        )

    @api.model
    def _retire_connector_manager_group(self, manager_xmlid, admin_xmlid, user_xmlid, extra_access_xmlids):
        manager = self.env.ref(manager_xmlid, raise_if_not_found=False)
        admin = self.env.ref(admin_xmlid, raise_if_not_found=False)
        user_group = self.env.ref(user_xmlid, raise_if_not_found=False)
        Access = self.env['ir.model.access'].sudo()
        for xmlid in extra_access_xmlids:
            rec = self.env.ref(xmlid, raise_if_not_found=False)
            if rec:
                rec.sudo().unlink()
        if not manager:
            return
        if admin:
            if manager.user_ids:
                manager.user_ids.sudo().write({
                    'group_ids': [(4, admin.id), (3, manager.id)],
                })
            implied = [(3, manager.id)]
            if user_group:
                implied.append((4, user_group.id))
            admin.sudo().write({'implied_ids': implied})
        Access.search([('group_id', '=', manager.id)]).unlink()
        manager.sudo().write({'implied_ids': [(5, 0, 0)]})
        manager.sudo().unlink()
 
