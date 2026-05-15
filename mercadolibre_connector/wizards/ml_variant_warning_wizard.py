# -*- coding: utf-8 -*-
"""
Wizard para mostrar un aviso en pantalla (ej. revisar precio de publicaciones con variantes).
"""
from odoo import models, fields, _


class MLVariantWarningWizard(models.TransientModel):
    _name = 'ml.variant.warning.wizard'
    _description = 'Aviso: Publicaciones con variantes'

    title = fields.Char(string='Título', required=True, default=lambda self: _('Atención'))
    message = fields.Text(string='Mensaje', required=True)

    def action_close(self):
        """Cerrar el diálogo."""
        return {'type': 'ir.actions.act_window_close'}
