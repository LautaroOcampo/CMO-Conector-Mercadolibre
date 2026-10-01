from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime
import base64
import requests
import logging
import json
import re
import hashlib
import pytz
import psycopg2
from psycopg2 import errorcodes

from .res_partner import parse_meli_billing_fiscal_fields

_logger = logging.getLogger(__name__)


def parse_ml_order_item_tax_rate(item_data):
    """
    Extrae la tasa de IVA (%) desde order_items[].taxes del payload de orden ML.

    ML envía una lista de impuestos por ítem; se priorizan entradas IVA/VAT.
    """
    if not item_data or not isinstance(item_data, dict):
        return 0.0

    taxes = item_data.get('taxes')
    if not taxes:
        return 0.0

    def _rate_from_entry(entry):
        if isinstance(entry, (int, float)):
            return float(entry)
        if not isinstance(entry, dict):
            return None
        for key in ('rate', 'percentage', 'value', 'aliquot', 'tax_rate'):
            raw = entry.get(key)
            if raw is None or raw == '':
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    if isinstance(taxes, dict):
        rate = _rate_from_entry(taxes)
        return rate if rate is not None else 0.0

    if not isinstance(taxes, (list, tuple)):
        return 0.0

    iva_rates = []
    other_rates = []
    for entry in taxes:
        rate = _rate_from_entry(entry)
        if rate is None:
            continue
        if isinstance(entry, dict):
            label = ' '.join(
                str(entry.get(key) or '')
                for key in ('type', 'id', 'name', 'tax_type', 'description')
            ).upper()
            if 'IVA' in label or 'VAT' in label:
                iva_rates.append(rate)
                continue
        other_rates.append(rate)

    if iva_rates:
        return iva_rates[0]
    if other_rates:
        return other_rates[0]
    return 0.0


def parse_ml_order_payment_terms(payments):
    """
    Extrae cuotas y resumen de términos de pago desde la lista `payments` del JSON de orden ML.
    Ignora pagos rechazados/cancelados/reembolsados.
    """
    if not payments:
        return 0, False, ''
    max_inst = 0
    method_labels = []
    for p in payments:
        st = str(p.get('status') or '').lower()
        if st in ('rejected', 'cancelled', 'refunded'):
            continue
        inst = p.get('installments')
        try:
            inst = int(inst) if inst is not None else 0
        except (TypeError, ValueError):
            inst = 0
        if inst > max_inst:
            max_inst = inst
        pm = p.get('payment_method_id')
        if isinstance(pm, dict):
            pm_name = pm.get('name') or pm.get('id') or ''
        else:
            pm_name = pm or ''
        pm_name = str(pm_name).strip() if pm_name else ''
        pt = p.get('payment_type_id')
        if isinstance(pt, dict):
            pt_name = pt.get('name') or pt.get('id') or ''
        else:
            pt_name = pt or ''
        pt_name = str(pt_name).strip() if pt_name else ''
        parts = [x for x in (pt_name, pm_name) if x]
        if parts:
            method_labels.append(' / '.join(parts))
    summary_bits = []
    if max_inst > 1:
        summary_bits.append(_('%s cuotas') % max_inst)
    elif max_inst == 1:
        summary_bits.append(_('Contado (1 pago)'))
    seen = set()
    uniq = []
    for lbl in method_labels:
        if lbl and lbl not in seen:
            seen.add(lbl)
            uniq.append(lbl)
    if uniq:
        summary_bits.append(', '.join(uniq))
    summary = ' | '.join(summary_bits) if summary_bits else ''
    return max_inst, bool(max_inst > 1), summary


def _meli_format_phone(phone_data):
    """Normaliza teléfono ML (dict area_code+number o string)."""
    if not phone_data:
        return ''
    if isinstance(phone_data, dict):
        area = str(phone_data.get('area_code') or '').strip()
        number = str(phone_data.get('number') or '').strip()
        extension = str(phone_data.get('extension') or '').strip()
        parts = []
        if area and number:
            parts.append(f'{area} {number}'.strip())
        elif number:
            parts.append(number)
        elif area:
            parts.append(area)
        if extension:
            parts.append(f'ext. {extension}')
        return ' '.join(parts).strip()
    return str(phone_data).strip()


def _meli_named_field(value):
    """Extrae nombre legible de campos ML que vienen como dict o string."""
    if isinstance(value, dict):
        return (
            value.get('name')
            or value.get('city_name')
            or value.get('state_name')
            or value.get('country_name')
            or value.get('id')
            or ''
        )
    return value or ''


def _meli_receiver_address_has_data(receiver_address):
    if not receiver_address or not isinstance(receiver_address, dict):
        return False
    text_keys = ('address_line', 'street_name', 'address', 'zip_code', 'zip', 'postal_code')
    for key in text_keys:
        val = receiver_address.get(key)
        if isinstance(val, str) and val.strip():
            return True
    for key in ('city', 'state', 'country'):
        val = receiver_address.get(key)
        if _meli_named_field(val):
            return True
    return False


def _meli_merge_receiver_address(order_ra, shipment_ra):
    """Combina receiver_address de orden y shipment (shipment completa huecos)."""
    order_ra = order_ra if isinstance(order_ra, dict) else {}
    shipment_ra = shipment_ra if isinstance(shipment_ra, dict) else {}
    if not shipment_ra:
        return dict(order_ra)
    if not order_ra:
        return dict(shipment_ra)
    merged = dict(order_ra)
    for key, val in shipment_ra.items():
        if val in (None, '', {}):
            continue
        if key in ('city', 'state', 'country') and isinstance(val, dict):
            existing = merged.get(key)
            if isinstance(existing, dict):
                merged[key] = {**existing, **{k: v for k, v in val.items() if v}}
            else:
                merged[key] = val
        elif not merged.get(key):
            merged[key] = val
    return merged


def _meli_order_shipping_needs_shipment_api(shipping):
    """True si la orden trae shipping.id pero sin dirección embebida."""
    if not isinstance(shipping, dict):
        return False
    if not shipping.get('id'):
        return False
    return not _meli_receiver_address_has_data(shipping.get('receiver_address'))


def parse_meli_shipping_fields(shipping, shipment_data=None):
    """
    Extrae campos de envío desde order.shipping y, opcionalmente, GET /shipments/{id}.
    """
    shipping = dict(shipping or {})
    shipment_data = shipment_data or {}

    if shipment_data:
        if shipment_data.get('receiver_address'):
            shipping['receiver_address'] = _meli_merge_receiver_address(
                shipping.get('receiver_address'),
                shipment_data.get('receiver_address'),
            )
        for key in ('receiver_name', 'receiver_phone'):
            if shipment_data.get(key) and not shipping.get(key):
                shipping[key] = shipment_data[key]
        receiver = shipment_data.get('receiver')
        if isinstance(receiver, dict):
            if receiver.get('name') and not shipping.get('receiver_name'):
                shipping['receiver_name'] = receiver.get('name')
            if receiver.get('phone') and not shipping.get('receiver_phone'):
                shipping['receiver_phone'] = receiver.get('phone')

    receiver_address = shipping.get('receiver_address', {}) or {}
    if not isinstance(receiver_address, dict):
        receiver_address = {}

    shipping_street = (
        receiver_address.get('address_line', '')
        or receiver_address.get('street_name', '')
        or receiver_address.get('address', '')
    )
    shipping_street_number = (
        receiver_address.get('street_number', '')
        or receiver_address.get('number', '')
    )
    shipping_floor = receiver_address.get('floor', '') or ''
    shipping_apartment = receiver_address.get('apartment', '') or receiver_address.get('unit', '') or ''
    shipping_city = _meli_named_field(receiver_address.get('city', ''))
    shipping_state = _meli_named_field(receiver_address.get('state', ''))
    shipping_country = _meli_named_field(receiver_address.get('country', ''))
    shipping_zip = (
        receiver_address.get('zip_code', '')
        or receiver_address.get('zip', '')
        or receiver_address.get('postal_code', '')
        or ''
    )

    receiver_name = shipping.get('receiver_name', '') or ''
    if not receiver_name and isinstance(shipping.get('receiver'), dict):
        receiver_name = shipping['receiver'].get('name', '') or ''

    receiver_phone_raw = shipping.get('receiver_phone', '')
    if not receiver_phone_raw and isinstance(shipping.get('receiver'), dict):
        receiver_phone_raw = shipping['receiver'].get('phone', '')
    receiver_phone = _meli_format_phone(receiver_phone_raw)

    return {
        'shipping_street': shipping_street,
        'shipping_street_number': shipping_street_number,
        'shipping_floor': shipping_floor,
        'shipping_apartment': shipping_apartment,
        'shipping_city': shipping_city,
        'shipping_state': shipping_state,
        'shipping_country': shipping_country,
        'shipping_zip': shipping_zip,
        'shipping_receiver_name': receiver_name,
        'shipping_receiver_phone': receiver_phone,
    }


def extract_ml_commission_from_order_data(order_data):
    """
    Suma la comisión de venta de Mercado Libre desde order_items[].sale_fee.
    Ver: https://developers.mercadolibre.com (manage sales / orders).
    """
    if not order_data or not isinstance(order_data, dict):
        return 0.0
    total = 0.0
    for item in order_data.get('order_items') or []:
        if not isinstance(item, dict):
            continue
        fee = item.get('sale_fee')
        if fee is None:
            continue
        try:
            total += float(fee)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


