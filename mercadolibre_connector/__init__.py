# -*- coding: utf-8 -*-

from . import controllers
from . import models
from . import wizards


def post_init_hook(cr, registry):
    """Una sola vez: rellenar new_price_ml con current_price_ml donde seguía en 0."""
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env["ir.config_parameter"].sudo()
    if icp.get_param("mercadolibre_connector.new_price_ml_backfill_v1"):
        return
    cr.execute(
        """
        UPDATE ml_publication
        SET new_price_ml = current_price_ml
        WHERE COALESCE(new_price_ml, 0) = 0
          AND COALESCE(current_price_ml, 0) > 0
        """
    )
    icp.set_param("mercadolibre_connector.new_price_ml_backfill_v1", "1")
