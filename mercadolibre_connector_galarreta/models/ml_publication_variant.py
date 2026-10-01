# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MLPublicationVariant(models.Model):
    _name = "ml.publication.variant"
    _description = "Variante de Publicación Mercado Libre"
    _order = "sequence, id"

    publication_id = fields.Many2one(
        "ml.publication",
        string="Publicación",
        required=True,
        ondelete="cascade",
    )
    
    product_variant_id = fields.Many2one(
        "product.product",
        string="Variante del Producto",
        required=False,
        help="Variante del producto en Odoo",
    )
    
    sequence = fields.Integer(string="Secuencia", default=10)

    variant_name = fields.Char(
        string="Nombre de la Variante",
        help="Nombre editable que se mostrará en Odoo para identificar esta variante.",
    )

    image_1920 = fields.Image(
        string="Imagen específica (legacy)",
        max_width=1920,
        max_height=1920,
        help="Imagen propia de la variante (deprecated - usar 'Imágenes' en su lugar). Se enviará a Mercado Libre para esta combinación.",
    )

    image_ml_picture_id = fields.Char(
        string="ID imagen ML (legacy)",
        readonly=True,
        help="Identificador de la imagen subida a Mercado Libre para esta variante (deprecated - usar 'Imágenes' en su lugar).",
    )
    
    # Múltiples imágenes para la variante
    image_ids = fields.One2many(
        "ml.publication.image",
        "variant_id",
        string="Imágenes de la Variante",
        help="Múltiples imágenes para esta variante. Se enviarán a Mercado Libre para esta combinación.",
    )

    # Atributos de la variante (combinación de atributos)
    attribute_combination_ids = fields.One2many(
        "ml.publication.variant.attribute",
        "variant_id",
        string="Combinación de Atributos",
        help="Atributos que definen esta variante (ej: Color, Talla)",
    )
    
    price = fields.Float(
        string="Precio",
        help="Precio específico para esta variante (si está vacío, usa el precio del producto)",
        readonly=True,
    )
    
    available_quantity = fields.Integer(
        string="Cantidad Disponible",
        help="Stock disponible para esta variante (si está vacío, usa el stock del producto)",
    )
    
    seller_sku = fields.Char(
        string="SKU",
        help="SKU específico para esta variante",
    )

    gtin = fields.Char(
        string="Código universal (GTIN)",
        help="Código de barras o GTIN específico para esta variante.",
    )
    
    # Campos computados
    display_name = fields.Char(compute="_compute_display_name", store=True)
    
    def _is_manual_override(self):
        self.ensure_one()
        sku = (self.seller_sku or "").strip()
        if sku and not sku.upper().startswith("PUB"):
            return True
        if self.variant_name and self.product_variant_id:
            base_name = self.product_variant_id.display_name or self.product_variant_id.name or ""
            if self.variant_name.strip() and self.variant_name.strip() != base_name.strip():
                return True
        return False
    
    @api.depends("variant_name", "product_variant_id", "attribute_combination_ids", "publication_id", "publication_id.title")
    def _compute_display_name(self):
        for record in self:
            # Prefijo: "Título publicación |" para que cada variante se vea como "Publicación | Variante"
            title = (record.publication_id.title or record.publication_id.name or "").strip() if record.publication_id else ""
            prefix = f"{title} | " if title else ""
            
            # PRIORIDAD 1: Si el usuario puso un nombre personalizado, usarlo (con prefijo)
            if record.variant_name and record.variant_name.strip():
                record.display_name = prefix + record.variant_name.strip()
                continue

            # PRIORIDAD 2: Construir nombre desde los atributos de la variante
            if record.product_variant_id:
                attr_names = []
                for attr in record.attribute_combination_ids:
                    if attr.value_name:
                        attr_names.append(attr.value_name)
                    elif attr.value_id:
                        attr_names.append(attr.value_id)
                
                if attr_names:
                    record.display_name = prefix + f"{record.product_variant_id.name} ({', '.join(attr_names)})"
                else:
                    record.display_name = prefix + (record.product_variant_id.name or "")
            else:
                record.display_name = prefix + (_("Variante sin producto") if not prefix else _("Variante sin producto"))
    
    def to_ml_format(self, picture_ids=None, valid_ml_attributes=None):
        """
        Convierte la variante al formato esperado por la API de Mercado Libre.
        Retorna un diccionario con la estructura de 'variations'.
        
        :param picture_ids: Lista de IDs de imágenes de Mercado Libre para esta variante.
                           Si no se proporciona, se usarán las imágenes de la publicación principal.
        :param valid_ml_attributes: Set o lista de IDs de atributos válidos en ML (opcional).
                                   Si se proporciona, solo se incluirán atributos que existan en ML.
        """
        self.ensure_one()
        
        # Obtener atributos válidos de la categoría ML si no se proporcionaron
        # NOTA: Los atributos de variación (como COLOR) pueden no estar en attribute_ids de la categoría
        # porque attribute_ids contiene atributos del item, no de variaciones.
        # Por lo tanto, NO validamos atributos de variación contra esta lista.
        # Solo validamos si se proporciona explícitamente una lista de válidos.
        if valid_ml_attributes is None and self.publication_id and self.publication_id.ml_attribute_ids:
            item_attributes = set(
                self.publication_id.ml_attribute_ids.mapped('ml_attribute_id')
            )
            # Atributos comunes de variación que siempre son válidos (no validar estos)
            common_variation_attributes = {'COLOR', 'SIZE', 'BRAND', 'MODEL', 'SELLER_SKU', 'GTIN'}
            # Solo validar atributos que NO son de variación común
            valid_ml_attributes = item_attributes
            _logger.debug("🔍 Atributos válidos de categoría ML (item): %s", valid_ml_attributes)
            _logger.debug("🔍 Atributos de variación comunes (no se validan): %s", common_variation_attributes)
        
        # Construir attribute_combinations, filtrando atributos inválidos
        attribute_combinations = []
        invalid_attributes = []
        for attr in self.attribute_combination_ids:
            if attr.ml_attribute_id:
                # Atributos comunes de variación siempre son válidos (COLOR, SIZE, etc.)
                common_variation_attrs = {'COLOR', 'SIZE', 'BRAND', 'MODEL', 'SELLER_SKU', 'GTIN'}
                is_common_variation_attr = attr.ml_attribute_id in common_variation_attrs
                
                # Solo validar si NO es un atributo de variación común Y se proporcionó lista de válidos
                if valid_ml_attributes is not None and not is_common_variation_attr:
                    if attr.ml_attribute_id not in valid_ml_attributes:
                        invalid_attributes.append(attr.ml_attribute_id)
                        _logger.warning("⚠️ Atributo '%s' no es válido en la categoría ML, será omitido", attr.ml_attribute_id)
                        continue
                
                attr_dict = {
                    "id": attr.ml_attribute_id,
                }
                if attr.value_id:
                    attr_dict["value_id"] = attr.value_id
                if attr.value_name:
                    attr_dict["value_name"] = attr.value_name
                attribute_combinations.append(attr_dict)
        
        if invalid_attributes:
            _logger.warning("⚠️ Variante %s: %d atributos inválidos omitidos: %s", 
                          self.display_name, len(invalid_attributes), invalid_attributes)
        
        # Obtener cantidad (precio: nunca se envía desde Odoo a ML)
        variant_qty = self.available_quantity if self.available_quantity is not None else (int(self.product_variant_id.qty_available) if self.product_variant_id else 0)
        result = {
            "attribute_combinations": attribute_combinations,
            "available_quantity": int(variant_qty),
        }
        
        # Agregar seller_custom_field: usar SKU (seller_sku, default_code, o generar uno)
        # IMPORTANTE: NO actualizar seller_sku automáticamente - respetar el valor que el usuario ponga
        sku_value = None
        
        # PRIMERO: intentar usar seller_sku si tiene valor (respetar lo que el usuario puso)
        if self.seller_sku and str(self.seller_sku).strip():
            sku_value = str(self.seller_sku).strip()
            _logger.info("📦 SKU para variante %s: usando seller_sku del usuario=%s", self.display_name, sku_value)
        # SEGUNDO: si seller_sku está vacío, intentar usar default_code del producto (solo para exportar, NO actualizar)
        elif self.product_variant_id:
            default_code = getattr(self.product_variant_id, 'default_code', None)
            if default_code and str(default_code).strip() and str(default_code).strip() != 'False':
                sku_value = str(default_code).strip()
                # NO actualizar seller_sku automáticamente - solo usar para exportar
                _logger.info("📦 SKU para variante %s: usando default_code=%s (NO se actualiza seller_sku)", self.display_name, sku_value)
            # TERCERO: si no hay default_code, generar un SKU basado en el ID de la variante (solo para exportar)
            else:
                # Generar SKU automático: usar el ID de la publicación y el ID de la variante
                if self.publication_id and self.publication_id.ml_item_id:
                    sku_value = f"{self.publication_id.ml_item_id}-VAR{self.id}"
                elif self.publication_id:
                    sku_value = f"PUB{self.publication_id.id}-VAR{self.id}"
                else:
                    sku_value = f"VAR{self.id}"
                # NO actualizar seller_sku automáticamente - solo usar para exportar
                _logger.info("📦 SKU para variante %s: generado automáticamente solo para exportar=%s (default_code=%s, seller_sku NO actualizado)", 
                          self.display_name, sku_value, default_code if default_code else "False/None")
        else:
            # Si no hay product_variant_id, generar SKU basado en el ID (solo para exportar)
            if self.publication_id and self.publication_id.ml_item_id:
                sku_value = f"{self.publication_id.ml_item_id}-VAR{self.id}"
            elif self.publication_id:
                sku_value = f"PUB{self.publication_id.id}-VAR{self.id}"
            else:
                sku_value = f"VAR{self.id}"
            # NO actualizar seller_sku automáticamente
            _logger.info("📦 SKU para variante %s: generado automáticamente solo para exportar=%s (sin product_variant_id, seller_sku NO actualizado)", 
                      self.display_name, sku_value)
        
        # Agregar variant_name al payload si está disponible
        # ML usa seller_custom_field para identificar variantes, así que usamos variant_name ahí
        variant_name_clean = None
        if self.variant_name and self.variant_name.strip():
            variant_name_clean = self.variant_name.strip()
            result["seller_custom_field"] = variant_name_clean
            _logger.info("📝 variant_name usado como seller_custom_field: %s", variant_name_clean)
        elif sku_value:
            # Si no hay variant_name pero hay SKU, usar SKU como seller_custom_field
            result["seller_custom_field"] = sku_value
            _logger.info("✅ SKU usado como seller_custom_field para variante %s: %s", self.display_name, sku_value)
        
        # Agregar imágenes a la variante (requerido por ML para categorías con variantes)
        if picture_ids:
            result["picture_ids"] = picture_ids
        else:
            # Usar imágenes de image_ids (múltiples imágenes)
            variant_picture_ids = []
            for img in self.image_ids:
                if img.ml_picture_id:
                    variant_picture_ids.append(img.ml_picture_id)
            # Si no hay imágenes en image_ids, usar image_ml_picture_id legacy (para compatibilidad)
            if not variant_picture_ids and self.image_ml_picture_id:
                variant_picture_ids = [self.image_ml_picture_id]
            if variant_picture_ids:
                result["picture_ids"] = variant_picture_ids

        variant_attributes = []
        # Agregar SKU como atributo SELLER_SKU (siempre, independientemente de variant_name)
        if sku_value:
            variant_attributes.append({
                "id": "SELLER_SKU",
                "value_name": sku_value,
            })
            _logger.info("✅ SKU agregado como atributo SELLER_SKU para variante %s: %s", self.display_name, sku_value)
        if self.gtin:
            variant_attributes.append({
                "id": "GTIN",
                "value_name": self.gtin,
            })

        if variant_attributes:
            result["attributes"] = variant_attributes
        
        return result

    def write(self, vals):
        if "image_1920" in vals:
            vals = vals.copy()
            vals["image_ml_picture_id"] = False
        return super().write(vals)


