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

    def action_pause_on_ml(self):
        """Pausa en Mercado Libre las publicaciones vinculadas a este producto."""
        pubs = self.mapped('ml_publication_ids').filtered(lambda p: p.ml_item_id)
        if not pubs:
            raise UserError(_('Este producto no tiene publicaciones de Mercado Libre.'))
        return pubs.action_pause_on_ml()

    def action_activate_on_ml(self):
        """Activa en Mercado Libre las publicaciones vinculadas a este producto."""
        pubs = self.mapped('ml_publication_ids').filtered(lambda p: p.ml_item_id)
        if not pubs:
            raise UserError(_('Este producto no tiene publicaciones de Mercado Libre.'))
        return pubs.action_activate_on_ml()
