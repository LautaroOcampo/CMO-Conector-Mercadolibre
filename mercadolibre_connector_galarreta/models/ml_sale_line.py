# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class MLSaleLine(models.Model):
    """Líneas de venta de MercadoLibre"""
    _name = 'ml.sale.line'
    _description = 'Línea de Venta de MercadoLibre'
    _order = 'sequence, id'

    order_id = fields.Many2one(
        'ml.sale',
        string='Venta',
        required=True,
        ondelete='cascade',
        index=True
    )
    
    sequence = fields.Integer(
        string='Secuencia',
        default=10,
        help='Orden de las líneas en la venta'
    )

    name = fields.Char(
        string='Descripción',
        required=True,
        help='Nombre del producto'
    )

    ml_item_id = fields.Char(
        string='ID Item ML',
        help='ID del item en MercadoLibre'
    )

    ml_variation_id = fields.Char(
        string='ID Variación ML',
        help='ID de variación en MercadoLibre (si aplica)',
    )

    publication_id = fields.Many2one(
        'ml.publication',
        string='Publicación ML',
        ondelete='set null',
        help='Publicación importada en Odoo; el producto se resuelve desde aquí',
    )

    quantity = fields.Float(
        string='Cantidad',
        required=True,
        default=1.0,
        digits=(16, 3)
    )

    price_unit = fields.Float(
        string='Precio Unitario',
        required=True,
        digits=(16, 2)
    )

    price_subtotal = fields.Float(
        string='Subtotal',
        compute='_compute_price_subtotal',
        digits=(16, 2)
    )

    ml_tax_rate = fields.Float(
        string='Tasa IVA ML (%)',
        digits=(16, 4),
        help='Tasa de IVA informada por Mercado Libre en order_items[].taxes al importar la orden.',
    )

    sku = fields.Char(
        string='SKU',
        help='Código SKU del producto'
    )

    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Producto en Odoo',
        ondelete='set null',
        help='Producto de Odoo relacionado'
    )

    product_id = fields.Many2one(
        'product.product',
        string='Variante de Producto',
        ondelete='set null',
        help='Variante específica del producto'
    )

    @api.depends('quantity', 'price_unit')
    def _compute_price_subtotal(self):
        """Calcula el subtotal de la línea"""
        for line in self:
            line.price_subtotal = line.quantity * line.price_unit

