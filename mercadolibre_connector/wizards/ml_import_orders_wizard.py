# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MLImportOrdersWizard(models.TransientModel):
    _name = 'ml.import.orders.wizard'
    _description = 'Wizard para importar órdenes de MercadoLibre'

    ml_account_id = fields.Many2one(
        'ml.account',
        string='Cuenta de MercadoLibre',
        required=True,
        readonly=True
    )
    
    create_odoo_orders = fields.Boolean(
        string='Crear Órdenes de Venta en Odoo',
        default=True,
        help='Si está marcado, se crearán automáticamente las órdenes de venta en Odoo y sus facturas. Si no está marcado, solo se importarán los registros de ventas de MercadoLibre.'
    )

    create_customers = fields.Boolean(
        string='Crear/Actualizar Contacto',
        default=True,
        help='Si está marcado, se creará o actualizará el contacto del cliente en Odoo (match principal por Buyer ID de MercadoLibre).'
    )
    
    date_from = fields.Date(
        string='Desde fecha',
        default=fields.Date.context_today,
        help='Solo importar órdenes creadas desde esta fecha (inclusive). Dejar vacío para no filtrar.'
    )
    date_to = fields.Date(
        string='Hasta fecha',
        default=fields.Date.context_today,
        help='Solo importar órdenes creadas hasta esta fecha (inclusive). Dejar vacío para no filtrar.'
    )

    def action_import_orders(self):
        """Ejecuta la importación de órdenes con la opción seleccionada"""
        self.ensure_one()

        # Para crear sale.order en Odoo se necesita un partner. Si el usuario desactiva el contacto,
        # no permitimos crear órdenes de venta para evitar errores a mitad de importación.
        if self.create_odoo_orders and not self.create_customers:
            raise UserError(_('Para crear Órdenes de Venta en Odoo es necesario crear/actualizar el Contacto. Active "Crear/Actualizar Contacto" o desactive "Crear Órdenes de Venta en Odoo".'))
        
        if not self.ml_account_id.access_token:
            raise UserError(_('La cuenta de MercadoLibre no tiene token de acceso configurado. Por favor, autorice la cuenta primero.'))
        
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_('La fecha "Desde" no puede ser posterior a la fecha "Hasta".'))
        
        # Ejecutar la importación con los parámetros
        # El método retorna una acción de notificación, así que la retornamos directamente
        return self.ml_account_id.action_import_all_orders_from_ml(
            create_odoo_orders=self.create_odoo_orders,
            create_customers=self.create_customers,
            date_from=self.date_from,
            date_to=self.date_to,
        )

