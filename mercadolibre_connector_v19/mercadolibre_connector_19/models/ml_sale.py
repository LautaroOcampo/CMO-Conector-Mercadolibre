from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime
import requests
import logging
import json
import re
import pytz
import psycopg2
from psycopg2 import errorcodes

_logger = logging.getLogger(__name__)


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


class MLSale(models.Model):
    _name = "ml.sale"
    _description = "Venta de Mercado Libre"
    _rec_name = "ml_order_id"
    _order = 'date_created desc'

    ml_order_id = fields.Char(
        string="ID de venta (ML)", 
        required=True, 
        copy=False,
        index=True,
        help='ID único de la orden en MercadoLibre'
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
    
    full_order_data = fields.Text(
        string='Datos Completos (JSON)',
        help='Todos los datos de la orden en formato JSON para referencia'
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
    
    _sql_constraints = [
        ('ml_order_id_unique', 'unique(ml_order_id, company_id)',
         'Ya existe una venta con este ID de MercadoLibre para esta compañía!')
    ]

    def write(self, vals):
        becoming_cancelled = self.env['ml.sale']
        if vals.get('status') == 'cancelled':
            becoming_cancelled = self.filtered(lambda r: r.status != 'cancelled')
        res = super().write(vals)
        if becoming_cancelled:
            becoming_cancelled._sync_odoo_on_ml_cancelled()
        return res

    def _sync_odoo_on_ml_cancelled(self):
        """
        Cuando ML cancela la orden (status=cancelled en ml.sale):
        - Sin orden Odoo: no hace nada más (el write ya guardó el estado).
        - Con orden Odoo: pickings, facturas, cancelación de venta y actividad según reglas.
        """
        for rec in self:
            sale = rec.odoo_sale_order_id
            if not sale:
                continue
            lines = []
            if sale.state == 'cancel':
                lines.append(_('Orden de venta Odoo %s ya estaba cancelada.') % sale.name)
                rec._ml_cancel_post_activity(sale, lines)
                continue

            pickings = sale.picking_ids
            if any(p.state == 'done' for p in pickings):
                lines.append(
                    _('Pickings: hay al menos una transferencia en estado Hecho (done). '
                      'No se cancela automáticamente la venta ni las facturas; gestionar devolución manualmente.')
                )
                rec._ml_cancel_post_activity(sale, lines)
                continue

            # 1) Pickings cancelables (draft / esperando / listo = assigned; sin done/cancel)
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

            # 2) Facturas (borrador cancelar; publicada → NC en borrador, sin publicar)
            lines.extend(rec._ml_cancel_handle_customer_invoices(sale))

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

            rec._ml_cancel_post_activity(sale, lines)

    def _ml_cancel_handle_customer_invoices(self, sale_order):
        """Facturas de cliente: borrador → cancelar; publicada → reversión en borrador (sin publicar)."""
        self.ensure_one()
        lines = []
        invoices = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )
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
                                _('Nota de crédito en borrador creada: %s.')
                                % (rev.name or str(rev.id))
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

    def _ml_cancel_post_activity(self, sale_order, summary_lines):
        """Actividad tipo tarea en la orden de venta con resumen para el responsable."""
        self.ensure_one()
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
                    title = item.get("item", {}).get("title")
                    quantity = item.get("quantity", 1)
                    sku = item.get("item", {}).get("seller_sku")

                    # Buscar producto en Odoo por SKU
                    product = None
                    if sku:
                        product = self.env["product.template"].search([("default_code", "=", sku)], limit=1)

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
    def update_or_create_from_meli(self, order_id, account_id=None, create_odoo_order=True, update_stock=True, create_customer=True):
        """
        Llama a la API de ML para obtener los datos COMPLETOS del pedido y actualiza o crea la venta.
        Incluye: todos los datos del cliente, dirección, y todos los items de la orden.
        
        Args:
            order_id: ID de la orden en MercadoLibre
            account_id: ID de la cuenta de ML (opcional, se busca automáticamente si no se proporciona)
            create_odoo_order: Si True, crea automáticamente la orden de venta en Odoo y la factura (default: True)
            update_stock: Si True, se descontará el stock al confirmar las órdenes (default: True)
            create_customer: Si True, crea/actualiza el contacto del cliente en Odoo (default: True).
                Si la cuenta tiene auto_create_invoice desactivado, se ignora y no se crean clientes.
        
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
                headers = {'Authorization': f'Bearer {acc.access_token}'}
                _logger.info("   Probando con cuenta ID=%d, Nombre='%s'...", acc.id, acc.name)
                try:
                    response = requests.get(url, headers=headers, timeout=10)
                    _logger.info("   Response status: %s", response.status_code)
                    if response.status_code == 200:
                        account = acc
                        _logger.info("   ✅ Orden obtenida exitosamente con cuenta ID=%d", acc.id)
                        break
                    else:
                        _logger.warning("   ⚠️ Error obteniendo orden: %s (Status: %s)", response.text[:200], response.status_code)
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

        if not account.auto_create_invoice:
            create_customer = False
            _logger.info(
                "ℹ️ create_customer=False (cuenta %s: auto_create_invoice desactivado; no se crea/actualiza contacto).",
                account.name,
            )

        url = f'https://api.mercadolibre.com/orders/{order_id}'
        headers = {'Authorization': f'Bearer {account.access_token}'}
        
        _logger.info("📤 Obteniendo orden desde API de MercadoLibre...")
        _logger.info("   URL: %s", url)
        _logger.info("   Headers: Authorization=Bearer %s...", account.access_token[:20] if account.access_token else 'None')
        
        try:
            sale_env = self.sudo()
            response = requests.get(url, headers=headers, timeout=30)
            _logger.info("📥 Response status: %s", response.status_code)
            _logger.debug("📥 Response headers: %s", dict(response.headers))
            
            response.raise_for_status()
            
            order_data = response.json()
            _logger.info("✅ Orden obtenida exitosamente desde API")
            
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
            buyer_phone = buyer.get('phone', {})
            if isinstance(buyer_phone, dict):
                buyer_phone = buyer_phone.get('number', '') or buyer_phone.get('area_code', '') + buyer_phone.get('number', '')
            
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
            shipping = order_data.get('shipping', {})
            _logger.info("📦 Datos de shipping recibidos: %s", json.dumps(shipping, indent=2, ensure_ascii=False, default=str))
            
            receiver_address = shipping.get('receiver_address', {})
            _logger.info("📦 Datos de receiver_address: %s", json.dumps(receiver_address, indent=2, ensure_ascii=False, default=str))
            
            # Extraer datos de dirección con múltiples formatos posibles
            shipping_street = receiver_address.get('address_line', '') or receiver_address.get('street_name', '') or receiver_address.get('address', '')
            shipping_street_number = receiver_address.get('street_number', '') or receiver_address.get('number', '')
            shipping_floor = receiver_address.get('floor', '')
            shipping_apartment = receiver_address.get('apartment', '') or receiver_address.get('unit', '')
            
            # Ciudad puede venir como dict o string
            city_data = receiver_address.get('city', {})
            if isinstance(city_data, dict):
                shipping_city = city_data.get('name', '') or city_data.get('city_name', '')
            else:
                shipping_city = city_data or ''
            
            # Estado/Provincia puede venir como dict o string
            state_data = receiver_address.get('state', {})
            if isinstance(state_data, dict):
                shipping_state = state_data.get('name', '') or state_data.get('state_name', '')
            else:
                shipping_state = state_data or ''
            
            # País puede venir como dict o string
            country_data = receiver_address.get('country', {})
            if isinstance(country_data, dict):
                shipping_country = country_data.get('name', '') or country_data.get('country_name', '') or country_data.get('id', '')
            else:
                shipping_country = country_data or ''
            
            shipping_zip = receiver_address.get('zip_code', '') or receiver_address.get('zip', '') or receiver_address.get('postal_code', '')
            
            receiver_name = shipping.get('receiver_name', '') or shipping.get('receiver', {}).get('name', '') if isinstance(shipping.get('receiver'), dict) else ''
            receiver_phone = shipping.get('receiver_phone', '') or shipping.get('receiver', {}).get('phone', '') if isinstance(shipping.get('receiver'), dict) else ''
            
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
            
            # =====================================================
            # 4. PREPARAR VALORES PARA LA ORDEN
            # =====================================================
            order_vals = {
                'ml_order_id': ml_order_id,
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
                    sku = item_info.get('seller_sku') or item_info.get('seller_custom_field', '')
                    
                    # Buscar producto en Odoo por SKU
                    product_tmpl = None
                    product_variant = None
                    if sku:
                        # Buscar primero en product.product (variantes)
                        product_variant = self.env['product.product'].search([
                            '|',
                            ('default_code', '=', sku),
                            ('barcode', '=', sku)
                        ], limit=1)
                        if product_variant:
                            product_tmpl = product_variant.product_tmpl_id
                        else:
                            # Si no se encuentra, buscar en product.template
                            product_tmpl = self.env['product.template'].search([
                                '|',
                                ('default_code', '=', sku),
                                ('barcode', '=', sku)
                            ], limit=1)
                    
                    # Usar el precio tal como viene de MercadoLibre (sin descontar IVA)
                    price_unit_final = unit_price
                    
                    line_vals = {
                        'order_id': order.id,
                        'name': title,
                        'ml_item_id': ml_item_id,
                        'quantity': quantity,
                        'price_unit': price_unit_final,
                        'sku': sku,
                        'product_tmpl_id': product_tmpl.id if product_tmpl else False,
                        'product_id': product_variant.id if product_variant else False,
                        'sequence': len(line_vals_list) * 10 + 10,  # Secuencia: 10, 20, 30, ...
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
                    partner = order._get_or_create_customer(allow_create=create_customer)
                    if partner:
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
            if create_odoo_order and not order.odoo_sale_order_id:
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
                        _logger.info("🔄 Creando orden de venta Odoo automáticamente para ML Order ID: %s (update_stock=%s, is_test_sale=%s)", 
                                   ml_order_id, update_stock, is_test_sale)
                        # Solo cancelar pickings si update_stock es False explícitamente (no por ser test)
                        actual_update_stock = update_stock  # Mantener el valor original, no cancelar por ser test
                        order_ctx = dict(self.env.context)
                        if not create_customer:
                            order_ctx['ml_skip_contact_create'] = True
                        order.with_context(**order_ctx).create_odoo_sale_order(update_stock=actual_update_stock)
                        _logger.info("✅ Orden de venta Odoo creada automáticamente: %s", order.odoo_sale_order_id.name if order.odoo_sale_order_id else 'N/A')
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
                        # Buscar la publicación correspondiente a este item
                        publication = self.env['ml.publication'].search([
                            ('ml_item_id', '=', ml_item_id),
                            ('ml_account_id', '=', account.id)
                        ], limit=1)
                        
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

        - Match principal: ``meli_buyer_id`` (ID comprador ML), único.
        - Creación de contacto nuevo: solo si ``allow_create`` y hay nombre, apellido y provincia.
        - Si no hay contacto reconocible y no se puede crear: ``ml.account.default_order_partner_id``.
        """
        self.ensure_one()

        def _normalize(value):
            return re.sub(r'\s+', ' ', (value or '').strip())

        default_partner = self.ml_account_id.default_order_partner_id

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
        if buyer_key:
            partner = self.env['res.partner'].search([
                ('meli_buyer_id', '=', buyer_key),
                '|',
                ('company_id', '=', False),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
        if partner:
            update_vals = {}
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
                "✅ Cliente ML por meli_buyer_id=%s: %s (id=%s)",
                buyer_key,
                partner.name,
                partner.id,
            )
            return partner

        if not allow_create:
            return _use_default('importación sin crear contacto (ml_skip_contact_create)')

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
            'meli_buyer_id': buyer_key or False,
            'is_company': False,
            'category_id': [(6, 0, [meli_category.id])],
            'street': normalized_street or False,
            'street2': ', '.join(street2_parts) if street2_parts else False,
            'city': self.shipping_city or False,
            'zip': self.shipping_zip or False,
        }
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

        partner = self.env['res.partner'].create(partner_vals)
        _logger.info("✅ Cliente ML creado (buyer_id nuevo): %s (id=%s)", partner.name, partner.id)
        self.odoo_partner_id = partner.id
        return partner
    
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

    def create_odoo_sale_order(self, update_stock=True):
        """
        Crea una orden de venta en Odoo desde esta venta de MercadoLibre.
        Incluye creación del cliente, líneas de venta y confirmación.
        
        Args:
            update_stock: Si True, se descontará el stock al confirmar (default: True)
        """
        self.ensure_one()
        
        # Verificar si ya existe una orden de venta
        if self.odoo_sale_order_id:
            _logger.info("⚠️ Ya existe una orden de venta Odoo para ML Order ID %s: %s", 
                        self.ml_order_id, self.odoo_sale_order_id.name)
            return self.odoo_sale_order_id
        
        # Lista de precios: solo la por defecto de la compañía (sin lista configurable en cuenta ML)
        pricelist = self.env['product.pricelist'].search([
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        if not pricelist:
            pricelist = self.env['product.pricelist'].search([], limit=1)
        
        # Crear o buscar cliente (respeta contexto ml_skip_contact_create desde import sin contacto)
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
                "⚠️ Usando contacto por defecto en create_odoo_sale_order para ML Order ID %s: %s (ID: %s)",
                self.ml_order_id, partner.name, partner.id
            )
        
        pay_suffix = ''
        if self.ml_payment_terms_summary:
            pay_suffix = f"\n[Pago ML: {self.ml_payment_terms_summary}]"

        # Crear líneas de venta
        order_lines = []
        fallback_product = self.ml_account_id.fallback_product_id
        used_fallback_product = False
        for line in self.line_ids:
            if not line.product_id and not line.product_tmpl_id:
                if fallback_product:
                    product = fallback_product
                    used_fallback_product = True
                    _logger.warning(
                        "⚠️ Línea sin producto relacionado '%s' (SKU: %s). Usando fallback: %s (ID: %s)",
                        line.name, line.sku, fallback_product.display_name, fallback_product.id
                    )
                else:
                    _logger.warning("⚠️ Línea sin producto relacionado: %s (SKU: %s)", line.name, line.sku)
                    continue
            else:
                # Usar product_id si está disponible, sino usar product_tmpl_id
                product = line.product_id
                if not product and line.product_tmpl_id:
                    # Usar la primera variante del template
                    product = line.product_tmpl_id.product_variant_id
            
            if not product:
                if fallback_product:
                    product = fallback_product
                    used_fallback_product = True
                    _logger.warning(
                        "⚠️ No se pudo resolver producto para línea '%s'. Usando fallback: %s (ID: %s)",
                        line.name, fallback_product.display_name, fallback_product.id
                    )
                else:
                    _logger.warning("⚠️ No se pudo encontrar producto para línea: %s", line.name)
                    continue
            
            # Verificar si el producto es un kit (tiene BOM de tipo phantom)
            bom_components = self._get_kit_components_from_bom(product)
            
            if bom_components:
                # Es un kit: Odoo expandirá automáticamente los componentes al confirmar si tiene BOM phantom
                # Solo creamos la línea del kit, Odoo manejará automáticamente la expansión y el descuento de stock
                _logger.info("📦 Kit detectado: '%s' tiene %d componentes en BOM. Odoo expandirá automáticamente al confirmar.", 
                           product.name, len(bom_components))
                
                # Crear solo la línea del kit (Odoo expandirá los componentes automáticamente)
                kit_order_line_vals = {
                    'product_id': product.id,
                    'product_uom_qty': line.quantity,
                    'price_unit': line.price_unit,  # Precio total del kit
                    'name': (line.name or '') + pay_suffix,
                }
                
                # Agregar impuesto si está configurado
                if self.tax_id:
                    kit_order_line_vals['tax_id'] = [(6, 0, [self.tax_id.id])]
                    _logger.info("🧾 Impuesto agregado a kit: %s (IVA %.2f%%)", line.name, self.tax_rate)
                
                order_lines.append((0, 0, kit_order_line_vals))
                _logger.info("✅ Kit agregado como línea única: %s x %.2f (componentes se expandirán automáticamente)", 
                           line.name, line.quantity)
            else:
                # Producto normal (no kit)
                # Crear línea de venta
                order_line_vals = {
                    'product_id': product.id,
                    'product_uom_qty': line.quantity,
                    'price_unit': line.price_unit,  # Ya viene sin IVA si estaba incluido
                    'name': (line.name or '') + pay_suffix,
                }
                
                # Agregar impuesto si está configurado
                if self.tax_id:
                    order_line_vals['tax_id'] = [(6, 0, [self.tax_id.id])]
                    _logger.info("🧾 Impuesto agregado a línea: %s (IVA %.2f%%)", line.name, self.tax_rate)
                
                order_lines.append((0, 0, order_line_vals))
        
        if not order_lines:
            raise UserError(_('No se pudieron crear líneas de venta. Verifique que los productos estén vinculados correctamente.'))
        
        # Obtener almacén configurado
        warehouse = self.ml_account_id.warehouse_id
        if not warehouse:
            _logger.warning("⚠️ No hay almacén configurado. Usando almacén por defecto de la compañía.")
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
        
        # No asignar payment_term_id: el vendedor no cobra en cuotas desde ML (liquidación ML ≠ cuotas del comprador).
        ml_order_url = f"https://api.mercadolibre.com/orders/{self.ml_order_id}"
        note_parts = [
            f"Venta MercadoLibre #{self.ml_order_id}",
            f"Link API ML: {ml_order_url}",
        ]
        if used_fallback_product:
            note_parts.append("ATENCION: Se uso producto fallback/no identificado en al menos una linea. Revisar antes de confirmar.")

        # Crear orden de venta
        sale_order_vals = {
            'partner_id': partner.id,
            'date_order': self.date_created or fields.Datetime.now(),
            'pricelist_id': pricelist.id if pricelist else False,
            'order_line': order_lines,
            'origin': f'MercadoLibre #{self.ml_order_id}',
            'warehouse_id': warehouse.id if warehouse else False,
            'company_id': self.env.company.id,
            'sale_origin': 'mercadolibre',
            'ml_sale_id': self.id,
            'note': '\n'.join(note_parts),
        }

        # Agregar dirección de envío si está disponible
        if self.shipping_street or self.shipping_city:
            # Crear dirección de envío
            shipping_partner_vals = {
                'parent_id': partner.id,
                'type': 'delivery',
                'name': self.shipping_receiver_name or partner.name,
                'street': self.shipping_street or '',
                'street2': f"{self.shipping_street_number or ''} {self.shipping_floor or ''} {self.shipping_apartment or ''}".strip(),
                'city': self.shipping_city or '',
                'zip': self.shipping_zip or '',
                'phone': self.shipping_receiver_phone or partner.phone or '',
            }
            shipping_partner = self.env['res.partner'].create(shipping_partner_vals)
            sale_order_vals['partner_shipping_id'] = shipping_partner.id
        
        sale_order = self.env['sale.order'].create(sale_order_vals)
        
        # Guardar referencia y actualizar campo relacionado
        self.write({
            'odoo_sale_order_id': sale_order.id,
        })
        
        if used_fallback_product:
            _logger.warning(
                "⚠️ Orden %s creada en borrador porque se usó producto fallback/no identificado. Requiere revisión manual.",
                sale_order.name
            )
            return sale_order

        # Confirmar la orden automáticamente
        try:
            _logger.info("🔄 Confirmando orden de venta: %s (update_stock=%s)", sale_order.name, update_stock)
            sale_order.action_confirm()
            
            # Verificar pickings creados
            pickings = sale_order.picking_ids
            _logger.info("📦 Pickings creados después de confirmar: %d (estados: %s)", 
                        len(pickings), [p.state for p in pickings])
            
            # Si no se debe actualizar stock, cancelar los pickings creados para evitar descuento de stock
            # Nota: Los pickings se cancelan solo si update_stock es False explícitamente
            if not update_stock:
                active_pickings = pickings.filtered(lambda p: p.state != 'cancel')
                if active_pickings:
                    _logger.info("📦 Cancelando %d pickings para evitar descuento de stock (update_stock=False)", len(active_pickings))
                    active_pickings.action_cancel()
                    _logger.info("✅ Pickings cancelados para orden: %s", sale_order.name)
                else:
                    _logger.info("ℹ️ No hay pickings activos para cancelar en orden: %s", sale_order.name)
            else:
                _logger.info("📦 Pickings creados y activos para orden: %s (update_stock=True, total: %d)", 
                           sale_order.name, len(pickings))
            
            _logger.info("✅ Orden de venta Odoo creada y confirmada: %s (update_stock=%s)", sale_order.name, update_stock)
        except Exception as e:
            _logger.warning("⚠️ Orden de venta creada pero no se pudo confirmar: %s", str(e))
            return sale_order
        
        # Crear y confirmar factura automáticamente solo si está configurado
        if self.ml_account_id.auto_create_invoice:
            try:
                self._create_invoice_from_sale_order(sale_order)
            except Exception as e:
                _logger.error("❌ Error creando factura automáticamente: %s", str(e), exc_info=True)
                # No fallar si no se puede crear la factura, la orden ya está confirmada
        else:
            _logger.info("ℹ️ Creación automática de factura deshabilitada para cuenta %s", self.ml_account_id.name)
        
        return sale_order
    
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
        invoices = sale_order._create_invoices()
        
        if invoices:
            # Tomar la primera factura (normalmente solo hay una)
            invoice = invoices[0] if len(invoices) > 0 else invoices
            
            # Configurar tipo de factura y diario
            invoice.write({
                'journal_id': invoice_journal.id,
                'move_type': 'out_invoice',
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
            invoice.action_post()
            _logger.info(
                "✅ Factura creada y confirmada: %s (Diario: %s). Sin pago automático.",
                invoice.name,
                invoice_journal.name,
            )
            return invoice
        else:
            _logger.warning("⚠️ No se pudo crear factura para orden de venta: %s", sale_order.name)
            return False
    
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