class MLSale(models.Model):
    _name = "ml.sale"
    _description = "Venta de Mercado Libre"
    _rec_name = "ml_order_id"
    _order = 'date_created desc'

    active = fields.Boolean(default=True, index=True)

    ml_order_id = fields.Char(
        string="ID de venta (ML)", 
        required=True, 
        copy=False,
        index=True,
        help='ID único de la orden en MercadoLibre'
    )

    ml_pack_id = fields.Char(
        string='Pack ID ML',
        copy=False,
        index=True,
        help=(
            'ID del pack/carrito en Mercado Libre. Varias órdenes del mismo comprador '
            'en la misma compra comparten este valor y se agrupan en un solo pedido Odoo.'
        ),
    )

    cancellation_pending = fields.Boolean(
        string='Cancelación pendiente de reconciliar',
        default=False,
        copy=False,
        index=True,
        help=(
            'Se activa al cancelar en ML. Permite generar NC de facturas publicadas '
            'después de la cancelación (carreras webhook / factura AFIP tardía).'
        ),
    )
    
    name = fields.Char(
        string='Número de Venta',
        compute='_compute_name',
        store=True,
        help='Número único de la venta generado por Odoo'
    )

    ml_account_id = fields.Many2one(
        "ml.account", 
        string="Cuenta de Mercado Libre", 
        required=True,
        index=True
    )

    # =====================================================
    # DATOS DE LA VENTA
    # =====================================================
    total_amount = fields.Float(
        string="Monto total",
        digits=(16, 2),
        help='Total de la orden incluyendo envío e impuestos'
    )
    
    subtotal = fields.Float(
        string='Subtotal',
        digits=(16, 2),
        help='Subtotal de los productos (sin envío ni impuestos)'
    )
    
    shipping_cost = fields.Float(
        string='Costo de Envío',
        digits=(16, 2),
        help='Costo del envío'
    )
    
    total_tax_amount = fields.Float(
        string='Total Impuestos',
        digits=(16, 2),
        help='Total de impuestos'
    )
    
    # Información de IVA
    price_includes_tax = fields.Boolean(
        string='Precio Incluye IVA',
        default=False,
        help='Indica si el precio de MercadoLibre incluye IVA'
    )
    
    tax_rate = fields.Float(
        string='Tasa de IVA (%)',
        digits=(16, 4),
        help='Tasa de IVA aplicada (porcentaje)'
    )
    
    tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto IVA',
        domain=[('type_tax_use', '=', 'sale')],
        help='Impuesto de IVA aplicado a esta venta'
    )
    
    currency_id = fields.Many2one(
        'res.currency',
        string='Moneda',
        default=lambda self: self.env.company.currency_id
    )
    
    date_created = fields.Datetime(
        string="Fecha de creación",
        required=True,
        index=True
    )
    
    date_closed = fields.Datetime(
        string="Fecha de cierre",
        help='Fecha en que se cerró la orden'
    )
    
    status = fields.Selection([
        ('confirmed', 'Confirmada'),
        ('paid', 'Pagada'),
        ('pending', 'Pendiente'),
        ('cancelled', 'Cancelada'),
        ('shipped', 'Enviada'),
        ('delivered', 'Entregada'),
    ], string="Estado", default='pending', index=True)
    
    payment_status = fields.Selection([
        ('pending', 'Pendiente'),
        ('paid', 'Pagada'),
        ('refunded', 'Reembolsada'),
        ('cancelled', 'Cancelada'),
    ], string='Estado de Pago', default='pending')
    
    payment_release_status = fields.Selection([
        ('pending_review', 'En Revisión'),
        ('pending_manual_review', 'Revisión Manual Pendiente'),
        ('pending_transfer', 'Transferencia Pendiente'),
        ('available', 'Disponible'),
        ('money_in_account', 'Dinero en Cuenta'),
        ('released', 'Liberado'),
        ('blocked', 'Bloqueado'),
        ('pending_waiting_payment', 'Esperando Pago'),
        ('pending_waiting_for_payment_method', 'Esperando Método de Pago'),
    ], string='Estado de Liberación del Dinero', default='pending_review', 
        help='Estado de liberación del dinero en MercadoLibre. Indica cuándo el dinero estará disponible en tu cuenta.')

    ml_payment_max_installments = fields.Integer(
        string='Cuotas máx. (ML)',
        default=0,
        help='Máximo de cuotas detectado en los pagos de la orden en Mercado Libre.',
    )
    ml_payment_has_installments = fields.Boolean(
        string='Pago en cuotas',
        help='True si hay más de una cuota en algún pago aprobado/relevante.',
    )
    ml_payment_terms_summary = fields.Char(
        string='Términos de pago (ML)',
        help='Resumen legible: cuotas y tipo/método de pago según la API de Mercado Libre.',
    )
    
    fulfillment_status = fields.Selection([
        ('to_pack', 'Por Empaquetar'),
        ('to_ship', 'Por Enviar'),
        ('shipped', 'Enviado'),
        ('delivered', 'Entregado'),
    ], string='Estado de Envío', default='to_pack')

    is_full = fields.Boolean(
        string='Mercado Libre Full',
        default=False,
        index=True,
        help=(
            'True solo si esta venta se despachó por logística Full (fulfillment), '
            'según logistic_type del envío. Ventas Flex (depósito propio) quedan en False.'
        ),
    )

    # =====================================================
    # DATOS DEL CLIENTE
    # =====================================================
    buyer_id = fields.Char(
        string='ID Comprador ML',
        help='ID del comprador en MercadoLibre'
    )
    
    buyer_nickname = fields.Char(
        string="Nickname Comprador",
        help='Nickname del comprador en MercadoLibre'
    )
    
    customer_name = fields.Char(
        string='Nombre Completo',
        help='Nombre completo del cliente'
    )
    
    customer_email = fields.Char(
        string='Email',
        help='Email del cliente'
    )
    
    customer_phone = fields.Char(
        string='Teléfono',
        help='Teléfono del cliente'
    )
    
    customer_dni = fields.Char(
        string='DNI/CUIT',
        help='Documento de identidad del cliente'
    )

    ml_is_b2b = fields.Boolean(
        string='Venta B2B',
        default=False,
        help='Indica si la orden ML tiene tag b2b.',
    )
    ml_billing_info_id = fields.Char(
        string='Billing Info ID (ML)',
        help='ID de billing_info en Mercado Libre para consultar datos fiscales.',
    )
    ml_taxpayer_type = fields.Char(
        string='Situación fiscal ML',
        help='taxpayer_type.description devuelto por billing-info de Mercado Libre.',
    )
    ml_billing_info_data = fields.Text(
        string='Billing Info ML (JSON)',
        help='Respuesta JSON de billing-info de Mercado Libre.',
    )
    ml_expected_invoice_class = fields.Char(
        string='Factura esperada (ML)',
        compute='_compute_ml_expected_invoice_class',
        help='Letra de factura que Odoo debería usar según la responsabilidad AFIP del comprador.',
    )
    
    # =====================================================
    # DATOS DE LA DIRECCIÓN
    # =====================================================
    shipping_street = fields.Char(
        string='Calle',
        help='Calle de la dirección de envío'
    )
    
    shipping_street_number = fields.Char(
        string='Número de Calle',
        help='Número de la calle'
    )
    
    shipping_floor = fields.Char(
        string='Piso',
        help='Piso (si aplica)'
    )
    
    shipping_apartment = fields.Char(
        string='Departamento',
        help='Departamento (si aplica)'
    )
    
    shipping_city = fields.Char(
        string='Ciudad',
        help='Ciudad de envío'
    )
    
    shipping_state = fields.Char(
        string='Provincia',
        help='Provincia/Estado de envío'
    )
    
    shipping_country = fields.Char(
        string='País',
        help='País de envío'
    )
    
    shipping_zip = fields.Char(
        string='Código Postal',
        help='Código postal'
    )
    
    shipping_receiver_name = fields.Char(
        string='Nombre del Receptor',
        help='Nombre de la persona que recibe el envío'
    )
    
    shipping_receiver_phone = fields.Char(
        string='Teléfono del Receptor',
        help='Teléfono de la persona que recibe el envío'
    )
    
    shipping_comment = fields.Text(
        string='Comentarios de Envío',
        help='Comentarios adicionales sobre la dirección de envío'
    )

    # =====================================================
    # RELACIONES Y DATOS ADICIONALES
    # =====================================================
    line_ids = fields.One2many(
        'ml.sale.line',
        'order_id',
        string='Líneas de Venta',
        help='Items de la orden'
    )
    
    test_sale = fields.Boolean(
        string="Es venta de prueba / histórica", 
        default=False,
        help="Activa esto si no querés que esta venta afecte stock ni contabilidad."
    )
    
    odoo_sale_order_id = fields.Many2one(
        'sale.order',
        string='Orden de Venta Odoo',
        ondelete='set null',
        help='Orden de venta creada en Odoo desde esta venta de MercadoLibre'
    )
    
    odoo_sale_order_name = fields.Char(
        string='Número Orden Odoo',
        related='odoo_sale_order_id.name',
        readonly=True,
        store=True,
        help='Número de la orden de venta en Odoo'
    )
    
    publication_count = fields.Integer(
        string='Publicaciones',
        compute='_compute_publication_count',
        help='Número de publicaciones relacionadas a esta venta'
    )
    
    odoo_partner_id = fields.Many2one(
        'res.partner',
        string='Cliente en Odoo',
        ondelete='set null',
        help='Cliente creado/buscado en Odoo'
    )
    
    notes = fields.Text(
        string='Notas',
        help='Notas adicionales sobre la orden'
    )

    ml_fiscal_document_id = fields.Char(
        string='ID documento fiscal ML',
        copy=False,
        help='ID del PDF de factura subido a Mercado Libre (fiscal_documents).',
    )
    
    full_order_data = fields.Text(
        string='Payload de la venta (JSON)',
        help='Payload completo de la orden devuelto por la API de Mercado Libre (GET /orders/{id}).',
    )

    ml_commission_amount = fields.Float(
        string='Comisión ML',
        digits=(16, 2),
        help='Suma de sale_fee de los ítems de la orden según el payload de Mercado Libre.',
    )

    ml_commission_move_id = fields.Many2one(
        'account.move',
        string='Asiento comisión ML',
        copy=False,
        help='Asiento contable de gasto por comisión ML (separado de la factura al cliente).',
    )
    
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True
    )
    
    @api.depends('ml_order_id', 'date_created')
    def _compute_name(self):
        """Genera un nombre único para la venta"""
        for record in self:
            if record.ml_order_id:
                record.name = f"ML-{record.ml_order_id}"
            else:
                record.name = _('Nueva Venta ML')
    
    _ml_order_id_company_uniq = models.Constraint(
        'UNIQUE(ml_order_id, company_id)',
        'Ya existe una venta con este ID de MercadoLibre para esta compañía!',
    )

    @api.depends('ml_taxpayer_type', 'customer_dni')
    def _compute_ml_expected_invoice_class(self):
        for rec in self:
            letter = ''
            if rec.ml_taxpayer_type:
                tt = rec.ml_taxpayer_type.lower()
                if 'responsable inscripto' in tt and 'no inscripto' not in tt:
                    letter = 'A'
                else:
                    letter = 'B'
            rec.ml_expected_invoice_class = letter

    def _ml_sync_partner_fiscal_from_billing(self, partner):
        """Aplica billing-info ML (o CUIT/DNI) al contacto (vat/nombre/dirección).

        Solo en ventas B2B. No B2B facturan a Consumidor Final sin tocar datos fiscales.
        """
        self.ensure_one()
        if not partner:
            return partner
        if not self.ml_is_b2b:
            _logger.info(
                'ℹ️ ML Order %s: no B2B — se omite sync fiscal del contacto',
                self.ml_order_id,
            )
            return partner
        default_cf = self.ml_account_id.default_order_partner_id
        if default_cf and partner.id == default_cf.id:
            _logger.info(
                'ℹ️ ML Order %s: no se aplica fiscal sobre el CF por defecto',
                self.ml_order_id,
            )
            return partner
        if self.ml_billing_info_data:
            try:
                payload = json.loads(self.ml_billing_info_data)
                if payload:
                    return partner._ml_apply_meli_billing_fiscal(payload)
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    '⚠️ ml_billing_info_data inválido en venta ML %s',
                    self.ml_order_id,
                )
        if self.customer_dni:
            return partner._ml_apply_fiscal_from_identification(
                ident_type='CUIT' if len(re.sub(r'[^0-9]', '', self.customer_dni)) == 11 else 'DNI',
                ident_number=self.customer_dni,
                is_b2b=self.ml_is_b2b,
            )
        return partner

    def write(self, vals):
        becoming_cancelled = self.env['ml.sale']
        if vals.get('status') == 'cancelled':
            becoming_cancelled = self.filtered(lambda r: r.status != 'cancelled')
            if becoming_cancelled and 'cancellation_pending' not in vals:
                vals = dict(vals, cancellation_pending=True)
        res = super().write(vals)
        if becoming_cancelled:
            becoming_cancelled._sync_odoo_on_ml_cancelled()
        return res

    @api.model
    def _ml_unpaid_cleanup_domain(self):
        """Ventas ML que no están pagadas (importaciones prematuras).

        Excluye ``paid`` (válidas) y ``cancelled`` (historial de cancelaciones).
        """
        return [
            ('active', '=', True),
            ('status', 'not in', ('paid', 'cancelled')),
        ]

    def action_open_cleanup_unpaid_wizard(self):
        """Abre wizard de limpieza: no pagadas → archivar ml.sale y cancelar SO."""
        unpaid = self.env['ml.sale'].search(self._ml_unpaid_cleanup_domain())
        wizard = self.env['ml.cleanup.unpaid.sales.wizard'].create({
            'sale_ids': [(6, 0, unpaid.ids)],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Limpiar ventas ML no pagadas'),
            'res_model': 'ml.cleanup.unpaid.sales.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _ml_cleanup_unpaid_record(self):
        """Cancela SO vinculada (si se puede) y archiva esta ml.sale.

        Returns:
            tuple: (ok: bool, message: str)
        """
        self.ensure_one()
        if self.status in ('paid', 'cancelled') or not self.active:
            return False, _('Omitida (status=%s / activa=%s)') % (self.status, self.active)

        sale = self.odoo_sale_order_id
        if sale and sale.state != 'cancel':
            pickings = sale.picking_ids
            if any(p.state == 'done' for p in pickings):
                return False, _(
                    'SO %s tiene picking Hecho: no se archiva (gestionar manualmente)'
                ) % sale.name
            # Reutiliza la misma lógica que cancelación ML
            self._sync_odoo_on_ml_cancelled()
            sale.invalidate_recordset()
            if sale.exists() and sale.state != 'cancel':
                return False, _(
                    'No se pudo cancelar SO %s (estado %s): no se archiva'
                ) % (sale.name, sale.state)

        so_name = sale.name if sale else '-'
        self.write({'active': False})
        return True, _('Archivada ml.sale %s (SO %s)') % (self.ml_order_id, so_name)

    def _ml_advisory_lock_order(self, order_id, company_id=None):
        """Lock transaccional por orden ML (evita duplicados/deadlocks en create)."""
        order_id = str(order_id or '').strip()
        if not order_id:
            return
        company_id = company_id or self.env.company.id
        digest = int(
            hashlib.md5(('ml.order:%s:%s' % (company_id, order_id)).encode('utf-8')).hexdigest()[:15],
            16,
        )
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', (digest,))

    def _ml_sale_orders_for_cancel(self):
        """Pedidos Odoo a reconciliar (esta venta y hermanas del mismo pack)."""
        self.ensure_one()
        orders = self.env['sale.order']
        if self.odoo_sale_order_id:
            orders |= self.odoo_sale_order_id
        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        if pack:
            siblings = self.sudo().search([
                ('ml_pack_id', '=', pack),
                ('company_id', '=', self.company_id.id if self.company_id else self.env.company.id),
                ('active', '=', True),
                ('odoo_sale_order_id', '!=', False),
            ])
            orders |= siblings.mapped('odoo_sale_order_id')
        return orders

    def _ml_invoice_has_open_credit_note(self, invoice):
        """True si ya existe NC (borrador o publicada) que revierte esta factura."""
        if not invoice:
            return False
        if 'reversal_move_ids' in invoice._fields and invoice.reversal_move_ids.filtered(
            lambda m: m.state != 'cancel'
        ):
            return True
        refund = self.env['account.move'].sudo().search([
            ('move_type', '=', 'out_refund'),
            ('reversed_entry_id', '=', invoice.id),
            ('state', '!=', 'cancel'),
        ], limit=1)
        return bool(refund)

    def _ml_cancel_handle_customer_invoices(self, sale_orders=None):
        """
        Facturas de cliente del/los pedidos:
        - borrador → cancelar
        - publicada sin NC → nota de crédito en borrador (sin publicar)
        """
        self.ensure_one()
        lines = []
        sale_orders = sale_orders or self._ml_sale_orders_for_cancel()
        invoices = sale_orders.mapped('invoice_ids').filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )
        # Deduplicar
        invoices = invoices.sorted('id')
        for inv in invoices:
            if inv.state == 'draft':
                try:
                    cancel_draft = getattr(inv, 'button_cancel', None) or getattr(inv, 'action_cancel', None)
                    if not cancel_draft:
                        raise ValueError('no draft cancel method on account.move')
                    cancel_draft()
                    lines.append(_('Factura borrador cancelada: %s.') % (inv.name or str(inv.id)))
                except Exception as err:
                    lines.append(
                        _('Factura borrador %s: no se pudo cancelar: %s')
                        % (inv.name or str(inv.id), err)
                    )
                    _logger.exception(
                        'Error button_cancel factura borrador %s (ml.sale %s)',
                        inv.id, self.ml_order_id
                    )
            elif inv.state == 'posted':
                if self._ml_invoice_has_open_credit_note(inv):
                    lines.append(
                        _('Factura %s ya tiene nota de crédito; no se duplica.')
                        % (inv.name or str(inv.id))
                    )
                    continue
                try:
                    reversals = inv._reverse_moves(cancel=False)
                    for rev in reversals:
                        if rev.state == 'posted':
                            lines.append(
                                _('Nota de crédito %s quedó publicada (revisar; se esperaba borrador).')
                                % (rev.name or str(rev.id))
                            )
                        else:
                            lines.append(
                                _('Nota de crédito en borrador creada: %s (revierte %s).')
                                % (rev.name or str(rev.id), inv.name or str(inv.id))
                            )
                except Exception as err:
                    lines.append(
                        _('Factura publicada %s: error al generar nota de crédito: %s')
                        % (inv.name or str(inv.id), err)
                    )
                    _logger.exception(
                        'Error _reverse_moves para factura %s (ml.sale %s)',
                        inv.id, self.ml_order_id
                    )
        return lines

    def _ml_reconcile_cancelled_invoices(self, post_activity=True):
        """
        Reconciliación fiscal post-cancelación: NC para toda factura posted sin reversión.

        Seguro de llamar varias veces (idempotente) y desde action_post de facturas tardías.
        """
        for rec in self:
            if rec.status != 'cancelled' and not rec.cancellation_pending:
                continue
            pack = rec._ml_normalize_pack_id(rec.ml_pack_id)
            if pack:
                rec._ml_advisory_lock_pack(pack)
            elif rec.ml_order_id:
                rec._ml_advisory_lock_order(rec.ml_order_id)

            sale_orders = rec._ml_sale_orders_for_cancel()
            if not sale_orders:
                if rec.cancellation_pending:
                    rec.cancellation_pending = False
                continue

            lines = rec._ml_cancel_handle_customer_invoices(sale_orders)
            # ¿Quedan facturas posted sin NC?
            open_posted = sale_orders.mapped('invoice_ids').filtered(
                lambda m: m.move_type == 'out_invoice'
                and m.state == 'posted'
                and not rec._ml_invoice_has_open_credit_note(m)
            )
            if not open_posted and rec.cancellation_pending:
                rec.cancellation_pending = False
            if post_activity and lines:
                lead_so = sale_orders[:1]
                rec._ml_cancel_post_activity(lead_so, lines)
        return True

    @api.model
    def cron_reconcile_cancelled_ml_invoices(self):
        """Cron: ventas canceladas con facturas posted sin NC → generar NC faltantes."""
        sales = self.sudo().search([
            ('active', '=', True),
            '|',
            ('status', '=', 'cancelled'),
            ('cancellation_pending', '=', True),
            ('odoo_sale_order_id', '!=', False),
        ], limit=80)
        if not sales:
            return True
        _logger.info(
            '🧾 Cron NC cancelación ML: %d venta(s) a reconciliar',
            len(sales),
        )
        sales._ml_reconcile_cancelled_invoices(post_activity=True)
        return True

    def _sync_odoo_on_ml_cancelled(self):
        """
        Cuando ML cancela la orden (status=cancelled en ml.sale):
        - Sin orden Odoo: no hace nada más (el write ya guardó el estado).
        - Con orden Odoo: pickings, facturas/NC, cancelación de venta y actividad.
        - Siempre intenta NC de facturas posted (también si la SO ya estaba cancelada).
        """
        for rec in self:
            pack = rec._ml_normalize_pack_id(rec.ml_pack_id)
            if pack:
                rec._ml_advisory_lock_pack(pack)

            sale_orders = rec._ml_sale_orders_for_cancel()
            sale = rec.odoo_sale_order_id
            if not sale and not sale_orders:
                continue

            lines = []
            # 1) Facturas / NC primero (también si SO ya cancelada o picking done)
            lines.extend(rec._ml_cancel_handle_customer_invoices(sale_orders or sale))

            if not sale:
                if lines:
                    rec._ml_cancel_post_activity(sale_orders[:1] if sale_orders else sale, lines)
                continue

            if sale.state == 'cancel':
                lines.append(_('Orden de venta Odoo %s ya estaba cancelada.') % sale.name)
                open_posted = sale.invoice_ids.filtered(
                    lambda m: m.move_type == 'out_invoice'
                    and m.state == 'posted'
                    and not rec._ml_invoice_has_open_credit_note(m)
                )
                if not open_posted and rec.cancellation_pending:
                    rec.cancellation_pending = False
                rec._ml_cancel_post_activity(sale, lines)
                continue

            pickings = sale.picking_ids
            if any(p.state == 'done' for p in pickings):
                lines.append(
                    _('Pickings: hay al menos una transferencia en estado Hecho (done). '
                      'No se cancela automáticamente la venta; gestionar devolución de stock manualmente. '
                      'Las facturas sí se reconciliaron (NC si correspondía).')
                )
                rec._ml_cancel_post_activity(sale, lines)
                continue

            # 2) Pickings cancelables
            for picking in pickings:
                if picking.state in ('cancel', 'done'):
                    continue
                if picking.state in ('draft', 'waiting', 'confirmed', 'assigned'):
                    try:
                        picking.action_cancel()
                        lines.append(_('Picking %s cancelado.') % (picking.name or str(picking.id)))
                    except Exception as err:
                        lines.append(
                            _('Picking %s: error al cancelar: %s')
                            % (picking.name or str(picking.id), err)
                        )
                        _logger.exception(
                            'Error cancelando picking %s para ml.sale %s',
                            picking.id, rec.ml_order_id
                        )
                else:
                    lines.append(
                        _('Picking %s: estado %s no tratado automáticamente.')
                        % (picking.name or str(picking.id), picking.state)
                    )

            # 3) Orden de venta
            if sale.state != 'cancel':
                try:
                    sale.action_cancel()
                    lines.append(_('Orden de venta %s cancelada.') % sale.name)
                except Exception as err:
                    lines.append(_('Error al cancelar la orden de venta: %s') % err)
                    _logger.exception(
                        'Error en action_cancel para sale.order %s (ml.sale %s)',
                        sale.id, rec.ml_order_id
                    )
            else:
                lines.append(_('Orden de venta %s quedó cancelada.') % sale.name)

            open_posted = sale_orders.mapped('invoice_ids').filtered(
                lambda m: m.move_type == 'out_invoice'
                and m.state == 'posted'
                and not rec._ml_invoice_has_open_credit_note(m)
            )
            if not open_posted and rec.cancellation_pending:
                rec.cancellation_pending = False

            rec._ml_cancel_post_activity(sale, lines)

    def _ml_cancel_post_activity(self, sale_order, summary_lines):
        """Actividad tipo tarea en la orden de venta con resumen para el responsable."""
        self.ensure_one()
        if not sale_order:
            return
        if not summary_lines:
            summary_lines = [
                _('Venta MercadoLibre %s marcada como cancelada.') % self.ml_order_id
            ]
        note = '\n'.join(summary_lines)
        try:
            todo_type = self.env.ref('mail.mail_activity_data_todo')
            assign_user = sale_order.user_id or sale_order.create_uid or self.env.user
            sale_order.activity_schedule(
                activity_type_id=todo_type.id,
                user_id=assign_user.id,
                summary=_('MercadoLibre: orden ML cancelada (%s)') % self.ml_order_id,
                note=note,
            )
        except Exception as err:
            _logger.warning(
                'No se pudo crear actividad en sale.order %s (ml.sale %s): %s',
                sale_order.id, self.ml_order_id, err
            )

    # 🔹 Método para importar ventas desde Mercado Libre
    @api.model
    def action_import_sales_from_ml(self):
        """Importa las ventas de Mercado Libre y las vincula con productos existentes."""
        account = self.env["ml.account"].search([], limit=1)
        if not account or not account.access_token:
            raise UserError("No se encontró una cuenta de Mercado Libre con token válido.")

        headers = {"Authorization": f"Bearer {account.access_token}"}

        try:
            # Obtener órdenes recientes (últimas 200)
            url = "https://api.mercadolibre.com/orders/search?seller=me&sort=date_desc&limit=200"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()

            orders = resp.json().get("results", [])
            if not orders:
                raise UserError("No se encontraron ventas en Mercado Libre.")

            for order in orders:
                ml_order_id = str(order.get("id"))
                buyer_nickname = order.get("buyer", {}).get("nickname")
                total_amount = order.get("total_amount")
                date_created = order.get("date_created")
                status = order.get("status")

                for item in order.get("order_items", []):
                    item_info = item.get("item", {}) or {}
                    title = item_info.get("title")
                    quantity = item.get("quantity", 1)
                    sku = item_info.get("seller_sku")
                    ml_item_id = str(item_info.get("id") or "")
                    variation_id = item_info.get("variation_id") or item.get("variation_id")

                    product = None
                    pub = self.env["ml.publication"]._ml_find_for_order_item(
                        ml_item_id, account.id, variation_id,
                    )
                    if pub:
                        product_tmpl, _variant = pub._ml_get_odoo_products_for_sale()
                        product = product_tmpl

                    vals = {
                        "ml_order_id": ml_order_id,
                        "buyer_nickname": buyer_nickname,
                        "total_amount": total_amount,
                        "date_created": date_created,
                        "status": status,
                        "product_name": title,
                        "quantity": quantity,
                        "sku": sku,
                        "product_tmpl_id": product.id if product else False,
                        "ml_account_id": account.id,
                        "test_sale": False,  # por defecto son reales
                    }

                    existing = self.env["ml.sale"].search([
                        ("ml_order_id", "=", ml_order_id),
                        ("sku", "=", sku)
                    ], limit=1)

                    if existing:
                        existing.write(vals)
                    else:
                        self.env["ml.sale"].create(vals)

            return {
                "effect": {
                    "fadeout": "slow",
                    "message": "✅ Ventas importadas correctamente desde Mercado Libre.",
                    "type": "rainbow_man",
                }
            }

        except requests.exceptions.RequestException as e:
            raise UserError(f"Error al conectarse con Mercado Libre: {e}")

    @api.model
    @api.model
    def _ml_order_status_is_completed_sale(self, order_data):
        """True si la orden ML es una venta hecha (pagada).

        No se importan órdenes pending / payment_required / payment_in_process, etc.
        Contexto ``ml_import_unpaid_orders`` permite forzar (p. ej. soporte).
        """
        if self.env.context.get('ml_import_unpaid_orders'):
            return True
        status = str((order_data or {}).get('status') or '').lower().strip()
        return status == 'paid'

    def update_or_create_from_meli(self, order_id, account_id=None, create_odoo_order=None, update_stock=True, create_customer=None):
        """
        Llama a la API de ML para obtener los datos COMPLETOS del pedido y actualiza o crea la venta.
        Incluye: todos los datos del cliente, dirección, y todos los items de la orden.
        
        Args:
            order_id: ID de la orden en MercadoLibre
            account_id: ID de la cuenta de ML (opcional, se busca automáticamente si no se proporciona)
            create_odoo_order: Si True, crea la orden de venta en Odoo según cuenta/config.
                None = usar ml.account.auto_create_sale_order.
            update_stock: Si True, se descontará el stock al confirmar las órdenes (default: True)
            create_customer: Si True, crea/actualiza el contacto. None = mismo valor que create_odoo_order.
        
        Returns:
            ml.sale: Registro creado o actualizado
        """
        _logger.info("=" * 80)
        _logger.info("📦 INICIO: update_or_create_from_meli")
        _logger.info("   Order ID: %s", order_id)
        _logger.info("   Account ID: %s", account_id)
        _logger.info("   Create Odoo Order: %s", create_odoo_order)
        _logger.info("   Update Stock: %s", update_stock)
        _logger.info("   Create/Update Customer: %s", create_customer)

        # Serializa create/update de la misma orden (webhook + polling concurrentes).
        self._ml_advisory_lock_order(order_id)
        
        # Buscar la cuenta correcta
        if account_id:
            account = self.env['ml.account'].sudo().browse(account_id)
            _logger.info("🔑 Usando cuenta específica: ID=%d, Nombre='%s', ML User ID='%s'", 
                        account.id, account.name, account.meli_user_id or 'N/A')
            
            if not account.exists():
                _logger.error("❌ La cuenta ID=%d no existe", account_id)
                return None
            
            if not account.access_token:
                _logger.error("❌ La cuenta ID=%d no tiene access_token", account_id)
                return None
        else:
            _logger.info("🔍 Buscando cuenta automáticamente...")
            # Buscar todas las cuentas con token válido
            accounts = self.env['ml.account'].sudo().search([
                ('access_token', '!=', False),
                ('is_connected', '=', True)
            ])
            
            _logger.info("   Cuentas encontradas: %d", len(accounts))
            for acc in accounts:
                _logger.info("   - ID=%d, Nombre='%s', ML User ID='%s', Token=%s", 
                           acc.id, acc.name, acc.meli_user_id or 'N/A', 
                           'SÍ' if acc.access_token else 'NO')
            
            if not accounts:
                _logger.warning("⚠️ No se encontró ninguna cuenta de Mercado Libre con token válido.")
                return None
            
            # Intentar obtener la orden con cada cuenta hasta encontrar la correcta
            account = None
            for acc in accounts:
                url = f'https://api.mercadolibre.com/orders/{order_id}'
                _logger.info("   Probando con cuenta ID=%d, Nombre='%s'...", acc.id, acc.name)
                try:
                    response = acc._ml_request_with_retry('GET', url, timeout=10)
                    _logger.info("   Response status: %s", response.status_code)
                    if response.status_code == 200:
                        account = acc
                        _logger.info("   ✅ Orden obtenida exitosamente con cuenta ID=%d", acc.id)
                        break
                    _logger.warning(
                        "   ⚠️ Error obteniendo orden: %s (Status: %s)",
                        (response.text or '')[:200],
                        response.status_code,
                    )
                except Exception as e:
                    _logger.warning("   ⚠️ Excepción al obtener orden: %s", str(e))
                    continue
            
            if not account:
                _logger.warning("⚠️ No se pudo obtener la orden %s con ninguna cuenta disponible.", order_id)
                return None
        
        _logger.info("✅ Cuenta seleccionada: ID=%d, Nombre='%s', ML User ID='%s'", 
                    account.id, account.name, account.meli_user_id or 'N/A')
        
        if not account.access_token:
            _logger.error("❌ La cuenta %s no tiene access_token válido.", account.name)
            return None

        if create_odoo_order is None:
            create_odoo_order = bool(account.auto_create_sale_order)
        if create_customer is None:
            create_customer = bool(create_odoo_order)
        elif not create_odoo_order:
            create_customer = False

        url = f'https://api.mercadolibre.com/orders/{order_id}'
        
        _logger.info("📤 Obteniendo orden desde API de MercadoLibre...")
        _logger.info("   URL: %s", url)
        _logger.info("   Headers: Authorization=Bearer %s...", account.access_token[:20] if account.access_token else 'None')
        
        try:
            sale_env = self.sudo()
            response = account._ml_request_with_retry('GET', url, timeout=30)
            _logger.info("📥 Response status: %s", response.status_code)
            _logger.debug("📥 Response headers: %s", dict(response.headers))
            
            response.raise_for_status()
            
            order_data = response.json()
            _logger.info("✅ Orden obtenida exitosamente desde API")

            if self.env.context.get('from_ml_webhook') and account._webhook_order_is_too_old(order_data):
                created_raw = order_data.get('date_created') or order_data.get('date_closed')
                _logger.info(
                    "⏭️ Webhook: orden %s (%s) anterior a webhook_active_since %s — omitida (go-live)",
                    order_id,
                    created_raw,
                    account.webhook_active_since,
                )
                return self.env['ml.sale']

            # Log completo del JSON para debugging
            order_json = json.dumps(order_data, indent=2, ensure_ascii=False, default=str)
            _logger.debug("📦 ORDEN COMPLETA RECIBIDA DESDE MERCADOLIBRE (JSON COMPLETO): %s", order_json)

            ml_order_id = str(order_data.get('id', order_id))

            # Buscar si ya existe la orden (idempotencia)
            # IMPORTANTE: ml.sale SÍ tiene company_id y la constraint unique lo usa.
            company_id = self.env.company.id
            existing_order = sale_env.search([
                ('ml_order_id', '=', ml_order_id),
                ('company_id', '=', company_id),
            ], limit=1)

            # Solo importar ventas hechas (pagadas). Si ya existe ml.sale, sí actualizar
            # (p. ej. cancelación / cambios de envío). Si aún no está pagada, esperar
            # el próximo webhook cuando ML pase a status=paid.
            ml_status = str(order_data.get('status') or '').lower().strip()
            if not existing_order and not self._ml_order_status_is_completed_sale(order_data):
                _logger.info(
                    "⏭️ Orden %s status=%s — no es venta hecha (solo se importa status=paid); "
                    "se omite hasta que esté pagada",
                    ml_order_id,
                    ml_status or '(vacío)',
                )
                return self.env['ml.sale']
            if existing_order and ml_status == 'cancelled':
                _logger.info(
                    "🔄 Orden %s ya importada y ahora cancelled — se actualiza estado",
                    ml_order_id,
                )
            
            # =====================================================
            # 1. DATOS DEL CLIENTE
            # =====================================================
            buyer = order_data.get('buyer', {})
            buyer_id = str(buyer.get('id', ''))
            buyer_nickname = buyer.get('nickname', '')
            
            # Obtener datos adicionales del comprador si están disponibles
            customer_name = buyer.get('first_name', '')
            if buyer.get('last_name'):
                customer_name = f"{customer_name} {buyer.get('last_name', '')}".strip()
            
            # Intentar obtener más datos del comprador desde la API
            buyer_email = buyer.get('email', '')
            buyer_phone = _meli_format_phone(buyer.get('phone', {}))
            
            # Intentar obtener DNI/documento del comprador
            # Puede estar en buyer.billing_info o buyer.identification
            customer_dni = None
            billing_info = buyer.get('billing_info', {})
            if billing_info:
                customer_dni = billing_info.get('doc_number') or billing_info.get('tax_id') or billing_info.get('dni')
            
            # Si no está en billing_info, buscar en identification
            if not customer_dni:
                identification = buyer.get('identification', {})
                if identification:
                    customer_dni = identification.get('number') or identification.get('value')
            
            # Si aún no está, buscar en otros campos posibles
            if not customer_dni:
                customer_dni = buyer.get('tax_id') or buyer.get('dni') or buyer.get('document_number')
            
            _logger.info("👤 Datos del comprador extraídos:")
            _logger.info("   Nombre: %s", customer_name)
            _logger.info("   Email: %s", buyer_email)
            _logger.info("   Teléfono: %s", buyer_phone)
            _logger.info("   DNI: %s", customer_dni or 'No disponible')

            order_tags = order_data.get('tags') or []
            static_tags = order_data.get('static_tags') or []
            ml_is_b2b = any(
                str(tag).lower() == 'b2b'
                for tag in (list(order_tags) + list(static_tags))
            )
            ml_billing_info_id = ''
            ml_taxpayer_type = ''
            ml_billing_info_data = ''
            billing_ref = buyer.get('billing_info') or {}
            if isinstance(billing_ref, dict) and billing_ref.get('id'):
                ml_billing_info_id = str(billing_ref.get('id'))
                billing_payload = account._fetch_meli_billing_info_data(
                    order_data, ml_billing_info_id,
                )
                if billing_payload:
                    ml_billing_info_data = json.dumps(
                        billing_payload, ensure_ascii=False, default=str,
                    )
                    fiscal_fields = parse_meli_billing_fiscal_fields(billing_payload)
                    if fiscal_fields.get('identification_number'):
                        customer_dni = fiscal_fields['identification_number']
                    if fiscal_fields.get('taxpayer_type'):
                        ml_taxpayer_type = fiscal_fields['taxpayer_type']
                    if fiscal_fields.get('name'):
                        customer_name = fiscal_fields['name']
                    if fiscal_fields.get('email') and not buyer_email:
                        buyer_email = fiscal_fields['email']
                    _logger.info(
                        '💼 Billing-info ML: CUIT/DNI=%s, situación=%s, B2B=%s',
                        customer_dni or '-',
                        ml_taxpayer_type or '-',
                        ml_is_b2b,
                    )
                else:
                    _logger.warning(
                        '⚠️ No se obtuvo billing-info para orden %s (id=%s)',
                        order_id,
                        ml_billing_info_id,
                    )
            
            # Determinar si es una venta test
            is_test_sale = False
            if buyer_id:
                test_users = self.env['ml.test.user'].sudo().search([
                    ('user_id', '=', buyer_id)
                ])
                if test_users:
                    is_test_sale = True
                    _logger.info("🧪 Orden %s identificada como venta test (buyer_id: %s)", order_id, buyer_id)
            
            # =====================================================
            # 2. DATOS DE LA DIRECCIÓN
            # =====================================================
            shipping = order_data.get('shipping', {}) or {}
            _logger.info("📦 Datos de shipping recibidos: %s", json.dumps(shipping, indent=2, ensure_ascii=False, default=str))

            shipment_data = {}
            if _meli_order_shipping_needs_shipment_api(shipping):
                shipment_id = shipping.get('id')
                _logger.info(
                    "📦 Orden sin dirección embebida; consultando GET /shipments/%s",
                    shipment_id,
                )
                shipment_data = account._fetch_meli_shipment_data(shipment_id)
                if shipment_data:
                    _logger.debug(
                        "📦 Shipment API: %s",
                        json.dumps(shipment_data, indent=2, ensure_ascii=False, default=str),
                    )

            shipping_fields = parse_meli_shipping_fields(shipping, shipment_data)
            shipping_street = shipping_fields['shipping_street']
            shipping_street_number = shipping_fields['shipping_street_number']
            shipping_floor = shipping_fields['shipping_floor']
            shipping_apartment = shipping_fields['shipping_apartment']
            shipping_city = shipping_fields['shipping_city']
            shipping_state = shipping_fields['shipping_state']
            shipping_country = shipping_fields['shipping_country']
            shipping_zip = shipping_fields['shipping_zip']
            receiver_name = shipping_fields['shipping_receiver_name']
            receiver_phone = shipping_fields['shipping_receiver_phone']

            if not buyer_phone and receiver_phone:
                buyer_phone = receiver_phone
                _logger.info("📞 Teléfono del comprador tomado del shipment (receiver_phone): %s", buyer_phone)

            _logger.info("📦 Dirección extraída:")
            _logger.info("   Calle: %s", shipping_street)
            _logger.info("   Número: %s", shipping_street_number)
            _logger.info("   Piso: %s", shipping_floor)
            _logger.info("   Departamento: %s", shipping_apartment)
            _logger.info("   Ciudad: %s", shipping_city)
            _logger.info("   Provincia: %s", shipping_state)
            _logger.info("   País: %s", shipping_country)
            _logger.info("   Código Postal: %s", shipping_zip)
            _logger.info("   Nombre Receptor: %s", receiver_name)
            _logger.info("   Teléfono Receptor: %s", receiver_phone)
            
            # =====================================================
            # 3. DATOS DE LA VENTA
            # =====================================================
            total_amount = float(order_data.get('total_amount', 0))
            
            # Convertir fechas de formato ISO 8601 a formato Odoo
            date_created_raw = order_data.get('date_created')
            date_closed_raw = order_data.get('date_closed')
            
            date_created = None
            date_closed = None
            
            def parse_iso_datetime(iso_string, account_obj=None):
                """
                Convierte fecha ISO 8601 de ML a string almacenable en Odoo (UTC, coherente con Datetime).
                - Con offset / Z: se respeta el instante absoluto.
                - Sin zona: se interpreta en `account_obj.timezone` (zona horaria de la cuenta ML).
                """
                if not iso_string:
                    return None
                try:
                    dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        tz_name = (account_obj and account_obj.timezone) or 'America/Argentina/Buenos_Aires'
                        try:
                            local_tz = pytz.timezone(tz_name)
                            dt = local_tz.localize(dt)
                            _logger.debug(
                                "🕐 Fecha sin zona interpretada en huso de cuenta ML (%s): %s",
                                tz_name, dt
                            )
                        except Exception as tz_err:
                            _logger.warning(
                                "⚠️ Error localizando fecha sin zona con %s: %s. Usando UTC.",
                                tz_name, tz_err
                            )
                            dt = pytz.UTC.localize(dt)
                    dt_utc = dt.astimezone(pytz.UTC).replace(tzinfo=None)
                    return fields.Datetime.to_string(dt_utc)
                except Exception as e:
                    _logger.warning("⚠️ Error parseando fecha ISO '%s': %s", iso_string, str(e))
                    try:
                        dt_str = iso_string.split('.')[0].replace('T', ' ')
                        dt_naive = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
                        if account_obj and account_obj.timezone:
                            try:
                                local_tz = pytz.timezone(account_obj.timezone)
                                dt_loc = local_tz.localize(dt_naive)
                                dt_utc = dt_loc.astimezone(pytz.UTC).replace(tzinfo=None)
                                return fields.Datetime.to_string(dt_utc)
                            except Exception:
                                pass
                        return fields.Datetime.to_string(
                            pytz.UTC.localize(dt_naive).replace(tzinfo=None)
                        )
                    except Exception:
                        return None
            
            if date_created_raw:
                date_created = parse_iso_datetime(date_created_raw, account)
            
            if date_closed_raw:
                date_closed = parse_iso_datetime(date_closed_raw, account)
            
            status = order_data.get('status', 'pending')
            
            # =====================================================
            # CONFIGURACIÓN DE IMPUESTOS
            # =====================================================
            # Usar impuesto configurado en la cuenta si existe
            tax_id = False
            tax_rate = 0.0
            price_includes_tax = False
            
            if account.default_tax_id:
                tax_id = account.default_tax_id
                tax_rate = tax_id.amount
                _logger.info("✅ Usando impuesto configurado en cuenta: %s (ID=%d, Tasa=%.2f%%)", 
                           tax_id.name, tax_id.id, tax_rate)
            
            # Calcular subtotales (sin descontar IVA del precio)
            order_items = order_data.get('order_items', [])
            subtotal = sum(float(item.get('unit_price', 0) * item.get('quantity', 0)) for item in order_items)
            shipping_cost = float(shipping.get('cost', 0) or 0)
            total_tax_amount = total_amount - subtotal - shipping_cost
            
            # Mapear estados
            payment_status = 'pending'
            if status in ['paid', 'payment_required']:
                payment_status = 'paid' if status == 'paid' else 'pending'
            elif status == 'cancelled':
                payment_status = 'cancelled'
            
            # Extraer estado de liberación del dinero desde los pagos
            payment_release_status = 'pending_review'  # Por defecto
            payments = order_data.get('payments', [])
            if payments:
                # Buscar el status_detail del primer pago (o el más relevante)
                # En MercadoLibre, el status_detail indica cuándo se libera el dinero
                for payment in payments:
                    status_detail = payment.get('status_detail', '').lower() or ''
                    status_payment = str(payment.get('status', '')).lower() or ''
                    
                    # Mapear status_detail común de MercadoLibre a nuestros estados
                    if 'pending_manual_review' in status_detail or 'manual_review' in status_detail:
                        payment_release_status = 'pending_manual_review'
                        break
                    elif 'pending_transfer' in status_detail or 'transfer' in status_detail:
                        payment_release_status = 'pending_transfer'
                        break
                    elif 'available' in status_detail or status_detail == 'accredited':
                        payment_release_status = 'available'
                        break
                    elif 'money_in_account' in status_detail or 'in_account' in status_detail:
                        payment_release_status = 'money_in_account'
                        break
                    elif 'blocked' in status_detail or status_payment == 'blocked':
                        payment_release_status = 'blocked'
                        break
                    elif 'pending_waiting_payment' in status_detail:
                        payment_release_status = 'pending_waiting_payment'
                        break
                    elif 'pending_waiting_for_payment_method' in status_detail:
                        payment_release_status = 'pending_waiting_for_payment_method'
                        break
                    
                    # Si el status del pago es "approved" o "accredited", generalmente el dinero está disponible
                    if status_payment in ['approved', 'accredited'] and payment_release_status == 'pending_review':
                        payment_release_status = 'available'
                
                _logger.info("💰 Estado de liberación del dinero detectado: %s (desde payments)", payment_release_status)
            else:
                _logger.info("ℹ️ No hay información de pagos para determinar estado de liberación")

            pay_max_inst, pay_has_inst, pay_summary = parse_ml_order_payment_terms(payments)
            if pay_summary:
                _logger.info("💳 Términos de pago ML: cuotas_max=%s, resumen=%s", pay_max_inst, pay_summary)
            
            fulfillment_status = 'to_pack'
            if status == 'shipped':
                fulfillment_status = 'shipped'
            elif status == 'delivered':
                fulfillment_status = 'delivered'

            is_full = account._meli_order_data_is_full(order_data)
            if is_full:
                _logger.info("📦 Orden %s detectada como Mercado Libre Full (fulfillment)", ml_order_id)

            ml_commission_amount = extract_ml_commission_from_order_data(order_data)
            if ml_commission_amount:
                _logger.info(
                    "💰 Comisión ML detectada en orden %s: %.2f",
                    ml_order_id,
                    ml_commission_amount,
                )
            
            # =====================================================
            # 4. PREPARAR VALORES PARA LA ORDEN
            # =====================================================
            order_vals = {
                'ml_order_id': ml_order_id,
                'ml_pack_id': self._ml_normalize_pack_id(order_data.get('pack_id')),
                'ml_account_id': account.id,
                'test_sale': is_test_sale,
                'company_id': company_id,
                
                # Datos de la venta
                'total_amount': total_amount,
                'subtotal': subtotal,
                'shipping_cost': shipping_cost,
                'total_tax_amount': total_tax_amount,
                'date_created': date_created,
                'date_closed': date_closed,
                'status': status,
                'payment_status': payment_status,
                'payment_release_status': payment_release_status,
                'ml_payment_max_installments': pay_max_inst,
                'ml_payment_has_installments': pay_has_inst,
                'ml_payment_terms_summary': pay_summary,
                'fulfillment_status': fulfillment_status,
                'is_full': is_full,
                'ml_commission_amount': ml_commission_amount,
                
                # Información de IVA
                'price_includes_tax': price_includes_tax,
                'tax_rate': tax_rate,
                'tax_id': tax_id.id if tax_id else False,
                
                # Datos del cliente
                'buyer_id': buyer_id,
                'buyer_nickname': buyer_nickname,
                'customer_name': customer_name,
                'customer_email': buyer_email,
                'customer_phone': buyer_phone,
                'customer_dni': customer_dni or '',
                'ml_is_b2b': ml_is_b2b,
                'ml_billing_info_id': ml_billing_info_id,
                'ml_taxpayer_type': ml_taxpayer_type,
                'ml_billing_info_data': ml_billing_info_data,
                
                # Datos de la dirección
                'shipping_street': shipping_street,
                'shipping_street_number': shipping_street_number,
                'shipping_floor': shipping_floor,
                'shipping_apartment': shipping_apartment,
                'shipping_city': shipping_city,
                'shipping_state': shipping_state,
                'shipping_country': shipping_country,
                'shipping_zip': shipping_zip,
                'shipping_receiver_name': receiver_name,
                'shipping_receiver_phone': receiver_phone,
                
                # Datos completos en JSON
                'full_order_data': order_json,
            }
            
            # =====================================================
            # 5. CREAR O ACTUALIZAR LA ORDEN
            # =====================================================
            if existing_order:
                existing_order.write(order_vals)
                order = existing_order
                _logger.info("✅ Orden actualizada: ML Order ID %s", ml_order_id)
            else:
                # El webhook llega duplicado y en paralelo (orders_v2 / reintentos).
                # Esto puede producir una carrera: 2 workers no ven existing_order y ambos intentan crear.
                # Manejar UniqueViolation para volver a leer y actualizar, en vez de abortar el flujo.
                try:
                    with self.env.cr.savepoint():
                        order = sale_env.create(order_vals)
                    _logger.info("✅ Orden creada: ML Order ID %s", ml_order_id)
                except psycopg2.IntegrityError as e:
                    if getattr(e, 'pgcode', None) == errorcodes.UNIQUE_VIOLATION:
                        _logger.warning(
                            "⚠️ UniqueViolation creando ml.sale (ml_order_id=%s, company_id=%s). Reintentando como actualización.",
                            ml_order_id, company_id
                        )
                        order = sale_env.search([
                            ('ml_order_id', '=', ml_order_id),
                            ('company_id', '=', company_id),
                        ], limit=1)
                        if order:
                            order.write(order_vals)
                            _logger.info("✅ Orden actualizada después de UniqueViolation: ML Order ID %s", ml_order_id)
                        else:
                            raise
                    else:
                        raise
            
            # =====================================================
            # 6. CREAR LAS LÍNEAS DE LA ORDEN
            # =====================================================
            if order_items:
                # Eliminar líneas existentes para recrearlas
                order.line_ids.unlink()
                
                line_vals_list = []
                for item_data in order_items:
                    item_info = item_data.get('item', {})
                    title = item_info.get('title', '')
                    quantity = float(item_data.get('quantity', 1))
                    unit_price = float(item_data.get('unit_price', 0))
                    ml_item_id = str(item_info.get('id', ''))
                    variation_id = item_info.get('variation_id') or item_data.get('variation_id')
                    sku = item_info.get('seller_sku') or item_info.get('seller_custom_field', '')

                    Publication = self.env['ml.publication']
                    pub = Publication._ml_find_for_order_item(
                        ml_item_id, account.id, variation_id,
                    )
                    product_tmpl = None
                    product_variant = None
                    if pub:
                        product_tmpl, product_variant = pub._ml_get_odoo_products_for_sale()
                        if not product_tmpl:
                            _logger.info(
                                "ℹ️ Publicación %s sin producto Odoo vinculado (item %s)",
                                pub.display_name, ml_item_id,
                            )
                    else:
                        _logger.info(
                            "ℹ️ Sin publicación Odoo para ML item %s (no importada o SKU no permitido)",
                            ml_item_id,
                        )

                    price_unit_final = unit_price
                    ml_tax_rate = parse_ml_order_item_tax_rate(item_data)

                    line_vals = {
                        'order_id': order.id,
                        'name': title,
                        'ml_item_id': ml_item_id,
                        'ml_variation_id': str(variation_id) if variation_id else False,
                        'publication_id': pub.id if pub else False,
                        'quantity': quantity,
                        'price_unit': price_unit_final,
                        'ml_tax_rate': ml_tax_rate,
                        'sku': sku,
                        'product_tmpl_id': product_tmpl.id if product_tmpl else False,
                        'product_id': product_variant.id if product_variant else False,
                        'sequence': len(line_vals_list) * 10 + 10,
                    }
                    line_vals_list.append((0, 0, line_vals))
                
                if line_vals_list:
                    order.write({'line_ids': line_vals_list})
                    _logger.info("✅ %d líneas creadas para orden %s", len(line_vals_list), ml_order_id)
            
            # =====================================================
            # 7. CREAR O ACTUALIZAR CLIENTE EN ODOO (OPCIONAL)
            # =====================================================
            if create_customer:
                try:
                    _logger.info("🔄 Creando/actualizando cliente en Odoo para ML Order ID: %s", ml_order_id)
                    allow_create = not order.ml_account_id.never_create_order_contacts
                    partner = order._get_or_create_customer(allow_create=allow_create)
                    if partner:
                        # Fiscal a medida solo en B2B y nunca sobre el CF compartido.
                        if order.ml_is_b2b:
                            default_cf = order.ml_account_id.default_order_partner_id
                            if not default_cf or partner.id != default_cf.id:
                                order._ml_sync_partner_fiscal_from_billing(partner)
                        _logger.info("✅ Cliente creado/actualizado: %s (ID: %s)", partner.name, partner.id)
                    else:
                        _logger.warning("⚠️ No se pudo crear o actualizar el cliente para ML Order ID: %s", ml_order_id)
                except Exception as e:
                    _logger.error("❌ Error creando/actualizando cliente en Odoo: %s", str(e), exc_info=True)
                    # No lanzamos la excepción para que la venta de ML se guarde igual
            else:
                _logger.info("ℹ️ Creación/actualización de contacto deshabilitada para ML Order ID: %s", ml_order_id)
            
            # =====================================================
            # 8. CREAR ORDEN DE VENTA EN ODOO AUTOMÁTICAMENTE
            # =====================================================
            # Nota: para crear sale.order necesitamos un partner. Si create_customer=False,
            # el wizard de importación masiva debe impedir esta combinación.
            # Solo crear SO cuando la venta ML está pagada (status=paid).
            if create_odoo_order and not order.odoo_sale_order_id and order.status != 'paid':
                _logger.info(
                    "ℹ️ Orden Odoo no creada para ML Order ID %s: status=%s (solo con paid)",
                    ml_order_id,
                    order.status,
                )
            elif create_odoo_order and not order.odoo_sale_order_id:
                if not order.odoo_partner_id:
                    default_partner = order.ml_account_id.default_order_partner_id
                    if default_partner:
                        order.odoo_partner_id = default_partner.id
                        _logger.warning(
                            "⚠️ No se pudo crear/matchear cliente para ML Order ID %s. "
                            "Usando contacto por defecto: %s (ID: %s)",
                            ml_order_id, default_partner.name, default_partner.id
                        )
                    else:
                        _logger.warning(
                            "⚠️ No se puede crear orden de venta Odoo sin contacto "
                            "(odoo_partner_id vacío y sin contacto por defecto en la cuenta). "
                            "ML Order ID: %s",
                            ml_order_id
                        )
                if not order.odoo_partner_id:
                    _logger.warning(
                        "⚠️ Orden Odoo no creada para ML Order ID %s por falta de contacto.",
                        ml_order_id
                    )
                else:
                    try:
                        actual_update_stock = update_stock
                        order_ctx = dict(self.env.context)
                        if not create_customer:
                            order_ctx['ml_skip_contact_create'] = True
                        pack_id = order._ml_normalize_pack_id(order.ml_pack_id)
                        if pack_id:
                            order._ml_sync_pack_orders(
                                account, pack_id, trigger_order_id=ml_order_id,
                            )
                            pending = order._ml_pack_pending_paid_orders(account, pack_id)
                            if pending:
                                _logger.info(
                                    "📦 Pack %s: importadas ml.sale; aguardando órdenes ML %s "
                                    "para pedido Odoo/factura unificados",
                                    pack_id, pending,
                                )
                            else:
                                _logger.info(
                                    "🔄 Pack %s completo — creando pedido Odoo unificado "
                                    "(ML Order ID %s, update_stock=%s)",
                                    pack_id, ml_order_id, actual_update_stock,
                                )
                                order.with_context(**order_ctx)._ml_ensure_pack_sale_order(
                                    update_stock=actual_update_stock,
                                )
                                _logger.info(
                                    "✅ Pedido Odoo pack %s: %s",
                                    pack_id,
                                    order.odoo_sale_order_id.name
                                    if order.odoo_sale_order_id else 'N/A',
                                )
                        else:
                            _logger.info(
                                "🔄 Creando orden de venta Odoo para ML Order ID: %s (update_stock=%s)",
                                ml_order_id, actual_update_stock,
                            )
                            order.with_context(**order_ctx).create_odoo_sale_order(
                                update_stock=actual_update_stock,
                            )
                            _logger.info(
                                "✅ Orden de venta Odoo creada: %s",
                                order.odoo_sale_order_id.name
                                if order.odoo_sale_order_id else 'N/A',
                            )
                    except Exception as e:
                        _logger.error("❌ Error creando orden de venta en Odoo automáticamente: %s", str(e), exc_info=True)
                        # No lanzamos la excepción para que la venta de ML se guarde igual
            elif not create_odoo_order:
                _logger.info("ℹ️ Creación de orden de venta Odoo deshabilitada para ML Order ID: %s", ml_order_id)
            elif order.odoo_sale_order_id:
                _logger.info("ℹ️ Orden de venta Odoo ya existe para ML Order ID: %s", ml_order_id)
            
            # =====================================================
            # 9. ACTUALIZAR current_stock_ml DESDE ML PARA CADA ITEM VENDIDO
            # =====================================================
            # Después de procesar la venta, actualizar el stock actual en ML para cada publicación
            # Esto asegura que current_stock_ml refleje el valor real después de que ML descuenta el stock
            if order_items:
                _logger.info("🔄 Actualizando current_stock_ml desde ML para items vendidos...")
                for item_data in order_items:
                    item_info = item_data.get('item', {})
                    ml_item_id = str(item_info.get('id', ''))
                    
                    if not ml_item_id:
                        continue
                    
                    try:
                        variation_id = item_info.get('variation_id') or item_data.get('variation_id')
                        publication = self.env['ml.publication']._ml_find_for_order_item(
                            ml_item_id, account.id, variation_id,
                        )

                        if publication:
                            # Actualizar current_stock_ml desde la API de ML
                            publication._update_current_stock_from_ml()
                        else:
                            _logger.debug("ℹ️ No se encontró publicación para ML Item ID %s, omitiendo actualización de current_stock_ml", 
                                        ml_item_id)
                    except Exception as e:
                        _logger.warning("⚠️ Error actualizando current_stock_ml para ML Item ID %s: %s", 
                                      ml_item_id, e)
                        # No fallar la operación si falla la actualización del stock
            
            return order
            
        except requests.exceptions.RequestException as e:
            _logger.error("❌ Error obteniendo orden %s desde ML: %s", order_id, str(e), exc_info=True)
            return None
        except Exception as e:
            _logger.error("❌ Error procesando orden %s: %s", order_id, str(e), exc_info=True)
            return None
    
    def _get_kit_components_from_bom(self, product):
        """
        Obtiene los componentes de un kit desde el BOM (Bill of Materials) de Odoo.
        
        Args:
            product: product.product - Producto a verificar si es kit
        
        Returns:
            list: Lista de líneas de BOM (mrp.bom.line) si el producto tiene BOM phantom, sino lista vacía
        """
        try:
            # Verificar si el módulo mrp está instalado
            if 'mrp.bom' not in self.env:
                _logger.debug("ℹ️ Módulo MRP no está instalado, no se pueden detectar kits por BOM")
                return []
            
            # Buscar BOM de tipo phantom para este producto
            bom = self.env['mrp.bom'].search([
                ('product_id', '=', product.id),
                ('type', '=', 'phantom'),
                ('company_id', 'in', [False, self.env.company.id])
            ], limit=1)
            
            # Si no se encuentra por product_id, buscar por product_tmpl_id
            if not bom and product.product_tmpl_id:
                bom = self.env['mrp.bom'].search([
                    ('product_tmpl_id', '=', product.product_tmpl_id.id),
                    ('type', '=', 'phantom'),
                    ('company_id', 'in', [False, self.env.company.id])
                ], limit=1)
            
            if bom:
                _logger.info("📦 BOM phantom encontrado para producto '%s': %d componentes", product.name, len(bom.bom_line_ids))
                return bom.bom_line_ids
            else:
                return []
        except Exception as e:
            _logger.warning("⚠️ Error verificando BOM para producto %s: %s", product.name, str(e))
            return []
    
    def _get_or_create_meli_category(self):
        """
        Obtiene o crea la categoría de contacto "Cliente MELI" con color amarillo.
        
        Returns:
            res.partner.category: Categoría creada o encontrada
        """
        # res.partner.category no tiene company_id, buscar solo por nombre
        category = self.env['res.partner.category'].search([
            ('name', '=', 'Cliente MELI')
        ], limit=1)
        
        if not category:
            # Crear categoría con color amarillo (color 3 es amarillo en Odoo)
            category = self.env['res.partner.category'].create({
                'name': 'Cliente MELI',
                'color': 3,  # Color amarillo
            })
            _logger.info("✅ Categoría 'Cliente MELI' creada (ID: %s, Color: Amarillo)", category.id)
        else:
            # Asegurar que tenga color amarillo
            if category.color != 3:
                category.write({'color': 3})
                _logger.info("✅ Categoría 'Cliente MELI' actualizada con color amarillo")
        
        return category
    
    def _get_or_create_customer(self, allow_create=True):
        """
        Busca o crea el contacto en Odoo para la venta ML.

        - Si la venta NO es B2B (``ml_is_b2b``): siempre ``ml.account.default_order_partner_id``
          (Consumidor Final). No se crean/actualizan contactos ni datos fiscales del comprador.
        - Si es B2B: match por ``meli_buyer_id`` / documento; alta con datos mínimos si está permitido;
          aplicar billing-info para factura a medida.
        - Con ``never_create_order_contacts``: no se crean contactos nuevos; B2B solo reutiliza existentes.
        """
        self.ensure_one()

        def _normalize(value):
            return re.sub(r'\s+', ' ', (value or '').strip())

        default_partner = self.ml_account_id.default_order_partner_id
        skip_create = bool(self.ml_account_id.never_create_order_contacts)
        effective_allow_create = allow_create and not skip_create

        def _use_default(reason_log):
            if not default_partner:
                _logger.warning(
                    "⚠️ ML Order %s: %s y no hay default_order_partner_id en la cuenta.",
                    self.ml_order_id,
                    reason_log,
                )
                return self.env['res.partner']
            _logger.info(
                "ℹ️ ML Order %s: %s — usando contacto por defecto de cuenta: %s (id=%s)",
                self.ml_order_id,
                reason_log,
                default_partner.name,
                default_partner.id,
            )
            self.odoo_partner_id = default_partner.id
            return default_partner

        # Ventas no B2B: siempre Consumidor Final (contacto por defecto de la cuenta).
        if not self.ml_is_b2b:
            return _use_default('venta no B2B → factura a Consumidor Final')

        normalized_full_name = _normalize(self.customer_name)
        name_parts = [part for part in normalized_full_name.split(' ') if part]
        first_name = name_parts[0] if name_parts else ''
        last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''
        normalized_state = _normalize(self.shipping_state)
        normalized_street = _normalize(self.shipping_street)
        normalized_email = _normalize(self.customer_email).lower()
        minimum_for_new = bool(first_name and last_name and normalized_state)

        meli_category = self._get_or_create_meli_category()
        account_cc = (self.ml_account_id.country_id.code or 'AR').upper()

        state = self.env['res.country.state'].browse()
        if normalized_state:
            state = self.env['res.country.state'].search([
                ('name', 'ilike', normalized_state),
                '|',
                ('country_id', '=', False),
                ('country_id.code', '=', account_cc),
            ], limit=1)

        partner = self.env['res.partner']
        buyer_key = (str(self.buyer_id).strip() if self.buyer_id else '') or ''
        if buyer_key and 'meli_buyer_id' in self.env['res.partner']._fields:
            partner = self.env['res.partner'].search([
                ('meli_buyer_id', '=', buyer_key),
                '|',
                ('company_id', '=', False),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
        # Fallback: mismo documento (DNI/CUIT) ya cargado en Odoo (compras repetidas sin meli_buyer_id).
        if not partner and self.customer_dni:
            dni_norm = re.sub(r'[^0-9]', '', str(self.customer_dni))
            if dni_norm:
                partners_vat = self.env['res.partner'].search([
                    '|',
                    ('company_id', '=', False),
                    ('company_id', '=', self.env.company.id),
                    '|',
                    ('vat', 'ilike', dni_norm),
                    ('vat', 'ilike', self.customer_dni),
                ], limit=20)
                for cand in partners_vat:
                    cand_vat = re.sub(r'[^0-9]', '', cand.vat or '')
                    if cand_vat == dni_norm:
                        partner = cand
                        break
        if partner:
            update_vals = {}
            if (
                buyer_key
                and 'meli_buyer_id' in partner._fields
                and not partner.meli_buyer_id
            ):
                update_vals['meli_buyer_id'] = buyer_key
            if meli_category.id not in partner.category_id.ids:
                update_vals['category_id'] = [(4, meli_category.id)]
            if normalized_email and (not partner.email or partner.email != normalized_email):
                update_vals['email'] = normalized_email
            if self.customer_phone and not partner.phone:
                update_vals['phone'] = self.customer_phone
            if self.customer_dni and not partner.vat:
                update_vals['vat'] = self.customer_dni
            if normalized_street and not partner.street:
                update_vals['street'] = normalized_street
            if self.shipping_city and not partner.city:
                update_vals['city'] = self.shipping_city or False
            if state and not partner.state_id:
                update_vals['state_id'] = state.id
            if update_vals:
                partner.write(update_vals)
            self.odoo_partner_id = partner.id
            _logger.info(
                "✅ Cliente ML reutilizado: %s (id=%s, buyer_id=%s)",
                partner.name,
                partner.id,
                buyer_key or 'N/A',
            )
            return partner

        if not effective_allow_create:
            reason = (
                'never_create_order_contacts — B2B sin contacto existente'
                if skip_create
                else 'importación sin crear contacto (ml_skip_contact_create)'
            )
            return _use_default(reason)

        if not minimum_for_new:
            return _use_default(
                "sin match por buyer_id y faltan nombre/apellido/provincia para crear contacto"
            )

        street2_parts = []
        if self.shipping_street_number:
            street2_parts.append(f"N° {self.shipping_street_number}")
        if self.shipping_floor:
            street2_parts.append(f"Piso {self.shipping_floor}")
        if self.shipping_apartment:
            street2_parts.append(f"Depto {self.shipping_apartment}")

        partner_vals = {
            'name': normalized_full_name,
            'email': normalized_email or False,
            'phone': self.customer_phone or False,
            'vat': self.customer_dni or False,
            'comment': (
                f'Cliente de MercadoLibre\n'
                f'Nickname: {self.buyer_nickname or "N/A"}\n'
                f'ID ML: {self.buyer_id or "N/A"}'
            ),
            'is_company': False,
            'category_id': [(6, 0, [meli_category.id])],
            'street': normalized_street or False,
            'street2': ', '.join(street2_parts) if street2_parts else False,
            'city': self.shipping_city or False,
            'zip': self.shipping_zip or False,
        }
        if buyer_key and 'meli_buyer_id' in self.env['res.partner']._fields:
            partner_vals['meli_buyer_id'] = buyer_key
        if state:
            partner_vals['state_id'] = state.id
        else:
            partner_vals['comment'] += f'\nProvincia: {normalized_state}'

        country = False
        if self.shipping_country:
            country = self.env['res.country'].search([
                ('name', 'ilike', self.shipping_country)
            ], limit=1)
        if not country:
            country = self.env['res.country'].search(
                [('code', '=', account_cc)], limit=1
            )
        if country:
            partner_vals['country_id'] = country.id

        lang = self.env['res.partner']._connector_argentina_lang()
        if lang:
            partner_vals['lang'] = lang

        partner = self.env['res.partner'].create(partner_vals)
        _logger.info("✅ Cliente ML creado (buyer_id nuevo): %s (id=%s)", partner.name, partner.id)
        self.odoo_partner_id = partner.id
        return partner

    def _ml_is_default_order_partner(self, partner):
        """True si el partner es el contacto por defecto (CF) de la cuenta ML."""
        self.ensure_one()
        default = self.ml_account_id.default_order_partner_id
        return bool(default and partner and partner.id == default.id)

    def _ml_get_shipping_partner(self, partner):
        """
        Resuelve dirección de entrega sin spamear contactos hijos.

        - Si el partner es el CF/default de la cuenta: se usa el mismo partner (no crea hijos).
        - Si hay calle/ciudad: reutiliza un hijo delivery existente con misma calle+CP, o crea uno.
        - Sin datos de envío: el partner comercial.
        """
        self.ensure_one()
        if not partner:
            return self.env['res.partner']

        if self._ml_is_default_order_partner(partner):
            return partner

        if not (self.shipping_street or self.shipping_city):
            return partner

        street = (self.shipping_street or '').strip()
        street2 = (
            f"{self.shipping_street_number or ''} "
            f"{self.shipping_floor or ''} "
            f"{self.shipping_apartment or ''}"
        ).strip()
        city = (self.shipping_city or '').strip()
        zipcode = (self.shipping_zip or '').strip()
        ship_name = (self.shipping_receiver_name or '').strip() or partner.name

        Partner = self.env['res.partner']
        existing = Partner.search([
            ('parent_id', '=', partner.id),
            ('type', '=', 'delivery'),
            ('street', '=', street),
            ('zip', '=', zipcode),
        ], limit=1)
        if existing:
            return existing

        delivery_vals = {
            'parent_id': partner.id,
            'type': 'delivery',
            'name': ship_name,
            'street': street,
            'street2': street2 or False,
            'city': city or False,
            'zip': zipcode or False,
            'phone': self.shipping_receiver_phone or partner.phone or False,
        }
        lang = Partner._connector_argentina_lang()
        if lang:
            delivery_vals['lang'] = lang
        return Partner.create(delivery_vals)
    
    @api.model
    def _get_or_create_tax_static(self, tax_rate, site_id, company_id):
        """
        Busca o crea un impuesto de IVA en Odoo según la tasa y el site.
        Método estático que puede ser llamado antes de crear el registro.
        
        Args:
            tax_rate: Tasa de impuesto (porcentaje, ej: 21.0 para 21%)
            site_id: ID del site de MercadoLibre (MLA, MLB, etc.)
            company_id: ID de la compañía
        
        Returns:
            account.tax: Registro del impuesto encontrado o creado
        """
        
        # Nombres de impuestos según site
        tax_names = {
            'MLA': f'IVA {tax_rate:.2f}%',
            'MLB': f'Imposto {tax_rate:.2f}%',
            'MLM': f'IVA {tax_rate:.2f}%',
            'MLC': f'IVA {tax_rate:.2f}%',
            'MCO': f'IVA {tax_rate:.2f}%',
            'MLV': f'IVA {tax_rate:.2f}%',
        }
        
        tax_name = tax_names.get(site_id, f'IVA {tax_rate:.2f}%')
        
        company = self.env['res.company'].browse(company_id)
        
        # Buscar impuesto existente
        tax = self.env['account.tax'].search([
            ('amount', '=', tax_rate),
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', company_id),
            ('price_include', '=', False),  # El precio en Odoo no incluye IVA (ya lo descontamos)
        ], limit=1)
        
        if tax:
            _logger.info("✅ Impuesto encontrado: %s (ID=%d, Tasa=%.2f%%)", tax.name, tax.id, tax.amount)
            return tax
        
        # Si no existe, buscar por nombre
        tax = self.env['account.tax'].search([
            ('name', 'ilike', tax_name),
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', company_id),
        ], limit=1)
        
        if tax:
            _logger.info("✅ Impuesto encontrado por nombre: %s (ID=%d, Tasa=%.2f%%)", tax.name, tax.id, tax.amount)
            return tax
        
        # Si no existe, crear uno nuevo
        try:
            # Obtener cuenta de impuestos por defecto
            tax_account = self.env['account.account'].search([
                ('code', 'like', '4.1.1%'),  # Cuenta de ingresos por ventas
                ('company_id', '=', company_id),
            ], limit=1)
            
            tax_account_received = self.env['account.account'].search([
                ('code', 'like', '2.1.3%'),  # Cuenta de impuestos a cobrar
                ('company_id', '=', company_id),
            ], limit=1)
            
            tax = self.env['account.tax'].create({
                'name': tax_name,
                'amount': tax_rate,
                'type_tax_use': 'sale',
                'price_include': False,  # El precio no incluye IVA (ya lo descontamos)
                'company_id': company_id,
                'account_id': tax_account.id if tax_account else False,
                'refund_account_id': tax_account.id if tax_account else False,
            })
            
            _logger.info("✅ Impuesto creado: %s (ID=%d, Tasa=%.2f%%)", tax.name, tax.id, tax.amount)
            return tax
        except Exception as e:
            _logger.error("❌ Error al crear impuesto: %s", str(e))
            return False
    
    def _get_or_create_tax(self, tax_rate, site_id):
        """
        Método de instancia que llama al método estático.
        
        Args:
            tax_rate: Tasa de impuesto (porcentaje, ej: 21.0 para 21%)
            site_id: ID del site de MercadoLibre (MLA, MLB, etc.)
        
        Returns:
            account.tax: Registro del impuesto encontrado o creado
        """
        self.ensure_one()
        return self._get_or_create_tax_static(tax_rate, site_id, self.env.company.id)

    # =====================================================
    # 🔹 PACK ML: importación y pedido Odoo unificado
    # =====================================================
    def _ml_fetch_pack_api_order_ids(self, account, pack_id):
        """IDs de órdenes ML del pack según GET /packs/{pack_id}."""
        pack_id = self._ml_normalize_pack_id(pack_id)
        if not pack_id:
            return []
        url = 'https://api.mercadolibre.com/packs/%s' % pack_id
        try:
            resp = account._ml_request_with_retry('GET', url, timeout=20)
            if not resp.ok:
                _logger.warning(
                    'Pack %s: GET /packs HTTP %s — %s',
                    pack_id, resp.status_code, (resp.text or '')[:200],
                )
                return []
            data = resp.json() or {}
            return [
                str(order.get('id'))
                for order in (data.get('orders') or [])
                if order.get('id')
            ]
        except Exception as e:
            _logger.warning('Pack %s: error consultando /packs: %s', pack_id, e)
            return []

    def _ml_sync_pack_orders(self, account, pack_id, trigger_order_id=None):
        """Importa el resto de órdenes del pack (sin crear pedido Odoo todavía)."""
        if self.env.context.get('ml_skip_pack_sync'):
            return
        pack_id = self._ml_normalize_pack_id(pack_id)
        if not pack_id:
            return
        order_ids = self._ml_fetch_pack_api_order_ids(account, pack_id)
        if not order_ids:
            return
        opts = account._ml_sale_import_options()
        sync_ctx = dict(self.env.context, ml_skip_pack_sync=True)
        for oid in order_ids:
            if trigger_order_id and str(oid) == str(trigger_order_id):
                continue
            try:
                self.with_context(**sync_ctx).update_or_create_from_meli(
                    str(oid),
                    account_id=account.id,
                    create_odoo_order=False,
                    create_customer=False,
                    update_stock=opts.get('update_stock', True),
                )
            except Exception as e:
                _logger.warning(
                    'Pack %s: error sincronizando orden ML %s: %s',
                    pack_id, oid, e,
                )

    def _ml_pack_pending_paid_orders(self, account, pack_id):
        """Órdenes del pack en ML que aún no están como ml.sale pagada con líneas."""
        pack_id = self._ml_normalize_pack_id(pack_id)
        api_ids = self._ml_fetch_pack_api_order_ids(account, pack_id)
        if not api_ids:
            return []
        company_id = self.env.company.id
        pending = []
        for oid in api_ids:
            sale = self.sudo().search([
                ('ml_order_id', '=', str(oid)),
                ('company_id', '=', company_id),
                ('active', '=', True),
            ], limit=1)
            if not sale or sale.status != 'paid' or not sale.line_ids:
                pending.append(str(oid))
        return pending

    def _ml_resolve_partner_for_odoo_order(self):
        """Partner comercial para el pedido Odoo (respeta ml_skip_contact_create)."""
        self.ensure_one()
        if self.odoo_partner_id:
            return self.odoo_partner_id
        allow_create = not self.env.context.get('ml_skip_contact_create', False)
        partner = self._get_or_create_customer(allow_create=allow_create)
        if not partner:
            default_partner = self.ml_account_id.default_order_partner_id
            if not default_partner:
                raise UserError(_(
                    'No se pudo crear/encontrar el cliente para la venta de MercadoLibre y '
                    'la cuenta no tiene configurado un contacto por defecto.'
                ))
            partner = default_partner
            self.odoo_partner_id = partner.id
            _logger.warning(
                "⚠️ Usando contacto por defecto para ML Order ID %s: %s (ID: %s)",
                self.ml_order_id, partner.name, partner.id,
            )
        return partner

    def _ml_finalize_sale_order(self, sale_order, used_fallback_product, update_stock=True):
        """Confirma pedido Odoo y factura (una sola vez por pack)."""
        self.ensure_one()

        # IVA desde ML en fallback (antes de confirmar/facturar).
        linked_sales = self.env['ml.sale'].sudo().search([
            ('odoo_sale_order_id', '=', sale_order.id),
            ('active', '=', True),
        ]) or self
        for sale in linked_sales:
            sale._ml_apply_fallback_ml_taxes_on_sale_order(sale_order)

        if self._ml_hold_sale_order_for_fallback(used_fallback_product):
            _logger.warning(
                "⚠️ Orden %s en borrador: producto fallback/no identificado. Revisión manual.",
                sale_order.name,
            )
            return sale_order

        if used_fallback_product:
            _logger.info(
                "ℹ️ Orden %s: líneas con producto fallback — confirmación/factura según cuenta ML.",
                sale_order.name,
            )

        try:
            _logger.info(
                "🔄 Confirmando orden de venta: %s (update_stock=%s)",
                sale_order.name, update_stock,
            )
            sale_order.action_confirm()
            pickings = sale_order.picking_ids
            if not update_stock:
                active_pickings = pickings.filtered(lambda p: p.state != 'cancel')
                if active_pickings:
                    active_pickings.action_cancel()
            _logger.info(
                "✅ Orden de venta Odoo confirmada: %s (update_stock=%s)",
                sale_order.name, update_stock,
            )
        except Exception as e:
            _logger.warning(
                "⚠️ Orden de venta creada pero no se pudo confirmar: %s", e,
            )
            return sale_order

        if self.ml_account_id.auto_create_invoice:
            try:
                self._create_invoice_from_sale_order(sale_order)
            except Exception as e:
                _logger.error(
                    "❌ Error creando factura automáticamente: %s", e, exc_info=True,
                )
        else:
            _logger.info(
                "ℹ️ Creación automática de factura deshabilitada para cuenta %s",
                self.ml_account_id.name,
            )
        return sale_order

    def _ml_create_sale_order_from_pack(self, pack_sales, update_stock=True):
        """Crea un único sale.order con las líneas de todas las ml.sale del pack."""
        self.ensure_one()
        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        leader = self

        partner = leader._ml_resolve_partner_for_odoo_order()

        order_lines = []
        used_fallback_product = False
        note_parts = [
            'Venta MercadoLibre — pack/carrito unificado',
            'Pack ML: %s' % pack,
        ]
        for sale in pack_sales:
            lines, used_fb = sale._ml_build_odoo_order_line_commands()
            order_lines.extend(lines)
            used_fallback_product = used_fallback_product or used_fb
            note_parts.append(
                'Orden ML #%s — %s' % (sale.ml_order_id, sale.name or sale.ml_order_id)
            )
            note_parts.append(
                'Link API ML: https://api.mercadolibre.com/orders/%s' % sale.ml_order_id
            )

        if not order_lines:
            raise UserError(_(
                'No se pudieron crear líneas de venta para el pack %s. '
                'Verifique publicaciones y productos vinculados.'
            ) % pack)

        account = leader.ml_account_id
        if leader.is_full and account.has_meli_full and not account.full_warehouse_id:
            raise UserError(
                _(
                    'La venta %s es Mercado Libre Full, pero la cuenta «%s» no tiene configurado '
                    'el almacén para ventas Full.'
                )
                % (leader.ml_order_id, account.name)
            )
        warehouse = account._get_warehouse_for_ml_sale(is_full=leader.is_full)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', self.env.company.id),
            ], limit=1)

        pricelist = self.env['product.pricelist'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not pricelist:
            pricelist = self.env.company.partner_id.property_product_pricelist

        order_ids = pack_sales.mapped('ml_order_id')
        order_dates = [s.date_created for s in pack_sales if s.date_created]
        sale_order_vals = {
            'partner_id': partner.id,
            'date_order': min(order_dates) if order_dates else fields.Datetime.now(),
            'pricelist_id': pricelist.id if pricelist else False,
            'order_line': order_lines,
            'origin': 'MercadoLibre pack #%s' % pack,
            'warehouse_id': warehouse.id if warehouse else False,
            'company_id': self.env.company.id,
            'sale_origin': 'mercadolibre',
            'ml_sale_id': leader.id,
            'ml_pack_id': pack,
            'ml_order_payload': leader.full_order_data,
            'ml_commission_amount': sum(pack_sales.mapped('ml_commission_amount')),
            'note': '\n'.join(note_parts),
        }
        shipping_partner = leader._ml_get_shipping_partner(partner)
        if shipping_partner and shipping_partner.id != partner.id:
            sale_order_vals['partner_shipping_id'] = shipping_partner.id

        sale_order = self.env['sale.order'].create(sale_order_vals)
        pack_sales.write({'odoo_sale_order_id': sale_order.id})
        self.env.flush_all()
        _logger.info(
            "✅ Pack %s: pedido Odoo %s creado con %d orden(es) ML: %s",
            pack, sale_order.name, len(pack_sales), ', '.join(order_ids),
        )
        return self._ml_finalize_sale_order(
            sale_order, used_fallback_product, update_stock=update_stock,
        )

    def _ml_ensure_pack_sale_order(self, update_stock=True):
        """Un solo pedido Odoo (y factura) para todas las ml.sale pagadas del pack."""
        self.ensure_one()
        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        if not pack:
            return self.with_context(ml_pack_building=True).create_odoo_sale_order(
                update_stock=update_stock,
            )

        self._ml_advisory_lock_pack(pack)
        company_id = self.env.company.id
        pack_sales = self.sudo().search([
            ('ml_pack_id', '=', pack),
            ('company_id', '=', company_id),
            ('status', '=', 'paid'),
            ('active', '=', True),
        ], order='id asc')
        if not pack_sales:
            return self.env['sale.order']

        existing_so = self.env['sale.order']
        for sale in pack_sales:
            so = sale._ml_find_pack_sale_order()
            if so:
                existing_so = so
                break

        if existing_so:
            for sale in pack_sales.filtered(
                lambda s: not s.odoo_sale_order_id
                or s.odoo_sale_order_id.id != existing_so.id
            ):
                if sale.odoo_sale_order_id and sale.odoo_sale_order_id != existing_so:
                    _logger.warning(
                        "⚠️ Pack %s: ml.sale %s ya vinculada a otra SO (%s); se omite.",
                        pack, sale.ml_order_id, sale.odoo_sale_order_id.name,
                    )
                    continue
                sale._ml_append_lines_to_sale_order(existing_so, update_stock=update_stock)
            return existing_so

        leader = pack_sales[0]
        return leader.with_context(ml_pack_building=True)._ml_create_sale_order_from_pack(
            pack_sales, update_stock=update_stock,
        )

    def _ml_normalize_pack_id(self, pack_id):
        """Normaliza pack_id a string estable (evita fallos de match por tipo/espacios)."""
        if pack_id in (None, False, ''):
            return False
        return str(pack_id).strip() or False

    def _ml_advisory_lock_pack(self, pack_id):
        """Lock transaccional para que webhooks concurrentes del mismo pack no dupliquen SO."""
        pack_id = self._ml_normalize_pack_id(pack_id)
        if not pack_id:
            return
        # hash() de Python no es estable entre procesos; usar md5.
        digest = int(hashlib.md5(('ml.pack:%s' % pack_id).encode('utf-8')).hexdigest()[:15], 16)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', (digest,))

    def _ml_find_pack_sale_order(self):
        """Pedido Odoo ya creado para este pack (otra ml.sale o SO con ml_pack_id)."""
        self.ensure_one()
        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        if not pack:
            return self.env['sale.order']

        # Crítico: flush para que un import anterior del mismo pack (misma tx/cron)
        # ya tenga odoo_sale_order_id visible en el SEARCH SQL.
        self.env.flush_all()

        sibling = self.sudo().search([
            ('ml_pack_id', '=', pack),
            ('odoo_sale_order_id', '!=', False),
            ('id', '!=', self.id),
            ('active', '=', True),
        ], order='id asc', limit=1)
        if sibling and sibling.odoo_sale_order_id:
            so = sibling.odoo_sale_order_id
            if so.state != 'cancel':
                return so

        # Fallback: SO ya etiquetada con este pack (aunque el link ml.sale esté demorado)
        if 'ml_pack_id' in self.env['sale.order']._fields:
            so = self.env['sale.order'].sudo().search([
                ('ml_pack_id', '=', pack),
                ('state', '!=', 'cancel'),
                '|',
                ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ], order='id asc', limit=1)
            if so:
                return so
        return self.env['sale.order']

    def _ml_resolve_product_for_sale_line(self, line, fallback_product):
        """Resuelve product.product desde publicación ML; indica si se usó fallback."""
        if line.publication_id:
            tmpl, variant = line.publication_id._ml_get_odoo_products_for_sale()
            if variant:
                return variant, False
            if tmpl and tmpl.product_variant_id:
                return tmpl.product_variant_id, False

        if line.product_id:
            return line.product_id, False
        if line.product_tmpl_id:
            variant = line.product_tmpl_id.product_variant_id
            if variant:
                return variant, False

        if fallback_product:
            return fallback_product, True
        return self.env['product.product'], False

    def _ml_hold_sale_order_for_fallback(self, used_fallback):
        """True si hay fallback y la cuenta no permite confirmar/facturar automático."""
        if not used_fallback:
            return False
        return not self.ml_account_id.auto_invoice_with_fallback_product

    def _ml_sale_order_line_tax_field(self):
        """Odoo 19+: tax_ids; versiones viejas: tax_id."""
        Sol = self.env['sale.order.line']
        return 'tax_ids' if 'tax_ids' in Sol._fields else 'tax_id'

    def _ml_get_fallback_tax_for_line(self, ml_line):
        """
        Impuesto para línea SO con producto fallback.

        Prioridad:
        1. tax_id de la publicación vinculada a la línea
        2. tax_id de ml.publication con seller_sku == SKU de la línea (misma cuenta)
        3. default_tax_id de la cuenta ML
        """
        self.ensure_one()
        account = self.ml_account_id
        Publication = self.env['ml.publication']

        pub = ml_line.publication_id
        if pub and pub.tax_id:
            return pub.tax_id

        sku = (ml_line.sku or '').strip()
        if sku and account:
            sku_norm = sku.upper()
            candidates = Publication.search([
                ('ml_account_id', '=', account.id),
                ('tax_id', '!=', False),
                ('seller_sku', 'ilike', sku),
            ])
            pub_by_sku = candidates.filtered(
                lambda p: (p.seller_sku or '').strip().upper() == sku_norm
            )[:1]
            if pub_by_sku:
                return pub_by_sku.tax_id

        return account.default_tax_id if account else self.env['account.tax']

    def _ml_prepare_sale_order_line_vals(self, ml_line, product, is_fallback, pay_suffix=''):
        """Descripción interna con referencia ML; nombre limpio en PDF solo si hay fallback."""
        ml_title = (ml_line.name or '').strip()
        order_line_vals = {
            'product_id': product.id,
            'product_uom_qty': ml_line.quantity,
            'price_unit': ml_line.price_unit,
            'name': '%s [ML %s]%s' % (ml_title or product.display_name, self.ml_order_id, pay_suffix),
        }
        if is_fallback:
            display = ml_title or (ml_line.sku or '').strip() or product.display_name
            if display:
                order_line_vals['ml_invoice_display_name'] = display
            # Fallback: impuesto de la publicación (por SKU) o default de la cuenta.
            tax = self._ml_get_fallback_tax_for_line(ml_line)
            if tax:
                tax_field = self._ml_sale_order_line_tax_field()
                order_line_vals[tax_field] = [(6, 0, [tax.id])]
        return order_line_vals

    def _ml_apply_fallback_ml_taxes_on_sale_order(self, sale_order):
        """
        Fuerza tax_ids en líneas fallback según tax_id de la publicación / default cuenta.

        Independiente de auto_invoice_with_fallback_product (ese flag solo
        confirma/factura). Necesario en Odoo 19 porque tax_ids es computed
        desde el producto y pisa el valor del create.
        """
        self.ensure_one()
        account = self.ml_account_id
        if not account or not sale_order:
            return

        tax_field = self._ml_sale_order_line_tax_field()
        fallback_product = account.fallback_product_id
        so_lines = sale_order.order_line.filtered(lambda l: not l.display_type)
        used_so = self.env['sale.order.line']

        for ml_line in self.line_ids:
            product, is_fallback = self._ml_resolve_product_for_sale_line(
                ml_line, fallback_product,
            )
            if not is_fallback or not product:
                continue
            tax = self._ml_get_fallback_tax_for_line(ml_line)
            if not tax:
                _logger.warning(
                    'Fallback ML %s línea "%s" (SKU %s): sin tax_id en publicación '
                    'ni default_tax_id en la cuenta',
                    self.ml_order_id, ml_line.name, ml_line.sku or '',
                )
                continue

            candidates = so_lines.filtered(
                lambda l, p=product, oid=self.ml_order_id: l.product_id == p
                and oid in (l.name or '')
                and l not in used_so
            )
            if not candidates:
                candidates = so_lines.filtered(
                    lambda l, p=product: l.product_id == p and l not in used_so
                )
            sol = candidates[:1]
            if not sol:
                continue
            used_so |= sol
            sol.write({tax_field: [(6, 0, [tax.id])]})
            _logger.info(
                'Impuesto fallback %s → línea SO %s (pub tax / default, orden ML %s, SKU %s)',
                tax.display_name,
                sol.id,
                self.ml_order_id,
                ml_line.sku or '',
            )

    def _ml_build_odoo_order_line_commands(self):
        """Arma comandos (0, 0, vals) de líneas sale.order desde line_ids de esta ml.sale."""
        self.ensure_one()
        pay_suffix = ''
        if self.ml_payment_terms_summary:
            pay_suffix = f"\n[Pago ML: {self.ml_payment_terms_summary}]"
        order_lines = []
        fallback_product = self.ml_account_id.fallback_product_id
        used_fallback_product = False
        for line in self.line_ids:
            product, is_fallback = self._ml_resolve_product_for_sale_line(line, fallback_product)
            if not product:
                if not line.publication_id and not line.product_id and not line.product_tmpl_id:
                    _logger.warning(
                        "⚠️ Línea sin publicación ni producto: %s (SKU: %s, item: %s)",
                        line.name, line.sku, line.ml_item_id,
                    )
                else:
                    _logger.warning(
                        "⚠️ No se pudo resolver producto para línea: %s (publicación: %s)",
                        line.name, line.publication_id.display_name if line.publication_id else 'N/A',
                    )
                continue

            if is_fallback:
                used_fallback_product = True
                _logger.warning(
                    "⚠️ Línea '%s' (SKU: %s). Usando fallback: %s (ID: %s)",
                    line.name, line.sku, product.display_name, product.id,
                )

            bom_components = self._get_kit_components_from_bom(product)
            if bom_components:
                _logger.info(
                    "📦 Kit detectado: '%s' tiene %d componentes en BOM.",
                    product.name, len(bom_components),
                )
            order_line_vals = self._ml_prepare_sale_order_line_vals(
                line, product, is_fallback, pay_suffix=pay_suffix,
            )
            order_lines.append((0, 0, order_line_vals))
        return order_lines, used_fallback_product

    def _ml_append_lines_to_sale_order(self, sale_order, update_stock=True):
        """Agrega las líneas de esta ml.sale a un pedido Odoo ya existente (mismo pack)."""
        self.ensure_one()
        if sale_order.state == 'cancel':
            _logger.warning(
                "⚠️ Pack %s: SO %s cancelada; se creará un pedido nuevo para orden ML %s",
                self.ml_pack_id, sale_order.name, self.ml_order_id,
            )
            return self.with_context(ml_ignore_pack_so=True).create_odoo_sale_order(
                update_stock=update_stock,
            )

        order_lines, used_fallback = self._ml_build_odoo_order_line_commands()
        if not order_lines:
            raise UserError(_(
                'No se pudieron crear líneas de venta para ML %s. '
                'Verifique que los productos estén vinculados.'
            ) % self.ml_order_id)

        was_confirmed = sale_order.state == 'sale'
        if was_confirmed:
            for _cmd, _zero, vals in order_lines:
                line_vals = dict(vals)
                line_vals['order_id'] = sale_order.id
                self.env['sale.order.line'].create(line_vals)
        else:
            sale_order.write({'order_line': order_lines})

        self._ml_apply_fallback_ml_taxes_on_sale_order(sale_order)

        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        pack_orders = self.sudo().search([
            ('ml_pack_id', '=', pack),
            ('active', '=', True),
        ])
        order_ids = pack_orders.mapped('ml_order_id')
        commission = sum(pack_orders.mapped('ml_commission_amount'))
        note_extra = 'Órdenes ML del pack: %s' % ', '.join(order_ids)
        so_vals = {
            'origin': 'MercadoLibre pack #%s' % pack,
            'ml_commission_amount': commission,
            'note': ((sale_order.note or '') + '\n' + note_extra).strip(),
        }
        if 'ml_pack_id' in sale_order._fields:
            so_vals['ml_pack_id'] = pack
        sale_order.write(so_vals)
        self.write({'odoo_sale_order_id': sale_order.id})
        self.env.flush_all()
        _logger.info(
            "✅ Pack %s: líneas de orden ML %s agregadas a SO %s",
            pack, self.ml_order_id, sale_order.name,
        )

        if used_fallback and sale_order.state in ('draft', 'sent'):
            if self._ml_hold_sale_order_for_fallback(used_fallback):
                return sale_order

        if sale_order.state in ('draft', 'sent') and (
            not used_fallback or not self._ml_hold_sale_order_for_fallback(used_fallback)
        ):
            try:
                sale_order.action_confirm()
            except Exception as e:
                _logger.warning(
                    "⚠️ Pack SO %s no se pudo confirmar tras agregar líneas: %s",
                    sale_order.name, e,
                )
                return sale_order

        if not update_stock and sale_order.picking_ids:
            active_pickings = sale_order.picking_ids.filtered(
                lambda p: p.state not in ('cancel', 'done')
            )
            if active_pickings:
                active_pickings.action_cancel()

        if self.ml_account_id.auto_create_invoice and sale_order.state == 'sale':
            pending = self._ml_pack_pending_paid_orders(
                self.ml_account_id, self.ml_pack_id,
            )
            if pending:
                _logger.info(
                    "📦 Pack %s: SO %s confirmada; factura diferida hasta órdenes %s",
                    self.ml_pack_id, sale_order.name, pending,
                )
            else:
                try:
                    to_invoice = sale_order.order_line.filtered(lambda l: l.qty_to_invoice > 0)
                    if to_invoice:
                        self._create_invoice_from_sale_order(sale_order)
                except Exception as e:
                    _logger.error(
                        "❌ Error facturando líneas nuevas del pack en SO %s: %s",
                        sale_order.name, e, exc_info=True,
                    )
        return sale_order

    def create_odoo_sale_order(self, update_stock=True):
        """
        Crea una orden de venta en Odoo desde esta venta de MercadoLibre.

        Si hay ``ml_pack_id``, espera a importar todas las órdenes del pack vía
        GET /packs/{pack_id} y genera un único pedido Odoo (y factura) para el carrito.
        """
        self.ensure_one()

        if self.odoo_sale_order_id:
            _logger.info(
                "⚠️ Ya existe una orden de venta Odoo para ML Order ID %s: %s",
                self.ml_order_id, self.odoo_sale_order_id.name,
            )
            return self.odoo_sale_order_id

        pack = self._ml_normalize_pack_id(self.ml_pack_id)
        if pack and self.ml_pack_id != pack:
            self.ml_pack_id = pack
        if (
            pack
            and not self.env.context.get('ml_ignore_pack_so')
            and not self.env.context.get('ml_pack_building')
        ):
            account = self.ml_account_id
            if not self.env.context.get('ml_skip_pack_sync'):
                self._ml_sync_pack_orders(account, pack, trigger_order_id=self.ml_order_id)
            pending = self._ml_pack_pending_paid_orders(account, pack)
            if pending:
                _logger.info(
                    "📦 Pack %s: esperando órdenes ML %s para pedido Odoo unificado",
                    pack, pending,
                )
                return self.env['sale.order']
            return self._ml_ensure_pack_sale_order(update_stock=update_stock)

        pricelist = self.env['product.pricelist'].search([
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        if not pricelist:
            pricelist = self.env.company.partner_id.property_product_pricelist

        partner = self._ml_resolve_partner_for_odoo_order()

        order_lines, used_fallback_product = self._ml_build_odoo_order_line_commands()

        if not order_lines:
            raise UserError(_('No se pudieron crear líneas de venta. Verifique que los productos estén vinculados correctamente.'))

        account = self.ml_account_id
        if self.is_full and account.has_meli_full and not account.full_warehouse_id:
            raise UserError(
                _(
                    'La venta %s es Mercado Libre Full, pero la cuenta «%s» no tiene configurado '
                    'el almacén para ventas Full. Configúrelo en la ficha de la cuenta ML.'
                )
                % (self.ml_order_id, account.name)
            )
        warehouse = account._get_warehouse_for_ml_sale(is_full=self.is_full)
        if self.is_full and warehouse:
            _logger.info(
                "📦 Venta Full %s: almacén Odoo para stock = %s (ID %s)",
                self.ml_order_id,
                warehouse.name,
                warehouse.id,
            )
        elif not warehouse:
            _logger.warning("No hay almacén configurado. Usando almacén por defecto de la compañía.")
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
        
        # No asignar payment_term_id: el vendedor no cobra en cuotas desde ML (liquidación ML ≠ cuotas del comprador).
        ml_order_url = f"https://api.mercadolibre.com/orders/{self.ml_order_id}"
        note_parts = [
            f"Venta MercadoLibre #{self.ml_order_id}",
            f"Link API ML: {ml_order_url}",
        ]
        if self.ml_pack_id:
            note_parts.append(f"Pack ML (carrito): {self.ml_pack_id}")
        if self.is_full:
            note_parts.append("Logística: Mercado Libre Full (fulfillment).")
            if warehouse:
                note_parts.append(f"Almacén Odoo (stock): {warehouse.name}")
        if used_fallback_product:
            if self.ml_account_id.auto_invoice_with_fallback_product:
                note_parts.append(
                    'INFO: Al menos una línea usa producto fallback; '
                    'se confirma y factura con el título ML en el PDF.'
                )
            else:
                note_parts.append(
                    'ATENCION: Se uso producto fallback/no identificado en al menos una linea. '
                    'Revisar antes de confirmar.'
                )

        origin = (
            f'MercadoLibre pack #{self.ml_pack_id}'
            if self.ml_pack_id
            else f'MercadoLibre #{self.ml_order_id}'
        )

        # Crear orden de venta
        sale_order_vals = {
            'partner_id': partner.id,
            'date_order': self.date_created or fields.Datetime.now(),
            'pricelist_id': pricelist.id if pricelist else False,
            'order_line': order_lines,
            'origin': origin,
            'warehouse_id': warehouse.id if warehouse else False,
            'company_id': self.env.company.id,
            'sale_origin': 'mercadolibre',
            'ml_sale_id': self.id,
            'ml_pack_id': self._ml_normalize_pack_id(self.ml_pack_id) or False,
            'ml_order_payload': self.full_order_data,
            'ml_commission_amount': self.ml_commission_amount,
            'note': '\n'.join(note_parts),
        }

        # Dirección de envío: reutilizar (no crear un hijo nuevo por cada venta).
        shipping_partner = self._ml_get_shipping_partner(partner)
        if shipping_partner and shipping_partner.id != partner.id:
            sale_order_vals['partner_shipping_id'] = shipping_partner.id
        
        sale_order = self.env['sale.order'].create(sale_order_vals)
        
        # Guardar referencia y actualizar campo relacionado
        self.write({
            'odoo_sale_order_id': sale_order.id,
        })
        self.env.flush_all()

        return self._ml_finalize_sale_order(
            sale_order, used_fallback_product, update_stock=update_stock,
        )

    def _create_invoice_from_sale_order(self, sale_order):
        """
        Crea la factura desde la orden de venta y la publica (action_post).
        No registra pagos: la factura queda con importe pendiente de cobro.
        """
        self.ensure_one()

        # Obtener diario de factura desde la configuración
        invoice_journal = self.ml_account_id.invoice_journal_id
        if not invoice_journal:
            # Buscar diario de ventas por defecto
            invoice_journal = self.env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', self.env.company.id)
            ], limit=1)
            if not invoice_journal:
                _logger.warning("⚠️ No se encontró diario de ventas. No se creará factura.")
                return False
        
        # Crear factura directamente desde la orden de venta
        partner = sale_order.partner_id
        if partner:
            self._ml_sync_partner_fiscal_from_billing(partner)

        invoices = sale_order._create_invoices()
        
        if invoices:
            # Tomar la primera factura (normalmente solo hay una)
            invoice = invoices[0] if len(invoices) > 0 else invoices
            
            # Configurar tipo de factura y diario + payload ML
            invoice.write({
                'journal_id': invoice_journal.id,
                'move_type': 'out_invoice',
                'ml_order_payload': self.full_order_data,
                'ml_commission_amount': self.ml_commission_amount,
            })

            # Aplicar cuenta de ingresos configurada en la cuenta ML, si existe.
            income_account = self.ml_account_id.default_income_account_id
            if income_account:
                invoice_lines = invoice.invoice_line_ids.filtered(lambda l: not l.display_type)
                if invoice_lines:
                    invoice_lines.write({'account_id': income_account.id})
                    _logger.info(
                        "✅ Cuenta de ingresos aplicada a factura %s: %s",
                        invoice.name or invoice.id,
                        income_account.display_name,
                    )

            # Validar y publicar la factura (confirmarla)
            # action_post dispara la subida del PDF a Mercado Libre + adjunto en ml.sale.
            invoice.action_post()
            self._ml_register_commission_expense(invoice)
            _logger.info(
                "✅ Factura creada y confirmada: %s (Diario: %s). Sin pago automático.",
                invoice.name,
                invoice_journal.name,
            )
            return invoice
        else:
            _logger.warning("⚠️ No se pudo crear factura para orden de venta: %s", sale_order.name)
            return False

    def _ml_render_invoice_pdf(self, invoice):
        """Genera el PDF de la factura (reportes estándar account)."""
        self.ensure_one()
        Report = self.env['ir.actions.report'].sudo()
        report_refs = (
            'account.account_invoices',
            'account.report_invoice_with_payments',
            'account.report_invoice',
        )
        last_err = None
        for report_ref in report_refs:
            try:
                pdf_content, _ctype = Report._render_qweb_pdf(report_ref, res_ids=invoice.ids)
                if pdf_content:
                    return pdf_content
            except Exception as err:
                last_err = err
                _logger.debug(
                    'ML factura PDF: reporte %s falló para move %s: %s',
                    report_ref, invoice.id, err,
                )
        if last_err:
            _logger.warning(
                '⚠️ No se pudo generar PDF de factura %s para ml.sale %s: %s',
                invoice.name or invoice.id,
                self.ml_order_id,
                last_err,
            )
        return False

    def _ml_attach_invoice_pdf_and_note(self, invoice, sale_order=None):
        """Adjunta el PDF de la factura a la venta ML y deja el nombre en notas.

        Si la SO está compartida por un pack, actualiza todas las ml.sale vinculadas.
        """
        self.ensure_one()
        if not invoice:
            return False

        invoice_name = (invoice.name or invoice.display_name or '').strip() or str(invoice.id)
        note_line = _('Factura: %s') % invoice_name

        targets = self
        so = sale_order or invoice.line_ids.mapped('sale_line_ids.order_id')[:1]
        if so:
            pack_sales = self.env['ml.sale'].sudo().search([
                ('odoo_sale_order_id', '=', so.id),
                ('active', '=', True),
            ])
            if pack_sales:
                targets = pack_sales

        pdf_content = self._ml_render_invoice_pdf(invoice)
        pdf_b64 = base64.b64encode(pdf_content) if pdf_content else False
        filename = '%s.pdf' % invoice_name.replace('/', '-').replace(' ', '_')

        Attachment = self.env['ir.attachment'].sudo()
        for sale in targets:
            notes = (sale.notes or '').strip()
            if note_line not in notes and invoice_name not in notes:
                sale.notes = ('%s\n%s' % (notes, note_line)).strip() if notes else note_line

            if not pdf_b64:
                continue
            existing = Attachment.search([
                ('res_model', '=', 'ml.sale'),
                ('res_id', '=', sale.id),
                ('name', '=', filename),
            ], limit=1)
            vals = {
                'name': filename,
                'type': 'binary',
                'datas': pdf_b64,
                'res_model': 'ml.sale',
                'res_id': sale.id,
                'mimetype': 'application/pdf',
                'description': _('Factura Odoo %s (venta ML %s)') % (invoice_name, sale.ml_order_id),
            }
            if existing:
                existing.write({'datas': pdf_b64, 'description': vals['description']})
            else:
                Attachment.create(vals)
            _logger.info(
                '📎 PDF factura %s adjunto a ml.sale %s (id=%s)',
                invoice_name, sale.ml_order_id, sale.id,
            )
        return True

    def _ml_get_commission_amount(self):
        self.ensure_one()
        commission = float(self.ml_commission_amount or 0.0)
        if commission <= 0 and self.full_order_data:
            try:
                order_data = json.loads(self.full_order_data)
                commission = extract_ml_commission_from_order_data(order_data)
            except (json.JSONDecodeError, TypeError):
                commission = 0.0
        return commission

    def _ml_register_commission_expense(self, invoice):
        """
        Registra la comisión ML como asiento contable de gasto (débito en cuenta de comisiones),
        separado de la factura al cliente — similar a una comisión bancaria.
        """
        self.ensure_one()
        if self.ml_commission_move_id:
            return self.ml_commission_move_id

        commission = self._ml_get_commission_amount()
        if commission <= 0:
            return False

        account_cfg = self.ml_account_id
        commission_account = account_cfg.ml_commission_account_id
        if not commission_account:
            _logger.warning(
                "⚠️ Orden ML %s: comisión %.2f sin registrar "
                "(configure «Cuenta de comisiones ML» en la cuenta Mercado Libre).",
                self.ml_order_id,
                commission,
            )
            return False

        company = invoice.company_id or self.env.company
        journal = account_cfg.payment_journal_id
        if not journal:
            journal = self.env['account.journal'].search([
                ('type', '=', 'general'),
                ('company_id', '=', company.id),
            ], limit=1)
        if not journal:
            _logger.warning(
                "⚠️ Orden ML %s: comisión %.2f sin asiento "
                "(configure «Diario para comisión ML» o un diario misceláneo).",
                self.ml_order_id,
                commission,
            )
            return False

        credit_account = journal.default_account_id
        if not credit_account:
            _logger.warning(
                "⚠️ Orden ML %s: diario %s sin cuenta por defecto para contrapartida de comisión.",
                self.ml_order_id,
                journal.display_name,
            )
            return False

        amount = round(abs(commission), 2)
        move_vals = {
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': invoice.date or fields.Date.context_today(self),
            'ref': _('Comisión ML %s') % self.ml_order_id,
            'line_ids': [
                (0, 0, {
                    'name': _('Comisión Mercado Libre'),
                    'account_id': commission_account.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _('Comisión Mercado Libre'),
                    'account_id': credit_account.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ],
        }
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        self.ml_commission_move_id = move.id
        _logger.info(
            "✅ Asiento de comisión ML %s: %.2f (Dr %s / Cr %s)",
            move.name or move.id,
            amount,
            commission_account.display_name,
            credit_account.display_name,
        )
        return move
    
    @api.depends('line_ids.ml_item_id')
    def _compute_publication_count(self):
        """Cuenta las publicaciones relacionadas a través de ml_item_id en las líneas"""
        for record in self:
            # Obtener todos los ml_item_id únicos de las líneas
            ml_item_ids = record.line_ids.mapped('ml_item_id')
            ml_item_ids = [item_id for item_id in ml_item_ids if item_id]
            
            if ml_item_ids:
                # Buscar publicaciones que coincidan con estos ml_item_id
                publications = self.env['ml.publication'].search([
                    ('ml_item_id', 'in', ml_item_ids),
                    ('ml_account_id', '=', record.ml_account_id.id)
                ])
                record.publication_count = len(publications)
            else:
                record.publication_count = 0
    
    def action_view_publications(self):
        """Abre la publicación vinculada a esta venta"""
        self.ensure_one()
        
        # Obtener todos los ml_item_id únicos de las líneas
        ml_item_ids = self.line_ids.mapped('ml_item_id')
        ml_item_ids = [item_id for item_id in ml_item_ids if item_id]
        
        if not ml_item_ids:
            raise UserError(_('No hay publicaciones relacionadas a esta venta'))
        
        # Buscar publicaciones que coincidan
        publications = self.env['ml.publication'].search([
            ('ml_item_id', 'in', ml_item_ids),
            ('ml_account_id', '=', self.ml_account_id.id)
        ])
        
        if not publications:
            raise UserError(_('No se encontraron publicaciones relacionadas a esta venta'))
        
        # Si hay múltiples publicaciones, usar la primera (más común: una sola publicación)
        publication = publications[0]
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Publicación'),
            'res_model': 'ml.publication',
            'res_id': publication.id,
            'view_mode': 'form',
            'target': 'current',
        }
    
    def action_view_partner(self):
        """Abre el contacto del cliente relacionado"""
        self.ensure_one()
        if not self.odoo_partner_id:
            raise UserError(_('No hay un contacto relacionado a esta venta'))
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Contacto'),
            'res_model': 'res.partner',
            'res_id': self.odoo_partner_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
    
    def action_view_odoo_sale_order(self):
        """Abre la orden de venta de Odoo relacionada"""
        self.ensure_one()
        if not self.odoo_sale_order_id:
            raise UserError(_('No hay una orden de venta de Odoo relacionada'))
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Orden de Venta'),
            'res_model': 'sale.order',
            'res_id': self.odoo_sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

