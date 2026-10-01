# -*- coding: utf-8 -*-
"""Elimina el modelo ml.allowed.sku y la relación de SKUs permitidos."""
import logging

from odoo import SUPERUSER_ID, api
from odoo.modules.module import MODULE_UNINSTALL_FLAG

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {MODULE_UNINSTALL_FLAG: True})

    leftover_field = env['ir.model.fields'].search([
        ('model', '=', 'ml.account'),
        ('name', '=', 'allowed_sku_ids'),
    ])
    if leftover_field:
        leftover_field.unlink()
        _logger.info('Campo ml.account.allowed_sku_ids eliminado.')

    cr.execute(
        """
        UPDATE ir_model
           SET state = 'manual'
         WHERE model = %s
           AND state != 'manual'
        """,
        ('ml.allowed.sku',),
    )
    model = env['ir.model'].search([('model', '=', 'ml.allowed.sku')])
    if model:
        model.invalidate_recordset(['state'])
        model.unlink()
        _logger.info('Modelo ml.allowed.sku eliminado.')

    cr.execute('DROP TABLE IF EXISTS ml_account_allowed_sku_rel CASCADE')
    cr.execute('DROP TABLE IF EXISTS ml_allowed_sku CASCADE')
