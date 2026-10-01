# -*- coding: utf-8 -*-

from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    ml_invoice_display_name = fields.Char(
        string='Nombre ML en factura (PDF)',
        copy=False,
        help=(
            'Título de Mercado Libre impreso en el PDF cuando la línea usa producto fallback. '
            'La descripción interna de la factura conserva la referencia al pedido ML.'
        ),
    )

    def get_channel_invoice_report_line_name(self):
        """Nombre de línea para PDF: título del canal si hay fallback, si no la descripción."""
        self.ensure_one()
        if self.ml_invoice_display_name:
            return self.ml_invoice_display_name
        if 'tn_invoice_display_name' in self._fields and self.tn_invoice_display_name:
            return self.tn_invoice_display_name
        return self.name
