# -*- coding: utf-8 -*-
import logging

from odoo import api, models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        res = super()._action_done(cancel_backorder=cancel_backorder)

        product_ids = {
            move.product_id.id
            for move in self
            if move.product_id
            and move.state == 'done'
            and (
                move.location_id.usage == 'internal'
                or move.location_dest_id.usage == 'internal'
            )
        }
        if not product_ids:
            return res

        accounts = self.env['ml.account'].search(
            [('auto_sync_stock_on_odoo_change', '=', True)]
        )
        if not accounts:
            return res

        dbname = self.env.cr.dbname
        uid = self.env.uid
        context = dict(self.env.context or {})
        product_ids_tuple = tuple(product_ids)
        account_ids_tuple = tuple(accounts.ids)

        def _run_after_commit():
            try:
                with Registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, context)
                    Quant = env['stock.quant']
                    accounts_cb = env['ml.account'].browse(account_ids_tuple).exists()
                    if not accounts_cb:
                        return
                    location_id = None
                    if len(accounts_cb) == 1:
                        wh = accounts_cb.warehouse_id
                        if wh and wh.lot_stock_id:
                            location_id = wh.lot_stock_id.id
                    for product_id in product_ids_tuple:
                        try:
                            Quant._ml_trigger_sync_for_product_location(
                                product_id,
                                location_id,
                                accounts=accounts_cb,
                                reason='stock_move._action_done',
                            )
                        except Exception:
                            _logger.exception(
                                'ML stock sync post-commit (stock_move._action_done): '
                                'product_id=%s',
                                product_id,
                            )
            except Exception:
                _logger.exception(
                    'ML stock sync post-commit: error abriendo cursor o entorno (dbname=%s)',
                    dbname,
                )

        try:
            postcommit = self.env.cr.postcommit
            if not hasattr(postcommit, 'add'):
                raise AttributeError('postcommit.add no disponible')
            postcommit.add(_run_after_commit)
        except AttributeError:
            _logger.debug(
                'postcommit no disponible; omitiendo sync ML desde stock.move._action_done'
            )
        except Exception:
            _logger.exception(
                'No se pudo registrar postcommit para sync ML desde stock.move._action_done'
            )

        return res
