from odoo import models, fields, _
from odoo.exceptions import UserError
import requests
import logging

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # ============================================================
    # 🔹 Campos Mercado Libre
    # ============================================================
    ml_item_id = fields.Char(string="ML Item ID", copy=False)
    ml_status = fields.Selection([
        ('active', 'Activa'),
        ('paused', 'Pausada'),
        ('closed', 'Cerrada'),
        ('not_published', 'No publicada'),
    ], string="Estado en Mercado Libre", readonly=True, default='not_published')

    ml_account_id = fields.Many2one('ml.account', string="Cuenta de Mercado Libre")
    ml_listing_type = fields.Selection([
        ('gold_special', 'Premium'),
        ('gold_pro', 'Pro'),
        ('gold', 'Standard'),
    ], string="Tipo de publicación", default='gold_special')

    product_brand_id = fields.Char(string="Marca")

    ml_publication_ids = fields.One2many(
        "ml.publication",
        "product_tmpl_id",
        string="Publicaciones Mercado Libre",
        help="Permite vincular publicaciones ya importadas desde Mercado Libre."
    )

    # ============================================================
    # 🔹 Stock y precios dinámicos según configuración de cuenta ML
    # ============================================================
    def _get_stock_from_warehouse(self):
        """Devuelve el stock disponible del producto en el almacén configurado en la cuenta ML."""
        self.ensure_one()
        warehouse = self.ml_account_id.warehouse_id
        if not warehouse:
            raise UserError(_("No se configuró un almacén para sincronizar stock."))
        stock_quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product_variant_id.id),
            ('location_id', 'child_of', warehouse.lot_stock_id.id)
        ], limit=1)
        return int(stock_quant.quantity) if stock_quant else 0

    # ============================================================
    # 🔹 Pausar publicación
    # ============================================================
    def action_pause_on_ml(self):
        """Pausar publicación en Mercado Libre"""
        for product in self:
            # 🔹 Buscar publicación vinculada si el campo del producto está vacío
            publication = False
            if product.ml_item_id:
                publication = self.env["ml.publication"].search([
                    ("ml_item_id", "=", product.ml_item_id)
                ], limit=1)
            elif product.ml_publication_ids:
                publication = product.ml_publication_ids.filtered(lambda p: p.ml_item_id)[:1]
    
            if not publication or not publication.ml_item_id:
                raise UserError(_("Este producto no tiene una publicación en Mercado Libre."))
    
            account = publication.ml_account_id or product.ml_account_id
            if not account or not account.access_token:
                raise UserError(_("No hay cuenta de Mercado Libre conectada."))
    
            headers = {
                "Authorization": f"Bearer {account.access_token}",
                "Content-Type": "application/json",
            }
            url = f"https://api.mercadolibre.com/items/{publication.ml_item_id}"
            payload = {"status": "paused"}
            resp = requests.put(url, headers=headers, json=payload)
    
            if resp.status_code not in (200, 201):
                _logger.error("❌ Error pausando publicación: %s", resp.text)
                raise UserError(f"Error pausando publicación: {resp.text}")
    
            # 🔹 Actualizar estados en ambos niveles
            publication.ml_status = "paused"
            product.ml_status = "paused"
    
            _logger.info("⏸️ Publicación pausada en Mercado Libre: %s", publication.ml_item_id)
    
        return True

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('ml_skip_price_sync'):
            if {'list_price', 'standard_price'} & set(vals):
                variants = self.mapped('product_variant_ids')
                if variants:
                    self.env['product.product']._ml_publication_sync_price_after_odoo_change(
                        variants,
                        reason='product.template.write',
                    )
        return res
