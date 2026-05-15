# -*- coding: utf-8 -*-
"""
Wizard para matchear publicaciones de MercadoLibre con productos de Odoo usando SKU.
"""
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MLMatchPublicationsWizard(models.TransientModel):
    _name = 'ml.match.publications.wizard'
    _description = 'Wizard para Matchear Publicaciones MELI con Productos Odoo'

    ml_account_id = fields.Many2one(
        'ml.account',
        string='Cuenta de Mercado Libre',
        required=True,
        help='Seleccione la cuenta de MercadoLibre'
    )
    
    matched_count = fields.Integer(
        string='Publicaciones Matcheadas',
        readonly=True,
        default=0
    )
    
    unmatched_count = fields.Integer(
        string='Publicaciones Sin Matchear',
        readonly=True,
        default=0
    )

    @api.model
    def default_get(self, fields_list):
        """Obtener cuenta por defecto si solo hay una."""
        res = super().default_get(fields_list)
        accounts = self.env['ml.account'].search([])
        if len(accounts) == 1:
            res['ml_account_id'] = accounts.id
        return res

    def action_match_publications(self):
        """
        Matchea publicaciones de MELI con productos de Odoo usando SKU.
        """
        self.ensure_one()
        
        if not self.ml_account_id:
            raise UserError(_('Debe seleccionar una cuenta de MercadoLibre'))
        
        matched = 0
        unmatched = 0
        
        # Buscar publicaciones sin producto relacionado
        publications = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.ml_account_id.id),
            ('ml_item_id', '!=', False),
            ('product_tmpl_id', '=', False)
        ])
        
        _logger.info("🔍 Buscando matcheo para %d publicaciones sin producto relacionado", len(publications))
        
        # Matchear cada publicación con productos de Odoo usando SKU
        for publication in publications:
            if self._match_publication_with_product(publication):
                matched += 1
            else:
                unmatched += 1
        
        # Actualizar contadores
        self.matched_count = matched
        self.unmatched_count = unmatched
        
        message = _(
            'Matcheo completado:\n\n'
            '✅ Publicaciones matcheadas: %d\n'
            '⚠️ Publicaciones sin matchear: %d\n\n'
            'Las publicaciones se matchearon usando el SKU únicamente contra la referencia interna (default_code) del producto. Si no hay coincidencia, no se relaciona.'
        ) % (matched, unmatched)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Matcheo Completado'),
                'message': message,
                'type': 'success' if matched > 0 else 'warning',
                'sticky': True,
            }
        }

    def _match_publication_with_product(self, publication):
        """
        Matchea una publicación con un producto de Odoo usando SKU.
        Solo busca por default_code (referencia interna). Si no hay coincidencia,
        no se intenta con ningún otro campo (barcode, nombre, etc.).
        
        :param publication: Record de ml.publication
        :return: True si se matcheó, False si no
        """
        # 1. Obtener SKU desde la publicación
        sku = None
        
        # Intentar desde seller_sku
        if publication.seller_sku:
            sku = publication.seller_sku.strip()
        
        # Intentar desde atributo SELLER_SKU
        if not sku:
            seller_sku_attr = publication.ml_attribute_ids.filtered(
                lambda a: a.ml_attribute_id == 'SELLER_SKU'
            )
            if seller_sku_attr and seller_sku_attr[0].value_name:
                sku = seller_sku_attr[0].value_name.strip()
        
        # Intentar desde seller_custom_field (variantes)
        if not sku and publication.ml_variant_ids:
            for variant in publication.ml_variant_ids:
                if variant.seller_sku:
                    sku = variant.seller_sku.strip()
                    break
        
        if not sku:
            _logger.debug("⚠️ Publicación %s no tiene SKU", publication.ml_item_id)
            return False
        
        # Buscar producto SOLO por default_code. No usar barcode ni otros campos.
        product_variant = self.env['product.product'].search([
            ('default_code', '=', sku),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        
        if product_variant:
            product = product_variant.product_tmpl_id
        else:
            # Buscar en product.template
            product = self.env['product.template'].search([
                ('default_code', '=', sku),
                ('company_id', '=', self.env.company.id)
            ], limit=1)
        
        if not product:
            _logger.debug("⚠️ No se encontró producto con SKU %s para publicación %s", sku, publication.ml_item_id)
            return False
        
        # 3. Matchear publicación con producto y asignar variante relacionada si aplica
        publication.product_tmpl_id = product.id
        if product_variant:
            publication.product_variant_id = product_variant.id
        elif len(product.product_variant_ids) == 1:
            publication.product_variant_id = product.product_variant_id.id
        else:
            publication.product_variant_id = False
        _logger.info("✅ Matcheado: Publicación %s (SKU: %s) -> Producto %s", 
                    publication.ml_item_id, sku, product.name)
        
        return True



