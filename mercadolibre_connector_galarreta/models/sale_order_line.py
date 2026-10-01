# -*- coding: utf-8 -*-

from odoo import fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    ml_invoice_display_name = fields.Char(
        string='Nombre ML en factura (PDF)',
        copy=True,
        help=(
            'Título de la publicación en Mercado Libre para el PDF de factura cuando '
            'la línea usa producto fallback. En Odoo se mantiene la descripción interna '
            'con referencia al pedido ML.'
        ),
    )

    def _prepare_invoice_line(self, **optional_values):
        res = super()._prepare_invoice_line(**optional_values)
        if self.ml_invoice_display_name:
            res['ml_invoice_display_name'] = self.ml_invoice_display_name
        return res
