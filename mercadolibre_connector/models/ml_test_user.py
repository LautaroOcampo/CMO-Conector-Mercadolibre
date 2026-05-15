# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


# =====================================================
# ⚠️ TEMPORAL: MODELO PARA ALMACENAR USUARIOS TEST
# TODO: REMOVER ESTE MODELO CUANDO YA NO SE NECESITE
# =====================================================
class MLTestUser(models.Model):
    """
    ⚠️ TEMPORAL: Modelo para almacenar usuarios test de MercadoLibre.
    TODO: REMOVER ESTE MODELO CUANDO YA NO SE NECESITE
    """
    _name = 'ml.test.user'
    _description = 'MercadoLibre Test User'
    _order = 'create_date desc'
    _rec_name = 'nickname'

    ml_account_id = fields.Many2one(
        'ml.account',
        string='Cuenta MercadoLibre',
        required=True,
        ondelete='cascade'
    )
    
    user_id = fields.Char(
        string='ID Usuario',
        required=True,
        help='ID del usuario test en MercadoLibre'
    )
    
    nickname = fields.Char(
        string='Nickname',
        required=True,
        help='Nickname del usuario test'
    )
    
    password = fields.Char(
        string='Password',
        required=True,
        help='Password del usuario test'
    )
    
    site_id = fields.Char(
        string='Site ID',
        help='Site ID (MLA, MLB, MLM, etc.)'
    )
    
    email = fields.Char(
        string='Email',
        help='Email del usuario test'
    )
    
    first_name = fields.Char(
        string='Nombre',
        help='Nombre del usuario test'
    )
    
    last_name = fields.Char(
        string='Apellido',
        help='Apellido del usuario test'
    )
    
    full_data = fields.Text(
        string='Datos Completos (JSON)',
        help='Todos los datos del usuario test en formato JSON'
    )
    
    create_date = fields.Datetime(
        string='Fecha de Creación',
        readonly=True
    )
    
    # =====================================================
    # FIN SECCIÓN TEMPORAL
    # =====================================================

