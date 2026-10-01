from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)

class MLPublicationImage(models.Model):
    _name = "ml.publication.image"
    _description = "Imágenes de Publicación Mercado Libre"
    _order = "sequence"

    publication_id = fields.Many2one(
        "ml.publication", 
        string="Publicación", 
        required=True, 
        ondelete="cascade",
        default=lambda self: self._default_publication_id(),
    )
    product_variant_id = fields.Many2one(
        "product.product",
        string="Variante del Producto",
        ondelete="set null",
        default=lambda self: self._default_product_variant_id(),
        domain="[('product_tmpl_id', '=', parent.product_tmpl_id)]",
        help="Si se especifica, esta imagen pertenece a una variante específica del producto. Si está vacío, pertenece a la publicación principal.",
    )
    
    # Mantener variant_id para compatibilidad, pero será calculado desde product_variant_id
    variant_id = fields.Many2one(
        "ml.publication.variant",
        string="Variante ML (calculado)",
        compute="_compute_variant_id",
        store=True,  # Almacenado para permitir búsquedas
        readonly=True,
        help="Variante de ML correspondiente a la variante del producto (calculado automáticamente).",
    )
    
    def _default_publication_id(self):
        """Obtener publication_id desde el contexto"""
        if self.env.context.get("default_publication_id"):
            return self.env.context.get("default_publication_id")
        return False
    
    def _default_product_variant_id(self):
        """Obtener product_variant_id desde el contexto"""
        if self.env.context.get("default_product_variant_id"):
            return self.env.context.get("default_product_variant_id")
        return False
    
    @api.depends('product_variant_id', 'publication_id')
    def _compute_variant_id(self):
        """Calcular variant_id de ML desde product_variant_id"""
        for record in self:
            if record.product_variant_id and record.publication_id:
                # Buscar la variante de ML que corresponde a esta variante del producto
                ml_variant = record.publication_id.ml_variant_ids.filtered(
                    lambda v: v.product_variant_id.id == record.product_variant_id.id
                )
                record.variant_id = ml_variant[:1] if ml_variant else False
            else:
                record.variant_id = False
    image_1920 = fields.Image("Imagen", max_width=1920, max_height=1920)
    sequence = fields.Integer(string="Secuencia", default=10)
    ml_picture_id = fields.Char(
        string="ID Imagen ML",
        readonly=True,
        help="Identificador de la imagen en Mercado Libre una vez que fue subida.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Establecer product_variant_id desde el contexto si no está presente
        for vals in vals_list:
            # Establecer product_variant_id desde el contexto si no está presente
            if not vals.get("product_variant_id") and self.env.context.get("default_product_variant_id"):
                vals["product_variant_id"] = self.env.context.get("default_product_variant_id")
                _logger.info(f"📝 product_variant_id establecido desde contexto: {vals['product_variant_id']}")
            
            # Si se especifica product_variant_id pero no publication_id, intentar obtenerlo desde la publicación padre
            if vals.get("product_variant_id") and not vals.get("publication_id"):
                # Si hay un parent en el contexto, usar ese publication_id
                if self.env.context.get("default_publication_id"):
                    vals["publication_id"] = self.env.context.get("default_publication_id")
                    _logger.info(f"📝 publication_id establecido desde contexto: {vals['publication_id']}")
        
        records = super().create(vals_list)
        if self.env.context.get("force_ml_publish"):
            for rec in records:
                if rec.publication_id:
                    _logger.info(f"🖼️ Nueva imagen agregada a publicación {rec.publication_id.id}, publicando en ML por contexto force_ml_publish...")
                    try:
                        rec.publication_id.action_publish_to_ml()
                    except Exception as e:
                        _logger.exception(f"❌ Error al actualizar ML desde create(): {e}")
        else:
            _logger.info("📝 Imagen creada sin publicar automáticamente (force_ml_publish ausente).")
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("force_ml_publish"):
            for rec in self:
                if rec.publication_id:
                    _logger.info(f"🖋️ Imagen modificada en publicación {rec.publication_id.id}, publicando en ML por contexto force_ml_publish...")
                    try:
                        rec.publication_id.action_publish_to_ml()
                    except Exception as e:
                        _logger.exception(f"❌ Error al actualizar ML desde write(): {e}")
        else:
            _logger.info("📝 Imagen modificada sin publicar automáticamente (force_ml_publish ausente).")
        return res