class MLPublicationVariantAttribute(models.Model):
    _name = "ml.publication.variant.attribute"
    _description = "Atributo de Variante de Publicación"
    _order = "sequence, id"
    _syncing_option = False

    variant_id = fields.Many2one(
        "ml.publication.variant",
        string="Variante",
        required=True,
        ondelete="cascade",
    )
    
    ml_attribute_id = fields.Char(
        string="ID Atributo ML",
        required=True,
        help="ID del atributo en Mercado Libre (ej: COLOR, SIZE, etc.)",
    )
    
    name = fields.Char(
        string="Nombre del Atributo",
        help="Nombre legible del atributo",
    )

    value_type = fields.Selection([
        ('value_id', 'Valor predefinido'),
        ('value_name', 'Texto libre'),
        ('boolean', 'Booleano'),
        ('number', 'Número'),
        ('number_unit', 'Número con unidad'),
    ], string="Tipo de valor", default='value_name')

    has_allowed_values = fields.Boolean(
        string="Tiene valores permitidos",
        compute="_compute_has_allowed_values",
    )

    value_id = fields.Char(
        string="ID del Valor",
        help="ID del valor en Mercado Libre (si el atributo tiene valores predefinidos)",
    )
    
    value_name = fields.Char(
        string="Nombre del Valor",
        help="Nombre del valor (si el atributo es de texto libre)",
    )
    
    sequence = fields.Integer(string="Secuencia", default=10)

    @api.depends("ml_attribute_id")
    def _compute_has_allowed_values(self):
        for record in self:
            record.has_allowed_values = False

    @api.model_create_multi
    def create(self, vals_list):
        sanitized_vals = []
        for vals in vals_list:
            data = vals.copy()
            if "price" in data and not self.env.context.get("allow_variant_price_write"):
                data.pop("price")
            self._prepare_value_fields(data)
            sanitized_vals.append(data)
        records = super().create(sanitized_vals)
        return records

    def write(self, vals):
        data = vals.copy()
        if "price" in data and not self.env.context.get("allow_variant_price_write"):
            data.pop("price")
        
        # IMPORTANTE: Preservar el campo name si ya existe y no se está actualizando explícitamente
        # Si se está actualizando ml_attribute_id pero no name, y el name actual es diferente del ml_attribute_id,
        # mantener el name existente (no sobrescribir nombres personalizados)
        if "ml_attribute_id" in data and "name" not in data:
            # No remover name del data, simplemente no incluirlo si el name actual es personalizado
            for record in self:
                if record.name and record.name != record.ml_attribute_id and record.name != data.get("ml_attribute_id"):
                    # El name es personalizado y diferente del ml_attribute_id, preservarlo
                    # No incluir name en data para que se mantenga el valor actual
                    pass
        
        self._prepare_value_fields(data)
        return super().write(data)

    def _prepare_value_fields(self, vals):
        return vals

