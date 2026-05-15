# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class MLPublicationValuesWizard(models.TransientModel):
    _name = "ml.publication.values.wizard"
    _description = "Wizard: actualizar título, precio, stock y SKU en Mercado Libre"

    publication_id = fields.Many2one(
        "ml.publication",
        string="Publicación",
        required=True,
        ondelete="cascade",
    )
    currency_id = fields.Many2one(
        related="publication_id.currency_id",
        readonly=True,
    )
    title_ml = fields.Char(string="Título ML", required=True)
    price = fields.Monetary(
        string="Precio",
        currency_field="currency_id",
        required=True,
    )
    stock = fields.Integer(string="Stock", required=True)
    sku = fields.Char(string="SKU")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        pub_id = self.env.context.get("default_publication_id") or self.env.context.get("active_id")
        if not pub_id:
            return res
        pub = self.env["ml.publication"].browse(pub_id)
        if not pub.exists():
            return res
        res["publication_id"] = pub.id
        res["title_ml"] = (pub.title or "").strip()
        price_ref = pub.new_price_ml or pub.current_price_ml or pub.price or 0.0
        res["price"] = float(price_ref or 0.0)
        stock_ref = pub.new_stock_ml
        if stock_ref in (False, None):
            stock_ref = pub.current_stock_ml
        if stock_ref in (False, None):
            pub.invalidate_recordset(["stock"])
            pub._compute_stock()
            stock_ref = pub.stock
        res["stock"] = int(stock_ref or 0)
        res["sku"] = (pub.seller_sku or "").strip()
        return res

    def action_apply(self):
        self.ensure_one()
        if not self.publication_id:
            raise UserError(_("No hay publicación seleccionada."))
        return self.publication_id._apply_values_wizard_to_ml(
            title_ml=(self.title_ml or "").strip(),
            price=float(self.price or 0.0),
            stock=int(self.stock or 0),
            sku=(self.sku or "").strip(),
        )
