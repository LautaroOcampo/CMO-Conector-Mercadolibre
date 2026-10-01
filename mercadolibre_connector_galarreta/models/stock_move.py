# -*- coding: utf-8 -*-
import logging

from odoo import api, models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    @api.model
    def _ml_moves_internal_product_ids(self, moves):
        return {
            move.product_id.id
            for move in moves
            if move.product_id
            and (
                move.location_id.usage == 'internal'
                or move.location_dest_id.usage == 'internal'
            )
        }

    @api.model
    def _ml_schedule_stock_sync_postcommit(self, product_ids, accounts, reason):
        """Agenda sync ML post-commit para uno o más productos."""
        if not product_ids or not accounts:
            return
        if self.env.context.get('ml_skip_stock_sync'):
            return

        dbname = self.env.cr.dbname
        uid = self.env.uid
        context = dict(self.env.context or {})
        product_ids_tuple = tuple(set(product_ids))
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
                                reason=reason,
                            )
                        except Exception:
                            _logger.exception(
                                'ML stock sync post-commit (%s): product_id=%s',
                                reason,
                                product_id,
                            )
            except Exception:
                _logger.exception(
                    'ML stock sync post-commit: error abriendo cursor (dbname=%s, reason=%s)',
                    dbname,
                    reason,
                )

        try:
            postcommit = self.env.cr.postcommit
            if not hasattr(postcommit, 'add'):
                raise AttributeError('postcommit.add no disponible')
            postcommit.add(_run_after_commit)
        except AttributeError:
            _logger.debug(
                'postcommit no disponible; omitiendo sync ML (%s)', reason,
            )
        except Exception:
            _logger.exception(
                'No se pudo registrar postcommit para sync ML (%s)', reason,
            )

    @api.model
    def _ml_schedule_sync_for_moves(self, moves, reason, expected_only=False):
        """
        Dispara sync hacia ML para productos de movimientos que tocan stock interno.

        expected_only=True: solo cuentas con stock_type=expected (virtual_available),
        p. ej. confirmación de OC sin recepción, reservas vía movimiento.
        """
        product_ids = self._ml_moves_internal_product_ids(moves)
        if not product_ids:
            return
        domain = [('auto_sync_stock_on_odoo_change', '=', True)]
        if expected_only:
            domain.append(('stock_type', '=', 'expected'))
        accounts = self.env['ml.account'].search(domain)
        self._ml_schedule_stock_sync_postcommit(product_ids, accounts, reason)

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals or 'product_uom_qty' in vals:
            self._ml_schedule_sync_for_moves(
                self,
                'stock_move.write(state|qty)',
                expected_only=True,
            )
        return res

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        self._ml_schedule_sync_for_moves(
            moves,
            'stock_move.create',
            expected_only=True,
        )
        return moves

    def unlink(self):
        product_ids = self._ml_moves_internal_product_ids(self)
        accounts = self.env['ml.account'].search([
            ('auto_sync_stock_on_odoo_change', '=', True),
            ('stock_type', '=', 'expected'),
        ])
        res = super().unlink()
        if product_ids and accounts:
            self._ml_schedule_stock_sync_postcommit(
                product_ids,
                accounts,
                'stock_move.unlink',
            )
        return res

    def _action_done(self, cancel_backorder=False):
        res = super()._action_done(cancel_backorder=cancel_backorder)
        done_moves = self.filtered(lambda m: m.state == 'done')
        self._ml_schedule_sync_for_moves(
            done_moves,
            'stock_move._action_done',
            expected_only=False,
        )
        return res
