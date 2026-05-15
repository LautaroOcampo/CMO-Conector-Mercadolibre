# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    meli_buyer_id = fields.Char(
        string='MercadoLibre Buyer ID',
        index=True,
        help='ID del comprador en MercadoLibre. Se usa para evitar duplicar contactos al importar ventas.'
    )


