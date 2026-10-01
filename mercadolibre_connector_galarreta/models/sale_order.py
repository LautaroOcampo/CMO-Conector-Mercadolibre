# -*- coding: utf-8 -*-

from odoo import models, fields, api


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sale_origin = fields.Selection(
        selection=[
            ('other', 'Otro'),
            ('mercadolibre', 'Mercado Libre'),
        ],
        string='Origen de Venta',
        default='other',
        help=(
            'Indica el canal de venta desde el que proviene esta orden. '
            'Los módulos de integración agregan sus propios valores a este campo.'
        ),
    )
    ml_sale_id = fields.Many2one(
        'ml.sale',
        string='Venta Mercado Libre',
        copy=False,
        help='Venta de Mercado Libre asociada a esta orden (si proviene de ML).'
    )

    ml_sale_ids = fields.One2many(
        'ml.sale',
        'odoo_sale_order_id',
        string='Ventas Mercado Libre',
        help='Ventas ML vinculadas a este pedido (una o varias si es pack/carrito).',
    )

    ml_order_id = fields.Char(
        string='ID Orden ML',
        related='ml_sale_id.ml_order_id',
        readonly=True,
    )

    ml_order_payload = fields.Text(
        string='Payload venta ML (JSON)',
        copy=False,
        help='JSON completo de la orden en Mercado Libre al importar la venta.',
    )

    ml_commission_amount = fields.Float(
        string='Comisión ML',
        digits=(16, 2),
        copy=False,
        help='Comisión de Mercado Libre leída del payload (sale_fee).',
    )

    ml_pack_id = fields.Char(
        string='Pack ID ML',
        copy=False,
        index=True,
        help='Pack/carrito ML: varias órdenes de la misma compra se agrupan en este pedido.',
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
