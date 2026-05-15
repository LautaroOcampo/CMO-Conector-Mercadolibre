# -*- coding: utf-8 -*-
"""
Sincronización automática de precios Odoo → Mercado Libre cuando la cuenta tiene
auto_sync_price_on_odoo_change y una lista de precios configurada.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


def _ml_price_from_pricelist(pricelist, product_variant):
    """Precio unitario desde lista de precios para una variante (compatibilidad entre versiones Odoo)."""
    if not pricelist or not product_variant:
        return None
    try:
        p = pricelist._get_product_price(product_variant, 1.0)
        return float(p) if p is not None else None
    except TypeError:
        p = pricelist._get_product_price(product_variant.product_tmpl_id, 1.0)
        return float(p) if p is not None else None
    except Exception:
        _logger.exception(
            'Error calculando precio desde lista %s para variante %s',
            pricelist.display_name,
            product_variant.display_name,
        )
        return None


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _ml_collect_variants_and_global_pricelists_from_pricelist_items(self, items):
        """Devuelve (variantes afectadas, ids de listas con regla global)."""
        variants = self.env['product.product']
        global_pl_ids = set()
        ProductTemplate = self.env['product.template']
        for it in items:
            applied = getattr(it, 'applied_on', None) or ''
            if applied == '3_global':
                if it.pricelist_id:
                    global_pl_ids.add(it.pricelist_id.id)
                continue
            if it.product_id:
                variants |= it.product_id
            elif it.product_tmpl_id:
                variants |= it.product_tmpl_id.product_variant_ids
            elif it.categ_id:
                tmpls = ProductTemplate.search([('categ_id', 'child_of', it.categ_id.id)])
                variants |= tmpls.mapped('product_variant_ids')
        return variants, global_pl_ids

    @api.model
    def _ml_publication_sync_price_after_odoo_change(
        self,
        variants,
        global_pricelist_ids=None,
        affected_pricelist_ids=None,
        reason='',
    ):
        """
        Para cuentas ML con auto_sync_price_on_odoo_change: envía a ML el precio de la lista
        configurada para cada publicación vinculada a las variantes indicadas.

        :param affected_pricelist_ids: si se pasa un set/list de ids de ``product.pricelist``,
            solo cuentas cuya ``sync_price_pricelist_id`` está en ese conjunto (cambios en ítems
            de lista). None = todas las cuentas con auto-sync (cambios en producto).
        """
        global_pricelist_ids = global_pricelist_ids or set()
        if self.env.context.get('ml_skip_price_sync'):
            return

        accounts = self.env['ml.account'].sudo().search(
            [
                ('auto_sync_price_on_odoo_change', '=', True),
                ('sync_price_pricelist_id', '!=', False),
            ]
        )
        if affected_pricelist_ids is not None:
            affected = set(affected_pricelist_ids)
            accounts = accounts.filtered(lambda a: a.sync_price_pricelist_id.id in affected)
        if not accounts:
            return

        MlPublication = self.env['ml.publication'].sudo()

        # Alguna cuenta debe ejecutar la rama "regla global" de lista de precios (todas las publicaciones).
        needs_global_branch = bool(global_pricelist_ids) and any(
            acc.sync_price_pricelist_id.id in global_pricelist_ids for acc in accounts
        )

        # Sin rama global: si ninguna variante tiene publicación ML vinculada, no hay nada que hacer
        # (evita N búsquedas vacías al crear/importar muchos product.product nuevos).
        if not needs_global_branch:
            if not variants:
                return
            tmpl_ids = list({tid for tid in variants.mapped('product_tmpl_id').ids if tid})
            if not tmpl_ids:
                return
            if not MlPublication.search_count(
                [
                    ('product_tmpl_id', 'in', tmpl_ids),
                    ('ml_item_id', '!=', False),
                    ('ml_account_id', 'in', accounts.ids),
                ],
            ):
                return

        for account in accounts:
            if not account.access_token:
                continue
            pl = account.sync_price_pricelist_id
            try:
                account._ensure_valid_token()
            except Exception as err:
                _logger.warning(
                    'ML precio auto-sync: no se pudo validar token (cuenta %s): %s',
                    account.display_name,
                    err,
                )
                continue

            pubs = MlPublication.browse()
            if pl.id in global_pricelist_ids:
                pubs |= MlPublication.search(
                    [
                        ('ml_account_id', '=', account.id),
                        ('ml_item_id', '!=', False),
                        ('product_tmpl_id', '!=', False),
                    ]
                )
                _logger.info(
                    'ML precio auto-sync (%s): lista %s con regla global → %d publicaciones',
                    reason or 'pricelist',
                    pl.display_name,
                    len(pubs),
                )
            if variants:
                for variant in variants:
                    tmpl = variant.product_tmpl_id
                    if not tmpl:
                        continue
                    pubs |= MlPublication.search(
                        [
                            ('ml_account_id', '=', account.id),
                            ('ml_item_id', '!=', False),
                            ('product_tmpl_id', '=', tmpl.id),
                            '|',
                            ('product_variant_id', '=', False),
                            ('product_variant_id', '=', variant.id),
                        ]
                    )

            for publication in pubs:
                try:
                    pub_variant = (
                        publication.product_variant_id
                        or publication.product_tmpl_id.product_variant_id
                    )
                    if not pub_variant:
                        continue
                    price = _ml_price_from_pricelist(pl, pub_variant)
                    if not price or price <= 0:
                        _logger.warning(
                            'ML precio auto-sync: precio inválido (%.2f) para pub %s / variante %s — omitido',
                            price or 0,
                            publication.ml_item_id,
                            pub_variant.display_name,
                        )
                        continue
                    publication._force_update_price_in_ml(price_value=price)
                except Exception:
                    _logger.exception(
                        'ML precio auto-sync: error en publicación %s (cuenta %s)',
                        publication.ml_item_id,
                        account.display_name,
                    )

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get('ml_skip_price_sync'):
            return res
        if {'lst_price', 'standard_price'} & set(vals):
            self._ml_publication_sync_price_after_odoo_change(
                self, reason='product.product.write'
            )
        return res

    @api.model_create_multi
    def create(self, vals_list):
        products = super().create(vals_list)
        if not self.env.context.get('ml_skip_price_sync'):
            products._ml_publication_sync_price_after_odoo_change(
                products, reason='product.product.create'
            )
        return products


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get('ml_skip_price_sync'):
            return res
        variants, global_ids = self.env[
            'product.product'
        ]._ml_collect_variants_and_global_pricelists_from_pricelist_items(self)
        pl_ids = set(self.mapped('pricelist_id').ids)
        self.env['product.product']._ml_publication_sync_price_after_odoo_change(
            variants,
            global_pricelist_ids=global_ids,
            affected_pricelist_ids=pl_ids,
            reason='product.pricelist.item.write',
        )
        return res

    @api.model_create_multi
    def create(self, vals_list):
        items = super().create(vals_list)
        if not self.env.context.get('ml_skip_price_sync'):
            variants, global_ids = self.env[
                'product.product'
            ]._ml_collect_variants_and_global_pricelists_from_pricelist_items(items)
            pl_ids = set(items.mapped('pricelist_id').ids)
            self.env['product.product']._ml_publication_sync_price_after_odoo_change(
                variants,
                global_pricelist_ids=global_ids,
                affected_pricelist_ids=pl_ids,
                reason='product.pricelist.item.create',
            )
        return items

    def unlink(self):
        variants, global_ids = self.env[
            'product.product'
        ]._ml_collect_variants_and_global_pricelists_from_pricelist_items(self)
        pl_ids = set(self.mapped('pricelist_id').ids)
        res = super().unlink()
        if not self.env.context.get('ml_skip_price_sync'):
            self.env['product.product']._ml_publication_sync_price_after_odoo_change(
                variants,
                global_pricelist_ids=global_ids,
                affected_pricelist_ids=pl_ids,
                reason='product.pricelist.item.unlink',
            )
        return res
