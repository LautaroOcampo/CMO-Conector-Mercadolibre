# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MLCleanupUnpaidSalesWizard(models.TransientModel):
    _name = 'ml.cleanup.unpaid.sales.wizard'
    _description = 'Limpiar ventas Mercado Libre no pagadas'

    sale_ids = fields.Many2many(
        'ml.sale',
        string='Ventas a limpiar',
        help='Ventas ML con estado distinto de Pagada/Cancelada.',
    )
    sale_count = fields.Integer(
        string='Cantidad',
        compute='_compute_sale_count',
    )
    with_odoo_order_count = fields.Integer(
        string='Con pedido Odoo',
        compute='_compute_sale_count',
    )
    summary = fields.Text(
        string='Detalle',
        compute='_compute_sale_count',
    )

    @api.depends('sale_ids', 'sale_ids.status', 'sale_ids.odoo_sale_order_id')
    def _compute_sale_count(self):
        for wiz in self:
            sales = wiz.sale_ids
            wiz.sale_count = len(sales)
            wiz.with_odoo_order_count = len(sales.filtered('odoo_sale_order_id'))
            lines = []
            for sale in sales[:50]:
                so = sale.odoo_sale_order_id.name if sale.odoo_sale_order_id else '-'
                lines.append(
                    '%s | %s | SO: %s' % (sale.ml_order_id, sale.status, so)
                )
            if len(sales) > 50:
                lines.append(_('… y %s más') % (len(sales) - 50))
            wiz.summary = '\n'.join(lines) if lines else _('No hay ventas no pagadas para limpiar.')

    def action_confirm_cleanup(self):
        self.ensure_one()
        if not self.sale_ids:
            raise UserError(_('No hay ventas no pagadas para limpiar.'))

        cleaned = 0
        skipped = []
        for sale in self.sale_ids:
            try:
                ok, msg = sale._ml_cleanup_unpaid_record()
                if ok:
                    cleaned += 1
                    _logger.info('[ML CLEANUP] %s', msg)
                else:
                    skipped.append(msg)
                    _logger.warning('[ML CLEANUP] %s', msg)
            except Exception as err:
                skipped.append('%s: %s' % (sale.ml_order_id, err))
                _logger.exception(
                    '[ML CLEANUP] error en ml.sale %s', sale.ml_order_id
                )

        body = _('Se archivaron %s venta(s) ML no pagadas.') % cleaned
        if skipped:
            body += '\n\n' + _('Omitidas / revisar (%s):') % len(skipped)
            body += '\n' + '\n'.join(skipped[:20])
            if len(skipped) > 20:
                body += '\n' + _('… y %s más') % (len(skipped) - 20)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Limpieza de ventas no pagadas'),
                'message': body,
                'type': 'success' if cleaned and not skipped else ('warning' if skipped else 'info'),
                'sticky': bool(skipped),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
