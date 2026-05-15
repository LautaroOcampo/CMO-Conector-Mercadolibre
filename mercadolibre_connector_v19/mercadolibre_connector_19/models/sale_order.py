# -*- coding: utf-8 -*-

from odoo import models, fields, api


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sale_origin = fields.Selection(
        selection_add=[('mercadolibre', 'Mercado Libre')],
        ondelete={'mercadolibre': 'set default'},
    )
    ml_sale_id = fields.Many2one(
        'ml.sale',
        string='Venta Mercado Libre',
        copy=False,
        help='Venta de Mercado Libre asociada a esta orden (si proviene de ML).'
    )

    def action_view_ml_sale(self):
        """Abre la venta de Mercado Libre asociada a esta orden."""
        self.ensure_one()
        if not self.ml_sale_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ml.sale',
            'res_id': self.ml_sale_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
