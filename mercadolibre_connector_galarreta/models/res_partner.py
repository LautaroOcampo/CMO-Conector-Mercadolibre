# -*- coding: utf-8 -*-
import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def _normalize_tax_id(value):
    return re.sub(r'[^0-9]', '', str(value or ''))


def parse_meli_billing_fiscal_fields(billing_payload):
    """Extrae datos de facturación útiles desde la respuesta billing-info de Mercado Libre."""
    if not billing_payload or not isinstance(billing_payload, dict):
        return {}

    buyer = billing_payload.get('buyer') or {}
    bi = buyer.get('billing_info') or {}
    ident = bi.get('identification') or {}
    taxes = bi.get('taxes') or {}
    taxpayer = taxes.get('taxpayer_type') or {}
    attrs = bi.get('attributes') or {}
    address = bi.get('address') or {}

    ident_type = (ident.get('type') or '').strip().upper()
    ident_number = _normalize_tax_id(ident.get('number'))
    taxpayer_desc = (taxpayer.get('description') or taxpayer.get('id') or '').strip()

    name = (bi.get('name') or '').strip()
    last_name = (bi.get('last_name') or '').strip()
    full_name = f'{name} {last_name}'.strip() if last_name else name

    email = ''
    if isinstance(attrs, dict):
        email = (attrs.get('email') or attrs.get('business_name') or '').strip()
        if not email and isinstance(attrs.get('default_buyer'), dict):
            email = (attrs['default_buyer'].get('email') or '').strip()

    street = address.get('street_name') or address.get('address_line') or ''
    street_number = address.get('street_number') or address.get('number') or ''
    city = address.get('city_name') or ''
    if not city and isinstance(address.get('city'), dict):
        city = address['city'].get('name') or ''
    state_name = ''
    state_data = address.get('state')
    if isinstance(state_data, dict):
        state_name = state_data.get('name') or ''
    elif state_data:
        state_name = str(state_data)
    zip_code = address.get('zip_code') or address.get('zip') or ''

    return {
        'identification_type': ident_type,
        'identification_number': ident_number,
        'taxpayer_type': taxpayer_desc,
        'name': full_name,
        'email': email,
        'street': street,
        'street_number': street_number,
        'city': city,
        'state_name': state_name,
        'zip_code': zip_code,
    }


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def _connector_argentina_lang(self):
        """Idioma para contactos creados desde conectores (facturas en español argentino)."""
        Lang = self.env['res.lang']
        lang = Lang._lang_get('es_AR')
        if lang:
            return lang.code
        lang = Lang.search([('code', '=', 'es_AR'), ('active', '=', True)], limit=1)
        return lang.code if lang else False

    meli_buyer_id = fields.Char(
        string='ID Comprador Mercado Libre',
        index=True,
        copy=False,
        help='ID del comprador en Mercado Libre (buyer.id). Usado para matchear ventas recurrentes.',
    )

    def _ml_apply_meli_billing_fiscal(self, billing_payload):
        """Actualiza vat/nombre/email/dirección del contacto desde billing-info ML."""
        self.ensure_one()
        if not billing_payload:
            return self

        fiscal = parse_meli_billing_fiscal_fields(billing_payload)
        if not fiscal:
            return self

        ident_number = fiscal.get('identification_number')
        vals = {}
        if ident_number:
            vals['vat'] = ident_number
        if fiscal.get('email') and not self.email:
            vals['email'] = fiscal['email']
        if fiscal.get('name') and (not self.name or self.name == 'Cliente MELI'):
            vals['name'] = fiscal['name']

        street_parts = []
        if fiscal.get('street'):
            street_parts.append(fiscal['street'])
        if fiscal.get('street_number'):
            street_parts.append(fiscal['street_number'])
        if street_parts and not self.street:
            vals['street'] = ' '.join(street_parts)
        if fiscal.get('city') and not self.city:
            vals['city'] = fiscal['city']
        if fiscal.get('zip_code') and not self.zip:
            vals['zip'] = fiscal['zip_code']
        if fiscal.get('state_name') and not self.state_id:
            state = self.env['res.country.state'].search([
                ('name', 'ilike', fiscal['state_name']),
                '|',
                ('country_id', '=', False),
                ('country_id.code', '=', 'AR'),
            ], limit=1)
            if state:
                vals['state_id'] = state.id

        if vals:
            self.write(vals)
            _logger.info(
                'Partner %s: billing ML aplicado (CUIT/DNI=%s)',
                self.id,
                ident_number or '-',
            )
        return self

    def _ml_apply_fiscal_from_identification(self, ident_type, ident_number, is_b2b=False):
        """Fallback cuando no hay billing-info API: aplica VAT desde CUIT/DNI."""
        self.ensure_one()
        ident_number = _normalize_tax_id(ident_number)
        if not ident_number:
            return self
        ident_type = (ident_type or '').upper()
        if not ident_type:
            if len(ident_number) == 11:
                ident_type = 'CUIT'
            elif len(ident_number) <= 8:
                ident_type = 'DNI'
        return self._ml_apply_meli_billing_fiscal({
            'buyer': {
                'billing_info': {
                    'identification': {
                        'type': ident_type,
                        'number': ident_number,
                    },
                },
            },
        })
