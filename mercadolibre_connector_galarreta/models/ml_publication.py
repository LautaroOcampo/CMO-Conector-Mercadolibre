# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import requests
import base64
import logging
import time
import json
from collections import defaultdict

_logger = logging.getLogger(__name__)


def _ml_domain_or(branches):
    """Combina dominios con OR (sin odoo.osv, compatible Odoo 19)."""
    branches = [list(b) for b in branches if b]
    if not branches:
        return []
    if len(branches) == 1:
        return branches[0]
    domain = ['|'] * (len(branches) - 1)
    for branch in branches:
        domain.extend(branch)
    return domain


class MLPublication(models.Model):
    _name = "ml.publication"
    _description = "Publicación de Mercado Libre"
    _rec_name = "title"

    # =====================================================
    # 🔹 CAMPOS PRINCIPALES
    # =====================================================
    name = fields.Char(string="Nombre interno", required=True)
    title = fields.Char(string="Título en Mercado Libre", required=True)
    description = fields.Text(string="Descripción para Mercado Libre")

    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Producto relacionado",
        ondelete="set null",
    )
    product_variant_id = fields.Many2one(
        "product.product",
        string="Variante relacionada",
        ondelete="set null",
        domain="[('product_tmpl_id', '=', product_tmpl_id)]",
        help="Cuando el producto tiene varias variantes, indique qué variante corresponde a esta publicación (precio y stock se toman de esta variante).",
    )

    ml_item_id = fields.Char(string="ML Item ID", copy=False)
    ml_variation_id = fields.Char(
        string="ID variación ML",
        copy=False,
        help="Si está definido, esta publicación representa una sola variación del ítem en ML. "
             "Permite tener una publicación por variación y matchear cada una por SKU con Odoo.",
    )
    ml_status = fields.Selection([
        ('active', 'Activa'),
        ('paused', 'Pausada'),
        ('closed', 'Cerrada'),
        ('under_review', 'En revisión'),
        ('inactive', 'Inactiva'),
        ('not_yet_active', 'Pendiente de activación'),
        ('payment_required', 'Pago requerido'),
    ], string="Estado en Mercado Libre", default=False)
    ml_sub_status = fields.Char(
        string='Sub-estado ML',
        readonly=True,
        copy=False,
        help='Sub-estados en Mercado Libre (ej. out_of_stock, paused_by_seller).',
    )
    is_full = fields.Boolean(
        string='Mercado Libre Full',
        default=False,
        help=(
            'El ítem en ML tiene logística Full en catálogo (puede convivir con Flex). '
            'No indica que cada venta sea Full; eso se ve en ml.sale.is_full.'
        ),
    )
    permalink = fields.Char(string="Enlace a ML")
    
    state = fields.Char(
        string="Estado",
        compute="_compute_state",
        readonly=True,
        store=False,
        help="Estado de la publicación en Mercado Libre (Publicado, Pausado, o No existe)"
    )
    
    error_log = fields.Text(
        string="Log de Errores",
        help="Registro de errores detallados durante la publicación en Mercado Libre",
        readonly=True,
    )

    ml_account_id = fields.Many2one(
        'ml.account',
        string="Cuenta de Mercado Libre",
        required=True
    )

    sync_log_ids = fields.One2many(
        'ml.sync.log',
        'publication_id',
        string='Historial de sincronización ML',
        readonly=True,
    )
    ml_listing_type = fields.Selection([
        ('gold_special', 'Premium'),
        ('gold_pro', 'Pro'),
    ], string="Tipo de publicación", default='gold_special')

    category_id = fields.Char(string="ID Categoría ML", help="ID de categoría en Mercado Libre (solo lectura al importar desde ML).")

    # =====================================================
    # 🔹 CAMPOS ADICIONALES DE PUBLICACIÓN
    # =====================================================
    local_pickup = fields.Boolean(string="Ofrece retiro en persona", default=False)
    free_shipping = fields.Boolean(string="Envío gratis", default=False, help="Ofrecer envío gratis para esta publicación")
    
    has_universal_code = fields.Boolean(string="Tiene código universal", default=False)
    universal_code = fields.Char(string="Código universal", help="Código universal del producto (ej: EAN, UPC, etc.)")
    
    seller_sku = fields.Char(string="SKU", help="SKU del vendedor (SELLER_SKU) - común a todas las categorías")

    tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto (fallback)',
        domain="[('type_tax_use', '=', 'sale')]",
        help=(
            'Impuesto de venta a usar en pedidos Odoo cuando esta publicación no tiene '
            'producto matcheado (línea con producto fallback). También puede forzarse '
            'aunque haya producto relacionado. Si está vacío, en fallback se usa el '
            'impuesto por defecto de la cuenta ML.'
        ),
    )

    brand = fields.Char(string="Marca", default="Genérica", help="Marca del producto")
    model = fields.Char(string="Modelo", help="Modelo del producto")
    
    warranty_type = fields.Selection([
        ('factory', 'Garantía de fábrica'),
        ('seller', 'Garantía del vendedor'),
        ('none', 'Sin garantía'),
    ], string="Tipo de garantía", default='none')
    
    warranty_days = fields.Integer(
        string="Días de garantía",
        help="Cantidad de días de garantía (solo aplica si el tipo es 'Garantía del vendedor' o 'Garantía de fábrica')",
        default=0,
    )
    
    availability_time = fields.Selection([
        ('1', '1 día'),
        ('2', '2 días'),
        ('3', '3 días'),
        ('4', '4 días'),
        ('5', '5 días'),
        ('6', '6 días'),
        ('7', '7 días'),
        ('10', '10 días'),
        ('15', '15 días'),
        ('20', '20 días'),
        ('30', '30 días'),
    ], string="Tiempo de disponibilidad", default='1', help="Tiempo estimado de disponibilidad del producto")
    
    condition = fields.Selection([
        ('new', 'Nuevo'),
        ('used', 'Usado'),
        ('refurbished', 'Reacondicionado'),
    ], string="Condición", default='new', required=True)

    def _prepare_attribute_commands(self, attrs_data, is_common=False, sequence_start=10):
        """
        Prepara comandos de Odoo para crear atributos en onchange.
        Retorna una lista de comandos [(0, 0, {...}), ...] que se pueden usar en One2many.
        """
        commands = []
        sequence = sequence_start
        
        for attr_data in attrs_data:
            # Validar que ml_attribute_id esté presente y no sea vacío
            ml_attribute_id = attr_data.get('ml_attribute_id')
            if not ml_attribute_id:
                _logger.warning("⚠️ Atributo sin ml_attribute_id, omitiendo: %s", attr_data.get('name', 'Sin nombre'))
                continue
            
            # Preparar valores permitidos como string JSON si es necesario
            allowed_values = attr_data.get('allowed_values')
            if allowed_values and isinstance(allowed_values, dict):
                import json
                allowed_values = json.dumps(allowed_values)
            elif allowed_values and isinstance(allowed_values, str):
                # Ya es string, usar directamente
                pass
            else:
                allowed_values = None
            
            # Asegurar que publication_id no esté en el comando (se asignará automáticamente)
            # ml_attribute_id es obligatorio, así que debe estar presente
            command_vals = {
                "ml_attribute_id": str(ml_attribute_id),  # Asegurar que sea string
                "name": attr_data.get('name', ml_attribute_id),  # Usar ml_attribute_id como fallback
                "value_type": attr_data.get('value_type', 'value_name'),
                "required": attr_data.get('required', False),
                "allowed_values": allowed_values,
                "allowed_units": attr_data.get('allowed_units'),
                "attribute_type": attr_data.get('attribute_type', 'string'),
                "is_common": is_common,
                "sequence": sequence,
            }

            # Si es number_unit e importamos unidades, preseleccionar la primera unidad disponible
            if command_vals["value_type"] == "number_unit":
                allowed_units_raw = attr_data.get('allowed_units')
                if allowed_units_raw:
                    try:
                        import json
                        allowed_units_list = json.loads(allowed_units_raw) if isinstance(allowed_units_raw, str) else allowed_units_raw
                        if allowed_units_list:
                            first_unit = allowed_units_list[0]
                            unit_code = first_unit.get("id") or first_unit.get("symbol") or first_unit.get("name")
                            if unit_code:
                                # Usar el código de unidad directamente (sin ml.attribute.allowed.unit)
                                command_vals["value_unit"] = unit_code
                                _logger.info("✅ Preseleccionada unidad %s para atributo %s desde allowed_units", 
                                          unit_code, ml_attribute_id)
                    except Exception as e:
                        _logger.warning("⚠️ Error preseleccionando unidad para %s: %s", ml_attribute_id, e)
            
            # Incluir value_name si está presente en attr_data
            if 'value_name' in attr_data and attr_data['value_name']:
                command_vals['value_name'] = attr_data['value_name']
            
            command = (0, 0, command_vals)
            commands.append(command)
            sequence += 10
        
        return commands
    
    # =====================================================
    # 🔹 CAMPOS RELACIONADOS DEL PRODUCTO
    # =====================================================
    price = fields.Float(compute="_compute_price", store=True, string="Precio")
    stock = fields.Integer(compute="_compute_stock", store=True)
    
    # Campos para valores actuales en Mercado Libre
    current_price_ml = fields.Float(
        string="Precio Actual en ML",
        readonly=True,
        help="Precio actualmente publicado en Mercado Libre"
    )
    
    current_stock_ml = fields.Integer(
        string="Stock Actual en ML",
        readonly=True,
        help="Stock actualmente publicado en Mercado Libre"
    )
    
    # Precio a enviar a ML: editable; el valor por defecto es siempre el precio actual en ML (current_price_ml)
    new_price_ml = fields.Float(
        string="Precio a enviar a ML",
        digits=(16, 2),
        help="Precio que se usará al sincronizar con Mercado Libre. Por defecto es igual a «Precio actual en ML»; puede modificarse antes de actualizar.",
    )

    new_stock_ml = fields.Integer(
        compute="_compute_new_price_stock_ml",
        string="Stock al Actualizar",
        help="Stock que tendría la publicación al actualizar desde el producto relacionado"
    )
    
    # =====================================================
    # 🔹 REGLAS DE STOCK PERSONALIZADAS
    # =====================================================
    # Campo deprecated - mantener por compatibilidad durante migración
    use_custom_stock_rules = fields.Boolean(
        string='Usar Reglas de Stock Personalizadas (Deprecated)',
        default=False,
        help='DEPRECATED: Usar use_custom_pause_rule y use_custom_activate_rule en su lugar.'
    )
    
    use_custom_pause_rule = fields.Boolean(
        string='Usar Regla Personalizada de Pausa',
        default=False,
        help='Si está activado, esta publicación usará su propia regla de pausa en lugar de la regla global de la cuenta.'
    )
    
    max_stock_to_pause = fields.Integer(
        string='Máximo de Stock para Pausar',
        default=False,
        help='Cuando el stock de esta publicación sea menor o igual a este valor (inclusive) y esté activa, se pausará automáticamente. Si está vacío, se usará el valor global de la cuenta.'
    )
    
    use_custom_activate_rule = fields.Boolean(
        string='Usar Regla Personalizada de Activación',
        default=False,
        help='Si está activado, esta publicación usará su propia regla de activación en lugar de la regla global de la cuenta.'
    )
    
    min_stock_to_activate = fields.Integer(
        string='Mínimo de Stock para Activar',
        default=False,
        help='Cuando el stock de esta publicación sea mayor o igual a este valor (inclusive) y esté pausada, se activará automáticamente. Si está vacío, se usará el valor global de la cuenta.'
    )

    @api.onchange('max_stock_to_pause')
    def _onchange_max_stock_to_pause(self):
        """UX: si el usuario carga un umbral, activar regla personalizada automáticamente."""
        for rec in self:
            if rec.max_stock_to_pause is not False:
                rec.use_custom_pause_rule = True

    @api.onchange('min_stock_to_activate')
    def _onchange_min_stock_to_activate(self):
        """UX: si el usuario carga un umbral, activar regla personalizada automáticamente."""
        for rec in self:
            if rec.min_stock_to_activate is not False and int(rec.min_stock_to_activate or 0) > 0:
                rec.use_custom_activate_rule = True

    @api.onchange('use_custom_pause_rule')
    def _onchange_use_custom_pause_rule(self):
        """Si desmarca la regla, limpiar el umbral para evitar confusión."""
        for rec in self:
            if not rec.use_custom_pause_rule:
                rec.max_stock_to_pause = False

    @api.onchange('use_custom_activate_rule')
    def _onchange_use_custom_activate_rule(self):
        """Si desmarca la regla, limpiar el umbral para evitar confusión."""
        for rec in self:
            if not rec.use_custom_activate_rule:
                rec.min_stock_to_activate = False
    
    currency_id = fields.Many2one(
        'res.currency',
        string='Moneda',
        compute='_compute_currency_id',
        readonly=True,
        store=False,
        help='Moneda de la cuenta de Mercado Libre'
    )
    
    @api.depends('ml_account_id')
    def _compute_currency_id(self):
        """Calcula la moneda desde la compañía actual"""
        for pub in self:
            pub.currency_id = self.env.company.currency_id
    main_image = fields.Image(related="product_tmpl_id.image_1920", store=False)
    ml_image_ids = fields.One2many('ml.publication.image', 'publication_id', string="Imágenes de la publicación")
    ml_attribute_ids = fields.One2many(
        'ml.publication.attribute', 
        'publication_id', 
        string="Atributos de Mercado Libre",
        copy=False,
    )
    
    ml_variant_ids = fields.One2many(
        'ml.publication.variant',
        'publication_id',
        string="Variantes",
        help="Variantes del producto para Mercado Libre (si el producto tiene variantes)",
        copy=False,
    )

    last_webhook_sync = fields.Datetime(
        string="Última sincronización por webhook",
        copy=False,
        help="Se actualiza cada vez que un webhook de Mercado Libre sincroniza esta publicación."
    )
    
    # Campo para forzar refresco de atributos cuando cambia la categoría
    
    # Campo para detectar si hay variantes
    has_variants = fields.Boolean(compute='_compute_has_variants', store=False, string="Tiene variantes")
    
    # Campos para separar atributos comunes y específicos
    common_attribute_ids = fields.One2many(
        'ml.publication.attribute', 
        'publication_id', 
        string="Atributos Comunes",
        domain=[('is_common', '=', True)],
        copy=False,
    )
    category_attribute_ids = fields.One2many(
        'ml.publication.attribute', 
        'publication_id', 
        string="Atributos de la Categoría",
        domain=[('is_common', '=', False)],
        copy=False,
    )
    
    @api.depends('ml_status', 'ml_item_id')
    def _compute_state(self):
        """Calcula el estado legible de la publicación."""
        for record in self:
            if not record.ml_item_id:
                record.state = "No existe"
            elif record.ml_status == 'active':
                record.state = "Publicado"
            elif record.ml_status == 'paused':
                record.state = "Pausado"
            elif record.ml_status in ['closed', 'inactive']:
                record.state = "No existe"
            else:
                # Para otros estados (under_review, not_yet_active, payment_required)
                # Mostrar el estado original o "No existe" si no está claro
                record.state = "No existe"
    
    @api.depends('product_tmpl_id', 'ml_variant_ids')
    def _compute_has_variants(self):
        """Campo computed para detectar si el producto tiene variantes."""
        for record in self:
            if record.product_tmpl_id:
                # Verificar si el producto tiene más de una variante
                variants_count = len(record.product_tmpl_id.product_variant_ids)
                record.has_variants = variants_count > 1 or len(record.ml_variant_ids) > 0
            else:
                record.has_variants = len(record.ml_variant_ids) > 0

    has_product_variants = fields.Boolean(
        compute="_compute_has_product_variants",
        string="Producto con variantes",
        store=False,
    )

    @api.depends("product_tmpl_id", "product_tmpl_id.product_variant_ids")
    def _compute_has_product_variants(self):
        """True si el producto relacionado tiene más de una variante (para mostrar selector de variante)."""
        for record in self:
            record.has_product_variants = bool(
                record.product_tmpl_id and len(record.product_tmpl_id.product_variant_ids) > 1
            )

    # =====================================================
    # 🔹 COMPUTE FIELDS
    # =====================================================
    @api.depends(
        "product_tmpl_id",
        "product_variant_id",
        "product_variant_id.qty_available",
        "product_variant_id.virtual_available",
        "ml_account_id.warehouse_id",
        "ml_account_id.stock_type",
        "product_tmpl_id.qty_available",
        "product_tmpl_id.virtual_available",
        "product_tmpl_id.product_variant_ids",
        "current_stock_ml",
        "ml_variation_id",
    )
    def _compute_stock(self):
        """Stock en Odoo por publicación. Optimizado: BOM en batch + agregación de stock.quant (sin N+1)."""
        pubs_with_tmpl = self.filtered(lambda p: p.product_tmpl_id)
        for pub in self - pubs_with_tmpl:
            pub.stock = int(pub.current_stock_ml or 0)

        if not pubs_with_tmpl:
            return

        accounts = pubs_with_tmpl.mapped('ml_account_id')
        warehouses = accounts.mapped('warehouse_id').filtered(lambda w: w)
        lot_stock_locs = warehouses.mapped('lot_stock_id').filtered(lambda l: l)

        tmpl_ids = list(set(pubs_with_tmpl.mapped('product_tmpl_id').ids))
        tmpls = self.env['product.template'].browse(tmpl_ids)
        tmpl_default_variant = {t.id: t.product_variant_id.id for t in tmpls}

        variant_by_pub = {}
        variant_ids = set()
        for pub in pubs_with_tmpl:
            vid = pub.product_variant_id.id or tmpl_default_variant.get(pub.product_tmpl_id.id)
            if vid:
                variant_by_pub[pub.id] = vid
                variant_ids.add(vid)

        products_for_bom = self.env['product.product'].browse(list(variant_ids))
        kit_lines_by_variant = self._get_kit_components_from_bom_batch(products_for_bom)

        component_ids = set()
        for lines in kit_lines_by_variant.values():
            for line in lines:
                if line.product_id:
                    component_ids.add(line.product_id.id)

        all_product_ids = set(variant_ids) | component_ids
        if not all_product_ids:
            for pub in pubs_with_tmpl:
                pub.stock = 0
            return

        loc_domain_branches = [[('location_id.usage', '=', 'internal')]]
        for loc in lot_stock_locs:
            loc_domain_branches.append([('location_id', 'child_of', loc.id)])
        loc_domain = loc_domain_branches[0] if len(loc_domain_branches) == 1 else _ml_domain_or(loc_domain_branches)

        qty_internal, qty_by_wh_lot = self._ml_stock_aggregate_quantities(
            list(all_product_ids), loc_domain, lot_stock_locs
        )

        virt_by_loc_product = {}
        pubs_expected = [p for p in pubs_with_tmpl if (p.ml_account_id.stock_type if p.ml_account_id else 'available') == 'expected']
        if pubs_expected:
            virt_needed = defaultdict(set)
            for pub in pubs_expected:
                wh = pub.ml_account_id.warehouse_id if pub.ml_account_id else None
                lot = wh.lot_stock_id if wh else None
                loc_key = lot.id if lot else False
                vid = variant_by_pub.get(pub.id)
                if vid:
                    virt_needed[loc_key].add(vid)
                    for line in kit_lines_by_variant.get(vid, ()):
                        if line.product_id:
                            virt_needed[loc_key].add(line.product_id.id)
            Product = self.env['product.product']
            for loc_key, pids in virt_needed.items():
                if not pids:
                    continue
                prods = Product.browse(list(pids))
                ctx = {'location': loc_key} if loc_key else {}
                for row in prods.with_context(**ctx).read(['virtual_available']):
                    virt_by_loc_product[(loc_key, row['id'])] = row['virtual_available'] or 0.0

        def _qty_available_for_product(product_id, lot_root):
            if lot_root:
                return float(qty_by_wh_lot.get((lot_root.id, product_id), 0.0))
            return float(qty_internal.get(product_id, 0.0))

        def _qty_expected_for_product(product_id, loc_key):
            return float(virt_by_loc_product.get((loc_key, product_id), 0.0))

        products_cached = self.env['product.product'].browse(list(all_product_ids))

        for pub in pubs_with_tmpl:
            try:
                stock_type = pub.ml_account_id.stock_type if pub.ml_account_id else 'available'
                use_expected = stock_type == 'expected'
                vid = variant_by_pub.get(pub.id)
                if not vid:
                    pub.stock = 0
                    continue
                product_variant = products_cached.browse(vid)
                wh = pub.ml_account_id.warehouse_id if pub.ml_account_id else None
                lot_root = wh.lot_stock_id if wh else None
                loc_key = lot_root.id if lot_root else False

                bom_components = kit_lines_by_variant.get(vid, ())

                if bom_components:
                    _logger.debug(
                        "📦 Kit al calcular stock: '%s' (%d componentes, %s)",
                        pub.product_tmpl_id.name,
                        len(bom_components),
                        stock_type,
                    )
                    kit_stock = float('inf')
                    for bom_line in bom_components:
                        comp_product = bom_line.product_id
                        if not comp_product:
                            continue
                        if use_expected:
                            comp_stock = int(_qty_expected_for_product(comp_product.id, loc_key))
                        else:
                            comp_stock = int(_qty_available_for_product(comp_product.id, lot_root))
                        if bom_line.product_qty > 0:
                            kits_posibles = int(comp_stock / bom_line.product_qty)
                            kit_stock = min(kit_stock, kits_posibles)
                            _logger.debug(
                                "   Componente '%s': stock=%d (%s), qty_bom=%.2f, kits=%d",
                                comp_product.name,
                                comp_stock,
                                stock_type,
                                bom_line.product_qty,
                                kits_posibles,
                            )
                    pub.stock = 0 if kit_stock == float('inf') else max(0, int(kit_stock))
                    _logger.debug("📦 Stock del kit: %d (%s)", pub.stock, stock_type)
                else:
                    if use_expected:
                        virtual_stock = int(_qty_expected_for_product(product_variant.id, loc_key))
                        pub.stock = max(0, virtual_stock)
                        _logger.debug(
                            "🔍 Stock esperado (virtual_available, loc=%s): %d → %d",
                            loc_key,
                            virtual_stock,
                            pub.stock,
                        )
                    else:
                        stock_sum = int(_qty_available_for_product(product_variant.id, lot_root))
                        pub.stock = max(0, stock_sum)
                        _logger.debug(
                            "🔍 Stock disponible (quants agregados, almacén=%s): %d",
                            lot_root.name if lot_root else '—',
                            pub.stock,
                        )
            except Exception as e:
                _logger.warning("⚠️ Error calculando stock para publicación %s: %s", pub.id, e)
                pub.stock = 0

    def _ml_stock_aggregate_quantities(self, product_ids, loc_domain, lot_stock_locs):
        """Un read_group sobre stock.quant: cantidades por producto (interno global y por ubicación de almacén)."""
        qty_internal = defaultdict(float)
        qty_by_wh_lot = defaultdict(float)
        if not product_ids:
            return qty_internal, qty_by_wh_lot

        Quant = self.env['stock.quant']
        domain = [('product_id', 'in', product_ids)] + list(loc_domain)
        groups = Quant.read_group(domain, ['quantity:sum'], ['product_id', 'location_id'], lazy=False)

        root_paths = {loc.id: (loc.parent_path or '') for loc in lot_stock_locs}

        loc_ids = set()
        for row in groups:
            loc_tuple = row.get('location_id')
            if loc_tuple and loc_tuple[0]:
                loc_ids.add(loc_tuple[0])
        loc_meta = {}
        if loc_ids:
            for loc in self.env['stock.location'].browse(list(loc_ids)):
                loc_meta[loc.id] = {
                    'path': loc.parent_path or '',
                    'usage': loc.usage,
                }

        def _best_root_for_location(loc_id):
            if loc_id in root_paths:
                return loc_id
            path = loc_meta.get(loc_id, {}).get('path', '')
            if not path:
                return None
            best_rid = None
            best_len = -1
            for rid, rpath in root_paths.items():
                if rpath and path.startswith(rpath) and len(rpath) > best_len:
                    best_len = len(rpath)
                    best_rid = rid
            return best_rid

        for row in groups:
            pid_tuple = row.get('product_id')
            if not pid_tuple or not pid_tuple[0]:
                continue
            pid = pid_tuple[0]
            loc_tuple = row.get('location_id')
            loc_id = loc_tuple[0] if loc_tuple else None
            qty = row.get('quantity_sum')
            if qty is None:
                qty = row.get('quantity', 0) or 0

            meta = loc_meta.get(loc_id) if loc_id else None
            if meta and meta.get('usage') == 'internal':
                qty_internal[pid] += qty

            rid = _best_root_for_location(loc_id) if loc_id else None
            if rid is not None:
                qty_by_wh_lot[(rid, pid)] += qty

        return qty_internal, qty_by_wh_lot

    def _get_kit_components_from_bom_batch(self, products):
        """Una búsqueda de BOM phantom para varios product.product; devuelve {variant_id: bom_line recordset}."""
        if not products or 'mrp.bom' not in self.env:
            return {}
        products = products.filtered(lambda p: p)
        if not products:
            return {}

        company = self.env.company
        variant_ids = products.ids
        tmpl_ids = list(set(products.mapped('product_tmpl_id').ids))
        Bom = self.env['mrp.bom']
        domain = [
            ('type', '=', 'phantom'),
            ('company_id', 'in', [False, company.id]),
            '|',
            ('product_id', 'in', variant_ids),
            '&',
            ('product_id', '=', False),
            ('product_tmpl_id', 'in', tmpl_ids),
        ]
        boms = Bom.search(domain, order='sequence, id')

        bom_by_pid = defaultdict(list)
        bom_by_tmpl = defaultdict(list)
        for bom in boms:
            if bom.product_id:
                bom_by_pid[bom.product_id.id].append(bom)
            elif bom.product_tmpl_id:
                bom_by_tmpl[bom.product_tmpl_id.id].append(bom)

        result = {}
        for p in products:
            chosen = None
            if bom_by_pid.get(p.id):
                chosen = bom_by_pid[p.id][0]
            elif bom_by_tmpl.get(p.product_tmpl_id.id):
                chosen = bom_by_tmpl[p.product_tmpl_id.id][0]
            if chosen:
                result[p.id] = chosen.bom_line_ids
        return result

    def _get_kit_components_from_bom(self, product):
        """
        Obtiene los componentes de un kit desde el BOM (Bill of Materials) de Odoo.

        Args:
            product: product.product - Producto a verificar si es kit

        Returns:
            recordset o lista vacía de líneas BOM (mrp.bom.line) si aplica.
        """
        try:
            if not product or 'mrp.bom' not in self.env:
                return []
            batch = self._get_kit_components_from_bom_batch(product)
            lines = batch.get(product.id)
            if lines:
                return lines
            return []
        except Exception as e:
            _logger.warning("⚠️ Error verificando BOM para producto %s: %s", product.name, str(e))
            return []
    
    @api.depends("current_price_ml")
    def _compute_price(self):
        """Precio de la publicación: solo el de Mercado Libre. Nunca el list_price del producto."""
        for pub in self:
            pub.price = float(pub.current_price_ml or 0.0)

    def _get_price_for_ml_export(self):
        """
        Precio permitido para altas en MercadoLibre (nunca list_price de Odoo).
        Usar new_price_ml o current_price_ml (importado / wizard).
        """
        self.ensure_one()
        return max(0.0, float(self.new_price_ml or self.current_price_ml or 0))

    @staticmethod
    def _strip_price_from_ml_payload(payload):
        """Quita precio del payload API; el precio solo se envía vía wizard Actualizar valores."""
        if not payload:
            return payload
        payload.pop("price", None)
        for variation in payload.get("variations") or []:
            if isinstance(variation, dict):
                variation.pop("price", None)
        return payload

    @api.depends("ml_account_id.warehouse_id", "ml_account_id.stock_type",
                 "product_tmpl_id.qty_available", "product_tmpl_id.virtual_available", "stock")
    def _compute_new_price_stock_ml(self):
        """Solo stock sugerido al actualizar; el precio a enviar es el campo editable new_price_ml."""
        for pub in self:
            try:
                pub.new_stock_ml = pub.stock
            except Exception as e:
                _logger.warning("⚠️ Error calculando nuevo stock ML para publicación %s: %s", pub.id, e)
                pub.new_stock_ml = 0

    @api.onchange("current_price_ml")
    def _onchange_current_price_ml_default_new_price(self):
        """El precio a enviar sigue al precio actual en ML (registro nuevo o aún sin precio a enviar)."""
        for rec in self:
            if (rec.current_price_ml or 0) <= 0:
                continue
            if not rec.id or (rec.new_price_ml or 0) == 0:
                rec.new_price_ml = float(rec.current_price_ml)

    def _set_default_new_price_from_current_ml(self):
        """Iguala new_price_ml a current_price_ml cuando ML tiene precio (p. ej. tras importar / refrescar)."""
        for rec in self:
            cp = float(rec.current_price_ml or 0)
            if cp > 0:
                rec.new_price_ml = cp

    @api.onchange("product_tmpl_id")
    def _onchange_product_tmpl_id(self):
        """Actualizar automáticamente valores desde el producto: stock, precio, SKU, marca, modelo y variantes"""
        if self.product_tmpl_id:
            # Variante: si solo hay una, asignarla; si hay varias, dejar que el usuario elija (limpiar si no pertenece al template)
            variants = self.product_tmpl_id.product_variant_ids
            if len(variants) == 1:
                self.product_variant_id = variants
            elif self.product_variant_id and self.product_variant_id.product_tmpl_id != self.product_tmpl_id:
                self.product_variant_id = False
            elif len(variants) > 1 and not self.product_variant_id:
                pass  # usuario debe elegir

            # El precio de la publicación no se toma del producto de Odoo.
            # 2. Actualizar STOCK (se actualizará automáticamente por _compute_stock, pero forzamos recálculo)
            # El campo stock es computed y se actualizará automáticamente
            
            # 3. Actualizar SKU desde default_code o barcode
            try:
                product_variant = self.product_variant_id or self.product_tmpl_id.product_variant_id
                new_sku = None
                
                if product_variant:
                    new_sku = product_variant.default_code or product_variant.barcode
                else:
                    new_sku = self.product_tmpl_id.default_code or self.product_tmpl_id.barcode
                
                if new_sku:
                    new_sku = str(new_sku).strip()
                    if new_sku and new_sku != 'False' and new_sku != 'None':
                        self.seller_sku = new_sku
                        _logger.info("📝 SKU actualizado desde producto: %s", new_sku)
            except Exception as e:
                _logger.warning("⚠️ Error actualizando SKU: %s", str(e))
            
            # 4. Obtener valores de marca y modelo desde el producto
            brand_value = None
            if hasattr(self.product_tmpl_id, "product_brand_id") and self.product_tmpl_id.product_brand_id:
                brand_field = self.product_tmpl_id.product_brand_id
                brand_value = getattr(brand_field, "name", None) or "Genérica"
            else:
                brand_value = "Genérica"
            
            model_value = self.product_tmpl_id.default_code or self.product_tmpl_id.name or ""
            
            # 5. Actualizar atributos BRAND y MODEL si existen
            # Usar filtered() para obtener solo los atributos específicos y evitar problemas con múltiples registros
            if self.ml_attribute_ids:
                brand_attrs = self.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == "BRAND" and not a.value_name and not a.value_id)
                if brand_attrs:
                    # Si hay múltiples, actualizar solo el primero
                    brand_attrs[0].value_name = brand_value
                    _logger.info("✅ Atributo BRAND actualizado desde producto: %s", brand_value)
                
                model_attrs = self.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == "MODEL" and not a.value_name and not a.value_id)
                if model_attrs:
                    # Si hay múltiples, actualizar solo el primero
                    model_attrs[0].value_name = model_value
                    _logger.info("✅ Atributo MODEL actualizado desde producto: %s", model_value)
            
            # 6. Cargar variantes si el producto tiene variantes
            if self.id:
                # Si el registro ya existe, cargar variantes directamente
                self._load_variants_from_product()
            else:
                # Para registros nuevos, preparar comandos de variantes SIN atributos anidados
                # Los atributos se cargarán después de guardar para evitar problemas con comandos One2many anidados
                variant_commands = self._prepare_variants_commands_simple()
                if variant_commands:
                    self.ml_variant_ids = variant_commands
                    _logger.info("🔄 Variantes preparadas para registro nuevo: %d variantes (sin atributos anidados)", len(variant_commands))

    @api.onchange("product_variant_id")
    def _onchange_product_variant_id(self):
        """Al elegir una variante, actualizar el SKU. El precio de la publicación no cambia."""
        if not self.product_tmpl_id or not self.product_variant_id:
            return
        product = self.product_variant_id
        new_sku = product.default_code or product.barcode
        if new_sku:
            new_sku = str(new_sku).strip()
            if new_sku and new_sku not in ('False', 'None'):
                self.seller_sku = new_sku

    # =====================================================
    # 🔹 MÉTODO PRINCIPAL DE PUBLICACIÓN / SINCRONIZACIÓN
    # =====================================================
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info("🔍 ml.publication.create() llamado con %d registros", len(vals_list))
        # Logs de diagnóstico de reglas personalizadas (solo si vienen en vals)
        for i, v in enumerate(vals_list):
            rule_keys = ("use_custom_pause_rule", "max_stock_to_pause", "use_custom_activate_rule", "min_stock_to_activate")
            if any(k in (v or {}) for k in rule_keys):
                _logger.info(
                    "🧪 ml.publication.create vals[%s] reglas=%s",
                    i,
                    {k: (v or {}).get(k) for k in rule_keys if k in (v or {})},
                )
        prepared_vals = [self._normalize_stock_rule_vals(self._prepare_category_vals(vals)) for vals in vals_list]
        for vals in prepared_vals:
            cp = float(vals.get('current_price_ml') or 0)
            np = vals.get('new_price_ml', None)
            np_is_empty = np is None or float(np or 0) == 0
            if cp > 0 and np_is_empty:
                vals['new_price_ml'] = cp

        # Limpiar comandos de atributos inválidos y duplicados antes de crear
        for idx, vals in enumerate(prepared_vals):
            if 'ml_attribute_ids' in vals:
                _logger.info("🔍 Procesando ml_attribute_ids en create: %d comandos", len(vals.get('ml_attribute_ids', [])))
                # Filtrar comandos (0, 0, {...}) que no tengan ml_attribute_id válido
                valid_commands = []
                seen_attr_ids = set()  # Para evitar duplicados
                
                # Crear un diccionario temporal para mapear sequence -> ml_attribute_id y name -> ml_attribute_id
                # Esto nos ayudará a recuperar ml_attribute_id cuando falte
                sequence_to_attr_id = {}
                name_to_attr_id = {}
                
                # Primera pasada: recopilar todos los ml_attribute_id disponibles de los comandos
                for cmd in vals['ml_attribute_ids']:
                    cmd_type = cmd[0]
                    if cmd_type == 0:  # Comando create
                        cmd_vals = cmd[2]
                        ml_attr_id = cmd_vals.get('ml_attribute_id')
                        sequence = cmd_vals.get('sequence')
                        name = cmd_vals.get('name')
                        if ml_attr_id:
                            ml_attr_id_str = str(ml_attr_id).strip()
                            if sequence:
                                sequence_to_attr_id[sequence] = ml_attr_id_str
                            if name:
                                name_to_attr_id[name] = ml_attr_id_str
                
                # Construir mapeo desde la categoría si hay comandos sin ml_attribute_id
                # Primero, verificar si hay comandos sin ml_attribute_id
                has_commands_without_id = False
                command_sequences = {}
                for cmd in vals['ml_attribute_ids']:
                    cmd_type = cmd[0]
                    if cmd_type == 0:  # Comando create
                        cmd_vals = cmd[2]
                        ml_attr_id = cmd_vals.get('ml_attribute_id')
                        sequence = cmd_vals.get('sequence')
                        name = cmd_vals.get('name')
                        if not ml_attr_id or not str(ml_attr_id).strip():
                            has_commands_without_id = True
                        if sequence:
                            command_sequences[sequence] = {
                                'name': name,
                                'value_name': cmd_vals.get('value_name'),
                                'has_ml_attr_id': bool(ml_attr_id and str(ml_attr_id).strip()),
                            }
                
                # Si hay comandos sin ml_attribute_id o el mapeo está vacío, construir desde la categoría
                if has_commands_without_id or (not sequence_to_attr_id and not name_to_attr_id):
                    _logger.info("🔍 Construyendo mapeo desde categoría (comandos sin ml_attribute_id: %s, mapeo vacío: %s)...", 
                               has_commands_without_id, not sequence_to_attr_id and not name_to_attr_id)
                    # Sin sección de categorías: mapeo por nombre desde comandos solo
                    try:
                        for seq, cmd_data in command_sequences.items():
                            name = (cmd_data.get('name') or '').strip()
                            attr_id = (cmd_data.get('ml_attribute_id') or '').strip()
                            if name and attr_id:
                                name_to_attr_id[name] = attr_id
                                sequence_to_attr_id[seq] = attr_id
                    except Exception as e:
                        _logger.warning("⚠️ Error construyendo mapeo de atributos: %s", e)
                
                # Segunda pasada: procesar comandos y recuperar ml_attribute_id si falta
                for cmd in vals['ml_attribute_ids']:
                    cmd_type = cmd[0]
                    
                    if cmd_type == 0:  # Comando create
                        cmd_vals = cmd[2]
                        ml_attr_id = cmd_vals.get('ml_attribute_id')
                        sequence = cmd_vals.get('sequence')
                        name = cmd_vals.get('name')
                        
                        # Si no tiene ml_attribute_id, intentar recuperarlo desde el mapeo
                        if not ml_attr_id or not str(ml_attr_id).strip():
                            # Intentar por sequence primero
                            if sequence and sequence in sequence_to_attr_id:
                                ml_attr_id = sequence_to_attr_id[sequence]
                                _logger.info("🔍 Recuperado ml_attribute_id=%s desde mapeo por sequence=%s", 
                                           ml_attr_id, sequence)
                            # Si no se encontró por sequence, intentar por name
                            elif name and name in name_to_attr_id:
                                ml_attr_id = name_to_attr_id[name]
                                _logger.info("🔍 Recuperado ml_attribute_id=%s desde mapeo por name=%s", 
                                           ml_attr_id, name)
                            elif sequence:
                                _logger.debug("🔍 ml_attribute_id no recuperado para sequence=%s (sin categoría)", sequence)
                            
                                if not ml_attr_id or not str(ml_attr_id).strip():
                                    _logger.warning("⚠️ Omitiendo comando de atributo sin ml_attribute_id válido: sequence=%s, name=%s", 
                                                  sequence, name)
                                    continue
                            else:
                                _logger.warning("⚠️ Omitiendo comando de atributo sin ml_attribute_id válido: sequence=%s, name=%s", 
                                              sequence, name)
                                continue
                        
                        # Convertir a string y limpiar
                        ml_attr_id_str = str(ml_attr_id).strip()
                        
                        # Verificar duplicados
                        if ml_attr_id_str in seen_attr_ids:
                            _logger.warning("⚠️ Omitiendo atributo duplicado: %s (ml_attribute_id=%s)", 
                                          cmd_vals.get('name', 'Sin nombre'), ml_attr_id_str)
                            continue
                        
                        seen_attr_ids.add(ml_attr_id_str)
                        
                        # Asegurar que ml_attribute_id esté en el comando
                        cmd_vals['ml_attribute_id'] = ml_attr_id_str
                        
                        # Asegurar que name esté presente
                        if not cmd_vals.get('name'):
                            cmd_vals['name'] = ml_attr_id_str
                        
                        valid_commands.append((0, 0, cmd_vals))
                    elif cmd_type == 1:  # Comando update (1, id, {...})
                        # Para comandos de actualización, obtener el registro y asegurar que tenga ml_attribute_id
                        attr_id = cmd[1]
                        cmd_vals = cmd[2]
                        
                        # Buscar el atributo existente para obtener su ml_attribute_id si no está en vals
                        attr_record = self.env['ml.publication.attribute'].browse(attr_id)
                        if attr_record.exists() and attr_record.ml_attribute_id:
                            # Si el comando no incluye ml_attribute_id, agregarlo desde el registro existente
                            if 'ml_attribute_id' not in cmd_vals:
                                cmd_vals['ml_attribute_id'] = attr_record.ml_attribute_id
                            
                            # IMPORTANTE: Si 'name' está en cmd_vals pero es igual a ml_attribute_id,
                            # y el registro existente tiene un name personalizado, NO actualizar el name
                            # Esto evita que nombres personalizados se cambien a ml_attribute_id
                            if 'name' in cmd_vals:
                                new_name = cmd_vals.get('name', '').strip()
                                current_name = (attr_record.name or '').strip()
                                ml_attr_id_str = str(attr_record.ml_attribute_id or '').strip()
                                
                                # Si el nuevo name es igual a ml_attribute_id y el actual es diferente (personalizado),
                                # NO actualizar el name - preservar el nombre personalizado
                                if new_name == ml_attr_id_str and current_name != ml_attr_id_str and current_name:
                                    _logger.debug("🔒 Preservando name personalizado '%s' (intento de cambio a ml_attribute_id '%s')", 
                                                current_name, new_name)
                                    # Eliminar 'name' de cmd_vals para preservar el valor existente
                                    cmd_vals.pop('name', None)
                                elif not new_name and current_name:
                                    # Si el nuevo name está vacío pero el actual tiene valor, preservar el actual
                                    _logger.debug("🔒 Preservando name existente '%s' (nuevo name vacío)", current_name)
                                    cmd_vals.pop('name', None)
                            # Si 'name' no está en cmd_vals, NO agregarlo - Odoo preservará el valor existente
                        
                        valid_commands.append((1, attr_id, cmd_vals))
                    else:
                        # Mantener otros comandos (2, 3, 4, 5, 6)
                        valid_commands.append(cmd)
                
                vals['ml_attribute_ids'] = valid_commands
                _logger.info("✅ Comandos de atributos validados: %d válidos de %d originales", 
                           len(valid_commands), len(vals.get('ml_attribute_ids', [])))
        
        # Procesar comandos de variantes del onchange - validar que tengan product_variant_id
        # IMPORTANTE: Eliminar TODOS los comandos de variantes del onchange - se cargarán después de crear
        # Esto evita problemas con comandos mal formados que pueden tener product_variant_id NULL
        for idx, vals in enumerate(prepared_vals):
            if 'ml_variant_ids' in vals:
                variant_commands = vals.get('ml_variant_ids', [])
                _logger.info("🔍 Eliminando comandos de variantes del onchange (registro %d): %d comandos - se cargarán después de crear", 
                           idx, len(variant_commands))
                # Verificar que los comandos no tengan product_variant_id antes de eliminarlos
                for cmd in variant_commands:
                    if cmd[0] == 0:  # Comando create
                        cmd_vals = cmd[2] if len(cmd) > 2 else {}
                        if not cmd_vals.get('product_variant_id'):
                            _logger.warning("⚠️ Comando de variante sin product_variant_id detectado y eliminado: %s", cmd_vals)
                # Eliminar todos los comandos de variantes - se cargarán después de crear
                del prepared_vals[idx]['ml_variant_ids']
                _logger.info("✅ Comandos de variantes eliminados del registro %d", idx)
        
        # Crear los registros SIN variantes
        records = super().create(prepared_vals)
        records._ensure_category_relations()
        
        # Verificar si los atributos ya fueron preparados en onchange
        # Si ya hay atributos, limpiar duplicados y validar que tengan ml_attribute_id
        for record in records:
            # IMPORTANTE: NO invalidar atributos aquí - pueden estar siendo procesados por Odoo
            # Solo leer los atributos existentes sin forzar refresco
            has_attributes = bool(record.ml_attribute_ids)
            valid_attributes = record.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id)
            
            # Preservar los valores que el usuario ingresó antes de eliminar atributos inválidos
            user_values = {}
            if has_attributes:
                for attr in record.ml_attribute_ids:
                    sequence = attr.sequence
                    if sequence:
                        user_values[sequence] = {
                            'value_name': attr.value_name,
                            'value_id': attr.value_id,
                            'value_number': attr.value_number,
                            'value_unit': attr.value_unit,
                        }
                if user_values:
                    _logger.info("💾 Preservando %d valores ingresados por el usuario antes de recargar atributos", len(user_values))
            
            # Si hay atributos pero ninguno tiene ml_attribute_id válido, eliminarlos y recargar
            if has_attributes and not valid_attributes:
                _logger.warning("⚠️ Todos los atributos de la publicación %s están sin ml_attribute_id. Eliminándolos y recargando...", record.id)
                record.ml_attribute_ids.unlink()
                has_attributes = False
            
            if has_attributes:
                # Si ya hay atributos (preparados en onchange), limpiar duplicados y validar
                _logger.info("ℹ️ Atributos ya preparados en onchange para publicación %s (%d atributos). Validando...", 
                           record.id, len(record.ml_attribute_ids))
                
                # 1. Eliminar atributos sin ml_attribute_id
                invalid_attrs = record.ml_attribute_ids.filtered(lambda a: not a.ml_attribute_id)
                if invalid_attrs:
                    _logger.warning("⚠️ Eliminando %d atributos sin ml_attribute_id", len(invalid_attrs))
                    invalid_attrs.unlink()
                
                # 2. Eliminar duplicados (mismo ml_attribute_id)
                # Usar un diccionario para trackear por ml_attribute_id
                seen_ids = {}
                duplicates = []
                for attr in record.ml_attribute_ids:
                    attr_id = attr.ml_attribute_id
                    if attr_id:
                        attr_id_str = str(attr_id).strip()
                        if attr_id_str in seen_ids:
                            # Este es un duplicado, mantener el primero y eliminar este
                            _logger.warning("⚠️ Atributo duplicado encontrado: %s (ml_attribute_id=%s), eliminando...", 
                                          attr.name, attr_id_str)
                            duplicates.append(attr)
                        else:
                            seen_ids[attr_id_str] = attr
                
                if duplicates:
                    _logger.warning("⚠️ Eliminando %d atributos duplicados", len(duplicates))
                    duplicates.unlink()
                
                _logger.info("✅ Atributos validados: %d atributos únicos después de limpiar", 
                           len(record.ml_attribute_ids))
            
            # IMPORTANTE: Solo cargar atributos si NO hay atributos válidos
            # Si el usuario ya ingresó atributos en el onchange, NO sobrescribirlos
            has_valid_attributes = record.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id)
            if not has_valid_attributes:
                _logger.info("🔄 Publicación nueva %s sin atributos (se cargan al importar desde ML).", record.id)
            else:
                _logger.info("✅ Preservando %d atributos existentes ingresados por el usuario (no recargando desde categoría)", 
                           len(has_valid_attributes))
                
                # Restaurar los valores que el usuario ingresó
                if user_values:
                    _logger.info("🔄 Restaurando valores ingresados por el usuario...")
                    for attr in record.ml_attribute_ids:
                        sequence = attr.sequence
                        if sequence in user_values:
                            vals = user_values[sequence]
                            update_vals = {}
                            if vals.get('value_name'):
                                update_vals['value_name'] = vals['value_name']
                            if vals.get('value_id'):
                                update_vals['value_id'] = vals['value_id']
                            if vals.get('value_number') is not None:
                                update_vals['value_number'] = vals['value_number']
                            if vals.get('value_unit'):
                                update_vals['value_unit'] = vals['value_unit']
                            
                            if update_vals:
                                attr.write(update_vals)
                                _logger.debug("✅ Valores restaurados para atributo %s (sequence=%s): %s", 
                                            attr.name, sequence, update_vals)
        
        # Cargar variantes si el producto tiene variantes
        # IMPORTANTE: Si las variantes ya existen (del onchange), asegurar que tengan product_variant_id válido
        # y cargar sus atributos. Si no existen, cargarlas desde el producto.
        for record in records:
            if record.product_tmpl_id:
                # Eliminar cualquier variante que se haya creado sin product_variant_id (comandos mal formados)
                variants_without_id = record.ml_variant_ids.filtered(lambda v: not v.product_variant_id)
                if variants_without_id:
                    _logger.warning("⚠️ Encontradas %d variantes sin product_variant_id, eliminándolas...", len(variants_without_id))
                    variants_without_id.unlink()
                
                # Si hay variantes válidas pero sin atributos, cargar los atributos
                variants_without_attrs = record.ml_variant_ids.filtered(lambda v: v.product_variant_id and not v.attribute_combination_ids)
                if variants_without_attrs:
                    _logger.info("🔄 Cargando atributos para %d variantes existentes...", len(variants_without_attrs))
                    for variant_record in variants_without_attrs:
                        if variant_record.product_variant_id:
                            # Cargar atributos de la variante
                            product_variant = variant_record.product_variant_id
                            for attr_value in product_variant.product_template_attribute_value_ids:
                                # Buscar el ml_attribute_id correspondiente
                                odoo_attr_name = attr_value.attribute_id.name
                                ml_attribute_id = None
                                
                                # Mapeo común de atributos de Odoo a ML
                                common_mapping = {
                                    'color': 'COLOR', 'colour': 'COLOR',
                                    'talla': 'SIZE', 'size': 'SIZE', 'tamaño': 'SIZE',
                                    'modelo': 'MODEL', 'model': 'MODEL',
                                    'marca': 'BRAND', 'brand': 'BRAND',
                                }
                                
                                odoo_attr_lower = odoo_attr_name.lower().strip()
                                if odoo_attr_lower in common_mapping:
                                    ml_attribute_id = common_mapping[odoo_attr_lower]
                                else:
                                    ml_attribute_id = odoo_attr_name.upper().replace(' ', '_').replace('-', '_')
                                
                                value_id = None
                                
                                # Crear atributo de variante
                                variant_attr_vals = {
                                    'variant_id': variant_record.id,
                                    'ml_attribute_id': ml_attribute_id,
                                    'name': odoo_attr_name,
                                    'value_name': attr_value.name if not value_id else None,
                                    'value_id': value_id,
                                    'sequence': len(variant_record.attribute_combination_ids) * 10 + 10,
                                }
                                self.env['ml.publication.variant.attribute'].create(variant_attr_vals)
                    _logger.info("✅ Atributos cargados para variantes existentes")
                
                # SIEMPRE cargar variantes desde el producto después de crear (incluso si ya existen)
                # Esto asegura que todas las variantes tengan product_variant_id válido y sus atributos
                try:
                    _logger.info("🔄 Cargando variantes desde producto después de create (publicación %d)...", record.id)
                    # Primero eliminar cualquier variante sin product_variant_id que pueda haber quedado
                    variants_without_id = record.ml_variant_ids.filtered(lambda v: not v.product_variant_id)
                    if variants_without_id:
                        _logger.warning("⚠️ Eliminando %d variantes sin product_variant_id antes de cargar desde producto...", len(variants_without_id))
                        variants_without_id.unlink()
                    # Cargar variantes desde el producto
                    record._load_variants_from_product()
                    _logger.info("✅ Variantes cargadas: %d variantes (con atributos: %d)", 
                               len(record.ml_variant_ids),
                               len(record.ml_variant_ids.filtered(lambda v: v.attribute_combination_ids)))
                except Exception as e:
                    _logger.warning("⚠️ Error cargando variantes después de create: %s", e, exc_info=True)
        
        # IMPORTANTE: NO invalidar ml_attribute_ids aquí - pueden tener valores del usuario
        # Solo invalidar campos computed que no afectan la persistencia
        for record in records:
            record.invalidate_recordset(['common_attribute_ids', 'category_attribute_ids', 'ml_variant_ids'])
            # NO invalidar ml_attribute_ids - preservar los atributos del usuario
        
        return records

    def write(self, vals):
        vals = self._normalize_stock_rule_vals(self._prepare_category_vals(vals))
        # Logs de diagnóstico: qué llega desde UI/API y qué queda persistido
        rule_keys = ("use_custom_pause_rule", "max_stock_to_pause", "use_custom_activate_rule", "min_stock_to_activate")
        if any(k in (vals or {}) for k in rule_keys):
            _logger.info(
                "🧪 ml.publication.write pre ids=%s incoming=%s current=%s",
                self.ids,
                {k: (vals or {}).get(k) for k in rule_keys if k in (vals or {})},
                {
                    "use_custom_pause_rule": bool(self[:1].use_custom_pause_rule),
                    "max_stock_to_pause": self[:1].max_stock_to_pause,
                    "use_custom_activate_rule": bool(self[:1].use_custom_activate_rule),
                    "min_stock_to_activate": self[:1].min_stock_to_activate,
                },
            )
        category_changed = "category_id" in vals
        
        # Procesar comandos de atributos antes de escribir
        if 'ml_attribute_ids' in vals:
            # Limpiar comandos de atributos inválidos y asegurar que tengan ml_attribute_id
            valid_commands = []
            seen_attr_ids = set()  # Para evitar duplicados
            
            for cmd in vals['ml_attribute_ids']:
                cmd_type = cmd[0]
                
                if cmd_type == 0:  # Comando create
                    cmd_vals = cmd[2]
                    ml_attr_id = cmd_vals.get('ml_attribute_id')
                    
                    # Validar que tenga ml_attribute_id
                    if not ml_attr_id or not str(ml_attr_id).strip():
                        _logger.warning("⚠️ Omitiendo comando de atributo sin ml_attribute_id válido: %s", cmd_vals.get('name', 'Sin nombre'))
                        continue
                    
                    # Convertir a string y limpiar
                    ml_attr_id_str = str(ml_attr_id).strip()
                    
                    # Verificar duplicados
                    if ml_attr_id_str in seen_attr_ids:
                        _logger.warning("⚠️ Omitiendo atributo duplicado: %s (ml_attribute_id=%s)", 
                                      cmd_vals.get('name', 'Sin nombre'), ml_attr_id_str)
                        continue
                    
                    seen_attr_ids.add(ml_attr_id_str)
                    
                    # Asegurar que ml_attribute_id esté en el comando
                    cmd_vals['ml_attribute_id'] = ml_attr_id_str
                    
                    # Asegurar que name esté presente
                    if not cmd_vals.get('name'):
                        cmd_vals['name'] = ml_attr_id_str
                    
                    valid_commands.append((0, 0, cmd_vals))
                elif cmd_type == 1:  # Comando update (1, id, {...})
                    # Para comandos de actualización, obtener el registro y asegurar que tenga ml_attribute_id
                    attr_id = cmd[1]
                    cmd_vals = cmd[2]
                    
                    _logger.debug("🔍 Procesando comando de actualización para atributo %s: %s", attr_id, cmd_vals)
                    
                    # Buscar el atributo existente para obtener su ml_attribute_id si no está en vals
                    attr_record = self.env['ml.publication.attribute'].browse(attr_id)
                    if attr_record.exists():
                        if attr_record.ml_attribute_id:
                            # Si el comando no incluye ml_attribute_id, agregarlo desde el registro existente
                            if 'ml_attribute_id' not in cmd_vals:
                                cmd_vals['ml_attribute_id'] = attr_record.ml_attribute_id
                                _logger.debug("✅ Agregado ml_attribute_id=%s desde registro existente", attr_record.ml_attribute_id)
                            
                            # IMPORTANTE: Proteger campos importantes que no deben perderse al guardar
                            
                            # 1. Proteger 'name' si tiene un valor personalizado
                            if 'name' in cmd_vals:
                                new_name = cmd_vals.get('name', '').strip()
                                current_name = (attr_record.name or '').strip()
                                ml_attr_id_str = str(attr_record.ml_attribute_id or '').strip()
                                
                                # Si el nuevo name es igual a ml_attribute_id y el actual es diferente (personalizado),
                                # NO actualizar el name - preservar el nombre personalizado
                                if new_name == ml_attr_id_str and current_name != ml_attr_id_str and current_name:
                                    _logger.debug("🔒 Preservando name personalizado '%s' (intento de cambio a ml_attribute_id '%s')", 
                                                current_name, new_name)
                                    cmd_vals.pop('name', None)
                                elif not new_name and current_name:
                                    # Si el nuevo name está vacío pero el actual tiene valor, preservar el actual
                                    _logger.debug("🔒 Preservando name existente '%s' (nuevo name vacío)", current_name)
                                    cmd_vals.pop('name', None)
                            
                            # 2. Proteger 'allowed_values' si ya tiene valores (preservar opciones del multiple choice)
                            if 'allowed_values' in cmd_vals:
                                if attr_record.allowed_values and not cmd_vals.get('allowed_values'):
                                    # Si el registro ya tiene allowed_values y se intenta limpiar, preservar el existente
                                    _logger.debug("🔒 Preservando allowed_values para atributo %s (ml_attribute_id=%s)", 
                                                attr_id, attr_record.ml_attribute_id)
                                    cmd_vals.pop('allowed_values', None)
                            
                            # 3. Proteger 'required' si ya está configurado (preservar configuración del usuario)
                            if 'required' in cmd_vals:
                                if attr_record.required is True and cmd_vals.get('required') is False:
                                    # Si el registro ya está marcado como required y se intenta desmarcar, preservar
                                    _logger.debug("🔒 Preservando required=True para atributo %s (ml_attribute_id=%s)", 
                                                attr_id, attr_record.ml_attribute_id)
                                    cmd_vals.pop('required', None)
                            
                            # Si 'name' no está en cmd_vals, NO agregarlo - Odoo preservará el valor existente
                        else:
                            _logger.warning("⚠️ Atributo %s no tiene ml_attribute_id, omitiendo actualización", attr_id)
                            continue
                    else:
                        _logger.warning("⚠️ Atributo %s no existe, omitiendo actualización", attr_id)
                        continue
                    
                    valid_commands.append((1, attr_id, cmd_vals))
                else:
                    # Mantener otros comandos (2, 3, 4, 5, 6)
                    valid_commands.append(cmd)
            
            vals['ml_attribute_ids'] = valid_commands
            _logger.info("✅ Comandos de atributos validados en write: %d válidos", len(valid_commands))
        
        if category_changed:
            _logger.info("🔄 Cambio de categoría detectado. Cargando atributos...")
        
        # Procesar comandos de variantes antes de escribir
        if 'ml_variant_ids' in vals:
            valid_variant_commands = []
            for cmd in vals['ml_variant_ids']:
                cmd_type = cmd[0]
                if cmd_type == 0:  # Comando create
                    cmd_vals = cmd[2]
                    if not cmd_vals.get('product_variant_id'):
                        _logger.warning("⚠️ Omitiendo comando de variante sin product_variant_id: %s", cmd_vals)
                        continue
                    valid_variant_commands.append(cmd)
                else:
                    # Mantener otros comandos (1, 2, 3, 4, 5, 6)
                    valid_variant_commands.append(cmd)
            vals['ml_variant_ids'] = valid_variant_commands
            _logger.info("✅ Comandos de variantes validados en write: %d válidos", len(valid_variant_commands))
        
        res = super().write(vals)
        if 'current_price_ml' in vals and 'new_price_ml' not in vals:
            to_sync = self.filtered(
                lambda p: (p.new_price_ml or 0) == 0 and (p.current_price_ml or 0) > 0
            )
            if to_sync:
                for pub in to_sync:
                    super(MLPublication, pub).write(
                        {'new_price_ml': float(pub.current_price_ml or 0)}
                    )
        self._ensure_category_relations()
        
        # IMPORTANTE: Solo cargar/actualizar atributos cuando cambió la categoría explícitamente
        # NO cargar atributos cuando solo se están guardando valores o actualizando otros campos
        # Esto evita que se refresque la ventana de atributos y se pierdan las selecciones
        
        if category_changed:
            self.invalidate_recordset(['common_attribute_ids', 'category_attribute_ids', 'ml_attribute_ids'])
        
        # Si cambió el producto, recargar variantes (salvo vínculo solo-por-SKU).
        if 'product_tmpl_id' in vals and not self.env.context.get('ml_sku_link_only'):
            _logger.info("🔄 Cambio de producto detectado. Recargando variantes...")
            for pub in self:
                try:
                    pub._load_variants_from_product()
                except Exception as e:
                    _logger.warning("⚠️ Error recargando variantes después de cambiar producto: %s", e)
        
        # NO invalidar campos One2many cuando solo se están actualizando valores
        # Esto evita que se refresque la ventana de atributos innecesariamente
        
        if any(k in (vals or {}) for k in rule_keys):
            _logger.info(
                "🧪 ml.publication.write post ids=%s saved=%s",
                self.ids,
                {
                    "use_custom_pause_rule": bool(self[:1].use_custom_pause_rule),
                    "max_stock_to_pause": self[:1].max_stock_to_pause,
                    "use_custom_activate_rule": bool(self[:1].use_custom_activate_rule),
                    "min_stock_to_activate": self[:1].min_stock_to_activate,
                },
            )
        return res

    def _prepare_category_vals(self, vals):
        vals = (vals or {}).copy()
        return vals

    def _normalize_stock_rule_vals(self, vals):
        """
        Normaliza valores de reglas personalizadas por publicación.

        Problema real observado en logs: usuarios cargan el umbral pero dejan el checkbox custom en False,
        entonces el sistema usa la regla global (custom_enabled=False).
        Para evitar confusiones, si el usuario escribe un umbral custom (>0) y no envía el checkbox,
        se activa automáticamente el checkbox correspondiente.

        Nota: max_stock_to_pause=0 es válido (pausar por stock agotado) pero NO se auto-activa;
        en ese caso el usuario debe tildar el checkbox explícitamente.
        """
        vals = (vals or {}).copy()
        original_vals = vals.copy()
        changed = False

        if "max_stock_to_pause" in vals and "use_custom_pause_rule" not in vals:
            try:
                max_val = vals.get("max_stock_to_pause")
                # Si el usuario escribe el umbral (incluso 0), activar la regla personalizada automáticamente.
                # Esto evita confusión con custom_enabled=False en logs.
                if max_val not in (None, False):
                    vals["use_custom_pause_rule"] = True
                    changed = True
            except Exception:
                # No bloquear el write/create por un valor inválido, Odoo validará el campo igualmente
                pass

        if "min_stock_to_activate" in vals and "use_custom_activate_rule" not in vals:
            try:
                min_val = vals.get("min_stock_to_activate")
                if min_val not in (None, False) and int(min_val) > 0:
                    vals["use_custom_activate_rule"] = True
                    changed = True
            except Exception:
                pass

        # Logs de diagnóstico: solo si se tocaron campos de reglas o si normalizamos algo
        if changed or any(k in original_vals for k in ("use_custom_pause_rule", "max_stock_to_pause", "use_custom_activate_rule", "min_stock_to_activate")):
            _logger.info(
                "🧪 normalize_stock_rules: in=%s out=%s",
                {k: original_vals.get(k) for k in ("use_custom_pause_rule", "max_stock_to_pause", "use_custom_activate_rule", "min_stock_to_activate") if k in original_vals},
                {k: vals.get(k) for k in ("use_custom_pause_rule", "max_stock_to_pause", "use_custom_activate_rule", "min_stock_to_activate") if k in vals},
            )

        return vals

    def _ensure_category_relations(self):
        """Sin sección de categorías: no-op (category_id se mantiene como string desde import)."""
        pass
    
    def _load_common_attributes(self):
        """Sin sección de categorías: no-op (atributos solo al importar desde ML)."""
        return
        self.ensure_one()
        
        try:
            common_attrs = []
            if not common_attrs:
                _logger.info("ℹ️ No hay atributos comunes identificados. Ejecute la importación de atributos primero.")
                return
            
            _logger.info("🔍 Cargando %d atributos comunes", len(common_attrs))
            
            # Obtener IDs de atributos comunes existentes
            existing_common_ids = set(
                self.ml_attribute_ids.filtered(lambda a: a.is_common and a.ml_attribute_id).mapped('ml_attribute_id')
            )
            common_attr_ids = set(attr['ml_attribute_id'] for attr in common_attrs if attr.get('ml_attribute_id'))
            
            # Eliminar atributos comunes que ya no son comunes
            to_remove = self.ml_attribute_ids.filtered(
                lambda a: a.is_common and a.ml_attribute_id and a.ml_attribute_id not in common_attr_ids
            )
            if to_remove:
                to_remove.unlink()
            
            # Crear o actualizar atributos comunes (solo los que no existen)
            attr_model = self.env['ml.publication.attribute']
            sequence = 10
            created_count = 0
            
            for attr_data in common_attrs:
                try:
                    attr_id = attr_data.get('ml_attribute_id')
                    if not attr_id:
                        continue
                    
                    # Si ya existe, actualizar (pero no crear duplicado)
                    existing = self.ml_attribute_ids.filtered(
                        lambda a: a.ml_attribute_id == attr_id and a.is_common
                    )
                    
                    if existing:
                        # IMPORTANTE: NO actualizar atributos existentes que ya tienen valores configurados
                        # Solo actualizar campos que están vacíos o que realmente necesitan actualización
                        # Esto preserva las selecciones del usuario y los valores configurados
                        
                        current_name = existing.name or ""
                        ml_attr_id_str = str(attr_id)
                        
                        # Solo actualizar el nombre si está vacío o es igual a ml_attribute_id
                        should_update_name = (
                            not current_name or 
                            current_name.strip() == "" or 
                            current_name.strip() == ml_attr_id_str
                        )
                        
                        # Construir update_vals solo con campos que realmente necesitan actualización
                        update_vals = {}
                        
                        # Solo actualizar value_type si está vacío o es diferente
                        new_value_type = attr_data.get('value_type', 'value_name')
                        if not existing.value_type or existing.value_type != new_value_type:
                            update_vals["value_type"] = new_value_type
                        
                        # NO actualizar required si ya está configurado (preservar configuración del usuario)
                        # Solo actualizar si está vacío o es None
                        if existing.required is None or existing.required == False:
                            new_required = attr_data.get('required', False)
                            if new_required != existing.required:
                                update_vals["required"] = new_required
                        
                        # NO actualizar allowed_values si ya tiene valores (preservar opciones del usuario)
                        # Solo actualizar si está vacío
                        if not existing.allowed_values:
                            new_allowed_values = attr_data.get('allowed_values')
                            if new_allowed_values:
                                update_vals["allowed_values"] = new_allowed_values
                        
                        # NO actualizar allowed_units si ya tiene unidades (preservar configuración del usuario)
                        # Solo actualizar si está vacío
                        if not existing.allowed_units:
                            new_allowed_units = attr_data.get('allowed_units')
                            if new_allowed_units:
                                update_vals["allowed_units"] = new_allowed_units
                        
                        # Actualizar attribute_type solo si está vacío
                        if not existing.attribute_type:
                            new_attribute_type = attr_data.get('attribute_type', 'string')
                            if new_attribute_type:
                                update_vals["attribute_type"] = new_attribute_type
                        
                        # Asegurar que is_common esté configurado
                        if not existing.is_common:
                            update_vals["is_common"] = True
                        
                        # Solo actualizar el nombre si no tiene un nombre personalizado
                        if should_update_name and attr_data.get('name'):
                            update_vals["name"] = attr_data.get('name')
                        
                        # Solo escribir si hay cambios reales
                        if update_vals:
                            existing.write(update_vals)
                        continue
                    
                    # Preparar valores para el atributo
                    attr_vals = {
                        "publication_id": self.id,
                        "ml_attribute_id": str(attr_id),  # Asegurar que sea string
                        "name": attr_data.get('name', attr_id),
                        "value_type": attr_data.get('value_type', 'value_name'),
                        "required": attr_data.get('required', False),
                        "allowed_values": attr_data.get('allowed_values'),
                        "allowed_units": attr_data.get('allowed_units'),
                        "attribute_type": attr_data.get('attribute_type', 'string'),
                        "is_common": True,
                        "sequence": sequence,
                    }
                    
                    # Llenar BRAND y MODEL desde el producto si están vacíos
                    if attr_id == "BRAND" and self.product_tmpl_id:
                        if hasattr(self.product_tmpl_id, "product_brand_id") and self.product_tmpl_id.product_brand_id:
                            brand_field = self.product_tmpl_id.product_brand_id
                            attr_vals["value_name"] = getattr(brand_field, "name", None) or "Genérica"
                        else:
                            attr_vals["value_name"] = "Genérica"
                        _logger.info("✅ Atributo BRAND creado con valor desde producto: %s", attr_vals.get("value_name"))
                    elif attr_id == "MODEL" and self.product_tmpl_id:
                        attr_vals["value_name"] = self.product_tmpl_id.default_code or self.product_tmpl_id.name or ""
                        _logger.info("✅ Atributo MODEL creado con valor desde producto: %s", attr_vals.get("value_name"))
                    
                    # Crear nuevo atributo común solo si no existe
                    attr_model.create(attr_vals)
                    sequence += 10
                    created_count += 1
                except Exception as e:
                    _logger.warning("⚠️ Error creando atributo común %s: %s", attr_data.get('ml_attribute_id'), e)
                    continue
            
            _logger.info("✅ Atributos comunes cargados: %d nuevos, %d totales", 
                       created_count, len(self.ml_attribute_ids.filtered(lambda a: a.is_common)))
            
            # Llenar automáticamente BRAND y MODEL desde el producto si están vacíos
            self._fill_brand_model_from_product()
            
        except Exception as e:
            _logger.exception("❌ Error cargando atributos comunes: %s", e)
    
    def _fill_brand_model_from_product(self):
        """Llena automáticamente los atributos BRAND y MODEL desde el producto si están vacíos."""
        if not self.product_tmpl_id:
            return
        
        self._deduplicate_attributes()
        self._deduplicate_variants()
        
        # Obtener valores de marca y modelo desde el producto
        brand_value = None
        if hasattr(self.product_tmpl_id, "product_brand_id") and self.product_tmpl_id.product_brand_id:
            brand_field = self.product_tmpl_id.product_brand_id
            brand_value = getattr(brand_field, "name", None) or "Genérica"
        else:
            brand_value = "Genérica"
        
        model_value = self.product_tmpl_id.default_code or self.product_tmpl_id.name or "Sin modelo"
        
        if self.ml_attribute_ids:
            brand_attrs = self.ml_attribute_ids.filtered(
                lambda a: a.ml_attribute_id == "BRAND" and (not a.value_name or not str(a.value_name).strip()) and not a.value_id
            )
            if brand_attrs:
                brand_attrs[:1].write({'value_name': brand_value})
                _logger.info("✅ Atributo BRAND actualizado desde producto: %s", brand_value)

            model_attrs = self.ml_attribute_ids.filtered(
                lambda a: a.ml_attribute_id == "MODEL" and (not a.value_name or not str(a.value_name).strip()) and not a.value_id
            )
            if model_attrs:
                model_attrs[:1].write({'value_name': model_value})
                _logger.info("✅ Atributo MODEL actualizado desde producto: %s", model_value)

    def _deduplicate_attributes(self):
        for publication in self:
            seen = set()
            duplicates = publication.ml_attribute_ids.sorted(key=lambda attr: attr.id)
            for attr in duplicates:
                key = attr.ml_attribute_id or f"name:{attr.name}"
                if not key:
                    continue
                if key in seen:
                    _logger.warning("🧹 Eliminando atributo duplicado %s (id=%s) en publicación %s", key, attr.id, publication.id)
                    attr.unlink()
                else:
                    seen.add(key)

    def _deduplicate_variants(self):
        for publication in self:
            seen = {}
            for variant in publication.ml_variant_ids.sorted(key=lambda v: (v.product_variant_id.id or 0, v.id)):
                key = variant.product_variant_id.id or f"manual-{variant.id}"
                existing = seen.get(key)
                if not existing:
                    seen[key] = variant
                    continue

                keep_existing = existing._is_manual_override()
                keep_new = variant._is_manual_override()

                if keep_new and not keep_existing:
                    _logger.info("🔁 Reemplazando variante duplicada %s por nueva edición manual en publicación %s", existing.id, publication.id)
                    existing.unlink()
                    seen[key] = variant
                else:
                    _logger.info("🗑️ Eliminando variante duplicada %s en publicación %s", variant.id, publication.id)
                    variant.unlink()

    def _should_skip_webhook_sync(self, interval_seconds=30):
        self.ensure_one()
        # Manejar el caso cuando el campo no existe en la BD (migración pendiente)
        try:
            if not hasattr(self, 'last_webhook_sync') or not self.last_webhook_sync:
                return False
            now = fields.Datetime.now()
            if isinstance(now, str):
                now = fields.Datetime.from_string(now)
            last = self.last_webhook_sync
            if isinstance(last, str):
                last = fields.Datetime.from_string(last)
            if not now or not last:
                return False
            return (now - last).total_seconds() < interval_seconds
        except Exception:
            # Si hay algún error (campo no existe, etc.), no saltar la sincronización
            return False
    
    def _load_category_attributes(self):
        """Sin sección de categorías: no-op."""
        return
        self.ensure_one()
        
        if not self.ml_category_ref_id:
            # IMPORTANTE: NO limpiar atributos automáticamente - preservar datos del usuario
            # El usuario puede querer mantener los atributos aunque cambie la categoría
            return
        
        try:
            # Obtener atributos comunes para excluirlos
            # get_common_attributes() devuelve una lista de diccionarios, no podemos crear un set directamente
            # porque los diccionarios no son hashables. Extraemos solo los ml_attribute_id.
            common_attrs = self.env['ml.category'].get_common_attributes()
            common_attr_ids = {attr['ml_attribute_id'] for attr in common_attrs if attr.get('ml_attribute_id')}
            
            # Obtener atributos de la categoría desde ml.category.attribute
            category_attrs = self.ml_category_ref_id.attribute_ids
            
            if not category_attrs:
                _logger.info("ℹ️ No hay atributos importados para la categoría %s. "
                           "Ejecute la importación de atributos primero.", self.ml_category_ref_id.name)
                # IMPORTANTE: NO limpiar atributos automáticamente - preservar datos del usuario
                return
            
            _logger.info("🔍 Cargando %d atributos específicos de la categoría %s", 
                       len(category_attrs), self.ml_category_ref_id.name)
            
            # Filtrar solo atributos que NO son comunes
            specific_attrs = category_attrs.filtered(
                lambda a: a.ml_attribute_id not in common_attr_ids
            )
            
            # Obtener IDs de atributos específicos existentes
            existing_specific_ids = set(
                self.ml_attribute_ids.filtered(lambda a: not a.is_common and a.ml_attribute_id).mapped('ml_attribute_id')
            )
            specific_attr_ids = set(attr.ml_attribute_id for attr in specific_attrs if attr.ml_attribute_id)
            
            # IMPORTANTE: NO eliminar atributos automáticamente - preservar datos del usuario
            # Solo agregar los faltantes, no eliminar los existentes
            
            # Crear nuevos atributos específicos desde ml.category.attribute
            attr_model = self.env['ml.publication.attribute']
            sequence = 1000  # Secuencia más alta para atributos específicos
            created_count = 0
            
            for cat_attr in specific_attrs:
                try:
                    attr_id = cat_attr.ml_attribute_id
                    if not attr_id:
                        continue
                    
                    # Si ya existe, actualizar en lugar de crear (evitar duplicados)
                    existing = self.ml_attribute_ids.filtered(
                        lambda a: a.ml_attribute_id == attr_id and not a.is_common
                    )
                    
                    if existing:
                        # IMPORTANTE: NO actualizar atributos existentes que ya tienen valores configurados
                        # Solo actualizar campos que están vacíos o que realmente necesitan actualización
                        # Esto preserva las selecciones del usuario y los valores configurados
                        
                        current_name = existing.name or ""
                        ml_attr_id_str = str(attr_id)
                        
                        # Solo actualizar el nombre si está vacío o es igual a ml_attribute_id
                        should_update_name = (
                            not current_name or 
                            current_name.strip() == "" or 
                            current_name.strip() == ml_attr_id_str
                        )
                        
                        # Construir update_vals solo con campos que realmente necesitan actualización
                        update_vals = {}
                        
                        # Solo actualizar value_type si está vacío o es diferente
                        if not existing.value_type or existing.value_type != cat_attr.value_type:
                            update_vals["value_type"] = cat_attr.value_type
                        
                        # NO actualizar required si ya está configurado (preservar configuración del usuario)
                        # Solo actualizar si está vacío o es None
                        if existing.required is None or existing.required == False:
                            if cat_attr.required != existing.required:
                                update_vals["required"] = cat_attr.required
                        
                        # NO actualizar allowed_values si ya tiene valores (preservar opciones del usuario)
                        # Solo actualizar si está vacío
                        if not existing.allowed_values:
                            if cat_attr.allowed_values:
                                update_vals["allowed_values"] = cat_attr.allowed_values
                        
                        # NO actualizar allowed_units si ya tiene unidades (preservar configuración del usuario)
                        # Solo actualizar si está vacío
                        if not existing.allowed_units:
                            if cat_attr.allowed_units:
                                update_vals["allowed_units"] = cat_attr.allowed_units
                        
                        # Actualizar attribute_type solo si está vacío
                        if not existing.attribute_type:
                            if cat_attr.attribute_type:
                                update_vals["attribute_type"] = cat_attr.attribute_type
                        
                        # Asegurar que is_common esté configurado correctamente
                        if existing.is_common:
                            update_vals["is_common"] = False
                        
                        # Solo actualizar el nombre si no tiene un nombre personalizado
                        if should_update_name and cat_attr.name:
                            update_vals["name"] = cat_attr.name
                        
                        # Solo escribir si hay cambios reales
                        if update_vals:
                            existing.write(update_vals)
                        continue
                    
                    # Crear nuevo atributo específico de publicación desde atributo de categoría
                    attr_model.create({
                        "publication_id": self.id,
                        "ml_attribute_id": str(attr_id),  # Asegurar que sea string
                        "name": cat_attr.name,
                        "value_type": cat_attr.value_type,
                        "required": cat_attr.required,
                        "allowed_values": cat_attr.allowed_values,
                        "allowed_units": cat_attr.allowed_units,
                        "attribute_type": cat_attr.attribute_type,
                        "is_common": False,
                        "sequence": sequence,
                    })
                    sequence += 10
                    created_count += 1
                except Exception as e:
                    _logger.warning("⚠️ Error creando atributo específico %s: %s", cat_attr.ml_attribute_id, e)
                    continue
            
            _logger.info("✅ Atributos específicos cargados: %d nuevos, %d totales", 
                       created_count, len(self.ml_attribute_ids.filtered(lambda a: not a.is_common)))
            
            # Llenar automáticamente BRAND y MODEL desde el producto si están vacíos
            self._fill_brand_model_from_product()
            
            # NO invalidar aquí - se invalidará en write() si es necesario
            # Esto evita refrescos innecesarios de la ventana de atributos
            
        except Exception as e:
            _logger.exception("❌ Error cargando atributos de categoría: %s", e)
    
    def _prepare_variants_commands_simple(self):
        """
        Prepara los comandos Odoo para crear variantes en registros nuevos SIN atributos anidados.
        Los atributos se cargarán después de guardar para evitar problemas con comandos One2many anidados.
        Retorna una lista de comandos (0, 0, {...}) para el campo ml_variant_ids.
        """
        if not self.product_tmpl_id:
            return []
        
        product = self.product_tmpl_id
        variants = product.product_variant_ids
        
        if len(variants) <= 1:
            # No hay variantes o solo hay una variante
            return []
        
        _logger.info("🔄 Preparando comandos SIMPLES para variantes: %d variantes encontradas", len(variants))
        
        variant_commands = []
        sequence = 10
        
        for variant in variants:
            # Verificar que la variante tenga ID válido ANTES de procesarla
            if not variant.id or variant.id is False:
                _logger.warning("⚠️ Variante sin ID válido, omitiendo: %s (id=%s)", 
                               variant.name if hasattr(variant, 'name') else 'Sin nombre', 
                               variant.id)
                continue
            
            # Asegurar que variant.id sea un entero
            try:
                variant_id_int = int(variant.id)
            except (ValueError, TypeError):
                _logger.warning("⚠️ Variante con ID inválido (no es entero), omitiendo: %s (id=%s, type=%s)", 
                               variant.name if hasattr(variant, 'name') else 'Sin nombre',
                               variant.id, type(variant.id))
                continue
            
            # Preparar comando SIMPLE para la variante (SIN attribute_combination_ids)
            variant_vals = {
                'product_variant_id': variant_id_int,
                'price': self._get_price_for_ml_export(),
                'available_quantity': int(variant.qty_available) if variant.qty_available else 0,
                'seller_sku': variant.default_code or '',
                'sequence': sequence,
                'variant_name': variant.display_name or variant.name,
                # NO incluir attribute_combination_ids aquí - se cargarán después de guardar
            }
            variant_commands.append((0, 0, variant_vals))
            _logger.info("✅ Comando SIMPLE de variante preparado: product_variant_id=%s, price=%s, qty=%s", 
                         variant_id_int, variant_vals['price'], variant_vals['available_quantity'])
            sequence += 10
        
        _logger.info("✅ Comandos SIMPLES preparados para %d variantes", len(variant_commands))
        return variant_commands
    
    def _prepare_variants_commands(self):
        """
        Prepara los comandos Odoo para crear variantes en registros nuevos.
        Retorna una lista de comandos (0, 0, {...}) para el campo ml_variant_ids.
        """
        if not self.product_tmpl_id:
            return []
        
        product = self.product_tmpl_id
        variants = product.product_variant_ids
        
        if len(variants) <= 1:
            # No hay variantes o solo hay una variante
            return []
        
        _logger.info("🔄 Preparando comandos para variantes: %d variantes encontradas", len(variants))
        
        variant_commands = []
        sequence = 10
        
        for variant in variants:
            # Verificar que la variante tenga ID válido ANTES de procesarla
            if not variant.id or variant.id is False:
                _logger.warning("⚠️ Variante sin ID válido, omitiendo: %s (id=%s)", 
                               variant.name if hasattr(variant, 'name') else 'Sin nombre', 
                               variant.id)
                continue
            
            # Asegurar que variant.id sea un entero
            try:
                variant_id_int = int(variant.id)
            except (ValueError, TypeError):
                _logger.warning("⚠️ Variante con ID inválido (no es entero), omitiendo: %s (id=%s, type=%s)", 
                               variant.name if hasattr(variant, 'name') else 'Sin nombre',
                               variant.id, type(variant.id))
                continue
            
            _logger.debug("🔄 Procesando variante: id=%s, name=%s", variant_id_int, variant.name if hasattr(variant, 'name') else 'Sin nombre')
            
            # Preparar atributos de la variante
            attr_commands = []
            attr_sequence = 10
            
            for attr_value in variant.product_template_attribute_value_ids:
                odoo_attr_name = attr_value.attribute_id.name
                ml_attribute_id = None
                
                # Mapeo común de atributos de Odoo a ML
                common_mapping = {
                    'color': 'COLOR',
                    'colour': 'COLOR',
                    'talla': 'SIZE',
                    'size': 'SIZE',
                    'tamaño': 'SIZE',
                    'modelo': 'MODEL',
                    'model': 'MODEL',
                    'marca': 'BRAND',
                    'brand': 'BRAND',
                }
                
                # Intentar mapeo común primero
                odoo_attr_lower = odoo_attr_name.lower().strip()
                if odoo_attr_lower in common_mapping:
                    ml_attribute_id = common_mapping[odoo_attr_lower]
                else:
                    ml_attribute_id = odoo_attr_name.upper().replace(' ', '_').replace('-', '_')
                
                value_id = None
                
                attr_vals = {
                    'ml_attribute_id': ml_attribute_id,
                    'name': odoo_attr_name,
                    'value_name': attr_value.name if not value_id else None,
                    'value_id': value_id,
                    'sequence': attr_sequence,
                }
                attr_commands.append((0, 0, attr_vals))
                attr_sequence += 10
            
            # Preparar comando para la variante
            # variant_id_int ya fue validado arriba
            variant_vals = {
                'product_variant_id': variant_id_int,
                'price': self._get_price_for_ml_export(),
                'available_quantity': int(variant.qty_available) if variant.qty_available else 0,
                'seller_sku': variant.default_code or '',
                'sequence': sequence,
                'variant_name': variant.display_name or variant.name,
                'attribute_combination_ids': attr_commands,
            }
            variant_commands.append((0, 0, variant_vals))
            _logger.info("✅ Comando de variante preparado: product_variant_id=%s, price=%s, qty=%s, attrs=%d", 
                         variant_id_int, variant_vals['price'], variant_vals['available_quantity'], len(attr_commands))
            sequence += 10
        
        _logger.info("✅ Comandos preparados para %d variantes", len(variant_commands))
        return variant_commands
    
    def _load_variants_from_product(self):
        """Carga las variantes del producto automáticamente si el producto tiene variantes."""
        self.ensure_one()
        
        if not self.product_tmpl_id:
            _logger.debug("ℹ️ No hay producto relacionado, no se cargan variantes")
            return
        
        product = self.product_tmpl_id
        
        # Verificar si el producto tiene variantes
        variants = product.product_variant_ids
        if len(variants) <= 1:
            # No hay variantes o solo hay una variante, no hacer nada
            _logger.debug("ℹ️ Producto sin variantes o con una sola variante, no se cargan variantes")
            return
        
        _logger.info("🔄 Cargando variantes desde producto: %d variantes encontradas", len(variants))
        
        # Eliminar variantes existentes que ya no existen en el producto
        # Solo considerar variantes que tienen product_variant_id válido
        existing_variant_product_ids = self.ml_variant_ids.filtered(lambda v: v.product_variant_id).mapped('product_variant_id').ids
        current_variant_ids = variants.ids
        
        # Eliminar variantes que ya no existen (solo las que tienen product_variant_id válido)
        to_remove = self.ml_variant_ids.filtered(
            lambda v: v.product_variant_id and v.product_variant_id.id not in current_variant_ids
        )
        if to_remove:
            _logger.info("🗑️ Eliminando %d variantes que ya no existen en el producto", len(to_remove))
            to_remove.unlink()
        
        variant_model = self.env['ml.publication.variant'].with_context(allow_variant_price_write=True)
        
        # Crear o actualizar variantes
        for variant in variants:
            # Buscar si ya existe una variante para este product_variant_id
            # También buscar variantes sin product_variant_id que puedan ser actualizadas
            existing_variant = self.ml_variant_ids.filtered(
                lambda v: v.product_variant_id and v.product_variant_id.id == variant.id
            )
            
            # Si no se encontró una variante con product_variant_id, buscar una sin product_variant_id para actualizar
            if not existing_variant:
                # Buscar variantes sin product_variant_id que puedan ser actualizadas
                variants_without_id = self.ml_variant_ids.filtered(lambda v: not v.product_variant_id)
                if variants_without_id:
                    # Usar la primera variante sin product_variant_id para actualizar
                    existing_variant = variants_without_id[0:1]
                    _logger.info("🔄 Actualizando variante sin product_variant_id con variante del producto: %s", variant.id)
            
            if existing_variant:
                # Actualizar precio y cantidad si están vacíos
                existing_variant = existing_variant[0]
                # Actualizar product_variant_id si está vacío
                if not existing_variant.product_variant_id:
                    existing_variant.product_variant_id = variant.id
                    _logger.info("✅ product_variant_id actualizado: %s", variant.id)
                # Actualizar precio y cantidad desde el producto si están vacíos o son 0
                update_vals = {}
                if existing_variant.available_quantity is None or existing_variant.available_quantity == 0:
                    update_vals['available_quantity'] = int(variant.qty_available) if variant.qty_available else 0
                if not existing_variant.seller_sku and variant.default_code:
                    update_vals['seller_sku'] = variant.default_code
                if not existing_variant.gtin and getattr(variant, "barcode", False):
                    update_vals['gtin'] = variant.barcode
                
                if update_vals:
                    existing_variant.with_context(allow_variant_price_write=True).write(update_vals)
                    _logger.info("✅ Variante actualizada: price=%s, qty=%s, sku=%s", 
                               update_vals.get('price'), update_vals.get('available_quantity'), update_vals.get('seller_sku'))

                if not existing_variant.variant_name and existing_variant.product_variant_id:
                    existing_variant.variant_name = existing_variant.product_variant_id.display_name or existing_variant.product_variant_id.name
                    _logger.info("📝 Nombre de variante actualizado automáticamente: %s", existing_variant.variant_name)
                
                # Si la variante no tiene atributos de combinación, cargarlos desde el producto
                if not existing_variant.attribute_combination_ids:
                    _logger.info("🔄 Cargando atributos de combinación para variante existente...")
                    # Cargar atributos de la variante desde product_template_attribute_value_ids
                    for attr_value in variant.product_template_attribute_value_ids:
                        # Buscar el ml_attribute_id correspondiente
                        odoo_attr_name = attr_value.attribute_id.name
                        ml_attribute_id = None
                        
                        # Mapeo común de atributos de Odoo a ML
                        common_mapping = {
                            'color': 'COLOR', 'colour': 'COLOR',
                            'talla': 'SIZE', 'size': 'SIZE', 'tamaño': 'SIZE',
                            'modelo': 'MODEL', 'model': 'MODEL',
                            'marca': 'BRAND', 'brand': 'BRAND',
                        }
                        
                        odoo_attr_lower = odoo_attr_name.lower().strip()
                        if odoo_attr_lower in common_mapping:
                            ml_attribute_id = common_mapping[odoo_attr_lower]
                        else:
                            ml_attribute_id = odoo_attr_name.upper().replace(' ', '_').replace('-', '_')
                        
                        value_id = None
                        
                        # Verificar si ya existe un atributo con este ml_attribute_id para esta variante
                        existing_attr = existing_variant.attribute_combination_ids.filtered(
                            lambda a: a.ml_attribute_id == ml_attribute_id
                        )
                        
                        if existing_attr:
                            # Si ya existe, solo actualizar si el name está vacío o es igual a ml_attribute_id
                            # NO sobrescribir nombres personalizados
                            if not existing_attr.name or existing_attr.name == existing_attr.ml_attribute_id:
                                existing_attr.write({
                                    'name': odoo_attr_name,
                                    'value_name': attr_value.name if not value_id else None,
                                    'value_id': value_id,
                                })
                        else:
                            # Crear nuevo atributo solo si no existe
                            variant_attr_vals = {
                                'variant_id': existing_variant.id,
                                'ml_attribute_id': ml_attribute_id,
                                'name': odoo_attr_name,
                                'value_name': attr_value.name if not value_id else None,
                                'value_id': value_id,
                                'sequence': len(existing_variant.attribute_combination_ids) * 10 + 10,
                            }
                            self.env['ml.publication.variant.attribute'].create(variant_attr_vals)
                    _logger.info("✅ Atributos de combinación cargados para variante existente")
            else:
                # Crear nueva variante
                variant_vals = {
                    'publication_id': self.id,
                    'product_variant_id': variant.id,
                    'price': self._get_price_for_ml_export(),
                    'available_quantity': int(variant.qty_available),
                    'seller_sku': variant.default_code or '',
                    'sequence': len(self.ml_variant_ids) * 10 + 10,
                    'variant_name': variant.display_name or variant.name,
                    'gtin': getattr(variant, "barcode", False),
                }
                
                new_variant = variant_model.create(variant_vals)
                
                # Cargar atributos de la variante desde product_template_attribute_value_ids
                for attr_value in variant.product_template_attribute_value_ids:
                    # Buscar el ml_attribute_id correspondiente en los atributos de la categoría
                    odoo_attr_name = attr_value.attribute_id.name
                    ml_attribute_id = None
                    
                    # Mapeo común de atributos de Odoo a ML
                    common_mapping = {
                        'color': 'COLOR',
                        'colour': 'COLOR',
                        'talla': 'SIZE',
                        'size': 'SIZE',
                        'tamaño': 'SIZE',
                        'modelo': 'MODEL',
                        'model': 'MODEL',
                        'marca': 'BRAND',
                        'brand': 'BRAND',
                    }
                    
                    # Intentar mapeo común primero
                    odoo_attr_lower = odoo_attr_name.lower().strip()
                    if odoo_attr_lower in common_mapping:
                        ml_attribute_id = common_mapping[odoo_attr_lower]
                        _logger.info("📝 Mapeo común: '%s' -> '%s'", odoo_attr_name, ml_attribute_id)
                    else:
                        ml_attribute_id = odoo_attr_name.upper().replace(' ', '_').replace('-', '_')
                    
                    value_id = None
                    
                    # Verificar si ya existe un atributo con este ml_attribute_id para esta variante
                    existing_attr = new_variant.attribute_combination_ids.filtered(
                        lambda a: a.ml_attribute_id == ml_attribute_id
                    )
                    
                    if not existing_attr:
                        # Solo crear si no existe
                        variant_attr_vals = {
                            'variant_id': new_variant.id,
                            'ml_attribute_id': ml_attribute_id,
                            'name': odoo_attr_name,
                            'value_name': attr_value.name if not value_id else None,  # Solo usar value_name si no hay value_id
                            'value_id': value_id,
                            'sequence': len(new_variant.attribute_combination_ids) * 10 + 10,
                        }
                        
                        self.env['ml.publication.variant.attribute'].create(variant_attr_vals)
                    else:
                        # Si ya existe, solo actualizar si el name está vacío o es igual a ml_attribute_id
                        # NO sobrescribir nombres personalizados
                        if not existing_attr.name or existing_attr.name == existing_attr.ml_attribute_id:
                            existing_attr.write({
                                'name': odoo_attr_name,
                                'value_name': attr_value.name if not value_id else None,
                                'value_id': value_id,
                            })
                
                _logger.info("✅ Variante creada: %s", new_variant.display_name)
        
        _logger.info("✅ Variantes cargadas: %d totales", len(self.ml_variant_ids))
        self._deduplicate_variants()

    def _validate_required_attributes(self):
        """
        Valida que todos los atributos requeridos tengan valores válidos.
        Retorna una lista de errores (atributos requeridos sin valor).
        """
        errors = []
        warnings = []
        
        for attr in self.ml_attribute_ids:
            if not attr.required:
                continue
            
            # Verificar si el atributo tiene un valor válido
            attr_dict = attr.to_ml_format()
            if attr_dict is None:
                # El atributo no tiene valor válido
                error_msg = f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) no tiene valor válido"
                
                # Si tiene valores permitidos, agregar ayuda
                if attr.has_allowed_values and attr.allowed_values:
                    import json
                    try:
                        values = json.loads(attr.allowed_values)
                        if values:
                            error_msg += f". Valores permitidos: {', '.join([v.get('name', '') for v in values[:5]])}"
                            if len(values) > 5:
                                error_msg += f" y {len(values) - 5} más"
                    except Exception:
                        pass
                if attr.value_type == "number_unit":
                    try:
                        if attr.allowed_units:
                            units = json.loads(attr.allowed_units)
                            if units:
                                unit_names = ", ".join(u.get("name") or u.get("id") for u in units[:5])
                                error_msg += f". Unidades válidas: {unit_names}"
                    except Exception:
                        pass
                
                errors.append(error_msg)
            elif attr.value_type == "value_id" and not attr.value_id:
                errors.append(f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) requiere seleccionar un valor de la lista")
            elif attr.value_type == "value_name" and not attr.value_name:
                errors.append(f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) requiere un valor de texto")
            elif attr.value_type == "picture":
                if not attr.value_id and not attr.value_file:
                    errors.append(f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) requiere subir un archivo/imagen")
            elif attr.value_type in ["number", "number_unit"]:
                if not attr.value_number or attr.value_number <= 0:
                    errors.append(f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) requiere un valor numérico mayor que 0")
                elif attr.value_type == "number_unit":
                    # Verificar que tenga unidad (value_unit)
                    has_unit = bool(attr.value_unit and str(attr.value_unit).strip())
                    if not has_unit:
                        error_msg = f"Atributo requerido '{attr.name}' ({attr.ml_attribute_id}) requiere una unidad"
                        if attr.allowed_units:
                            try:
                                import json
                                units = json.loads(attr.allowed_units)
                                if units:
                                    unit_names = ", ".join(u.get("name") or u.get("id") for u in units[:3])
                                    error_msg += f" (válidas: {unit_names})"
                            except Exception:
                                pass
                        errors.append(error_msg)
        
        return errors, warnings

    def action_publish_to_ml(self):
        """
        Publica o actualiza la publicación en Mercado Libre.
        
        Comportamiento:
        - Usa solo los atributos ya existentes en ml_attribute_ids (NO agrega nuevos)
        - Mantiene los valores en español tal como están configurados
        - Valida antes de publicar: categoría con atributos, atributos requeridos, imágenes
        - Actualiza el estado según la respuesta de ML
        - Guarda errores detallados en error_log
        """
        self.ensure_one()
        
        # Limpiar error_log anterior
        self.error_log = False
        
        # =====================================================
        # 🔹 VALIDACIONES PREVIAS
        # =====================================================
        
        # 1. Verificar categoría (ID de ML)
        if not self.category_id:
            error_msg = "No se puede publicar: la publicación no tiene categoría de Mercado Libre (importe la publicación desde ML primero)."
            self.error_log = error_msg
            raise UserError(_(error_msg))
        
        # 2. Verificar que existan atributos en la publicación
        if not self.ml_attribute_ids:
            error_msg = (
                "No se puede publicar: no hay atributos configurados para esta publicación.\n\n"
                "Por favor, asegúrese de que la categoría tenga atributos importados y que se hayan cargado "
                "en esta publicación."
            )
            self.error_log = error_msg
            raise UserError(_(error_msg))
        
        # 3. Verificar que existan imágenes
        has_images = False
        if self.product_tmpl_id and self.product_tmpl_id.image_1920:
            has_images = True
        elif self.ml_image_ids:
            has_images = any(img.image_1920 for img in self.ml_image_ids)
        elif self.product_tmpl_id and hasattr(self.product_tmpl_id, 'product_image_ids'):
            has_images = any(img.image_1920 for img in self.product_tmpl_id.product_image_ids)
        
        if not has_images:
            error_msg = (
                "No se puede publicar: debe agregar al menos una imagen a la publicación.\n\n"
                "Puede agregar imágenes desde:\n"
                "• El producto relacionado\n"
                "• La sección 'Imágenes' de esta publicación"
            )
            self.error_log = error_msg
            raise UserError(_(error_msg))
        
        # 4. Validar atributos requeridos antes de publicar
        self._ensure_category_relations()
        self._deduplicate_attributes()
        self._deduplicate_variants()
        
        errors, warnings = self._validate_required_attributes()
        if errors:
            error_message = "No se puede publicar porque faltan atributos requeridos:\n\n" + "\n".join(f"• {e}" for e in errors)
            if warnings:
                error_message += "\n\nAdvertencias:\n" + "\n".join(f"• {w}" for w in warnings)
            self.error_log = error_message
            raise UserError(_(error_message))
        
        # Asegurar que las variantes estén cargadas correctamente antes de exportar,
        # pero sin sobrescribir los cambios manuales del usuario.
        if self.product_tmpl_id:
            try:
                product_variants = self.product_tmpl_id.product_variant_ids
                has_variants = len(product_variants) > 1
                managed_variants = self.ml_variant_ids.filtered(lambda v: v.product_variant_id)
                variants_with_attrs = managed_variants.filtered(lambda v: v.attribute_combination_ids)
                reload_variants = False
                
                if has_variants:
                    if not managed_variants:
                        reload_variants = True
                    else:
                        managed_ids = set(managed_variants.mapped('product_variant_id').ids)
                        missing_ids = set(product_variants.ids) - managed_ids
                        if missing_ids:
                            reload_variants = True
                        elif not variants_with_attrs:
                            reload_variants = True

                if reload_variants:
                    _logger.info(
                        "🔄 Cargando variantes antes de exportar (total actuales: %d, con product_variant_id: %d, con atributos: %d)...",
                        len(self.ml_variant_ids),
                        len(managed_variants),
                        len(variants_with_attrs),
                    )
                    self._load_variants_from_product()
                    self._deduplicate_variants()
                    _logger.info(
                        "✅ Variantes cargadas antes de exportar: %d totales (con atributos: %d)",
                                   len(self.ml_variant_ids),
                        len(self.ml_variant_ids.filtered(lambda v: v.attribute_combination_ids)),
                    )
                else:
                    _logger.info(
                        "✅ Variantes editadas manualmente detectadas: %d totales (con atributos: %d) - se respetan sin recargar.",
                                   len(self.ml_variant_ids),
                        len(variants_with_attrs),
                    )
            except Exception as e:
                _logger.warning("⚠️ Error cargando variantes antes de exportar: %s", e, exc_info=True)

        account = self.ml_account_id
        if not account or not account.access_token:
            raise UserError("No se encontró una cuenta de Mercado Libre con token válido.")
        
        # Verificar y refrescar token si es necesario
        account._ensure_valid_token()

        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Content-Type": "application/json",
        }

        product = self.product_tmpl_id
        if not product:
            raise UserError("Debe asignar un producto relacionado antes de publicar en Mercado Libre.")

        # =====================================================
        # 🔹 SUBIR IMÁGENES
        # =====================================================
        image_payloads = []

        # ✅ FIX: tomar imagen principal
        if product.image_1920:
            image_payloads.append({
                "image": product.image_1920,
                "source": "product_main",
                "ml_image_id": False,
            })

        # ✅ FIX: si existen imágenes adicionales del producto
        if hasattr(product, 'product_image_ids'):
            for img in product.product_image_ids:
                if img.image_1920:
                    image_payloads.append({
                        "image": img.image_1920,
                        "source": "product_gallery",
                        "ml_image_id": False,
                    })
        
        # ✅ FIX: también incluir imágenes de ml.publication.image
        # Si la imagen tiene variant_id, incluirla en el payload con ese variant_id
        if self.ml_image_ids:
            for ml_img in self.ml_image_ids:
                if ml_img.image_1920:
                    payload = {
                        "image": ml_img.image_1920,
                        "source": "ml_publication_image",
                        "ml_image_id": ml_img.id,
                    }
                    # Si la imagen pertenece a una variante del producto, incluir el product_variant_id (nuevo método)
                    if ml_img.product_variant_id:
                        payload["product_variant_id"] = int(ml_img.product_variant_id.id)
                        _logger.info("📸 Imagen de ml.publication.image con product_variant_id: product_variant_id=%s, image_id=%s", 
                                   ml_img.product_variant_id.id, ml_img.id)
                    # También incluir variant_id si está disponible (método legacy para compatibilidad)
                    if ml_img.variant_id:
                        payload["variant_id"] = int(ml_img.variant_id.id)
                        _logger.info("📸 Imagen de ml.publication.image con variant_id (legacy): variant_id=%s, image_id=%s", 
                                   ml_img.variant_id.id, ml_img.id)
                    image_payloads.append(payload)

        # 🔹 Agregar imágenes específicas por variante (si las hubiera)
        for variant in self.ml_variant_ids:
            variant_image_count = 0
            # Procesar imágenes desde image_ids (múltiples imágenes)
            for img in variant.image_ids:
                if img.image_1920:
                    variant_image_count += 1
                    image_payloads.append({
                        "image": img.image_1920,
                        "source": f"variant_{variant.id}_image_{img.id}",
                        "variant_id": int(variant.id),  # Asegurar que sea entero
                        "ml_image_id": img.id,  # Para guardar el ml_picture_id después
                    })
                    _logger.info("📸 Imagen de variante agregada a payload: variant_id=%s, image_id=%s, ml_picture_id=%s", 
                               variant.id, img.id, img.ml_picture_id or "Nuevo")
            # También procesar image_1920 legacy (para compatibilidad)
            if variant.image_1920:
                variant_image_count += 1
                image_payloads.append({
                    "image": variant.image_1920,
                    "source": f"variant_{variant.id}_legacy",
                    "variant_id": int(variant.id),  # Asegurar que sea entero
                })
                _logger.info("📸 Imagen legacy de variante agregada a payload: variant_id=%s", variant.id)
            if variant_image_count > 0:
                _logger.info("✅ Total de imágenes para variante %s (id=%s): %d", variant.display_name, variant.id, variant_image_count)

        # 🔹 Subir imágenes a Mercado Libre
        uploaded_pics = self._upload_images_to_ml(account, image_payloads)

        if self.ml_listing_type == 'gold_special' and not uploaded_pics:
            raise UserError(_("La publicación tipo Premium requiere imágenes; la subida falló o no hay imágenes en el producto."))

        # 🔹 Construir payload de publicación
        # Asegurar que la descripción siempre tenga contenido
        description_text = self.description or product.description_sale or product.name or "Sin descripción"
        
        # Verificar si hay atributo ITEM_CONDITION en los atributos
        item_condition_attr = self.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == 'ITEM_CONDITION')
        has_item_condition_attr = bool(item_condition_attr and item_condition_attr.value_name)
        
        # Si hay atributo ITEM_CONDITION, NO usar el campo condition
        # Si no hay atributo, usar el campo condition como antes
        condition_to_use = None
        if not has_item_condition_attr:
            condition_to_use = self.condition or "new"
            try:
                # Consultar la categoría para ver qué condiciones acepta
                category_url = f"https://api.mercadolibre.com/categories/{self.category_id}"
                category_response = requests.get(category_url, headers=headers, timeout=10)
                if category_response.ok:
                    category_data = category_response.json()
                    # Obtener condiciones permitidas (puede estar en settings o en la respuesta)
                    # Si la condición seleccionada no es válida, usar "new" o "not_specified"
                    # Por defecto, las categorías suelen aceptar: new, used, not_specified
                    valid_conditions = ["new", "used", "not_specified"]
                    if condition_to_use not in valid_conditions:
                        _logger.warning("⚠️ La condición '%s' no es válida para la categoría %s. Usando 'new' por defecto.", 
                                      condition_to_use, self.category_id)
                        condition_to_use = "new"
            except Exception as e:
                _logger.warning("⚠️ Error validando condición: %s. Usando condición por defecto.", e)
                # Si refurbished no es válido, usar "used" como alternativa cercana
                if condition_to_use == "refurbished":
                    condition_to_use = "used"
                    _logger.info("📝 Cambiando condición de 'refurbished' a 'used' (no soportada por la categoría)")
        else:
            _logger.info("📝 Atributo ITEM_CONDITION encontrado. No se usará el campo 'condition' para evitar conflicto.")
        
        is_update = bool(self.ml_item_id)

        data = {
            "title": self.title or product.name,
            "category_id": self.category_id,
            "currency_id": "ARS",
            "available_quantity": int(product.qty_available),
            "buying_mode": "buy_it_now",
            "listing_type_id": self.ml_listing_type,
            # NOTA: La descripción se envía a un endpoint separado después de crear/actualizar el item
            "attributes": [],
        }
        
        # Solo agregar condition si no hay atributo ITEM_CONDITION
        if condition_to_use:
            data["condition"] = condition_to_use
        
        # Sincronizar campo seller_sku con atributo SELLER_SKU (solo si el atributo ya existe)
        # IMPORTANTE: NO crear atributos nuevos durante la publicación
        seller_sku_attr = self.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == 'SELLER_SKU')
        if seller_sku_attr:
            # Si el campo seller_sku tiene valor pero el atributo no, usar el campo
            if self.seller_sku and not seller_sku_attr.value_name:
                seller_sku_attr.value_name = self.seller_sku
                _logger.info("📝 Sincronizado: campo seller_sku -> atributo SELLER_SKU: %s", self.seller_sku)
            # Si el atributo tiene valor pero el campo no, usar el atributo
            elif seller_sku_attr.value_name and not self.seller_sku:
                self.seller_sku = seller_sku_attr.value_name
                _logger.info("📝 Sincronizado: atributo SELLER_SKU -> campo seller_sku: %s", seller_sku_attr.value_name)
        # NO crear atributo SELLER_SKU si no existe - debe estar importado previamente
        
        # Asegurar que BRAND y MODEL tengan valores antes de exportar
        self._fill_brand_model_from_product()
        
        # =====================================================
        # 🔹 VALIDACIÓN: Verificar que los atributos pertenezcan a la categoría
        # =====================================================
        valid_category_attr_ids = set(self.ml_attribute_ids.mapped('ml_attribute_id')) if self.ml_attribute_ids else set()
        _logger.info("🔍 Atributos en la publicación: %d", len(valid_category_attr_ids))
        
        # IMPORTANTE: No rechazar atributos que no están importados en Odoo
        # Si un atributo existe en ML pero no está importado, ML lo validará
        # Solo rechazar atributos que claramente no pertenecen (atributos de sistema)
        # Identificar atributos que NO pertenecen a la categoría (solo atributos de sistema)
        invalid_attributes = []
        system_attrs = {'PACKAGE_HEIGHT', 'PACKAGE_WIDTH', 'PACKAGE_LENGTH', 'PACKAGE_WEIGHT',
                       'SELLER_PACKAGE_HEIGHT', 'SELLER_PACKAGE_WIDTH', 'SELLER_PACKAGE_LENGTH', 
                       'SELLER_PACKAGE_WEIGHT', 'SELLER_PACKAGE_TYPE', 'SELLER_PACKAGE_DATA_SOURCE',
                       'PACKAGE_DATA_SOURCE', 'IS_FLAMMABLE', 'PRODUCT_FEATURES', 'DESCRIPTIVE_TAGS',
                       'PRODUCT_CHEMICAL_FEATURES', 'FOODS_AND_DRINKS', 'MEDICINES', 'BATTERIES_FEATURES',
                       'SHIPMENT_PACKING', 'ADDITIONAL_INFO_REQUIRED', 'EXCLUDED_PLATFORMS',
                       'IS_SUITABLE_FOR_SHIPMENT', 'PRODUCT_DATA_SOURCE', 'LIMITED_MARKETPLACE_VISIBILITY_REASONS',
                       'HAS_COMPATIBILITIES', 'CATALOG_TITLE', 'SEARCH_ENHANCEMENT_FIELDS', 'IS_NEW_OFFER',
                       'SYI_PYMES_ID', 'WITH_POSITIVE_IMPACT', 'HAZMAT_TRANSPORTABILITY', 'IS_KIT'}
        
        for attr in self.ml_attribute_ids:
            if attr.ml_attribute_id:
                # Solo rechazar atributos de sistema que claramente no deben enviarse
                if attr.ml_attribute_id in system_attrs:
                    invalid_attributes.append({
                        'id': attr.ml_attribute_id,
                        'name': attr.name or attr.ml_attribute_id,
                    })
                    _logger.warning("⚠️ Atributo de sistema '%s' (%s) detectado - será omitido", 
                                  attr.name or attr.ml_attribute_id, attr.ml_attribute_id)
        
        # Si hay atributos de sistema inválidos, solo advertir (no bloquear)
        if invalid_attributes:
            invalid_names = [f"• {attr['name']} ({attr['id']})" for attr in invalid_attributes]
            _logger.warning("⚠️ Se detectaron %d atributo(s) de sistema que serán omitidos: %s", 
                          len(invalid_attributes), ", ".join([a['id'] for a in invalid_attributes]))
            # NO bloquear la publicación - estos atributos serán filtrados automáticamente
        
        # =====================================================
        # 🔹 CONSTRUCCIÓN ROBUSTA DE ATRIBUTOS USANDO BUILDER
        # =====================================================
        _logger.info("🔍 Construyendo payload de atributos usando builder robusto...")
        
        # Usar el builder robusto para construir el payload de atributos
        builder = self.env['ml.payload.builder']
        attributes_list, warnings_dict, errors_list = builder.build_attributes_payload(
            self, 
            category_attrs_def=None,  # Se obtendrá automáticamente desde la categoría
            include_variation_attrs=False  # No incluir atributos de variación en item.attributes
        )
        
        # Registrar warnings
        if warnings_dict:
            _logger.warning("⚠️ Se generaron %d warnings durante la construcción de atributos:", len(warnings_dict))
            for attr_id, warnings in warnings_dict.items():
                for warning in warnings:
                    _logger.warning("   - %s: %s", attr_id, warning)
        
        # Registrar errores críticos (atributos requeridos faltantes)
        if errors_list:
            _logger.error("❌ ERRORES CRÍTICOS: %d atributos requeridos tienen valores inválidos:", len(errors_list))
            for error in errors_list:
                _logger.error("   - %s (%s): %s", 
                            error['attr_id'], error['attr_name'], error['reason'])
            # NO bloquear la publicación aquí - ML decidirá si rechazarla o no
        
        # Agregar atributos válidos al payload
        data["attributes"] = attributes_list
        existing_attr_ids = [attr.get('id') for attr in attributes_list]
        
        _logger.info("✅ %d atributos válidos agregados al payload", len(attributes_list))
        
        # Verificar que BRAND y MODEL estén en los atributos exportados (requeridos por ML)
        # IMPORTANTE: NO agregar atributos nuevos durante la publicación
        # Si faltan, deben estar importados previamente o se mostrará error en validación
        if 'BRAND' not in existing_attr_ids:
            _logger.warning("⚠️ BRAND no encontrado en atributos exportados. Debe estar importado previamente.")
            # NO agregar automáticamente - debe estar en ml_attribute_ids
        
        if 'MODEL' not in existing_attr_ids:
            _logger.warning("⚠️ MODEL no encontrado en atributos exportados. Debe estar importado previamente.")
            # NO agregar automáticamente - debe estar en ml_attribute_ids
        
        # No agregar BRAND, MODEL o GTIN desde campos directos - deben venir de atributos
        _logger.info("📤 Total de atributos a exportar: %d", len(data["attributes"]))
        
        # Verificación de seguridad: si hay ITEM_CONDITION en los atributos, eliminar condition del payload
        # (ya debería estar manejado arriba, pero esto es una verificación adicional)
        has_item_condition = any(attr.get("id") == "ITEM_CONDITION" for attr in data["attributes"])
        if has_item_condition and "condition" in data:
            _logger.debug("📝 Eliminando campo 'condition' del payload (ya existe ITEM_CONDITION en atributos)")
            del data["condition"]
        
        sale_terms = []
        
        # Agregar garantía si está configurada
        # MercadoLibre requiere el atributo WARRANTY_TYPE con value_name o value_id según la categoría
        _logger.info("🔍 DEBUG: warranty_type=%s", self.warranty_type)
        if self.warranty_type != 'none':
            try:
                warranty_value_name = None
                warranty_value_id = None
                warranty_attr_id = "WARRANTY_TYPE"
                
                warranty_pub_attr = None
                warranty_attr_ids = ["WARRANTY_TYPE", "WARRANTY", "GARANTIA", "GARANTÍA"]
                for attr_id in warranty_attr_ids:
                    warranty_pub_attr = self.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == attr_id)
                    if warranty_pub_attr:
                        warranty_pub_attr = warranty_pub_attr[0]
                        warranty_attr_id = attr_id
                        break
                if not warranty_pub_attr:
                    for pub_attr in self.ml_attribute_ids:
                        attr_name = (pub_attr.name or "").lower()
                        if "garantía" in attr_name or "garantia" in attr_name or "warranty" in attr_name:
                            warranty_pub_attr = pub_attr
                            warranty_attr_id = pub_attr.ml_attribute_id
                            break
                
                if warranty_pub_attr:
                    import json
                    allowed_values = warranty_pub_attr.allowed_values
                    if allowed_values:
                        if isinstance(allowed_values, str):
                            values = json.loads(allowed_values)
                        else:
                            values = allowed_values
                        
                        _logger.info("🔍 DEBUG: Valores disponibles para garantía: %s", 
                                   [{"name": v.get("name"), "id": v.get("id")} for v in values[:10]] if isinstance(values, list) else [])
                        
                        if isinstance(values, list):
                            if self.warranty_type == 'seller':
                                # Buscar "Garantía del vendedor" o variantes
                                for val in values:
                                    val_name = (val.get("name") or "").lower()
                                    if "vendedor" in val_name or "seller" in val_name:
                                        warranty_value_name = val.get("name")
                                        warranty_value_id = val.get("id")
                                        _logger.info("🔍 DEBUG: Valor encontrado para seller: name=%s, id=%s", warranty_value_name, warranty_value_id)
                                        break
                            else:  # factory
                                # Buscar "Garantía de fábrica" o variantes
                                for val in values:
                                    val_name = (val.get("name") or "").lower()
                                    if "fábrica" in val_name or "factory" in val_name or "fabricante" in val_name:
                                        warranty_value_name = val.get("name")
                                        warranty_value_id = val.get("id")
                                        _logger.info("🔍 DEBUG: Valor encontrado para factory: name=%s, id=%s", warranty_value_name, warranty_value_id)
                                        break
                
                # Si no se encontró en los atributos importados, usar valores por defecto
                if not warranty_value_name:
                    if self.warranty_type == 'seller':
                        warranty_value_name = "Garantía del vendedor"
                    else:  # factory
                        warranty_value_name = "Garantía de fábrica"
                    _logger.info("🔍 DEBUG: Usando valor por defecto: %s", warranty_value_name)
                
                # Construir el atributo de garantía usando el ID encontrado o el por defecto
                warranty_attr = {
                    "id": warranty_attr_id
                }
                
                # Usar value_id si está disponible, sino value_name
                if warranty_value_id:
                    warranty_attr["value_id"] = str(warranty_value_id)
                    _logger.info("📝 Agregando garantía con value_id: attr_id=%s, tipo=%s, value_id=%s", 
                               warranty_attr["id"], self.warranty_type, warranty_value_id)
                else:
                    warranty_attr["value_name"] = warranty_value_name
                    _logger.info("📝 Agregando garantía con value_name: attr_id=%s, tipo=%s, value_name=%s", 
                               warranty_attr["id"], self.warranty_type, warranty_value_name)
                
                sale_terms.append(warranty_attr)
                _logger.info("✅ DEBUG: Garantía agregada a sale_terms: %s", warranty_attr)
                
                # Agregar días de garantía si están especificados
                if self.warranty_days and self.warranty_days > 0:
                    warranty_time_attr = {
                        "id": "WARRANTY_TIME",
                        "value_name": f"{self.warranty_days} días"
                    }
                    sale_terms.append(warranty_time_attr)
                    _logger.info("✅ DEBUG: Días de garantía agregados a sale_terms: %s días", self.warranty_days)
                
            except Exception as e:
                _logger.exception("⚠️ Error obteniendo valores de garantía de la API: %s", e)
                # Fallback: usar valores por defecto
                warranty_attr = {
                    "id": "WARRANTY_TYPE",
                    "value_name": "Garantía del vendedor" if self.warranty_type == 'seller' else "Garantía de fábrica"
                }
                sale_terms.append(warranty_attr)
                _logger.info("📝 Agregando garantía (fallback): tipo=%s, value_name=%s", 
                           self.warranty_type, warranty_attr["value_name"])
                
                # Agregar días de garantía si están especificados
                if self.warranty_days and self.warranty_days > 0:
                    warranty_time_attr = {
                        "id": "WARRANTY_TIME",
                        "value_name": f"{self.warranty_days} días"
                    }
                    sale_terms.append(warranty_time_attr)
                    _logger.info("📝 Agregando días de garantía (fallback): %s días", self.warranty_days)
        else:
            _logger.info("🔍 DEBUG: warranty_type es 'none', no se agrega garantía")
        
        # Agregar shipping y tiempo de disponibilidad
        _logger.info("🔍 DEBUG: local_pickup=%s, free_shipping=%s, availability_time=%s", 
                    self.local_pickup, self.free_shipping, self.availability_time)
        
        # Configurar shipping
        shipping_data = {}
        if self.local_pickup:
            shipping_data["local_pick_up"] = True
            _logger.info("📦 Configurando retiro en persona")
        
        if self.free_shipping:
            shipping_data["free_shipping"] = True
            _logger.info("🚚 Configurando envío gratis")
        
        if shipping_data:
            data["shipping"] = shipping_data
            _logger.info("✅ DEBUG: Shipping configurado: %s", shipping_data)
        
        # Agregar tiempo de disponibilidad como sale_term (MANUFACTURING_TIME)
        # Esto es más compatible que handling_time en shipping
        if self.availability_time:
            try:
                # Agregar MANUFACTURING_TIME como sale_term
                manufacturing_time = int(self.availability_time)
                sale_terms.append({
                    "id": "MANUFACTURING_TIME",
                    "value_name": f"{manufacturing_time} días"
                })
                _logger.info("⏱️ Configurando tiempo de disponibilidad como sale_term: %s días", manufacturing_time)
            except Exception as e:
                _logger.warning("⚠️ Error configurando MANUFACTURING_TIME: %s", e)

        if sale_terms:
            data["sale_terms"] = sale_terms
            _logger.info("✅ DEBUG: Sale terms configurados: %s", sale_terms)

        general_picture_ids = []
        all_variant_picture_ids = set()  # Para recopilar todas las imágenes de variaciones
        if uploaded_pics:
            seen_picture_ids = set()
            pictures_payload = []
            for pic in uploaded_pics:
                pic_id = pic.get("id")
                if pic_id and pic_id not in seen_picture_ids:
                    seen_picture_ids.add(pic_id)
                    pictures_payload.append({"id": pic_id})
                if pic_id and not pic.get("variant_id"):
                    general_picture_ids.append(pic_id)

            if pictures_payload:
                data["pictures"] = pictures_payload
        
        # Agregar variantes si existen
        if self.ml_variant_ids:
            _logger.info("🔄 Procesando %d variantes para exportar a Mercado Libre", len(self.ml_variant_ids))
            variations = []
            for variant in self.ml_variant_ids:
                try:
                    # Validar que la variante tenga attribute_combinations antes de exportar
                    if not variant.attribute_combination_ids:
                        _logger.warning("⚠️ Variante %s no tiene attribute_combinations, omitiendo...", variant.display_name)
                        continue
                    
                    # Pasar los IDs de las imágenes subidas a la variante
                    # ML requiere que cada variante tenga al menos 1 imagen
                    variant_picture_ids = []
                    
                    # PRIMERO: buscar en uploaded_pics (imágenes recién subidas en esta sesión)
                    # Esto es importante porque las imágenes nuevas aún no tienen ml_picture_id en la BD
                    variant_id_int = int(variant.id) if variant.id else None
                    _logger.info("🔍 Buscando imágenes para variante %s (id=%s) en uploaded_pics (total: %d imágenes)", 
                               variant.display_name, variant_id_int, len(uploaded_pics) if uploaded_pics else 0)
                    
                    if uploaded_pics:
                        variant_picture_ids = []
                        for pic in uploaded_pics:
                            pic_variant_id = pic.get("variant_id")
                            pic_product_variant_id = pic.get("product_variant_id")
                            pic_id = pic.get("id")
                            
                            # Comparar por product_variant_id (nuevo método, prioridad)
                            if variant.product_variant_id and pic_product_variant_id:
                                try:
                                    pic_product_variant_id = int(pic_product_variant_id)
                                    if pic_product_variant_id == variant.product_variant_id.id and pic_id:
                                        variant_picture_ids.append(pic_id)
                                        _logger.info("✅ Imagen encontrada para variante %s (product_variant_id=%s): pic_id=%s", 
                                                   variant.display_name, pic_product_variant_id, pic_id)
                                        continue
                                except (ValueError, TypeError):
                                    pass
                            
                            # Comparar por variant_id (método legacy para compatibilidad)
                            if pic_variant_id is not None:
                                try:
                                    pic_variant_id = int(pic_variant_id)
                                    if pic_variant_id == variant_id_int and pic_id:
                                        variant_picture_ids.append(pic_id)
                                        _logger.info("✅ Imagen encontrada para variante %s (variant_id=%s): pic_id=%s", 
                                                   variant.display_name, pic_variant_id, pic_id)
                                except (ValueError, TypeError):
                                    _logger.warning("⚠️ No se pudo convertir pic_variant_id a entero: %s (type: %s)", 
                                                  pic_variant_id, type(pic_variant_id))
                        
                        _logger.info("🔍 Imágenes recién subidas para variante %s (id=%s): %d imágenes - IDs: %s", 
                                   variant.display_name, variant_id_int, len(variant_picture_ids), variant_picture_ids)
                    
                    # SEGUNDO: buscar imágenes desde image_ids que ya tienen ml_picture_id (imágenes previamente subidas)
                    for img in variant.image_ids:
                        if img.ml_picture_id and img.ml_picture_id not in variant_picture_ids:
                            variant_picture_ids.append(img.ml_picture_id)
                    if variant_picture_ids:
                        _logger.info("🔍 Imágenes existentes en BD desde variant.image_ids para variante %s: %d imágenes", variant.display_name, len(variant_picture_ids))
                    
                    # SEGUNDO-B: buscar imágenes desde ml.publication.image que tengan esta variante del producto en product_variant_id
                    # Primero buscar por product_variant_id (nuevo método)
                    if variant.product_variant_id:
                        ml_images_with_product_variant = self.ml_image_ids.filtered(
                            lambda img: img.product_variant_id and img.product_variant_id.id == variant.product_variant_id.id
                        )
                    for img in ml_images_with_product_variant:
                        if img.ml_picture_id and img.ml_picture_id not in variant_picture_ids:
                            variant_picture_ids.append(img.ml_picture_id)
                        if ml_images_with_product_variant:
                            _logger.info("🔍 Imágenes existentes en BD desde ml.publication.image con product_variant_id=%s para variante %s: %d imágenes adicionales", 
                                       variant.product_variant_id.id, variant.display_name, len([img for img in ml_images_with_product_variant if img.ml_picture_id]))
                    
                    # También buscar por variant_id calculado (método legacy para compatibilidad)
                    # Nota: variant_id es un campo calculado, no se puede usar en filtered directamente
                    # Buscamos por product_variant_id que ya cubrimos arriba
                    
                    # TERCERO: usar image_ml_picture_id legacy (para compatibilidad)
                    if not variant_picture_ids and variant.image_ml_picture_id:
                        variant_picture_ids = [variant.image_ml_picture_id]
                        _logger.info("🔍 Usando imagen legacy para variante %s", variant.display_name)
                    
                    # CUARTO: si aún no hay imágenes, usar las imágenes generales de la publicación
                    if not variant_picture_ids:
                        if general_picture_ids:
                            variant_picture_ids = general_picture_ids[:]
                            _logger.warning("⚠️ Variante %s no tiene imágenes específicas, usando imágenes generales", variant.display_name)
                        elif uploaded_pics:
                            # Último recurso: usar todas las imágenes subidas
                            variant_picture_ids = [pic["id"] for pic in uploaded_pics if pic.get("id")]
                            _logger.warning("⚠️ Variante %s no tiene imágenes específicas, usando todas las imágenes subidas", variant.display_name)
                    
                    _logger.info("✅ Imágenes finales para variante %s: %d imágenes - IDs: %s", 
                               variant.display_name, len(variant_picture_ids), variant_picture_ids)
                    
                    # Recopilar todas las imágenes de variaciones para incluirlas en pictures del item principal
                    if variant_picture_ids:
                        all_variant_picture_ids.update(variant_picture_ids)
                    
                    valid_ml_attributes = set(self.ml_attribute_ids.mapped('ml_attribute_id')) if self.ml_attribute_ids else None
                    
                    # Pasar picture_ids a to_ml_format solo si hay imágenes
                    # Si variant_picture_ids está vacío, pasar None para que to_ml_format busque en image_ids
                    variant_dict = variant.to_ml_format(
                        picture_ids=variant_picture_ids if variant_picture_ids else None,
                        valid_ml_attributes=valid_ml_attributes
                    )
                    _logger.info("🔍 variant_dict después de to_ml_format para %s: picture_ids=%s", 
                               variant.display_name, variant_dict.get("picture_ids"))
                    
                    # Recopilar también las imágenes del variant_dict (por si se agregaron en to_ml_format)
                    if variant_dict.get("picture_ids"):
                        all_variant_picture_ids.update(variant_dict.get("picture_ids"))
                    
                    # Validar que el diccionario tenga attribute_combinations
                    if not variant_dict.get("attribute_combinations"):
                        _logger.warning("⚠️ Variante %s no tiene attribute_combinations en formato ML, omitiendo...", variant.display_name)
                        continue
                    
                    # Validar que la variante tenga imágenes (requerido por ML para categorías con variantes)
                    if not variant_dict.get("picture_ids"):
                        _logger.warning("⚠️ Variante %s no tiene imágenes en variant_dict, intentando agregar...", variant.display_name)
                        # Si variant_picture_ids tiene imágenes pero no se agregaron al dict, forzarlas
                        if variant_picture_ids:
                            variant_dict["picture_ids"] = variant_picture_ids
                            _logger.info("✅ Imágenes forzadas en variant_dict: %s", variant_picture_ids)
                        elif uploaded_pics:
                            variant_dict["picture_ids"] = [pic["id"] for pic in uploaded_pics if pic.get("id")]
                        else:
                            _logger.warning("⚠️ No hay imágenes disponibles para la variante %s", variant.display_name)
                    
                    variations.append(variant_dict)
                    _logger.info("✅ Variante agregada: %s (stock=%s, SKU=%s, atributos=%d, imágenes=%d)", 
                               variant.display_name, 
                               variant_dict.get("available_quantity"),
                               variant_dict.get("seller_custom_field", "Sin SKU"),
                               len(variant_dict.get("attribute_combinations", [])), 
                               len(variant_dict.get("picture_ids", [])))
                except Exception as e:
                    _logger.warning("⚠️ Error procesando variante %s: %s", variant.display_name, e, exc_info=True)
            
            if variations:
                # Validar que no haya variaciones duplicadas después de filtrar atributos inválidos
                seen_combinations = {}
                unique_variations = []
                duplicates_removed = 0
                
                for idx, variation in enumerate(variations):
                    # Crear una clave única basada en attribute_combinations
                    attr_combo_key = tuple(
                        sorted(
                            (attr.get("id"), attr.get("value_id"), attr.get("value_name"))
                            for attr in variation.get("attribute_combinations", [])
                        )
                    )
                    
                    if attr_combo_key in seen_combinations:
                        _logger.warning("⚠️ Variación duplicada detectada (índice %d): %s. Se omitirá.", 
                                      idx, variation.get("seller_custom_field", "Sin nombre"))
                        duplicates_removed += 1
                        continue
                    
                    seen_combinations[attr_combo_key] = True
                    unique_variations.append(variation)
                
                if duplicates_removed > 0:
                    _logger.warning("⚠️ Se eliminaron %d variaciones duplicadas después de filtrar atributos inválidos", 
                                  duplicates_removed)
                    if not unique_variations:
                        raise UserError(_("Todas las variaciones quedaron duplicadas después de filtrar atributos inválidos. "
                                       "Por favor, revise los atributos de combinación de las variantes."))
                
                data["variations"] = unique_variations
                _logger.info("📤 Total de variantes a exportar: %d (después de eliminar %d duplicados)", 
                           len(unique_variations), duplicates_removed)
                
                # Asegurar que todas las imágenes de las variaciones estén en pictures del item principal
                # ML requiere que las imágenes de las variaciones estén en la lista principal de imágenes
                if all_variant_picture_ids:
                    if "pictures" not in data:
                        data["pictures"] = []
                    existing_picture_ids = {pic.get("id") for pic in data["pictures"] if pic.get("id")}
                    for variant_pic_id in all_variant_picture_ids:
                        if variant_pic_id not in existing_picture_ids:
                            data["pictures"].append({"id": variant_pic_id})
                            _logger.info("✅ Imagen de variación agregada a pictures principal: %s", variant_pic_id)
                    _logger.info("📸 Total de imágenes en pictures principal (incluyendo variaciones): %d", len(data["pictures"]))
            else:
                _logger.warning("⚠️ No se pudo procesar ninguna variante válida")

        # Log completo del payload antes de enviar
        _logger.info("🔍 DEBUG: Payload completo antes de enviar:")
        _logger.info("  - title: %s", data.get("title"))
        _logger.info("  - description: %s", data.get("description"))
        _logger.info("  - condition: %s", data.get("condition"))
        _logger.info("  - attributes: %s", data.get("attributes"))
        _logger.info("  - shipping: %s", data.get("shipping"))
        _logger.info("  - pictures: %d imágenes", len(data.get("pictures", [])))

        # =====================================================
        # CREAR O ACTUALIZAR EN ML
        # =====================================================
        if self.ml_item_id:
            url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
            
            # Obtener el estado actual del item en ML para saber qué campos podemos actualizar
            item_info = None
            try:
                item_url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
                item_response = requests.get(item_url, headers=headers, timeout=10)
                if item_response.ok:
                    item_info = item_response.json()
                    current_status = item_info.get("status")
                    _logger.info("📋 Estado actual del item en ML: %s", current_status)
            except Exception as e:
                _logger.warning("⚠️ No se pudo obtener información del item: %s", e)
            
            # Al actualizar, incluir atributos (garantía, etc.)
            current_status = item_info.get("status") if item_info else self.ml_status
            _logger.info("🔍 DEBUG: Estado actual del item: %s", current_status)
            _logger.info("🔍 DEBUG: Atributos a enviar: %s", data.get("attributes"))
            
            data_update = {
                "attributes": data["attributes"],  # Incluir todos los atributos (marca, modelo, garantía, etc.)
            }
            
            # Precio: nunca desde Odoo en actualizaciones (solo wizard «Actualizar valores»).
            # Cantidad: solo si el ítem NO está activo/pausado (ML no lo permite en activos).
            if current_status not in ["active", "paused"]:
                data_update["available_quantity"] = data["available_quantity"]
                _logger.info("📝 Item no está activo/pausado, se actualizará cantidad (sin precio).")
            else:
                _logger.info("⚠️ Item está activo/pausado, ML no permite modificar cantidad directamente.")
                if "variations" in data:
                    variations_stock_only = []
                    for variation in data["variations"]:
                        if not isinstance(variation, dict):
                            continue
                        var_payload = dict(variation)
                        var_payload.pop("price", None)
                        variations_stock_only.append(var_payload)
                    if variations_stock_only:
                        data_update["variations"] = variations_stock_only
                    _logger.info("📝 Actualizando variantes (solo stock) para item activo/pausado.")
            
            # NOTA: La descripción se envía a un endpoint separado, no en el payload de actualización
            # Solo intentar actualizar descripción si el item NO está activo
            if current_status != "active":
                _logger.info("📝 Item no está activo, se intentará actualizar descripción en endpoint separado")
            else:
                _logger.info("⚠️ Item está activo, ML no permite modificar descripción")
            
            # No enviar status desde Odoo: pausar/activar solo desde Mercado Libre.
            # (Antes: data_update["status"] = self.ml_status podía reactivar ítems pausados en ML.)

            # Incluir shipping si está configurado
            if "shipping" in data:
                shipping_data = data["shipping"].copy()
                # Si el item está activo, ML no permite modificar free_shipping directamente
                if current_status == "active" and "free_shipping" in shipping_data:
                    del shipping_data["free_shipping"]
                    _logger.warning("⚠️ Item activo, ML no permite modificar 'free_shipping' directamente. Se omite del payload de actualización.")
                if shipping_data:
                    data_update["shipping"] = shipping_data
                    _logger.info("🔍 DEBUG: Shipping en actualización: %s", shipping_data)
            
            # Incluir sale_terms si está configurado (MANUFACTURING_TIME)
            if "sale_terms" in data:
                data_update["sale_terms"] = data["sale_terms"]
                _logger.info("🔍 DEBUG: Sale terms en actualización: %s", data["sale_terms"])
            
            # Si hay imágenes, incluirlas en la actualización
            # IMPORTANTE: Asegurar que todas las imágenes de las variaciones estén en pictures
            if "pictures" in data:
                data_update["pictures"] = data["pictures"]
                _logger.info("🔍 DEBUG: Imágenes en actualización: %d", len(data["pictures"]))
            
            # Si hay variaciones, asegurar que todas sus imágenes estén en pictures
            if "variations" in data_update:
                variation_picture_ids = set()
                for variation in data_update["variations"]:
                    if "picture_ids" in variation:
                        variation_picture_ids.update(variation["picture_ids"])
                
                if variation_picture_ids:
                    if "pictures" not in data_update:
                        data_update["pictures"] = []
                    existing_pic_ids = {pic.get("id") for pic in data_update["pictures"] if pic.get("id")}
                    for var_pic_id in variation_picture_ids:
                        if var_pic_id not in existing_pic_ids:
                            data_update["pictures"].append({"id": var_pic_id})
                            _logger.info("✅ Imagen de variación agregada a pictures en actualización: %s", var_pic_id)
                    _logger.info("📸 Total de imágenes en pictures (actualización, incluyendo variaciones): %d", len(data_update["pictures"]))
            
            # Verificación final: nunca enviar precio; quitar cantidad si el item está activo/pausado
            self._strip_price_from_ml_payload(data_update)
            if current_status in ["active", "paused"]:
                if "available_quantity" in data_update:
                    del data_update["available_quantity"]
                    _logger.warning("⚠️ Eliminando 'available_quantity' del payload final (item activo/pausado)")
                if "variations" in data_update:
                    for variation in data_update["variations"]:
                        if isinstance(variation, dict):
                            variation.pop("available_quantity", None)
            
            _logger.info("🔍 DEBUG: Payload de actualización completo:")
            _logger.info("  - price: %s", data_update.get("price"))
            _logger.info("  - available_quantity: %s", data_update.get("available_quantity"))
            _logger.info("  - description: %s", data_update.get("description"))
            _logger.info("  - attributes: %s", data_update.get("attributes"))
            _logger.info("  - shipping: %s", data_update.get("shipping"))
            _logger.info("  - status: %s", data_update.get("status"))
            _logger.info("  - variations: %s", "Sí" if "variations" in data_update else "No")
            _logger.info("📤 Actualizando publicación en ML con payload: %s", data_update)
            response = requests.put(url, json=data_update, headers=headers)
        else:
            url = "https://api.mercadolibre.com/items"
            create_payload = dict(data)
            self._strip_price_from_ml_payload(create_payload)
            _logger.info("📤 Creando nueva publicación en ML sin precio desde Odoo: %s", create_payload)
            response = requests.post(url, json=create_payload, headers=headers)
            if response.ok:
                self.ml_item_id = response.json().get("id")

        if not response.ok:
            _logger.error("❌ Error al publicar en ML: %s", response.text)
            
            # Guardar error completo en error_log
            self.error_log = f"Error al publicar en Mercado Libre:\n\n{response.text}"
            
            # Actualizar estado a error
            self.ml_status = 'inactive'
            
            # Usar el builder para manejar errores de forma inteligente
            builder = self.env['ml.payload.builder']
            should_retry, attributes_to_remove, warnings, errors = builder.handle_ml_response_errors(
                response.text, self
            )
            
            # Construir mensaje de error para el usuario con formato claro
            error_messages = []
            
            # Agregar errores bloqueantes con formato claro
            if errors:
                error_messages.append("❌ ERRORES QUE IMPIDEN LA PUBLICACIÓN:\n")
                for i, e in enumerate(errors, 1):
                    title = e.get('title', 'Error')
                    message = e.get('message', '')
                    solution = e.get('solution', '')
                    attr_id = e.get('attr_id', '')
                    attr_name = e.get('attr_name', '')
                    
                    # Construir mensaje claro
                    error_messages.append(f"{i}. {title}")
                    
                    # Mostrar información del atributo de forma destacada
                    if attr_name:
                        # Si attr_name ya incluye el ID y valor, mostrarlo completo
                        if attr_id and attr_id in attr_name:
                            error_messages.append(f"   🔹 Atributo: {attr_name}")
                        else:
                            error_messages.append(f"   🔹 Atributo: {attr_name}")
                            if attr_id and attr_id != attr_name:
                                error_messages.append(f"      ID: {attr_id}")
                    elif attr_id:
                        error_messages.append(f"   🔹 Atributo ID: {attr_id}")
                    else:
                        # Si no tenemos información del atributo, intentar extraerla del mensaje original
                        original_msg = e.get('original_message', '')
                        if original_msg:
                            # Buscar patrones comunes
                            import re
                            attr_match = re.search(r'\[([^\]]+)\]', original_msg)
                            if attr_match:
                                found_attr = attr_match.group(1)
                                error_messages.append(f"   🔹 Atributo: {found_attr} (extraído del mensaje de error)")
                    
                    error_messages.append(f"   Problema: {message}")
                    if solution:
                        # Mejorar la solución si tenemos información del atributo
                        if attr_name and 'Revise los atributos' in solution:
                            solution = solution.replace('Revise los atributos', f"Revise el atributo '{attr_name.split('(')[0].strip()}'")
                        error_messages.append(f"   Solución: {solution}")
                    error_messages.append("")  # Línea en blanco entre errores
            
            # Agregar warnings importantes (aunque no bloqueen)
            if warnings:
                important_warnings = []
                for w in warnings:
                    title = w.get('title', 'Advertencia')
                    message = w.get('message', '')
                    solution = w.get('solution', '')
                    attr_id = w.get('attr_id', '')
                    attr_name = w.get('attr_name', '')
                    
                    warning_text = f"⚠️ {title}"
                    if attr_name and attr_name != attr_id:
                        warning_text += f" - {attr_name} ({attr_id})"
                    elif attr_id:
                        warning_text += f" - {attr_id}"
                    warning_text += f": {message}"
                    if solution:
                        warning_text += f"\n   Solución: {solution}"
                    important_warnings.append(warning_text)
                
                if important_warnings:
                    if error_messages:
                        error_messages.append("")
                    error_messages.append("⚠️ ADVERTENCIAS (no bloquean la publicación):\n")
                    error_messages.extend(important_warnings)
            
            # Actualizar error_log con mensaje formateado
            if error_messages:
                error_summary = "\n".join(error_messages)
                self.error_log = f"Error al publicar en Mercado Libre:\n\n{error_summary}\n\nRespuesta completa de ML:\n{response.text}"
                raise UserError(_("Error al sincronizar con Mercado Libre:\n\n%s") % error_summary)
            
            # Si no hay errores ni warnings importantes, mostrar el error genérico
            self.error_log = f"Error al publicar en Mercado Libre:\n\n{response.text}"
            raise UserError(_("Error al sincronizar con Mercado Libre:\n\n%s") % response.text)

        result = response.json()
        
        # Actualizar estado según respuesta de ML
        ml_status = result.get("status")
        if ml_status:
            self.ml_status = ml_status
        self.permalink = result.get("permalink")
        
        # Limpiar error_log si la publicación fue exitosa
        self.error_log = False
        
        # Actualizar estado calculado
        self._compute_state()
        
        # Obtener el ID del item (puede ser nuevo o existente)
        item_id = result.get("id") or self.ml_item_id
        
        # =====================================================
        # 🔹 ENVIAR DESCRIPCIÓN A ENDPOINT ESPECÍFICO
        # =====================================================
        # ML requiere que la descripción se envíe a un endpoint separado
        # Al crear la publicación debemos enviarla SIEMPRE, incluso si el item queda activo
        # En actualizaciones solo cuando el item no esté activo
        if item_id and description_text:
            send_description = False
            item_status_check = result.get("status") or self.ml_status
            if not is_update:
                send_description = True
            else:
                if item_status_check != "active":
                    send_description = True
                else:
                    _logger.warning("⚠️ Item está activo, ML no permite modificar descripción en actualización. Omitiendo envío.")

            if send_description:
                try:
                    desc_url = f"https://api.mercadolibre.com/items/{item_id}/description"
                    desc_payload = {"plain_text": description_text}
                    _logger.info("📝 Enviando descripción a endpoint específico: %s", desc_url)
                    desc_response = requests.put(desc_url, json=desc_payload, headers=headers, timeout=10)
                    if desc_response.ok:
                        _logger.info("✅ Descripción enviada correctamente")
                    else:
                        _logger.warning("⚠️ Error enviando descripción: %s", desc_response.text)
                except Exception as e:
                    _logger.warning("⚠️ Error enviando descripción a endpoint específico: %s", e)
        if not item_id:
            _logger.warning("⚠️ No se pudo obtener el ID del item de ML")
        else:
            # Consultar el item completo para verificar qué se aplicó realmente
            try:
                item_check_url = f"https://api.mercadolibre.com/items/{item_id}"
                item_check_response = requests.get(item_check_url, headers=headers, timeout=10)
                if item_check_response.ok:
                    item_full = item_check_response.json()
                    _logger.info("🔍 DEBUG: Verificación completa del item en ML (consulta directa):")
                    
                    # Descripción
                    desc_full = item_full.get("description")
                    if desc_full:
                        if isinstance(desc_full, dict):
                            desc_text = desc_full.get("plain_text", "")
                        else:
                            desc_text = str(desc_full)
                        _logger.info("  ✅ description: %s", desc_text[:200] if desc_text else "Vacía")
                    else:
                        _logger.warning("  ❌ description: No encontrada en el item")
                    
                    # Sale terms (garantía y manufacturing)
                    sale_terms_full = item_full.get("sale_terms", [])
                    warranty_terms_full = [term for term in sale_terms_full if "WARRANTY" in term.get("id", "")]
                    if warranty_terms_full:
                        _logger.info("  ✅ sale_terms (garantía): %s", warranty_terms_full)
                    else:
                        _logger.warning("  ❌ sale_terms (garantía): No encontrados")
                    
                    # Shipping
                    shipping_full = item_full.get("shipping", {})
                    _logger.info("  - shipping completo: %s", shipping_full)
                    
                    manufacturing_time_term = next((term for term in sale_terms_full if term.get("id") == "MANUFACTURING_TIME"), None)
                    if manufacturing_time_term:
                        _logger.info("  ✅ sale_terms (MANUFACTURING_TIME): %s", manufacturing_time_term)
                    else:
                        _logger.warning("  ❌ sale_terms (MANUFACTURING_TIME): No encontrado")
                    _logger.info("  - sale_terms completo: %s", sale_terms_full)
                    
                else:
                    _logger.warning("⚠️ No se pudo consultar el item completo para verificación")
            except Exception as e:
                _logger.warning("⚠️ Error consultando item completo: %s", e)

        # Log de debug: respuesta inicial de ML
        _logger.info("🔍 DEBUG: Respuesta inicial de ML:")
        _logger.info("  - status: %s", result.get("status"))
        
        # Descripción en respuesta inicial
        description_result = result.get("description")
        if description_result:
            if isinstance(description_result, dict):
                desc_text = description_result.get("plain_text", "N/A")
            else:
                desc_text = str(description_result)
            _logger.info("  - description (en respuesta): %s", desc_text[:200] if desc_text else "N/A")
        else:
            _logger.info("  - description (en respuesta): No disponible (esto es normal, ML no siempre la devuelve)")
        
        # Sale terms en respuesta inicial
        sale_terms_resp = result.get("sale_terms", [])
        warranty_terms_resp = [term for term in sale_terms_resp if "WARRANTY" in term.get("id", "")]
        if warranty_terms_resp:
            _logger.info("  ✅ sale_terms (garantía en respuesta): %s", warranty_terms_resp)
        else:
            _logger.info("  - sale_terms (garantía en respuesta): No encontrados (pero pueden estar aplicados)")
        
        # Shipping en respuesta inicial
        shipping_result = result.get("shipping", {})
        _logger.info("  - shipping (en respuesta): %s", shipping_result)
        _logger.info("  - shipping.handling_time (en respuesta): %s", shipping_result.get("handling_time", "No disponible (esto es normal)"))
        
        _logger.info("  - condition: %s", result.get("condition"))

        _logger.info("✅ Publicación sincronizada correctamente con ML ID: %s", self.ml_item_id)

    # =====================================================
    # 🔹 SUBIR IMÁGENES A MERCADO LIBRE (con validaciones)
    # =====================================================
    def _upload_images_to_ml(self, account, image_payloads):
        import base64
        import requests
        from io import BytesIO
        from PIL import Image
    
        images = []

        if not image_payloads:
            _logger.warning("⚠️ No se encontraron imágenes para subir a Mercado Libre.")
            return images

        total_images = len(image_payloads)
    
        for idx, img_payload in enumerate(image_payloads, start=1):
            try:
                img_input = img_payload.get("image")
                _logger.warning("🧩 Procesando imagen %s tipo=%s", idx, type(img_input))

                if not img_input:
                    _logger.warning("⚠️ Imagen %s es vacía o inválida, omitiendo...", idx)
                    continue

                # =====================================================
                # 🔹 NORMALIZAR: decodificar siempre desde base64
                # =====================================================
                img_bytes = None
                try:
                    # En Odoo, los campos Image devuelven strings base64
                    # Pero pueden venir como bytes en algunos contextos
                    
                    if isinstance(img_input, memoryview):
                        img_input = img_input.tobytes()
                    
                    if isinstance(img_input, bytes):
                        # Verificar si los bytes son datos binarios de imagen (magic bytes)
                        # o si son base64 codificado
                        is_binary_image = False
                        if len(img_input) >= 4:
                            # Verificar magic bytes de formatos comunes
                            magic = img_input[:4]
                            # PNG: 89 50 4E 47
                            # JPEG: FF D8 FF E0/E1/E2/E3/E8/E9
                            # GIF: 47 49 46 38
                            # WEBP: 52 49 46 46 (RIFF)
                            if (magic[:2] == b'\x89\x50' or  # PNG
                                magic[:2] == b'\xff\xd8' or  # JPEG
                                magic[:3] == b'GIF' or       # GIF
                                magic[:4] == b'RIFF'):       # WEBP
                                is_binary_image = True
                                _logger.warning("🔍 Imagen %s detectada como binaria (magic bytes: %s)", 
                                              idx, magic[:4].hex())
                        
                        if is_binary_image:
                            # Ya es imagen binaria, usar directamente
                            img_bytes = img_input
                        else:
                            # Intentar decodificar como base64
                            # Primero verificar si parece base64 válido
                            try:
                                # Intentar decodificar como UTF-8
                                img_str = img_input.decode('utf-8')
                                
                                # Verificar si parece base64 (caracteres alfanuméricos, +, /, =)
                                if all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r' 
                                       for c in img_str[:100] if c):
                                    # Quitar prefijos de data:image si existen
                                    if "," in img_str and "base64" in img_str:
                                        img_str = img_str.split(",")[-1]
                                    # Limpiar espacios en blanco
                                    img_str = img_str.strip().replace('\n', '').replace('\r', '')
                                    img_bytes = base64.b64decode(img_str, validate=True)
                                    _logger.warning("🔍 Imagen %s decodificada desde base64 (UTF-8)", idx)
                                else:
                                    # No parece base64, tratar como binaria
                                    img_bytes = img_input
                                    _logger.warning("🔍 Imagen %s no parece base64, tratada como binaria", idx)
                            except (UnicodeDecodeError, Exception) as e:
                                # Si no se puede decodificar como UTF-8, puede ser:
                                # 1. Base64 con encoding diferente
                                # 2. Datos binarios de imagen
                                try:
                                    # Intentar con latin-1 (más permisivo)
                                    img_str = img_input.decode('latin-1')
                                    if "," in img_str and "base64" in img_str:
                                        img_str = img_str.split(",")[-1]
                                    img_str = img_str.strip().replace('\n', '').replace('\r', '')
                                    # Verificar si parece base64
                                    if all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' 
                                           for c in img_str[:100] if c):
                                        img_bytes = base64.b64decode(img_str, validate=False)
                                        _logger.warning("🔍 Imagen %s decodificada desde base64 (latin-1)", idx)
                                    else:
                                        raise ValueError("No parece base64")
                                except Exception:
                                    # Último recurso: asumir que ya es imagen binaria
                                    img_bytes = img_input
                                    _logger.warning("🔍 Imagen %s tratada como binaria directa (falló decodificación)", idx)
    
                    elif isinstance(img_input, str):
                        # En Odoo, los campos Image devuelven strings base64
                        # Quitar prefijos de data:image si existen
                        if "," in img_input and "base64" in img_input:
                            img_input = img_input.split(",")[-1]
                        img_bytes = base64.b64decode(img_input, validate=True)
                        _logger.warning("🔍 Imagen %s decodificada desde string base64", idx)
                    else:
                        _logger.warning("⚠️ Tipo de imagen no manejado: %s", type(img_input))
                        continue
                except Exception as decode_err:
                    _logger.error("💥 Error decodificando imagen %s: %s", idx, decode_err, exc_info=True)
                    continue
    
                if not img_bytes or len(img_bytes) == 0:
                    _logger.error("🚫 Imagen %s está vacía después de decodificar", idx)
                    continue
                
                # Validar tamaño mínimo (una imagen válida debe tener al menos algunos bytes)
                # Una imagen PNG válida normalmente tiene al menos 1-2 KB
                # Una imagen JPEG válida normalmente tiene al menos 500 bytes
                MIN_IMAGE_SIZE = 500  # Tamaño mínimo razonable para una imagen válida
                if len(img_bytes) < MIN_IMAGE_SIZE:
                    _logger.error("🚫 Imagen %s es demasiado pequeña (%d bytes < %d bytes), probablemente está corrupta o incompleta. Omitiendo...", 
                                idx, len(img_bytes), MIN_IMAGE_SIZE)
                    continue
                
                _logger.warning("🔍 Imagen %s: %d bytes luego de decodificar", idx, len(img_bytes))
    
                # Mostrar primeros bytes para debugging
                if len(img_bytes) >= 16:
                    _logger.warning("🔍 Primeros bytes (hex): %s", img_bytes[:16].hex())
    
                # =====================================================
                # 🔹 Validar y abrir con PIL (reemplaza imghdr)
                # =====================================================
                try:
                    # Verificar si es WEBP (PIL puede no soportarlo sin plugin)
                    is_webp = False
                    if len(img_bytes) >= 12 and img_bytes[:4] == b'RIFF' and b'WEBP' in img_bytes[:12]:
                        is_webp = True
                        _logger.warning("🔍 Imagen %s es WEBP, intentando convertir...", idx)
                        # Intentar convertir WEBP usando Wand si está disponible
                        try:
                            from wand.image import Image as WandImage
                            with WandImage(blob=img_bytes) as wand_img:
                                wand_img.format = 'jpeg'
                                img_bytes = wand_img.make_blob('jpeg')
                                img_buffer = BytesIO(img_bytes)
                                image = Image.open(img_buffer)
                                image.load()
                                _logger.warning("✅ WEBP convertido a JPEG usando Wand")
                                is_webp = False  # Ya convertido
                        except ImportError:
                            _logger.warning("⚠️ WEBP no soportado por PIL y Wand no disponible. Saltando imagen WEBP.")
                            _logger.warning("💡 Solución: Instala Wand (pip install Wand) o convierte la imagen a PNG/JPEG antes de subirla.")
                            continue
                        except Exception as wand_err:
                            _logger.error("💥 Error convirtiendo WEBP con Wand: %s", wand_err)
                            continue
                    
                    # Si no es WEBP o ya fue convertido, abrir con PIL
                    if not is_webp:
                        # Intentar abrir la imagen con PIL
                        _logger.warning("🔍 Imagen %s no es WEBP, abriendo con PIL...", idx)
                        img_buffer = BytesIO(img_bytes)
                        try:
                            image = Image.open(img_buffer)
                            # Forzar carga completa de la imagen
                            image.load()
                            _logger.warning("✅ Imagen %s abierta con PIL exitosamente", idx)
                        except Exception as pil_err:
                            if 'WEBP' in str(pil_err) or 'cannot identify' in str(pil_err).lower():
                                _logger.warning("⚠️ PIL no puede abrir la imagen. Intentando con Wand...")
                                try:
                                    from wand.image import Image as WandImage
                                    with WandImage(blob=img_bytes) as wand_img:
                                        wand_img.format = 'jpeg'
                                        img_bytes = wand_img.make_blob('jpeg')
                                        img_buffer = BytesIO(img_bytes)
                                        image = Image.open(img_buffer)
                                        image.load()
                                        _logger.warning("✅ Imagen convertida a JPEG usando Wand")
                                except ImportError:
                                    _logger.error("💥 No se puede procesar la imagen. Wand no disponible.")
                                    continue
                                except Exception as wand_err:
                                    _logger.error("💥 Error procesando imagen con Wand: %s", wand_err)
                                    continue
                            else:
                                raise pil_err
                    
                    # Verificar que la imagen es válida y que la variable image existe
                    if 'image' not in locals():
                        raise ValueError("La variable 'image' no fue definida - la imagen no se pudo abrir")
                    if not hasattr(image, 'format') or not image.format:
                        raise ValueError("No se pudo determinar el formato de la imagen")
                    
                    # Validar que la imagen tenga un tamaño razonable
                    if not hasattr(image, 'size') or not image.size:
                        raise ValueError("La imagen no tiene tamaño válido")
                    
                    width, height = image.size
                    if width < 1 or height < 1:
                        raise ValueError(f"La imagen tiene dimensiones inválidas: {width}x{height}")
                    
                    _logger.warning("📸 Imagen %s abierta correctamente (formato: %s, tamaño: %s, modo: %s)", 
                                  idx, image.format, image.size, image.mode)
                except Exception as e:
                    _logger.error("💥 Imagen %s irreparable: %s", idx, e, exc_info=True)
                    # Intentar mostrar más información sobre los bytes
                    if len(img_bytes) >= 8:
                        _logger.error("💥 Primeros 8 bytes (hex): %s", img_bytes[:8].hex())
                        _logger.error("💥 Primeros 8 bytes (repr): %s", repr(img_bytes[:8]))
                    continue
    
                # =====================================================
                # 🔹 Validar y redimensionar si es necesario
                # ML requiere: mínimo 500px en un lado, mínimo 50px en el otro
                # =====================================================
                width, height = image.size
                min_side = min(width, height)
                max_side = max(width, height)
                
                # ML requiere: al menos 500px en un lado y 50px en el otro
                ML_MIN_LARGE = 500
                ML_MIN_SMALL = 50
                
                needs_resize = False
                if max_side < ML_MIN_LARGE or min_side < ML_MIN_SMALL:
                    needs_resize = True
                    _logger.warning("⚠️ Imagen %s es muy pequeña (%dx%d). Redimensionando para cumplir requisitos de ML...", 
                                  idx, width, height)
                    
                    # Calcular nuevo tamaño manteniendo aspect ratio
                    # Prioridad 1: Asegurar que el lado pequeño sea >= 50px
                    # Prioridad 2: Asegurar que el lado grande sea >= 500px
                    
                    if min_side < ML_MIN_SMALL:
                        # Primero escalar para que el lado pequeño sea al menos 50px
                        scale = ML_MIN_SMALL / min_side
                        new_width = int(width * scale)
                        new_height = int(height * scale)
                    else:
                        new_width = width
                        new_height = height
                    
                    # Luego verificar que el lado grande sea >= 500px
                    new_max_side = max(new_width, new_height)
                    if new_max_side < ML_MIN_LARGE:
                        scale = ML_MIN_LARGE / new_max_side
                        new_width = int(new_width * scale)
                        new_height = int(new_height * scale)
                    
                    # Redimensionar con alta calidad (compatible con versiones antiguas de PIL)
                    try:
                        # Pillow >= 9.1.0
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:
                        # Pillow < 9.1.0
                        resample = Image.LANCZOS
                    image = image.resize((new_width, new_height), resample)
                    _logger.warning("✅ Imagen %s redimensionada a %dx%d", idx, new_width, new_height)
    
                # =====================================================
                # 🔹 Convertir a JPG
                # =====================================================
                if image.mode in ("RGBA", "LA", "P"):
                    # Convertir imágenes con transparencia o paleta a RGB
                    if image.mode == "P" and "transparency" in image.info:
                        image = image.convert("RGBA")
                    bg = Image.new("RGB", image.size, (255, 255, 255))
                    if image.mode == "RGBA":
                        bg.paste(image, mask=image.split()[-1])
                    else:
                        bg.paste(image)
                    image = bg
                elif image.mode != "RGB":
                    image = image.convert("RGB")
    
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=90)
                img_jpeg = buffer.getvalue()
                
                # Verificar tamaño final
                final_width, final_height = image.size
                _logger.info("📐 Imagen %s final: %dx%d (%d bytes)", idx, final_width, final_height, len(img_jpeg))
    
                # =====================================================
                # 🔹 Subir a Mercado Libre
                # =====================================================
                # La API de ML para subir imágenes requiere el token como query parameter
                # según la documentación: https://api.mercadolibre.com/pictures?access_token=TOKEN
                if not account.access_token:
                    _logger.error("❌ No hay token de acceso disponible para subir imagen %s", idx)
                    continue
                
                upload_url = f"https://api.mercadolibre.com/pictures?access_token={account.access_token}"
                files = {'file': (f'image_{idx}.jpg', img_jpeg, 'image/jpeg')}
    
                _logger.info("📤 Subiendo imagen %s/%s (%d bytes) a ML con token: %s...", 
                           idx, total_images, len(img_jpeg), account.access_token[:20] + "..." if account.access_token else "None")
                resp = requests.post(upload_url, files=files, timeout=30)
    
                if resp.ok:
                    data = resp.json()
                    # La API de ML devuelve el ID de la imagen, que es lo que necesitamos
                    picture_id = data.get("id") or data.get("picture_id")
                    if not picture_id:
                        _logger.error("❌ La respuesta de ML no contiene ID de imagen. Respuesta completa: %s", data)
                        continue
                    
                    # Obtener URL de la variación más grande disponible
                    variations = data.get("variations", [])
                    best_url = None
                    if variations:
                        # Buscar la variación más grande (normalmente la 'B' de 800x800)
                        for var in variations:
                            if var.get("size") == "800x800":
                                best_url = var.get("secure_url") or var.get("url")
                                break
                        # Si no hay 800x800, tomar la primera
                        if not best_url and variations:
                            best_url = variations[0].get("secure_url") or variations[0].get("url")
                    
                    # Guardar el ID de la imagen para usar en el payload
                    # ML espera {"id": picture_id} en el array de pictures
                    variant_id_from_payload = img_payload.get("variant_id")
                    ml_image_id_from_payload = img_payload.get("ml_image_id")
                    
                    # Obtener product_variant_id desde la imagen si está disponible
                    product_variant_id_from_payload = None
                    if ml_image_id_from_payload:
                        ml_image = self.env["ml.publication.image"].browse(ml_image_id_from_payload)
                        if ml_image.product_variant_id:
                            product_variant_id_from_payload = ml_image.product_variant_id.id
                    
                    image_dict = {
                        "id": picture_id,
                        "source": best_url or data.get("secure_url") or data.get("url") or data.get("source"),
                        "ml_image_id": ml_image_id_from_payload,
                        "variant_id": variant_id_from_payload,  # Mantener para compatibilidad
                        "product_variant_id": product_variant_id_from_payload,  # Nuevo campo
                    }
                    images.append(image_dict)
                    _logger.info("📝 image_dict agregado: picture_id=%s, variant_id=%s, product_variant_id=%s, ml_image_id=%s", 
                               picture_id, variant_id_from_payload, product_variant_id_from_payload, ml_image_id_from_payload)

                    # Guardar ml_picture_id en el registro de ml.publication.image
                    if ml_image_id_from_payload:
                        ml_image = self.env["ml.publication.image"].with_context(skip_ml_publish=True).browse(ml_image_id_from_payload)
                        ml_image.write({"ml_picture_id": picture_id})
                        _logger.info("✅ ml_picture_id guardado en ml.publication.image (id=%s): %s", ml_image_id_from_payload, picture_id)
                        
                        # Si la imagen tiene variant_id, también actualizar las imágenes de la variante
                        if ml_image.variant_id:
                            variant = ml_image.variant_id
                            # Buscar si ya existe una imagen en variant.image_ids con este ml_image_id
                            existing_img = variant.image_ids.filtered(lambda img: img.id == ml_image_id_from_payload)
                            if existing_img:
                                existing_img.write({"ml_picture_id": picture_id})
                                _logger.info("✅ ml_picture_id también guardado en variante.image_ids (variant_id=%s, image_id=%s): %s", 
                                           variant.id, ml_image_id_from_payload, picture_id)

                    # Guardar image_ml_picture_id legacy en la variante (para compatibilidad)
                    if variant_id_from_payload:
                        self.env["ml.publication.variant"].browse(variant_id_from_payload).write({"image_ml_picture_id": picture_id})
                        _logger.info("✅ image_ml_picture_id guardado en variante (id=%s): %s", variant_id_from_payload, picture_id)

                    _logger.info("✅ Imagen subida correctamente - ID: %s, URL: %s, Tamaño: %s", 
                               picture_id, best_url or data.get("secure_url") or data.get("url"), data.get("max_size", "N/A"))
                else:
                    _logger.error("❌ Error al subir imagen a ML (status %s): %s", resp.status_code, resp.text)
    
            except Exception as e:
                _logger.error("💥 Error procesando imagen %s: %s", idx, e, exc_info=True)
    
        if not images:
            _logger.warning("🚫 No se subieron imágenes válidas a ML.")
    
        return images

    # =====================================================
    # 🔹 ACTUALIZAR ESTADO PUBLICACIÓN EN ML
    # =====================================================
    def _fetch_ml_item_status(self):
        """Estado actual del ítem en Mercado Libre (active, paused, closed, …)."""
        self.ensure_one()
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return None
        account = self.ml_account_id
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = account._ml_request_with_retry("GET", url, headers=headers)
            if resp.status_code in (200, 201):
                return (resp.json() or {}).get("status")
        except Exception as e:
            _logger.warning("No se pudo consultar estado ML del ítem %s: %s", self.ml_item_id, e)
        return None

    def _ml_status_action_notification(self, message, notif_type="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Mercado Libre"),
                "message": message,
                "type": notif_type,
                "sticky": False,
            },
        }

    def _update_ml_status(self, new_status):
        """Actualiza el estado en ML. Una sola llamada por (ml_item_id, cuenta)."""
        if not self:
            return
        if new_status not in ("active", "paused"):
            raise UserError(_("Estado no válido para Mercado Libre: %s") % new_status)

        done = set()
        for rec in self:
            if not rec.ml_item_id:
                raise UserError(_("Esta publicación no tiene un ML Item ID."))
            account = rec.ml_account_id
            if not account or not account.access_token:
                raise UserError(_("No hay una cuenta de Mercado Libre conectada."))

            key = (rec.ml_item_id, account.id)
            if key in done:
                continue
            done.add(key)

            url = f"https://api.mercadolibre.com/items/{rec.ml_item_id}"
            headers = {
                "Authorization": f"Bearer {account.access_token}",
                "Content-Type": "application/json",
            }
            payload = {"status": new_status}

            # ML suele exigir stock > 0 para reactivar; enviamos el stock calculado en Odoo.
            if new_status == "active":
                rec.invalidate_recordset(["stock"])
                rec._compute_stock()
                qty = max(0, int(rec.stock or 0))
                if qty > 0:
                    if rec.ml_variation_id:
                        variations = rec._get_ml_variations_current()
                        if variations:
                            payload["variations"] = [
                                {
                                    "id": v.get("id"),
                                    "available_quantity": (
                                        int(qty)
                                        if str(v.get("id")) == str(rec.ml_variation_id)
                                        else int(v.get("available_quantity") or 0)
                                    ),
                                }
                                for v in variations
                            ]
                    else:
                        payload["available_quantity"] = qty
                else:
                    _logger.warning(
                        "Activando ítem %s sin stock en Odoo (qty=0); ML puede rechazar la activación.",
                        rec.ml_item_id,
                    )

            _logger.info(
                "Cambiando estado en ML: item=%s → %s | cuenta=%s",
                rec.ml_item_id,
                new_status,
                account.name,
            )
            try:
                resp = account._ml_request_with_retry("PUT", url, headers=headers, json=payload)
            except Exception as e:
                _logger.exception("Error de red al cambiar estado ML item %s", rec.ml_item_id)
                raise UserError(_("Error de conexión con Mercado Libre: %s") % e) from e

            if resp.status_code not in (200, 201):
                err_msg = resp.text
                try:
                    body = resp.json()
                    err_msg = body.get("message") or body.get("error") or err_msg
                    causes = body.get("cause") or []
                    if causes and isinstance(causes, list):
                        extra = "; ".join(
                            c.get("message", str(c)) for c in causes if isinstance(c, dict)
                        )
                        if extra:
                            err_msg = f"{err_msg} ({extra})"
                except Exception:
                    pass
                _logger.error(
                    "Error al cambiar estado ML (HTTP %s): %s",
                    resp.status_code,
                    resp.text,
                )
                raise UserError(_("Error al cambiar estado en Mercado Libre: %s") % err_msg)

            result = resp.json() if resp.text else {}
            final_status = result.get("status") or new_status
            same_item = self.env["ml.publication"].search([
                ("ml_item_id", "=", rec.ml_item_id),
                ("ml_account_id", "=", account.id),
            ])
            same_item.write({"ml_status": final_status})
            _logger.info(
                "Estado actualizado en ML: item=%s → %s (%d filas Odoo)",
                rec.ml_item_id,
                final_status,
                len(same_item),
            )

    def action_pause_on_ml(self):
        """Pausa la publicación en Mercado Libre (status=paused)."""
        if not self:
            return False
        self._update_ml_status("paused")
        return self._ml_status_action_notification(
            _("La publicación se pausó en Mercado Libre.")
        )

    def action_activate_on_ml(self):
        """Activa la publicación en Mercado Libre (status=active)."""
        if not self:
            return False
        self._update_ml_status("active")
        return self._ml_status_action_notification(
            _("La publicación se activó en Mercado Libre.")
        )

    def action_toggle_status(self):
        """Pausa si está activa; activa si está pausada."""
        for rec in self:
            if rec.ml_status == "active":
                rec.action_pause_on_ml()
            elif rec.ml_status == "paused":
                rec.action_activate_on_ml()
            else:
                raise UserError(_(
                    "Solo se puede pausar o activar una publicación activa o pausada. "
                    "Estado actual: %s."
                ) % (rec.ml_status or _("sin estado")))
        return self._ml_status_action_notification(
            _("Estado actualizado en Mercado Libre.")
        )

    def _stock_status_rule(self):
        """
        Resuelve pausa/activación: la regla de la publicación pisa la de la cuenta.

        - Pausa (despublicar): stock <= máximo.
        - Activación (publicar): stock >= mínimo. El mínimo 0 no activa (evita republicar todo).
        """
        self.ensure_one()
        account = self.ml_account_id
        pause_enabled = False
        activate_enabled = False
        pause_max = 0
        activate_min = 0

        if self.use_custom_pause_rule:
            pause_enabled = True
            pause_max = int(self.max_stock_to_pause or 0)
        elif account and account.auto_pause_when_max_stock:
            pause_enabled = True
            pause_max = int(account.max_stock_to_pause or 0)

        if self.use_custom_activate_rule:
            activate_min = int(self.min_stock_to_activate or 0)
            activate_enabled = activate_min > 0
        elif account and account.auto_activate_when_min_stock:
            activate_min = int(account.min_stock_to_activate or 0)
            activate_enabled = activate_min > 0

        return pause_enabled, pause_max, activate_enabled, activate_min

    def _check_stock_rules_and_update_status(self):
        """Pausa o activa en ML según el stock de Odoo y las reglas global / de la publicación."""
        seen = set()
        for pub in self:
            if not pub.ml_item_id or not pub.ml_account_id:
                continue
            if pub.ml_status not in ("active", "paused"):
                continue
            key = (pub.ml_item_id, pub.ml_account_id.id)
            if key in seen:
                continue
            seen.add(key)

            pause_enabled, pause_max, activate_enabled, activate_min = pub._stock_status_rule()
            if not pause_enabled and not activate_enabled:
                continue

            account = pub.ml_account_id
            qty = pub._get_product_stock_in_warehouse(account.warehouse_id) if account.warehouse_id else max(0, int(pub.stock or 0))
            target = pub.ml_status
            if pause_enabled and pub.ml_status == "active" and qty <= pause_max:
                target = "paused"
                _logger.info(
                    "⏸️ Regla de pausa ML ítem %s: stock %s <= máximo %s",
                    pub.ml_item_id, qty, pause_max,
                )
            elif activate_enabled and pub.ml_status == "paused" and qty >= activate_min:
                target = "active"
                _logger.info(
                    "▶️ Regla de activación ML ítem %s: stock %s >= mínimo %s",
                    pub.ml_item_id, qty, activate_min,
                )

            if target == pub.ml_status:
                continue
            try:
                pub._update_ml_status(target)
            except Exception as e:
                _logger.exception(
                    "Error aplicando regla de estado ML ítem %s: %s",
                    pub.ml_item_id, e,
                )

    def _ml_request_with_retry(self, method, url, headers=None, payload=None, allowed_status=None, max_retries=3):
        allowed_status = allowed_status or (200, 201)
        retryable_status = {408, 409, 423, 429, 500, 502, 503, 504}
        last_response = None
        for attempt in range(1, max_retries + 1):
            try:
                last_response = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
            except Exception as exc:
                _logger.warning("⚠️ Error en request a ML (%s %s): %s", method, url, exc, exc_info=True)
                time.sleep(attempt)
                continue

            if last_response.status_code in allowed_status:
                return last_response

            if last_response.status_code in retryable_status and attempt < max_retries:
                wait_time = attempt * 2
                _logger.warning(
                    "⏳ Mercado Libre devolvió %s (%s). Reintentando en %s segundos (intento %s/%s)...",
                    last_response.status_code,
                    last_response.text,
                    wait_time,
                    attempt,
                    max_retries,
                )
                time.sleep(wait_time)
                continue

            break

        return last_response

    def _update_meli_publication(self):
        """Actualiza la publicación en Mercado Libre desde un webhook.
        No actualiza price ni available_quantity si el item está activo."""
        self.ensure_one()
        
        # Verificar y refrescar token si es necesario
        if self.ml_account_id:
            self.ml_account_id._ensure_valid_token()
        
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        
        # Obtener el estado actual del item en ML
        item_info = None
        try:
            item_response = requests.get(url, headers=headers, timeout=10)
            if item_response.ok:
                item_info = item_response.json()
                current_status = item_info.get("status")
                _logger.info("📋 Estado actual del item en ML (webhook): %s", current_status)
        except Exception as e:
            _logger.warning("⚠️ No se pudo obtener información del item en webhook: %s", e)
        
        # Solo actualizar cantidad si el item NO está activo o pausado (precio: solo wizard Actualizar valores)
        payload = {}
        current_status = item_info.get("status") if item_info else self.ml_status
        
        if current_status not in ["active", "paused"]:
            payload["available_quantity"] = int(self.stock)
            _logger.info("📝 Item no está activo/pausado, se actualizará cantidad (sin precio).")
        else:
            _logger.info("⚠️ Item está activo/pausado, NO se actualizará cantidad (ML no lo permite).")
            # Si hay variantes, el precio y la cantidad se manejan a nivel de variante
            if self.ml_variant_ids:
                # Preparar variaciones para actualizar
                variations = []
                for variant in self.ml_variant_ids:
                    if variant.attribute_combination_ids:
                        # Buscar imágenes para esta variante
                        variant_picture_ids = []
                        # Buscar en variant.image_ids
                        for img in variant.image_ids:
                            if img.ml_picture_id:
                                variant_picture_ids.append(img.ml_picture_id)
                        # Buscar en ml.publication.image con product_variant_id (variant_id es calculado y no se puede buscar)
                        if variant.product_variant_id:
                            ml_images_with_product_variant = self.ml_image_ids.filtered(
                                lambda img: img.product_variant_id and img.product_variant_id.id == variant.product_variant_id.id
                            )
                            for img in ml_images_with_product_variant:
                                if img.ml_picture_id and img.ml_picture_id not in variant_picture_ids:
                                    variant_picture_ids.append(img.ml_picture_id)
                        # Si no hay imágenes específicas, usar imágenes generales
                        if not variant_picture_ids:
                            general_pics = [pic for pic in self.ml_image_ids.mapped("ml_picture_id") if pic]
                            if general_pics:
                                variant_picture_ids = general_pics
                        
                        variant_dict = variant.to_ml_format(picture_ids=variant_picture_ids if variant_picture_ids else None)
                        if variant_dict.get("attribute_combinations"):
                            variations.append(variant_dict)
                if variations:
                    payload["variations"] = variations
                    _logger.info("📝 Actualizando variantes (stock/imágenes) para item activo/pausado.")
        
        self._strip_price_from_ml_payload(payload)
        if not payload:
            _logger.info("ℹ️ No hay campos para actualizar en el webhook (item activo y sin variantes).")
            return
        
        _logger.info("🔄 Actualizando publicación %s en ML con payload %s", self.ml_item_id, payload)
        resp = self._ml_request_with_retry("PUT", url, headers=headers, payload=payload)
        if resp is None or resp.status_code not in (200, 201):
            if resp is not None and resp.status_code in (409, 429):
                error_detail = resp.text or resp.reason
                _logger.warning("⚠️ Mercado Libre devolvió %s después de reintentos: %s", resp.status_code, error_detail)
                raise ValueError(_("Mercado Libre rechazó la actualización por conflicto o límite de peticiones. Intente nuevamente en unos segundos.\nDetalle: %s") % (error_detail))
            error_msg = resp.text if resp is not None else _("Sin respuesta")
            _logger.error("❌ Error al actualizar ML: %s", error_msg)
            raise ValueError(_("Error al actualizar publicación en Mercado Libre: %s") % error_msg)
        _logger.info("✅ Publicación actualizada correctamente en Mercado Libre.")
        self.last_webhook_sync = fields.Datetime.now()

    # =====================================================
    # 🔹 MATCHEO VENTAS: PUBLICACIÓN → PRODUCTO ODOO
    # =====================================================
    @api.model
    def _ml_find_for_order_item(self, ml_item_id, ml_account_id, variation_id=None):
        """Busca la publicación Odoo vinculada a un ítem de orden ML."""
        if not ml_item_id or not ml_account_id:
            return self.browse()

        domain = [
            ('ml_item_id', '=', str(ml_item_id)),
            ('ml_account_id', '=', ml_account_id),
        ]
        if variation_id:
            var_str = str(variation_id)
            pub = self.search(domain + [('ml_variation_id', '=', var_str)], limit=1)
            if pub:
                return pub
            parent = self.search(domain + [('ml_variation_id', 'in', (False, ''))], limit=1)
            return parent

        pub = self.search(domain + [('ml_variation_id', 'in', (False, ''))], limit=1)
        if pub:
            return pub
        return self.search(domain, limit=1)

    def _ml_get_odoo_products_for_sale(self):
        """Producto template y variante Odoo vinculados a esta publicación."""
        self.ensure_one()
        if not self.product_tmpl_id:
            return self.env['product.template'], self.env['product.product']
        tmpl = self.product_tmpl_id
        variant = self.product_variant_id
        if not variant and len(tmpl.product_variant_ids) == 1:
            variant = tmpl.product_variant_id
        return tmpl, variant

    # =====================================================
    # 🔹 MATCHEO CON PRODUCTOS DE ODOO POR SKU
    # =====================================================
    def _get_sku_for_product_match(self):
        """SKU de la publicación (seller_sku, atributo SELLER_SKU o variantes)."""
        self.ensure_one()
        sku = False
        if self.seller_sku:
            sku = str(self.seller_sku).strip()
        if not sku:
            seller_sku_attr = self.ml_attribute_ids.filtered(
                lambda a: a.ml_attribute_id == 'SELLER_SKU'
            )
            if seller_sku_attr and seller_sku_attr[0].value_name:
                sku = str(seller_sku_attr[0].value_name).strip()
        if not sku and self.ml_variant_ids:
            for variant in self.ml_variant_ids:
                if variant.seller_sku:
                    sku = str(variant.seller_sku).strip()
                    break
        if sku in ('', 'False', 'None'):
            return False
        return sku or False

    def _find_odoo_product_by_sku(self, sku):
        """
        Busca producto Odoo por default_code (variante o plantilla).
        Returns:
            tuple(product.template record|False, product.product record|False)
        """
        self.ensure_one()
        if not sku:
            return self.env['product.template'], self.env['product.product']
        company_domain = [
            '|',
            ('company_id', '=', self.env.company.id),
            ('company_id', '=', False),
        ]
        ProductProduct = self.env['product.product']
        product_variant = ProductProduct.search(
            [('default_code', '=', sku)] + company_domain,
            limit=1,
        )
        if product_variant:
            return product_variant.product_tmpl_id, product_variant
        product = self.env['product.template'].search(
            [('default_code', '=', sku)] + company_domain,
            limit=1,
        )
        return product, self.env['product.product']

    def _sync_product_link_by_sku(self):
        """
        Relaciona esta publicación con un producto Odoo por SKU.

        Solo escribe product_tmpl_id / product_variant_id (o los limpia).
        No envía stock ni precio a ML, no toca current_*/new_* ni variantes ML,
        y no dispara recarga de variantes desde el producto.

        Returns:
            str: 'matched' | 'updated' | 'unlinked' | 'unchanged' | 'no_sku' | 'no_product'
        """
        self.ensure_one()
        sku = self._get_sku_for_product_match()
        had_product = bool(self.product_tmpl_id)
        old_product_name = self.product_tmpl_id.display_name if self.product_tmpl_id else ''
        link_self = self.with_context(ml_sku_link_only=True)

        if not sku:
            if had_product:
                link_self.write({'product_tmpl_id': False, 'product_variant_id': False})
                _logger.info(
                    "Publicación %s sin SKU: se quitó producto «%s»",
                    self.ml_item_id or self.id,
                    old_product_name,
                )
                return 'unlinked'
            return 'no_sku'

        product, product_variant = self._find_odoo_product_by_sku(sku)

        if product:
            vals = {'product_tmpl_id': product.id}
            if product_variant:
                vals['product_variant_id'] = product_variant.id
            elif len(product.product_variant_ids) == 1:
                vals['product_variant_id'] = product.product_variant_id.id
            else:
                vals['product_variant_id'] = False

            same_tmpl = self.product_tmpl_id.id == product.id
            same_variant = self.product_variant_id.id == vals.get('product_variant_id')
            if same_tmpl and same_variant:
                return 'unchanged'

            link_self.write(vals)
            if had_product:
                _logger.info(
                    "Publicación %s (SKU %s): producto actualizado «%s» → «%s»",
                    self.ml_item_id or self.id,
                    sku,
                    old_product_name,
                    product.display_name,
                )
                return 'updated'
            _logger.info(
                "Publicación %s matcheada con %s (SKU %s)",
                self.ml_item_id or self.id,
                product.display_name,
                sku,
            )
            return 'matched'

        if had_product:
            link_self.write({'product_tmpl_id': False, 'product_variant_id': False})
            _logger.info(
                "Publicación %s (SKU %s): sin producto en Odoo, se quitó «%s»",
                self.ml_item_id or self.id,
                sku,
                old_product_name,
            )
            return 'unlinked'

        return 'no_product'

    def _auto_match_product_by_sku(self):
        """
        Matchea automáticamente esta publicación con un producto de Odoo usando SKU.
        Solo si aún no tiene producto relacionado (importación / flujos automáticos).
        """
        self.ensure_one()
        if self.product_tmpl_id:
            return False
        result = self._sync_product_link_by_sku()
        return result in ('matched', 'updated')
    
    def action_match_product_by_sku(self):
        """
        Matchea esta publicación con un producto de Odoo usando SKU.
        Revalida el vínculo actual: si el SKU cambió o no hay producto en Odoo, quita el relacionado.
        """
        self.ensure_one()
        sku = self._get_sku_for_product_match()
        result = self._sync_product_link_by_sku()

        if result == 'no_sku':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin SKU'),
                    'message': _(
                        'Esta publicación no tiene SKU. Si tenía producto relacionado, se quitó el vínculo.'
                    ),
                    'type': 'warning',
                    'sticky': False,
                },
            }
        if result == 'no_product':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Producto no encontrado'),
                    'message': _('No hay producto en Odoo con SKU «%s».') % sku,
                    'type': 'warning',
                    'sticky': False,
                },
            }
        if result == 'unchanged':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin cambios'),
                    'message': _('El producto relacionado ya coincide con el SKU «%s».') % sku,
                    'type': 'info',
                    'sticky': False,
                },
            }
        if result == 'unlinked':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Vínculo quitado'),
                    'message': _('No existe producto en Odoo con SKU «%s». Se quitó el producto relacionado.') % sku,
                    'type': 'warning',
                    'sticky': False,
                },
            }

        product = self.product_tmpl_id
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Matcheo exitoso'),
                'message': _('Publicación vinculada a: %s (SKU: %s)') % (product.display_name, sku),
                'type': 'success',
                'sticky': False,
            },
        }

    # =====================================================
    # 🔹 IMPORTACIÓN MANUAL DESDE MERCADO LIBRE
    # =====================================================
    def _update_current_stock_from_ml(self):
        """
        Actualiza el campo current_stock_ml consultando el stock actual desde la API de Mercado Libre.
        Se usa después de procesar una venta para sincronizar el stock real de ML.
        
        Returns:
            bool: True si se actualizó correctamente, False en caso contrario
        """
        self.ensure_one()
        
        if not self.ml_item_id:
            _logger.warning("⚠️ No se puede actualizar current_stock_ml: publicación %s no tiene ml_item_id", self.id)
            return False
        
        if not self.ml_account_id:
            _logger.warning("⚠️ No se puede actualizar current_stock_ml: publicación %s no tiene ml_account_id", self.id)
            return False
        
        if not self.ml_account_id.access_token:
            _logger.warning("⚠️ No se puede actualizar current_stock_ml: cuenta %s no tiene access_token", self.ml_account_id.name)
            return False
        
        try:
            # Verificar y refrescar token si es necesario
            self.ml_account_id._ensure_valid_token()
            
            # Consultar el stock actual desde la API de ML
            url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
            headers = {
                "Authorization": f"Bearer {self.ml_account_id.access_token}",
                "Content-Type": "application/json",
            }
            
            _logger.info("🔄 Consultando stock actual desde ML para publicación %s (ML Item ID: %s)", 
                        self.title, self.ml_item_id)
            
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 401:
                # Token expirado, intentar refrescar y reintentar
                _logger.warning("🔄 Token expirado, refrescando...")
                self.ml_account_id.refresh_access_token()
                headers["Authorization"] = f"Bearer {self.ml_account_id.access_token}"
                response = requests.get(url, headers=headers, timeout=10)
            
            if response.ok:
                item_data = response.json()
                ml_stock = item_data.get('available_quantity', 0)
                
                # Actualizar current_stock_ml con el valor real de ML
                self.sudo().write({
                    'current_stock_ml': int(ml_stock) if ml_stock else 0
                })
                
                _logger.info("✅ current_stock_ml actualizado desde ML: %s → %d (ML Item ID: %s)", 
                            self.title, ml_stock, self.ml_item_id)
                return True
            else:
                _logger.warning("⚠️ Error consultando stock desde ML para publicación %s: %s (status: %d)", 
                              self.title, response.text, response.status_code)
                return False
                
        except Exception as e:
            _logger.exception("❌ Error actualizando current_stock_ml desde ML para publicación %s: %s", 
                            self.title, e)
            return False

    def action_import_from_ml(self):
        """
        Importa manualmente la publicación desde Mercado Libre.
        
        Consulta la API de ML, obtiene los datos del item y los sincroniza con Odoo.
        Muestra errores claros al usuario si algo falla.
        """
        self.ensure_one()
        
        if not self.ml_item_id:
            raise UserError(_("No se puede importar: esta publicación no tiene un ML Item ID asignado."))
        
        if not self.ml_account_id:
            raise UserError(_("No se puede importar: no hay una cuenta de Mercado Libre asignada."))
        
        if not self.ml_account_id.access_token:
            raise UserError(_("No se puede importar: la cuenta de Mercado Libre no tiene un token de acceso válido."))
        
        # Verificar y refrescar token si es necesario
        self.ml_account_id._ensure_valid_token()
        
        # Log detallado de la cuenta que se está usando
        _logger.info("🔑 Importando publicación ID=%d, ML Item ID=%s", self.id, self.ml_item_id)
        _logger.info("🔑 Usando cuenta: ID=%d, Nombre=%s, ML User ID=%s", 
                    self.ml_account_id.id, self.ml_account_id.name, 
                    self.ml_account_id.meli_user_id or 'N/A')
        
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        
        try:
            # Consultar el item desde ML
            url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
            _logger.info("📥 Importando publicación desde ML: %s (Cuenta ID: %d)", url, self.ml_account_id.id)
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 404:
                # Item no existe o fue eliminado
                self.ml_status = False
                # El campo state se calculará automáticamente con _compute_state
                raise UserError(_("El item no existe en Mercado Libre o fue eliminado.\n\nItem ID: %s") % self.ml_item_id)
            
            if response.status_code == 401:
                raise UserError(_("Error de autenticación: el token de acceso ha expirado o es inválido.\n\nPor favor, reconecte su cuenta de Mercado Libre."))
            
            if response.status_code == 403:
                raise UserError(_("Error de permisos: no tiene permisos para acceder a este item.\n\nVerifique que el item pertenezca a su cuenta de Mercado Libre."))
            
            if not response.ok:
                error_data = response.text
                try:
                    error_json = response.json()
                    error_message = error_json.get('message', error_data)
                    error_cause = error_json.get('cause', [])
                    if error_cause:
                        cause_messages = [c.get('message', '') for c in error_cause if isinstance(c, dict)]
                        if cause_messages:
                            error_message += "\n\nDetalles:\n" + "\n".join(f"• {msg}" for msg in cause_messages)
                except Exception:
                    error_message = error_data

                _logger.error("❌ Error al importar desde ML (status %s): %s", response.status_code, error_message)
                raise UserError(_("Error al importar publicación desde Mercado Libre:\n\n%s") % error_message)
            
            # Parsear respuesta
            item_data = response.json()
            
            # Si esta publicación representa una sola variación (ml_variation_id), tomar datos de esa variación
            if self.ml_variation_id:
                _logger.warning("[SKU_VARIANT] action_import_from_ml pub_id=%s ml_item_id=%s ml_variation_id=%s", self.id, self.ml_item_id, self.ml_variation_id)
                item_variations = item_data.get('variations') or []
                _logger.warning("[SKU_VARIANT]   item_data.variations count=%s", len(item_variations))
                for i, v in enumerate(item_variations[:3]):
                    _logger.warning("[SKU_VARIANT]   item var[%s] id=%s keys=%s seller_custom_field=%s seller_sku=%s inventory_id=%s attributes=%s",
                                    i, v.get('id'), list(v.keys()), v.get('seller_custom_field'), v.get('seller_sku'), v.get('inventory_id'), v.get('attributes'))
                var = None
                for v in item_variations:
                    if str(v.get('id') or '') == str(self.ml_variation_id):
                        var = v
                        break
                _logger.warning("[SKU_VARIANT]   var encontrada en item_data: %s", 'sí' if var else 'no')
                # GET /items/:id/variations/:variation_id devuelve la variación con attributes (incl. SELLER_SKU)
                need_sku = not var or not (var.get('seller_custom_field') or var.get('seller_sku') or (var.get('attributes') and any((a.get('id') or '').upper() == 'SELLER_SKU' for a in (var.get('attributes') or []))))
                if need_sku and self.ml_variation_id:
                    try:
                        one_url = f"https://api.mercadolibre.com/items/{self.ml_item_id}/variations/{self.ml_variation_id}"
                        one_resp = requests.get(one_url, headers=headers, timeout=15)
                        if one_resp.ok:
                            one_var = one_resp.json()
                            if isinstance(one_var, dict):
                                var = one_var if not var else {**var, **one_var}
                                _logger.info("📦 Variación %s (detalle): attributes=%s", self.ml_variation_id, one_var.get('attributes'))
                    except Exception as e:
                        _logger.debug("GET /items/.../variations/%s: %s", self.ml_variation_id, e)
                if need_sku and (not var or not (var.get('attributes') and any((a.get('id') or '').upper() == 'SELLER_SKU' for a in (var.get('attributes') or [])))):
                    try:
                        var_resp = requests.get(f"https://api.mercadolibre.com/items/{self.ml_item_id}/variations", headers=headers, timeout=15)
                        if var_resp.ok:
                            var_payload = var_resp.json()
                            var_list = var_payload.get('variations') if isinstance(var_payload, dict) else (var_payload if isinstance(var_payload, list) else [])
                            for v in var_list or []:
                                if str(v.get('id') or '') == str(self.ml_variation_id):
                                    var = v if not var else {**var, **v}
                                    break
                    except Exception as e:
                        _logger.debug("GET /variations: %s", e)
                if var is not None:
                    item_title = item_data.get('title', self.title)
                    combo = var.get('attribute_combinations') or []
                    parts = [str(ac.get('value_name') or ac.get('value_id', '')).strip() for ac in combo if ac.get('value_name') or ac.get('value_id')]
                    variant_label = ', '.join(parts) if parts else (var.get('seller_custom_field') or '').strip() or self.ml_variation_id
                    self.title = f"{item_title} | {variant_label}"
                    self.name = self.title  # Solo para diferenciar variantes; no incluir cuenta
                    self.current_price_ml = float(var.get('price') or 0)
                    self.current_stock_ml = int(var.get('available_quantity') or 0)
                    # SKU: seller_custom_field, attributes SELLER_SKU, seller_sku, inventory_id (fallback)
                    sku_var = (var.get('seller_custom_field') or var.get('seller_sku') or '').strip()
                    _logger.warning("[SKU_VARIANT]   action_import sku paso1 seller_custom_field/seller_sku=%s", sku_var or '(vacío)')
                    if not sku_var and var.get('attributes'):
                        for a in var.get('attributes', []):
                            _logger.warning("[SKU_VARIANT]   attribute id=%s value_name=%s value_id=%s", a.get('id'), a.get('value_name'), a.get('value_id'))
                            if (a.get('id') or '').upper() == 'SELLER_SKU' and (a.get('value_name') or a.get('value_id')):
                                sku_var = str(a.get('value_name') or a.get('value_id', '')).strip()
                                _logger.warning("[SKU_VARIANT]   SKU desde attributes: %s", sku_var)
                                break
                    if not sku_var and combo:
                        for ac in combo:
                            if (ac.get('id') or '').upper() == 'SELLER_SKU' and (ac.get('value_name') or ac.get('value_id')):
                                sku_var = str(ac.get('value_name') or ac.get('value_id', '')).strip()
                                _logger.warning("[SKU_VARIANT]   SKU desde attribute_combinations: %s", sku_var)
                                break
                    if not sku_var and var.get('inventory_id'):
                        sku_var = str(var.get('inventory_id', '')).strip()
                        _logger.info("📝 Usando inventory_id como SKU para variación: %s", sku_var)
                    if not sku_var and var.get('user_product_id'):
                        sku_var = str(var.get('user_product_id', '')).strip()
                        _logger.info("📝 Usando user_product_id como SKU (ML no devolvió SELLER_SKU): %s", sku_var)
                    if not sku_var:
                        sku_var = str(self.ml_variation_id or '')
                        _logger.info("📝 Usando id variación ML como SKU para matcheo: %s", sku_var)
                    _logger.warning("[SKU_VARIANT]   action_import sku_var FINAL=%s (seller_sku actual pub=%s)", sku_var or '(VACÍO)', self.seller_sku or '(vacío)')
                    if sku_var:
                        self.seller_sku = sku_var
                        _logger.info("📝 SKU de variación importado: %s", self.seller_sku)
                    elif not self.seller_sku:
                        _logger.warning("⚠️ Variación ML %s sin SKU (seller_custom_field=%s, attributes=%s, inventory_id=%s); matcheo por SKU no podrá asociar producto.",
                                      self.ml_variation_id, var.get('seller_custom_field'), [a.get('id') for a in (var.get('attributes') or [])], var.get('inventory_id'))
                # ml_status y permalink a nivel ítem
                self.ml_status = item_data.get('status', self.ml_status)
                sub_status = self._ml_format_sub_status(item_data)
                if sub_status is not False:
                    self.ml_sub_status = sub_status or False
                self.permalink = item_data.get('permalink', self.permalink)
                if self.ml_account_id:
                    self.is_full = self.ml_account_id._meli_item_data_is_full(item_data)
            else:
                # Publicación de ítem completo (sin variaciones o ítem sin variaciones)
                self.title = item_data.get('title', self.title)
                self.ml_status = item_data.get('status', self.ml_status)
                sub_status = self._ml_format_sub_status(item_data)
                if sub_status is not False:
                    self.ml_sub_status = sub_status or False
                self.permalink = item_data.get('permalink', self.permalink)
                if self.ml_account_id:
                    self.is_full = self.ml_account_id._meli_item_data_is_full(item_data)
                if 'price' in item_data:
                    self.current_price_ml = item_data.get('price', 0.0)
                if 'available_quantity' in item_data:
                    self.current_stock_ml = item_data.get('available_quantity', 0)
                seller_custom_field = item_data.get('seller_custom_field')
                sku_imported = False
                if seller_custom_field:
                    seller_sku_str = str(seller_custom_field).strip()
                    if seller_sku_str and seller_sku_str not in ('False', 'None'):
                        self.seller_sku = seller_sku_str
                        sku_imported = True
                    else:
                        self.seller_sku = ''
                if not sku_imported and item_data.get('attributes'):
                    for attr in item_data.get('attributes', []):
                        if attr.get('id') == 'SELLER_SKU' and attr.get('value_name'):
                            sku_value = str(attr.get('value_name')).strip()
                            if sku_value and sku_value not in ('False', 'None'):
                                self.seller_sku = sku_value
                                break
            
            # Actualizar categoría si está disponible
            category_id_ml = item_data.get('category_id')
            if category_id_ml:
                self.category_id = category_id_ml
            
            # Sincronizar atributos desde ML (manteniendo español)
            if item_data.get('attributes'):
                self._sync_attributes_from_ml(item_data.get('attributes'), category_id_ml, headers)
            
            # Solo sincronizar variaciones si esta publicación NO es una variación (ítem con varias variaciones = una pub con ml_variant_ids)
            if not self.ml_variation_id:
                self._sync_variations_from_ml(item_data)
            
            # Importar imágenes desde ML
            if self.env.context.get('ml_import_download_images', True):
                self._import_images_from_ml(item_data, headers)
            
            # Intentar matchear automáticamente con producto de Odoo por SKU si no tiene producto relacionado
            if not self.product_tmpl_id and self.seller_sku:
                self._auto_match_product_by_sku()
            
            # Actualizar estado calculado
            self._compute_state()

            # Precio a enviar: por defecto = precio actual en ML (el usuario puede editarlo después)
            self._set_default_new_price_from_current_ml()
            
            _logger.info("✅ Publicación importada correctamente desde ML: %s (Estado: %s)", self.ml_item_id, self.state)

            if self.env.context.get('ml_import_silent'):
                return True

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Importación exitosa'),
                    'message': _('La publicación se importó correctamente desde Mercado Libre.\nEstado: %s') % self.state,
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except UserError:
            # Re-lanzar UserError tal cual (ya tiene mensaje claro)
            raise
        except requests.exceptions.Timeout:
            _logger.error("❌ Timeout al importar desde ML: %s", self.ml_item_id)
            raise UserError(_("Timeout al conectar con Mercado Libre.\n\nPor favor, intente nuevamente en unos momentos."))
        except requests.exceptions.ConnectionError:
            _logger.error("❌ Error de conexión al importar desde ML: %s", self.ml_item_id)
            raise UserError(_("Error de conexión con Mercado Libre.\n\nVerifique su conexión a internet e intente nuevamente."))
        except Exception as e:
            _logger.error("❌ Error inesperado al importar desde ML: %s", str(e), exc_info=True)
            raise UserError(_("Error inesperado al importar publicación:\n\n%s\n\nPor favor, contacte al administrador si el problema persiste.") % str(e))
    
    def action_update_values_from_product(self):
        """
        Actualiza el stock desde el producto relacionado y lo envía a MercadoLibre.
        El precio no se sincroniza desde Odoo; use el wizard «Actualizar valores».
        """
        self.ensure_one()
        
        if not self.product_tmpl_id:
            raise UserError(_("No hay producto relacionado para actualizar los valores."))
        
        if not self.ml_item_id:
            raise UserError(_("Esta publicación no está publicada en MercadoLibre. Publique primero la publicación."))
        
        if not self.ml_account_id or not self.ml_account_id.access_token:
            raise UserError(_("La cuenta de MercadoLibre no está conectada. Autorice la cuenta primero."))
        
        # Verificar y refrescar token si es necesario
        self.ml_account_id._ensure_valid_token()
        
        updated_fields = []
        stock_changed = False
        
        # Actualizar STOCK
        # Usar el método _compute_stock que ya maneja kits correctamente
        try:
            # Leer stock anterior antes de recalcular
            current_stock = int(self.stock or 0)
            
            # Recalcular stock (esto ya maneja kits)
            self._compute_stock()
            
            # Leer el nuevo stock calculado
            new_stock = int(self.stock or 0)
            
            _logger.info("📦 Comparando stock: actual=%d, nuevo=%d", current_stock, new_stock)
            if current_stock != new_stock:
                # El campo stock es computed con store=True, ya se actualizó con _compute_stock
                # Solo invalidar cache para asegurar que se refleje
                self.invalidate_recordset(['stock'])
                updated_fields.append(f"Stock: {new_stock}")
                stock_changed = True
                _logger.info("📦 Stock actualizado: %d (anterior: %d)", new_stock, current_stock)
            else:
                _logger.info("📦 Stock no cambió: %d", new_stock)
        except Exception as e:
            _logger.warning("⚠️ Error actualizando stock: %s", str(e))
        
        # Enviar solo stock a MercadoLibre (precio: wizard «Actualizar valores»)
        try:
            if not self._force_update_stock_in_ml():
                raise UserError(_("MercadoLibre no aceptó la actualización del stock."))
            _logger.info("✅ Stock sincronizado con MercadoLibre (stock=%d)", self.stock)
        except UserError:
            raise
        except Exception as e:
            _logger.error("❌ Error actualizando stock en MercadoLibre: %s", str(e))
            raise UserError(_("Error al actualizar stock en MercadoLibre:\n\n%s") % str(e))
        
        # Preparar mensaje de resultado
        if updated_fields:
            message = _("Stock actualizado correctamente:\n\n") + "\n".join(f"• {field}" for field in updated_fields)
            message += _("\n\n✅ Stock sincronizado con MercadoLibre")
            message_type = 'success'
        else:
            message = _("El stock ya estaba sincronizado con el producto:\n\n") + \
                      f"• Stock: {int(self.stock or 0)}\n\n" + \
                      _("✅ Stock sincronizado con MercadoLibre")
            message_type = 'success'
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Actualización de stock'),
                'message': message,
                'type': message_type,
                'sticky': False,
            }
        }
    
    def _get_ml_variations_current(self):
        """Obtiene la lista actual de variaciones del ítem desde ML. Retorna lista de dicts con id, price, available_quantity."""
        self.ensure_one()
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return []
        self.ml_account_id._ensure_valid_token()
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}/variations"
        headers = {"Authorization": f"Bearer {self.ml_account_id.access_token}", "Content-Type": "application/json"}
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if not r.ok:
                return []
            data = r.json()
            return data if isinstance(data, list) else (data.get("variations") or [])
        except Exception as e:
            _logger.warning("Error obteniendo variaciones ML: %s", e)
            return []

    def _ml_item_put_error_message(self, response):
        text = (response.text or "").strip()
        try:
            err = response.json()
            if isinstance(err, dict):
                return err.get("message") or err.get("error") or text or str(response.status_code)
        except Exception:
            pass
        return text or _("HTTP %s") % response.status_code

    def _fetch_ml_item_variations_for_put(self):
        """Estado de variaciones para armar PUT (subrecurso o ítem completo)."""
        self.ensure_one()
        variations = self._get_ml_variations_current()
        if variations:
            return variations
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return []
        self.ml_account_id._ensure_valid_token()
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {"Authorization": f"Bearer {self.ml_account_id.access_token}"}
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if not r.ok:
                return []
            data = r.json() if r.text else {}
            return data.get("variations") or []
        except Exception as e:
            _logger.warning("Error obteniendo variaciones desde ítem ML: %s", e)
            return []

    def _force_update_title_in_ml(self, title_ml):
        """PUT solo título en el ítem ML."""
        self.ensure_one()
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            raise UserError(_("Falta ítem en Mercado Libre o la cuenta no tiene token válido."))
        self.ml_account_id._ensure_valid_token()
        title_ml = (title_ml or "").strip()
        if not title_ml:
            raise UserError(_("El título no puede estar vacío."))
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        old_title = (self.title or "").strip()
        start = time.time()
        try:
            response = requests.put(url, headers=headers, json={"title": title_ml}, timeout=30)
            duration_ms = int((time.time() - start) * 1000)
            http_status = response.status_code
            if response.status_code in (200, 201):
                self.env["ml.sync.log"]._log_sync(
                    account=self.ml_account_id,
                    operation="item_update",
                    result="ok",
                    publication_id=self,
                    value_before=old_title,
                    value_after=title_ml,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    trigger="manual",
                )
                return True
            err = self._ml_item_put_error_message(response)
            self.env["ml.sync.log"]._log_sync(
                account=self.ml_account_id,
                operation="item_update",
                result="error",
                publication_id=self,
                value_before=old_title,
                value_after=title_ml,
                http_status=http_status,
                error_message=err,
                duration_ms=duration_ms,
                trigger="manual",
            )
            raise UserError(_("MercadoLibre rechazó la actualización del título:\n\n%s") % err)
        except UserError:
            raise
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.time() - start) * 1000)
            self.env["ml.sync.log"]._log_sync(
                account=self.ml_account_id,
                operation="item_update",
                result="error",
                publication_id=self,
                value_before=old_title,
                value_after=title_ml,
                http_status=0,
                error_message=str(e),
                duration_ms=duration_ms,
                trigger="manual",
            )
            raise UserError(_("Error de conexión al actualizar el título en MercadoLibre:\n\n%s") % str(e)) from e

    def _force_update_seller_sku_in_ml(self, sku_str):
        """Envía SKU a ML (seller_custom_field en ítem o en la variación de esta publicación)."""
        self.ensure_one()
        sku_str = (sku_str or "").strip()
        if not sku_str:
            return True
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            raise UserError(_("Falta ítem en Mercado Libre o la cuenta no tiene token válido."))
        self.ml_account_id._ensure_valid_token()
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        old_sku = (self.seller_sku or "").strip()
        start = time.time()
        try:
            if self.ml_variation_id:
                variations = self._fetch_ml_item_variations_for_put()
                if not variations:
                    raise UserError(
                        _("No se pudieron leer las variaciones del ítem en MercadoLibre; no se puede actualizar el SKU de la variación.")
                    )
                payload = {
                    "variations": [
                        {
                            "id": v.get("id"),
                            "available_quantity": int(v.get("available_quantity") or 0),
                            **(
                                {"seller_custom_field": sku_str}
                                if str(v.get("id")) == str(self.ml_variation_id)
                                else {}
                            ),
                        }
                        for v in variations
                    ]
                }
            else:
                payload = {"seller_custom_field": sku_str}
            response = requests.put(url, headers=headers, json=payload, timeout=30)
            duration_ms = int((time.time() - start) * 1000)
            http_status = response.status_code
            if response.status_code in (200, 201):
                self.env["ml.sync.log"]._log_sync(
                    account=self.ml_account_id,
                    operation="item_update",
                    result="ok",
                    publication_id=self,
                    value_before=old_sku or "",
                    value_after=sku_str,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    trigger="manual",
                )
                return True
            err = self._ml_item_put_error_message(response)
            self.env["ml.sync.log"]._log_sync(
                account=self.ml_account_id,
                operation="item_update",
                result="error",
                publication_id=self,
                value_before=old_sku or "",
                value_after=sku_str,
                http_status=http_status,
                error_message=err,
                duration_ms=duration_ms,
                trigger="manual",
            )
            raise UserError(_("MercadoLibre rechazó la actualización del SKU:\n\n%s") % err)
        except UserError:
            raise
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.time() - start) * 1000)
            self.env["ml.sync.log"]._log_sync(
                account=self.ml_account_id,
                operation="item_update",
                result="error",
                publication_id=self,
                value_before=old_sku or "",
                value_after=sku_str,
                http_status=0,
                error_message=str(e),
                duration_ms=duration_ms,
                trigger="manual",
            )
            raise UserError(_("Error de conexión al actualizar el SKU en MercadoLibre:\n\n%s") % str(e)) from e

    def action_open_ml_values_wizard(self):
        self.ensure_one()
        if not self.ml_item_id:
            raise UserError(_("Esta publicación no tiene ítem en MercadoLibre. Publique o importe la publicación primero."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Actualizar valores"),
            "res_model": "ml.publication.values.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_publication_id": self.id},
        }

    def _apply_values_wizard_to_ml(self, title_ml, price, stock, sku):
        """
        Aplica título, stock y SKU en MercadoLibre. El precio no se envía desde Odoo.
        """
        self.ensure_one()
        if not self.ml_item_id:
            raise UserError(_("Esta publicación no está publicada en MercadoLibre."))
        if not self.ml_account_id or not self.ml_account_id.access_token:
            raise UserError(_("La cuenta de MercadoLibre no está conectada. Autorice la cuenta primero."))
        self.ml_account_id._ensure_valid_token()
        price = max(0.0, float(price or 0.0))
        stock = max(0, int(stock or 0))

        self._force_update_title_in_ml(title_ml)
        self._force_update_seller_sku_in_ml(sku)

        if not self._force_update_stock_in_ml(stock_value=stock):
            raise UserError(
                _("MercadoLibre no aceptó la actualización del stock. Revise el historial de sincronización o los mensajes de error de la API.")
            )

        write_vals = {
            "title": (title_ml or "").strip(),
            "seller_sku": sku or False,
            "new_stock_ml": stock,
        }
        if price > 0:
            write_vals["new_price_ml"] = price

        self.write(write_vals)

        message = (
            _("Valores aplicados en MercadoLibre:\n\n")
            + f"• {_('Título')}: {title_ml}\n"
            + f"• {_('Stock')}: {stock}\n"
            + f"• {_('SKU')}: {sku or '-'}\n"
        )
        if price > 0:
            message += _("\n• Precio guardado solo en Odoo (no se envía a MercadoLibre): %.2f\n") % price
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Actualizar valores"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def _force_update_price_stock_in_ml(self):
        """
        Fuerza la actualización de stock en MercadoLibre.
        El precio no se envía desde Odoo; use el wizard «Actualizar valores».
        """
        self.ensure_one()
        
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return False
        
        self.ml_account_id._ensure_valid_token()
        
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        
        stock_value = max(0, int(self.stock))
        payload = {"available_quantity": stock_value}
        
        _logger.info("📤 Actualizando stock en ML (forzado): stock=%d", stock_value)
        
        try:
            response = requests.put(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code in (200, 201):
                self.write({'current_stock_ml': stock_value})
                _logger.info("✅ Stock actualizado correctamente en MercadoLibre (stock=%d)", stock_value)
                return True
            else:
                error_text = response.text
                _logger.error("❌ Error actualizando stock en ML (status %d): %s", response.status_code, error_text)
                try:
                    error_json = response.json()
                    error_message = error_json.get('message', error_text)
                    raise UserError(_("Error al actualizar stock en MercadoLibre:\n\n%s") % error_message)
                except UserError:
                    raise
                except Exception:
                    raise UserError(_("Error al actualizar stock en MercadoLibre:\n\n%s") % error_text)
        except requests.exceptions.RequestException as e:
            _logger.error("❌ Error de conexión actualizando stock en ML: %s", str(e))
            raise UserError(_("Error de conexión con MercadoLibre:\n\n%s") % str(e))
    
    def _force_update_price_in_ml(self, price_value=None):
        """
        Deshabilitado: Odoo no envía precio a MercadoLibre.
        """
        self.ensure_one()
        _logger.info(
            "ℹ️ Envío de precio a ML omitido (publicación %s). Gestionar precio en MercadoLibre.",
            self.ml_item_id or self.id,
        )
        return False

    def _fetch_ml_item_info(self):
        """GET /items/{id} con datos actuales del ítem en MercadoLibre."""
        self.ensure_one()
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return None
        self.ml_account_id._ensure_valid_token()
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {"Authorization": f"Bearer {self.ml_account_id.access_token}"}
        try:
            response = requests.get(url, headers=headers, timeout=20)
            if response.ok:
                return response.json() if response.text else {}
        except Exception as e:
            _logger.warning("Error obteniendo ítem ML %s: %s", self.ml_item_id, e)
        return None

    @staticmethod
    def _ml_format_sub_status(item_data):
        """Serializa sub_status del ítem ML para guardar en Odoo."""
        if not item_data or not isinstance(item_data, dict):
            return False
        sub = item_data.get('sub_status') or []
        if isinstance(sub, str):
            parts = [sub.strip()] if sub.strip() else []
        elif isinstance(sub, (list, tuple)):
            parts = [str(s).strip() for s in sub if s and str(s).strip()]
        else:
            return False
        return ','.join(parts) if parts else False

    def _ml_sub_status_tokens(self, item_info=None):
        """Lista normalizada de sub_status (ej. out_of_stock)."""
        self.ensure_one()
        if item_info and isinstance(item_info, dict):
            raw = item_info.get('sub_status') or []
        elif self.ml_sub_status:
            raw = [s.strip() for s in self.ml_sub_status.split(',') if s.strip()]
        else:
            return []
        if isinstance(raw, str):
            raw = [raw]
        return [
            str(s).strip().lower().replace(' ', '_')
            for s in raw
            if s and str(s).strip()
        ]

    def _ml_has_out_of_stock_substatus(self, item_info=None):
        """True si ML marca la publicación con sub_status out_of_stock."""
        return 'out_of_stock' in self._ml_sub_status_tokens(item_info=item_info)

    def _ml_refresh_sub_status_from_item(self, item_info):
        """Persiste estado y sub_status local desde payload GET /items."""
        self.ensure_one()
        if not item_info:
            return
        vals = {}
        status = item_info.get('status')
        if status and status != self.ml_status:
            vals['ml_status'] = status
        formatted = self._ml_format_sub_status(item_info)
        if formatted != (self.ml_sub_status or False):
            vals['ml_sub_status'] = formatted or False
        if vals:
            self.write(vals)

    def _ml_current_item_status(self, item_info=None):
        """Estado ML del ítem (active, paused, …)."""
        self.ensure_one()
        if item_info and item_info.get('status'):
            return str(item_info.get('status')).lower()
        return str(self.ml_status or '').lower()

    def _ml_should_skip_stock_sync(self, item_info=None):
        """
        True si no debe enviarse stock a ML.
        Por defecto: ninguna publicación pausada recibe stock.
        Excepción (config): out_of_stock si sync_stock_reactivate_out_of_stock está activo.
        """
        self.ensure_one()
        account = self.ml_account_id
        status = self._ml_current_item_status(item_info)
        if status != 'paused':
            return False
        if account.sync_stock_reactivate_out_of_stock and self._ml_has_out_of_stock_substatus(item_info):
            _logger.info(
                "ℹ️ Ítem %s pausado out_of_stock: sync stock permitido (config «Reactivar out_of_stock»)",
                self.ml_item_id,
            )
            return False
        sub = self.ml_sub_status or self._ml_format_sub_status(item_info) or '—'
        _logger.info(
            "⏭️ Stock omitido ítem %s: publicación pausada en ML (sub_status=%s). "
            "No se envía stock desde Odoo.",
            self.ml_item_id,
            sub,
        )
        return True

    def _resolve_user_product_id(self, item_info=None):
        """Obtiene user_product_id del ítem o de su variación en ML."""
        self.ensure_one()
        item_info = item_info or self._fetch_ml_item_info()
        if not item_info:
            return None
        up_id = item_info.get("user_product_id")
        if up_id:
            return up_id
        variations = item_info.get("variations") or []
        if self.ml_variation_id:
            for var in variations:
                if str(var.get("id")) == str(self.ml_variation_id):
                    return var.get("user_product_id")
        if len(variations) == 1:
            return variations[0].get("user_product_id")
        up_ids = {v.get("user_product_id") for v in variations if v.get("user_product_id")}
        if len(up_ids) == 1:
            return up_ids.pop()
        return None

    @staticmethod
    def _ml_response_has_not_modifiable_stock(error_text):
        return "available_quantity.not_modifiable" in (error_text or "")

    def _get_product_stock_in_warehouse(self, warehouse):
        """Stock Odoo del producto de la publicación en un almacén concreto (respeta tipo disponible/esperado y kits)."""
        self.ensure_one()
        if not warehouse or not warehouse.lot_stock_id:
            return 0
        account = self.ml_account_id
        use_expected = account.stock_type == 'expected' if account else False
        lot_root = warehouse.lot_stock_id
        loc_key = lot_root.id

        if warehouse == account.warehouse_id:
            self.invalidate_recordset(['stock'])
            self._compute_stock()
            return max(0, int(self.stock or 0))

        product_variant = self.product_variant_id or (
            self.product_tmpl_id.product_variant_id if self.product_tmpl_id else None
        )
        if not product_variant:
            return 0

        kit_map = self._get_kit_components_from_bom_batch(product_variant)
        kit_lines = kit_map.get(product_variant.id, ())
        if kit_lines:
            kit_stock = float('inf')
            for bom_line in kit_lines:
                comp_product = bom_line.product_id
                if not comp_product or bom_line.product_qty <= 0:
                    continue
                comp_ctx = comp_product.with_context(location=loc_key)
                comp_stock = int(comp_ctx.virtual_available or 0) if use_expected else int(comp_ctx.qty_available or 0)
                kit_stock = min(kit_stock, int(comp_stock / bom_line.product_qty))
            return 0 if kit_stock == float('inf') else max(0, int(kit_stock))

        product_ctx = product_variant.with_context(location=loc_key)
        if use_expected:
            return max(0, int(product_ctx.virtual_available or 0))
        return max(0, int(product_ctx.qty_available or 0))

    def _resolve_ml_sync_stocks(self, flex_stock_override=None):
        """
        Stock a sincronizar discriminado por almacén Odoo:
        - flex: almacén depósito (warehouse_id) → Flex / multi-origen en ML
        - full: almacén Full (full_warehouse_id) → meli_facility en ML (solo lectura/comparación)
        """
        self.ensure_one()
        account = self.ml_account_id
        flex_stock = (
            max(0, int(flex_stock_override))
            if flex_stock_override is not None
            else self._get_product_stock_in_warehouse(account.warehouse_id)
        )
        full_stock = None
        if account.has_meli_full and account.full_warehouse_id:
            full_stock = self._get_product_stock_in_warehouse(account.full_warehouse_id)
        return {'flex': flex_stock, 'full': full_stock}

    def _log_full_stock_comparison(self, meli_full_locs, full_stock):
        """Compara stock Full Odoo vs ML (ML no permite escribir meli_facility vía API)."""
        self.ensure_one()
        if full_stock is None or not meli_full_locs:
            return
        ml_full_qty = sum(int(loc.get('quantity') or 0) for loc in meli_full_locs)
        odoo_full = int(full_stock)
        full_wh = self.ml_account_id.full_warehouse_id
        _logger.info(
            "📊 Stock Full — Odoo almacén '%s': %d | ML meli_facility: %d (solo lectura en ML)",
            full_wh.name if full_wh else '?',
            odoo_full,
            ml_full_qty,
        )
        if odoo_full != ml_full_qty:
            _logger.warning(
                "⚠️ Diferencia stock Full ítem %s: Odoo=%d, ML=%d. "
                "El stock Full en Mercado Libre solo aumenta enviando mercadería a sus depósitos.",
                self.ml_item_id,
                odoo_full,
                ml_full_qty,
            )

    def _pick_ml_seller_warehouse_location(self, seller_wh_locations):
        """Depósito seller_warehouse de ML (cuenta con un solo almacén Odoo)."""
        if not seller_wh_locations:
            return None
        if len(seller_wh_locations) == 1:
            return seller_wh_locations[0]
        _logger.warning(
            "⚠️ Ítem %s tiene %d depósitos seller_warehouse; se usa el primero.",
            self.ml_item_id,
            len(seller_wh_locations),
        )
        return seller_wh_locations[0]

    def _force_update_stock_via_user_product(self, user_product_id, flex_stock, full_stock=None):
        """
        Actualiza stock en User Product discriminando depósito vs Full:
        - flex_stock (warehouse_id Odoo) → selling_address o seller_warehouse en ML
        - full_stock (full_warehouse_id Odoo) → solo comparación con meli_facility (ML no permite PUT)
        """
        self.ensure_one()
        self.ml_account_id._ensure_valid_token()
        headers = {"Authorization": f"Bearer {self.ml_account_id.access_token}"}
        stock_url = f"https://api.mercadolibre.com/user-products/{user_product_id}/stock"
        flex_stock = max(0, int(flex_stock))

        def _get_stock():
            return requests.get(stock_url, headers=headers, timeout=30)

        get_resp = _get_stock()
        if not get_resp.ok:
            _logger.error(
                "❌ No se pudo leer stock UP %s (status %s): %s",
                user_product_id,
                get_resp.status_code,
                get_resp.text,
            )
            return False

        x_version = get_resp.headers.get("x-version") or get_resp.headers.get("X-Version")
        locations = (get_resp.json() or {}).get("locations") or []
        meli_full = [loc for loc in locations if loc.get("type") == "meli_facility"]
        seller_wh = [loc for loc in locations if loc.get("type") == "seller_warehouse"]
        selling = [loc for loc in locations if loc.get("type") == "selling_address"]

        self._log_full_stock_comparison(meli_full, full_stock)

        flex_wh = self.ml_account_id.warehouse_id
        if flex_wh:
            _logger.info(
                "🏭 Depósito: Odoo '%s' → %d u. | Full Odoo '%s' → %s u.",
                flex_wh.name,
                flex_stock,
                self.ml_account_id.full_warehouse_id.name if self.ml_account_id.full_warehouse_id else '—',
                full_stock if full_stock is not None else '—',
            )

        editable_flex = selling or seller_wh
        if not editable_flex:
            if meli_full:
                _logger.warning(
                    "⚠️ Ítem %s solo tiene stock Full en ML; no hay depósito editable (Flex/multi-origen).",
                    self.ml_item_id,
                )
            return False

        put_headers = {
            **headers,
            "Content-Type": "application/json",
        }
        if x_version is not None:
            put_headers["x-version"] = str(x_version)

        if selling:
            put_url = f"https://api.mercadolibre.com/user-products/{user_product_id}/stock/type/selling_address"
            payload = {"quantity": flex_stock}
            _logger.info(
                "📤 Depósito → Flex (selling_address): up=%s item=%s qty=%d",
                user_product_id,
                self.ml_item_id,
                flex_stock,
            )
        else:
            target_loc = self._pick_ml_seller_warehouse_location(seller_wh)
            locs_payload = []
            for loc in seller_wh:
                locs_payload.append({
                    "store_id": str(loc.get("store_id")),
                    "network_node_id": loc.get("network_node_id"),
                    "quantity": (
                        flex_stock
                        if loc is target_loc
                        else int(loc.get("quantity") or 0)
                    ),
                })
            put_url = f"https://api.mercadolibre.com/user-products/{user_product_id}/stock/type/seller_warehouse"
            payload = {"locations": locs_payload}
            _logger.info(
                "📤 Depósito → multi-origen (seller_warehouse): up=%s item=%s qty=%d store_id=%s",
                user_product_id,
                self.ml_item_id,
                flex_stock,
                target_loc.get("store_id") if target_loc else "?",
            )

        response = requests.put(put_url, headers=put_headers, json=payload, timeout=30)
        if response.status_code == 409:
            get_resp = _get_stock()
            if get_resp.ok:
                x_version = get_resp.headers.get("x-version") or get_resp.headers.get("X-Version")
                if x_version is not None:
                    put_headers["x-version"] = str(x_version)
                response = requests.put(put_url, headers=put_headers, json=payload, timeout=30)

        if response.status_code in (200, 201, 204):
            self.write({"current_stock_ml": flex_stock})
            _logger.info(
                "✅ Stock depósito actualizado vía UP %s (ítem %s, flex=%d)",
                user_product_id,
                self.ml_item_id,
                flex_stock,
            )
            return True

        _logger.error(
            "❌ Error actualizando stock depósito UP %s (status %s): %s",
            user_product_id,
            response.status_code,
            response.text,
        )
        return False

    def _build_ml_stock_put_payload(self, stock_value, item_info=None):
        """Arma payload PUT /items para stock (clásico o por variación)."""
        self.ensure_one()
        if self.ml_variation_id:
            variations = self._get_ml_variations_current()
            if not variations and item_info:
                variations = item_info.get("variations") or []
            if variations:
                var_payload = []
                for v in variations:
                    entry = {
                        "id": v.get("id"),
                        "available_quantity": (
                            int(stock_value)
                            if str(v.get("id")) == str(self.ml_variation_id)
                            else int(v.get("available_quantity") or 0)
                        ),
                    }
                    if v.get("price") is not None:
                        entry["price"] = float(v.get("price"))
                    var_payload.append(entry)
                return {"variations": var_payload}
        return {"available_quantity": int(stock_value)}

    def _force_update_stock_in_ml(self, stock_value=None):
        """
        Actualiza stock en MercadoLibre desde Odoo discriminando almacenes:
        - warehouse_id (depósito) → Flex / multi-origen en ML
        - full_warehouse_id → comparación con meli_facility (ML no permite escribir Full)
        """
        self.ensure_one()
        
        if not self.ml_item_id or not self.ml_account_id or not self.ml_account_id.access_token:
            return False
        
        self.ml_account_id._ensure_valid_token()

        item_info = self._fetch_ml_item_info()
        if item_info:
            self._ml_refresh_sub_status_from_item(item_info)
        if self._ml_should_skip_stock_sync(item_info):
            return True

        stocks = self._resolve_ml_sync_stocks(
            flex_stock_override=stock_value if stock_value is not None else None,
        )
        flex_stock = stocks['flex']
        full_stock = stocks['full']
        _logger.info(
            "📊 Stock sync ítem %s — depósito (Odoo→ML): %d | Full Odoo: %s",
            self.ml_item_id,
            flex_stock,
            full_stock if full_stock is not None else 'N/A',
        )

        user_product_id = self._resolve_user_product_id(item_info)
        item_tags = (item_info or {}).get("tags") or []
        if user_product_id or "warehouse_management" in item_tags:
            _logger.info(
                "ℹ️ Ítem %s: stock vía API user-products (UP=%s)",
                self.ml_item_id,
                user_product_id or "?",
            )
            up_id = user_product_id or self._resolve_user_product_id(item_info)
            if up_id:
                result = self._force_update_stock_via_user_product(up_id, flex_stock, full_stock)
                if result:
                    try:
                        self._check_stock_rules_and_update_status()
                    except Exception as e:
                        _logger.warning("⚠️ Error verificando reglas de stock: %s", e)
                return result
            if "warehouse_management" in item_tags:
                _logger.warning(
                    "⚠️ Ítem %s tiene warehouse_management pero no se encontró user_product_id. "
                    "Reimporte la publicación desde ML o verifique en MercadoLibre.",
                    self.ml_item_id,
                )
                return False
        
        url = f"https://api.mercadolibre.com/items/{self.ml_item_id}"
        headers = {
            "Authorization": f"Bearer {self.ml_account_id.access_token}",
            "Content-Type": "application/json",
        }
        payload = self._build_ml_stock_put_payload(flex_stock, item_info=item_info)
        
        if self.ml_variation_id and payload.get("variations"):
            _logger.info(
                "📤 Enviando stock variación %s en ML: item_id=%s, stock depósito=%d",
                self.ml_variation_id,
                self.ml_item_id,
                flex_stock,
            )
        else:
            _logger.info("📤 [3] Enviando a ML: item_id=%s, stock depósito=%d", self.ml_item_id, flex_stock)

        old_stock = self.current_stock_ml
        start = time.time()
        try:
            response = requests.put(url, headers=headers, json=payload, timeout=30)
            duration_ms = int((time.time() - start) * 1000)
            http_status = response.status_code

            if response.status_code in (200, 201):
                response_data = response.json() if response.text else {}
                stock_ml_recibido = response_data.get('available_quantity', flex_stock)

                self.write({
                    'current_stock_ml': flex_stock,
                })
                _logger.info(
                    "✅ [4] ML respondió: stock depósito enviado=%d, recibido=%s, current_stock_ml=%d",
                    flex_stock,
                    stock_ml_recibido,
                    flex_stock,
                )

                try:
                    self._check_stock_rules_and_update_status()
                except Exception as e:
                    _logger.warning("⚠️ Error verificando reglas de stock después de actualizar stock: %s", e)

                self.env['ml.sync.log']._log_sync(
                    account=self.ml_account_id,
                    operation='stock_update',
                    result='ok',
                    publication_id=self,
                    value_before='' if old_stock in (False, None) else str(old_stock),
                    value_after=str(flex_stock),
                    http_status=http_status,
                    duration_ms=duration_ms,
                )
                return True
            else:
                error_text = response.text
                if self._ml_response_has_not_modifiable_stock(error_text):
                    up_id = user_product_id or self._resolve_user_product_id(item_info)
                    if up_id and self._force_update_stock_via_user_product(up_id, flex_stock, full_stock):
                        self.env['ml.sync.log']._log_sync(
                            account=self.ml_account_id,
                            operation='stock_update',
                            result='ok',
                            publication_id=self,
                            value_before='' if old_stock in (False, None) else str(old_stock),
                            value_after=str(flex_stock),
                            http_status=204,
                            duration_ms=duration_ms,
                        )
                        try:
                            self._check_stock_rules_and_update_status()
                        except Exception as e:
                            _logger.warning("⚠️ Error verificando reglas de stock: %s", e)
                        return True
                    _logger.warning(
                        "⚠️ ML no permite modificar stock del ítem %s (available_quantity.not_modifiable). "
                        "Si es publicación Full sin Flex, el stock se gestiona solo en MercadoLibre.",
                        self.ml_item_id,
                    )
                _logger.error("❌ Error actualizando stock en ML (status %d): %s", response.status_code, error_text)
                self.env['ml.sync.log']._log_sync(
                    account=self.ml_account_id,
                    operation='stock_update',
                    result='error',
                    publication_id=self,
                    value_before='' if old_stock in (False, None) else str(old_stock),
                    value_after=str(flex_stock),
                    http_status=http_status,
                    error_message=error_text or '',
                    duration_ms=duration_ms,
                )
                return False
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.time() - start) * 1000)
            _logger.error("❌ Error de conexión actualizando stock en ML: %s", str(e))
            self.env['ml.sync.log']._log_sync(
                account=self.ml_account_id,
                operation='stock_update',
                result='error',
                publication_id=self,
                value_before='' if old_stock in (False, None) else str(old_stock),
                value_after=str(flex_stock),
                http_status=0,
                error_message=str(e),
                duration_ms=duration_ms,
            )
            return False

    # =====================================================
    # 🔹 IMPORTACIÓN DE VARIACIONES DESDE ML
    # =====================================================
    def _sync_variations_from_ml(self, item_data):
        """
        Sincroniza las variaciones del ítem de ML a ml.publication.variant.
        Crea una variante por cada variación en item_data['variations'], con
        variant_name para mostrar como "Título publicación | Nombre variante".
        
        :param item_data: Datos del item desde ML API (debe contener 'variations')
        """
        self.ensure_one()
        variations = item_data.get("variations") or []
        if not variations:
            _logger.debug("📦 Item sin variaciones en ML, no se sincronizan variantes")
            return
        
        _logger.info("📦 Sincronizando %d variaciones desde Mercado Libre para publicación %s", len(variations), self.ml_item_id)
        title = (self.title or self.name or "Publicación").strip()
        
        # Reemplazar variantes existentes por las de ML (mantener consistencia con el ítem)
        if self.ml_variant_ids:
            self.ml_variant_ids.unlink()
        
        variant_model = self.env["ml.publication.variant"].with_context(allow_variant_price_write=True)
        attr_model = self.env["ml.publication.variant.attribute"]
        sequence = 10
        
        for idx, var in enumerate(variations):
            # Nombre de variante: desde attribute_combinations (value_name o value_id) o seller_custom_field
            combo = var.get("attribute_combinations") or []
            parts = []
            for ac in combo:
                name = ac.get("value_name") or ac.get("value_id")
                if name:
                    parts.append(str(name).strip())
            variant_name_auto = ", ".join(parts) if parts else (var.get("seller_custom_field") or "").strip()
            if not variant_name_auto:
                variant_name_auto = _("Variante %s") % (idx + 1)
            
            # SKU: seller_custom_field o atributo SELLER_SKU en attributes de la variación
            seller_sku = (var.get("seller_custom_field") or "").strip()
            if not seller_sku and var.get("attributes"):
                for attr in var.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                        seller_sku = str(attr.get("value_name", "")).strip()
                        break
            
            price = float(var.get("price") or 0)
            available_quantity = int(var.get("available_quantity") or 0)
            
            variant_vals = {
                "publication_id": self.id,
                "variant_name": variant_name_auto,
                "price": price,
                "available_quantity": available_quantity,
                "seller_sku": seller_sku or "",
                "sequence": sequence,
            }
            new_variant = variant_model.create(variant_vals)
            sequence += 10
            
            # Crear atributos de combinación (ml.publication.variant.attribute)
            attr_sequence = 10
            for ac in combo:
                ac_id = ac.get("id")
                value_id = ac.get("value_id")
                value_name = ac.get("value_name")
                if not ac_id:
                    continue
                attr_model.create({
                    "variant_id": new_variant.id,
                    "ml_attribute_id": str(ac_id),
                    "name": str(ac_id).replace("_", " ").title(),
                    "value_id": str(value_id) if value_id else None,
                    "value_name": str(value_name).strip() if value_name else None,
                    "sequence": attr_sequence,
                })
                attr_sequence += 10
            
            _logger.info("   ✅ Variante %d: %s | %s (precio=%s, stock=%s, SKU=%s)", 
                         idx + 1, title, variant_name_auto, price, available_quantity, seller_sku or "-")
        
        _logger.info("✅ Sincronizadas %d variantes desde ML", len(variations))

    # =====================================================
    # 🔹 IMPORTACIÓN DE IMÁGENES DESDE ML
    # =====================================================
    def _import_images_from_ml(self, item_data, headers):
        """
        Importa las imágenes de la publicación desde MercadoLibre.
        
        :param item_data: Datos del item desde ML API
        :param headers: Headers para hacer requests a la API de ML
        """
        self.ensure_one()
        
        pictures = item_data.get('pictures', [])
        if not pictures:
            _logger.info("📸 No hay imágenes en la publicación de ML")
            return

        download_binary = self.env.context.get('ml_import_download_images', True)
        if not download_binary:
            for idx, picture in enumerate(pictures, 1):
                picture_id = picture.get('id')
                if not picture_id:
                    continue
                if self.ml_image_ids.filtered(lambda img: img.ml_picture_id == str(picture_id)):
                    continue
                self.env['ml.publication.image'].create({
                    'publication_id': self.id,
                    'ml_picture_id': str(picture_id),
                    'sequence': idx * 10,
                })
            _logger.info("📸 Registrados %d IDs de imagen ML (sin descargar binario)", len(pictures))
            return

        _logger.info("📸 Importando %d imágenes desde MercadoLibre", len(pictures))
        
        # Importar cada imagen
        for idx, picture in enumerate(pictures, 1):
            try:
                picture_id = picture.get('id')
                if not picture_id:
                    _logger.warning("⚠️ Imagen %d sin ID, omitiendo", idx)
                    continue
                
                # Verificar si la imagen ya existe
                existing_image = self.ml_image_ids.filtered(lambda img: img.ml_picture_id == str(picture_id))
                if existing_image:
                    _logger.info("📸 Imagen %d ya existe (ID ML: %s), omitiendo", idx, picture_id)
                    continue
                
                # Obtener URL de la imagen (preferir secure_url, luego url)
                image_url = None
                if picture.get('variations'):
                    # Buscar la variación más grande disponible (normalmente 800x800)
                    for var in picture.get('variations', []):
                        if var.get('size') == '800x800':
                            image_url = var.get('secure_url') or var.get('url')
                            break
                    # Si no hay 800x800, tomar la primera disponible
                    if not image_url and picture.get('variations'):
                        first_var = picture.get('variations')[0]
                        image_url = first_var.get('secure_url') or first_var.get('url')
                else:
                    # Si no hay variations, usar secure_url o url directamente
                    image_url = picture.get('secure_url') or picture.get('url')
                
                if not image_url:
                    _logger.warning("⚠️ Imagen %d (ID ML: %s) sin URL disponible, omitiendo", idx, picture_id)
                    continue
                
                # Descargar la imagen
                try:
                    _logger.info("📥 Descargando imagen %d/%d desde ML: %s", idx, len(pictures), image_url[:100])
                    img_response = requests.get(image_url, timeout=30)
                    if not img_response.ok:
                        _logger.warning("⚠️ Error descargando imagen %d (ID ML: %s): %s", idx, picture_id, img_response.status_code)
                        continue
                    
                    # Convertir a base64 para Odoo
                    image_base64 = base64.b64encode(img_response.content).decode('utf-8')
                    
                    # Crear registro de imagen
                    self.env['ml.publication.image'].create({
                        'publication_id': self.id,
                        'ml_picture_id': str(picture_id),
                        'image_1920': image_base64,
                        'sequence': idx * 10,  # Secuencia: 10, 20, 30, ...
                    })
                    
                    _logger.info("✅ Imagen %d/%d importada correctamente (ID ML: %s)", idx, len(pictures), picture_id)
                    
                except requests.exceptions.RequestException as e:
                    _logger.error("❌ Error descargando imagen %d (ID ML: %s): %s", idx, picture_id, str(e))
                    continue
                except Exception as e:
                    _logger.error("❌ Error procesando imagen %d (ID ML: %s): %s", idx, picture_id, str(e))
                    continue
                    
            except Exception as e:
                _logger.error("❌ Error importando imagen %d: %s", idx, str(e))
                continue
        
        _logger.info("✅ Importación de imágenes completada: %d imágenes procesadas", len(pictures))
    
    # =====================================================
    # 🔹 SINCRONIZACIÓN DE ATRIBUTOS DESDE ML (IMPORTACIÓN)
    # =====================================================
    def _sync_attributes_from_ml(self, ml_attributes, category_id_ml, headers):
        """
        Sincroniza atributos desde Mercado Libre manteniendo español y creando los que falten.
        
        :param ml_attributes: Lista de atributos del item desde ML API
        :param category_id_ml: ID de la categoría en ML (ej: MLA1002)
        :param headers: Headers para hacer requests a la API de ML
        """
        self.ensure_one()
        
        if not category_id_ml:
            _logger.warning("⚠️ No se puede sincronizar atributos sin categoría")
            return
        
        # Normalizar ml_attributes (puede ser None o lista vacía)
        ml_attributes = ml_attributes or []
        
        _logger.info("🔄 Sincronizando %d atributos desde ML (categoría: %s)", len(ml_attributes), category_id_ml)
        
        # 1. Obtener atributos de la categoría desde la API de ML para obtener nombres en español
        category_attrs_map = {}  # {ml_attribute_id: {name, value_type, ...}}
        try:
            category_attrs_url = f"https://api.mercadolibre.com/categories/{category_id_ml}/attributes"
            category_attrs_response = requests.get(category_attrs_url, headers=headers, timeout=30)
            if category_attrs_response.ok:
                category_attrs_data = category_attrs_response.json()
                for attr_data in category_attrs_data:
                    attr_id = attr_data.get('id')
                    if attr_id:
                        category_attrs_map[attr_id] = {
                            'name': attr_data.get('name', attr_id),  # Nombre en español desde ML
                            'value_type_ml': attr_data.get('value_type', 'string'),
                            'tags': attr_data.get('tags', {}),
                            'values': attr_data.get('values', []),
                            'allowed_units': attr_data.get('allowed_units', []),
                            'full_data': attr_data,  # Datos completos para crear si falta
                        }
                _logger.info("✅ Obtenidos %d atributos de categoría desde API de ML", len(category_attrs_map))
            else:
                _logger.warning("⚠️ No se pudieron obtener atributos de categoría desde API: %s", category_attrs_response.status_code)
        except Exception as e:
            _logger.warning("⚠️ Error obteniendo atributos de categoría desde API: %s", e)
        
        self.category_id = category_id_ml
        
        def _value_type_from_attr_info(attr_info):
            vt = (attr_info or {}).get('value_type_ml', 'string')
            if vt in ('boolean', 'boolean_radio'):
                return 'boolean'
            if vt == 'number_unit':
                return 'number_unit'
            if vt == 'number':
                return 'number'
            if (attr_info or {}).get('values'):
                return 'value_id'
            return 'value_name'
        
        # 3. Sincronizar valores de atributos en ml.publication.attribute
        # Mapear atributos existentes por ml_attribute_id
        existing_pub_attrs = {}
        for pub_attr in self.ml_attribute_ids:
            if pub_attr.ml_attribute_id:
                existing_pub_attrs[pub_attr.ml_attribute_id] = pub_attr
        
        # Procesar cada atributo desde ML
        for ml_attr in ml_attributes:
            ml_attr_id = ml_attr.get('id')
            if not ml_attr_id:
                continue
            
            # Obtener información del atributo desde la categoría (para nombre en español)
            attr_info = category_attrs_map.get(ml_attr_id, {})
            attr_name_es = attr_info.get('name', ml_attr_id)  # Nombre en español
            
            # Buscar o crear el atributo en ml.publication.attribute
            pub_attr = existing_pub_attrs.get(ml_attr_id)
            
            value_type = _value_type_from_attr_info(attr_info)
            tags = (attr_info or {}).get('tags', {})
            is_required = tags.get('required', False) or tags.get('catalog_required', False)
            
            if not pub_attr:
                value_updates = self._extract_attribute_value_from_ml(ml_attr, value_type, ml_attr_id)
                pub_attr_vals = {
                    'publication_id': self.id,
                    'ml_attribute_id': ml_attr_id,
                    'name': attr_name_es,
                    'value_type': value_type,
                    'required': is_required,
                    'is_common': False,
                    'sequence': len(self.ml_attribute_ids) * 10 + 10,
                }
                pub_attr_vals.update(value_updates)
                pub_attr = self.env['ml.publication.attribute'].create(pub_attr_vals)
                _logger.info("✅ Creado atributo en publicación: %s (%s)", attr_name_es, ml_attr_id)
                if ml_attr_id == 'SELLER_SKU' and value_updates.get('value_name'):
                    sku_value = value_updates.get('value_name')
                    if sku_value and str(sku_value).strip() and str(sku_value).strip() != 'False':
                        self.seller_sku = str(sku_value).strip()
            else:
                new_values = self._extract_attribute_value_from_ml(ml_attr, value_type, ml_attr_id)
                value_changed = any(getattr(pub_attr, f, None) != v for f, v in new_values.items())
                if value_changed:
                    update_vals = new_values.copy()
                    if not pub_attr.name or pub_attr.name == pub_attr.ml_attribute_id:
                        update_vals['name'] = attr_name_es
                    pub_attr.write(update_vals)
                    if ml_attr_id == 'SELLER_SKU' and update_vals.get('value_name'):
                        sku_value = update_vals.get('value_name')
                        if sku_value and str(sku_value).strip():
                            self.seller_sku = str(sku_value).strip()
                elif not pub_attr.name or pub_attr.name == pub_attr.ml_attribute_id:
                    if pub_attr.name != attr_name_es:
                        pub_attr.write({'name': attr_name_es})
        
        # 5. Analizar y clasificar atributos personalizables según criterios estrictos
        # Obtener IDs de atributos ya procesados del item
        processed_attr_ids = set(ml_attr.get('id') for ml_attr in ml_attributes if ml_attr.get('id'))
        
        # Clasificar atributos personalizables
        classified_attrs = self._classify_customizable_attributes(category_attrs_map)
        
        # Identificar atributos requeridos y condicionalmente requeridos que faltan
        # NOTA: Los atributos opcionales NO se agregan automáticamente para evitar atributos no deseados
        # Solo se agregan si vienen en el item de ML o si son requeridos/condicionalmente requeridos
        required_attrs_missing = []
        conditional_required_attrs_missing = []
        
        # Procesar atributos requeridos
        for attr_id, attr_info in classified_attrs.get('required_attributes', []):
            if attr_id not in processed_attr_ids and attr_id not in existing_pub_attrs:
                required_attrs_missing.append((attr_id, attr_info))
        
        # Procesar atributos condicionalmente requeridos
        for attr_id, attr_info in classified_attrs.get('conditional_required_attributes', []):
            if attr_id not in processed_attr_ids and attr_id not in existing_pub_attrs:
                conditional_required_attrs_missing.append((attr_id, attr_info))
        
        # NO procesar atributos opcionales automáticamente
        # Solo se agregan si vienen en el item de ML durante la importación
        
        # Combinar listas para procesarlas juntas
        # Solo incluir requeridos y condicionalmente requeridos
        # Los opcionales NO se agregan automáticamente para evitar atributos no deseados
        all_missing_attrs = required_attrs_missing + conditional_required_attrs_missing
        
        # Crear atributos requeridos y personalizables faltantes
        if all_missing_attrs:
            if required_attrs_missing:
                _logger.info("📝 Creando %d atributos requeridos faltantes que no vinieron en el item", len(required_attrs_missing))
            if conditional_required_attrs_missing:
                _logger.info("📝 Creando %d atributos condicionalmente requeridos faltantes que no vinieron en el item", len(conditional_required_attrs_missing))
            
            for attr_id, attr_info in all_missing_attrs:
                attr_name_es = attr_info.get('name', attr_id)
                value_type = _value_type_from_attr_info(attr_info)
                tags = attr_info.get('tags', {})
                is_required = tags.get('required', False) or tags.get('catalog_required', False)
                try:
                    pub_attr_vals = {
                        'publication_id': self.id,
                        'ml_attribute_id': attr_id,
                        'name': attr_name_es,
                        'value_type': value_type,
                        'required': is_required,
                        'is_common': False,
                        'sequence': len(self.ml_attribute_ids) * 10 + 10,
                    }
                    self.env['ml.publication.attribute'].create(pub_attr_vals)
                    _logger.info("✅ Creado atributo requerido faltante en publicación: %s (%s)", attr_name_es, attr_id)
                except Exception as e:
                    _logger.error("❌ Error creando atributo requerido %s en publicación: %s", attr_id, e)
        
        _logger.info("✅ Sincronización de atributos completada")
    
    def _classify_customizable_attributes(self, category_attrs_map):
        """
        Analiza y clasifica atributos de Mercado Libre según si son personalizables.
        
        Clasifica en tres categorías:
        - required_attributes: Atributos obligatorios (required o catalog_required)
        - conditional_required_attributes: Atributos condicionalmente requeridos
        - optional_attributes: Atributos opcionales pero personalizables
        
        :param category_attrs_map: Dict con atributos de la categoría desde ML API
        :return: Dict con las tres listas clasificadas
        """
        required_attributes = []
        conditional_required_attributes = []
        optional_attributes = []
        
        # Atributos internos del sistema que NO deben incluirse
        internal_attr_prefixes = [
            'PACKAGE_',
            'SELLER_PACKAGE_',
            'PRODUCT_FEATURES',
            'IS_FLAMMABLE',
        ]
        
        for attr_id, attr_info in category_attrs_map.items():
            tags = attr_info.get('tags', {})
            full_data = attr_info.get('full_data', {})
            
            # Excluir atributos que NO son personalizables
            is_hidden = tags.get('hidden', False)
            is_read_only = tags.get('read_only', False)
            is_calculated = tags.get('calculated', False)
            is_fixed = tags.get('fixed', False)
            is_inferred = tags.get('inferred', False)
            is_variation = tags.get('variation_attribute', False)
            is_multivalued = tags.get('multivalued', False)
            
            # Excluir si tiene tags que lo hacen no personalizable
            if is_hidden or is_read_only or is_calculated or is_fixed or is_inferred:
                continue
            
            # Excluir atributos de variación (se manejan en variantes)
            if is_variation:
                continue
            
            # Excluir multivalued excepto GTIN/MPN si aplica
            if is_multivalued and attr_id not in ['GTIN', 'MPN']:
                continue
            
            # Excluir atributos internos del sistema
            is_internal = any(attr_id.startswith(prefix) for prefix in internal_attr_prefixes)
            if is_internal:
                continue
            
            # Preparar información del atributo
            attr_data = {
                'id': attr_id,
                'name': attr_info.get('name', attr_id),
                'value_type': attr_info.get('value_type_ml', 'string'),
                'values': attr_info.get('values', []),
                'allowed_units': attr_info.get('allowed_units', []),
                'default_unit': full_data.get('default_unit'),
                'tags': tags,
                'full_data': full_data,
            }
            
            # Generar explicación corta según el tipo
            value_type = attr_data['value_type']
            if value_type in ['list', 'value_id']:
                explanation = f"Seleccione un valor de la lista desplegable"
            elif value_type == 'boolean':
                explanation = "Seleccione Sí o No"
            elif value_type == 'number':
                explanation = "Ingrese un valor numérico"
            elif value_type == 'number_unit':
                explanation = "Ingrese un valor numérico y seleccione la unidad"
            elif value_type in ['picture', 'file']:
                explanation = "Suba una imagen o archivo"
            else:
                explanation = "Ingrese un valor de texto"
            
            attr_data['explanation'] = explanation
            
            # Clasificar según tags de requerimiento
            is_required = tags.get('required', False) or tags.get('catalog_required', False)
            is_conditional_required = tags.get('conditional_required', False)
            
            if is_required:
                required_attributes.append((attr_id, attr_data))
            elif is_conditional_required:
                conditional_required_attributes.append((attr_id, attr_data))
            else:
                optional_attributes.append((attr_id, attr_data))
        
        return {
            'required_attributes': required_attributes,
            'conditional_required_attributes': conditional_required_attributes,
            'optional_attributes': optional_attributes,
        }
    
    def _extract_attribute_value_from_ml(self, ml_attr, value_type, ml_attribute_id=None):
        """
        Extrae el valor del atributo desde ML según su tipo.
        
        :param ml_attr: Diccionario del atributo desde ML API
        :param value_type: Tipo de valor en Odoo (value_id, value_name, boolean, number, number_unit)
        :param ml_attribute_id: ID del atributo en ML (opcional, para buscar unidades)
        :return: Diccionario con los campos a actualizar
        """
        result = {}
        
        if value_type == 'value_id':
            # Atributo con valor predefinido
            value_id = ml_attr.get('value_id')
            if value_id:
                result['value_id'] = str(value_id)
                # También intentar obtener value_name si está disponible
                value_name = ml_attr.get('value_name')
                if value_name:
                    result['value_name'] = str(value_name)
        
        elif value_type == 'boolean':
            # IMPORTANTE: Leer value_boolean si está disponible
            value_boolean = ml_attr.get('value_boolean')
            if value_boolean is not None:
                result['value_boolean'] = bool(value_boolean)
                # También mapear a value_id para compatibilidad (242084=Sí, 242085=No)
                result['value_id'] = '242084' if value_boolean else '242085'
            else:
                # Fallback: leer desde value_id (compatibilidad con formato antiguo)
                value_id = ml_attr.get('value_id')
                if value_id:
                    result['value_id'] = str(value_id)
                    # Mapear a value_boolean
                    result['value_boolean'] = (str(value_id) == '242084')
        
        elif value_type == 'number':
            # Atributo numérico
            value_number = ml_attr.get('value_number')
            if value_number is not None:
                result['value_number'] = float(value_number)
        
        elif value_type == 'number_unit':
            # Atributo numérico con unidad
            value_struct = ml_attr.get('value_struct')
            if value_struct:
                number = value_struct.get('number')
                unit = value_struct.get('unit')
                if number is not None:
                    result['value_number'] = float(number)
                if unit:
                    result['value_unit'] = str(unit)
        
        elif value_type == 'picture':
            # Atributo de imagen
            value_id = ml_attr.get('value_id')
            if value_id:
                result['value_id'] = str(value_id)
        
        else:
            # value_name (texto libre) - por defecto
            value_name = ml_attr.get('value_name')
            if value_name:
                result['value_name'] = str(value_name)
        
        return result

    def _classify_customizable_attributes(self, category_attrs_map):
        """
        Analiza y clasifica atributos de Mercado Libre según si son personalizables.
        
        Clasifica en tres categorías:
        - required_attributes: Atributos obligatorios (required o catalog_required)
        - conditional_required_attributes: Atributos condicionalmente requeridos
        - optional_attributes: Atributos opcionales pero personalizables
        
        :param category_attrs_map: Dict con atributos de la categoría desde ML API
        :return: Dict con las tres listas clasificadas
        """
        required_attributes = []
        conditional_required_attributes = []
        optional_attributes = []
        
        # Atributos internos del sistema que NO deben incluirse
        internal_attr_prefixes = [
            'PACKAGE_',
            'SELLER_PACKAGE_',
            'PRODUCT_FEATURES',
            'IS_FLAMMABLE',
        ]
        
        for attr_id, attr_info in category_attrs_map.items():
            tags = attr_info.get('tags', {})
            full_data = attr_info.get('full_data', {})
            
            # Excluir atributos que NO son personalizables
            is_hidden = tags.get('hidden', False)
            is_read_only = tags.get('read_only', False)
            is_calculated = tags.get('calculated', False)
            is_fixed = tags.get('fixed', False)
            is_inferred = tags.get('inferred', False)
            is_variation = tags.get('variation_attribute', False)
            is_multivalued = tags.get('multivalued', False)
            
            # Excluir si tiene tags que lo hacen no personalizable
            if is_hidden or is_read_only or is_calculated or is_fixed or is_inferred:
                continue
            
            # Excluir atributos de variación (se manejan en variantes)
            if is_variation:
                continue
            
            # Excluir multivalued excepto GTIN/MPN si aplica
            if is_multivalued and attr_id not in ['GTIN', 'MPN']:
                continue
            
            # Excluir atributos internos del sistema
            is_internal = any(attr_id.startswith(prefix) for prefix in internal_attr_prefixes)
            if is_internal:
                continue
            
            # Preparar información del atributo
            attr_data = {
                'id': attr_id,
                'name': attr_info.get('name', attr_id),
                'value_type': attr_info.get('value_type_ml', 'string'),
                'values': attr_info.get('values', []),
                'allowed_units': attr_info.get('allowed_units', []),
                'default_unit': full_data.get('default_unit'),
                'tags': tags,
                'full_data': full_data,
            }
            
            # Generar explicación corta según el tipo
            value_type = attr_data['value_type']
            if value_type in ['list', 'value_id']:
                explanation = f"Seleccione un valor de la lista desplegable"
            elif value_type == 'boolean':
                explanation = "Seleccione Sí o No"
            elif value_type == 'number':
                explanation = "Ingrese un valor numérico"
            elif value_type == 'number_unit':
                explanation = "Ingrese un valor numérico y seleccione la unidad"
            elif value_type in ['picture', 'file']:
                explanation = "Suba una imagen o archivo"
            else:
                explanation = "Ingrese un valor de texto"
            
            attr_data['explanation'] = explanation
            
            # Clasificar según tags de requerimiento
            is_required = tags.get('required', False) or tags.get('catalog_required', False)
            is_conditional_required = tags.get('conditional_required', False)
            
            if is_required:
                required_attributes.append((attr_id, attr_data))
            elif is_conditional_required:
                conditional_required_attributes.append((attr_id, attr_data))
            else:
                optional_attributes.append((attr_id, attr_data))
        
        return {
            'required_attributes': required_attributes,
            'conditional_required_attributes': conditional_required_attributes,
            'optional_attributes': optional_attributes,
        }

    # =====================================================
    # 🔹 CREATE: SIN PUBLICACIÓN AUTOMÁTICA
    # =====================================================
    # ⚠️ PUBLICACIÓN AUTOMÁTICA DESACTIVADA
    # El método create() con @api.model_create_multi (línea 594) ya maneja la creación.
    # NO hay publicación automática al guardar.
    # Use el botón "Publicar en ML" en el formulario para publicar manualmente.
