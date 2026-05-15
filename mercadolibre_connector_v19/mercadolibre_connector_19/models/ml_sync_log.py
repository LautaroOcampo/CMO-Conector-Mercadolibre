# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models
import logging

_logger = logging.getLogger(__name__)


class MLSyncLog(models.Model):
    _name = 'ml.sync.log'
    _description = 'Auditoría de sincronizaciones MercadoLibre'
    _order = 'create_date desc'
    _rec_name = 'create_date'

    ml_account_id = fields.Many2one(
        'ml.account',
        string='Cuenta ML',
        required=True,
        ondelete='cascade',
        index=True,
    )
    publication_id = fields.Many2one(
        'ml.publication',
        string='Publicación',
        ondelete='set null',
        index=True,
    )
    operation = fields.Selection(
        [
            ('stock_update', 'Actualización de stock'),
            ('price_update', 'Actualización de precio'),
            ('item_update', 'Actualización de ítem (título/SKU)'),
            ('status_change', 'Cambio de estado'),
            ('order_import', 'Importación de orden'),
            ('token_refresh', 'Refresco de token'),
        ],
        string='Operación',
        required=True,
        index=True,
    )
    trigger = fields.Selection(
        [('auto', 'Automático'), ('manual', 'Manual')],
        string='Origen',
        default='auto',
        required=True,
    )
    value_before = fields.Char(string='Valor antes')
    value_after = fields.Char(string='Valor después')
    result = fields.Selection(
        [('ok', 'OK'), ('error', 'Error')],
        string='Resultado',
        required=True,
        index=True,
    )
    http_status = fields.Integer(string='HTTP ML')
    error_message = fields.Text(string='Mensaje de error')
    duration_ms = fields.Integer(string='Duración (ms)')

    @api.model
    def _log_sync(self, account, operation, result, **kwargs):
        """
        Registra una sincronización con Mercado Libre.
        No crea línea si result='ok' y value_before == value_after (sin cambio real).
        """
        if not account:
            return self.browse()
        vb = kwargs.get('value_before')
        va = kwargs.get('value_after')
        if result == 'ok' and vb == va:
            return self.browse()

        allowed_keys = {
            'publication_id',
            'trigger',
            'value_before',
            'value_after',
            'http_status',
            'error_message',
            'duration_ms',
        }
        vals = {k: v for k, v in kwargs.items() if k in allowed_keys}
        pub = vals.get('publication_id')
        if pub is not None and hasattr(pub, 'id'):
            vals['publication_id'] = pub.id

        vals.update({
            'ml_account_id': account.id,
            'operation': operation,
            'result': result,
        })
        return self.sudo().create(vals)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        accounts = records.mapped('ml_account_id')
        if accounts:
            accounts.invalidate_recordset(['sync_log_error_recent_count'])
        pubs = records.mapped('publication_id')
        if pubs:
            pubs.invalidate_recordset(['sync_log_ids'])
        return records

    @api.model
    def cron_purge_old_ok_logs(self):
        """Elimina logs OK con más de 30 días; conserva errores."""
        threshold = fields.Datetime.now() - timedelta(days=30)
        to_remove = self.search([
            ('result', '=', 'ok'),
            ('create_date', '<', threshold),
        ])
        if to_remove:
            n = len(to_remove)
            to_remove.unlink()
            _logger.info('ml.sync.log: eliminados %d registros OK anteriores a %s', n, threshold)
