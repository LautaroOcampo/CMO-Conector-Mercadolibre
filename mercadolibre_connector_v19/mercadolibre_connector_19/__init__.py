# -*- coding: utf-8 -*-

from . import controllers
from . import models
from . import wizards


def post_init_hook(env_or_cr, registry=None):
    """Hooks post-instalación / actualización del módulo.

    Odoo 19+: ``post_init_hook(env)``.
    Versiones anteriores: ``post_init_hook(cr, registry)``.
    """
    if registry is not None:
        from odoo import api, SUPERUSER_ID

        cr = env_or_cr
        env = api.Environment(cr, SUPERUSER_ID, {})
    else:
        env = env_or_cr
    cr = env.cr
    icp = env["ir.config_parameter"].sudo()

    if not icp.get_param("mercadolibre_connector.new_price_ml_backfill_v1"):
        cr.execute(
            """
            UPDATE ml_publication
            SET new_price_ml = current_price_ml
            WHERE COALESCE(new_price_ml, 0) = 0
              AND COALESCE(current_price_ml, 0) > 0
            """
        )
        icp.set_param("mercadolibre_connector.new_price_ml_backfill_v1", "1")

    if icp.get_param("mercadolibre_connector.ml_account_country_id_migrate_v1"):
        return

    cr.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'ml_account'
          AND column_name = 'country'
        """
    )
    if not cr.fetchone():
        icp.set_param("mercadolibre_connector.ml_account_country_id_migrate_v1", "1")
        return

    cr.execute(
        """
        SELECT id, country FROM ml_account
        WHERE country IS NOT NULL AND TRIM(country) <> ''
          AND (country_id IS NULL)
        """
    )
    rows = cr.fetchall()
    Country = env["res.country"].sudo()
    for ml_id, raw in rows:
        code = (raw or "").strip().upper()
        if len(code) > 2:
            if "ARGENT" in code or code == "AR":
                code = "AR"
            elif "BRASIL" in code or "BRA" in code:
                code = "BR"
            elif "MEX" in code:
                code = "MX"
            else:
                code = code[:2] if len(code) >= 2 else "AR"
        country = Country.search([("code", "=", code[:2])], limit=1)
        if not country:
            country = Country.search([("code", "=", "AR")], limit=1)
        if country:
            cr.execute(
                "UPDATE ml_account SET country_id = %s WHERE id = %s",
                (country.id, ml_id),
            )
    icp.set_param("mercadolibre_connector.ml_account_country_id_migrate_v1", "1")
