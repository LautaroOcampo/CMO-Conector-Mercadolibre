# -*- coding: utf-8 -*-
import logging

import requests

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    ml_order_payload = fields.Text(
        string='Payload venta ML (JSON)',
        copy=False,
        help='JSON completo de la orden en Mercado Libre al facturar la venta.',
    )

    ml_commission_amount = fields.Float(
        string='Comisión ML',
        digits=(16, 2),
        copy=False,
        help='Comisión de Mercado Libre informada desde el payload ML (referencia; el gasto se registra en asiento aparte).',
    )

    ml_fiscal_document_id = fields.Char(
        string='ID documento fiscal ML',
        copy=False,
        help='ID del fiscal_document subido a Mercado Libre (packs/.../fiscal_documents).',
    )

    def action_post(self):
        res = super().action_post()
        ml_invoices = self.filtered(
            lambda m: m.state == 'posted' and m.move_type == 'out_invoice'
        )
        for move in ml_invoices:
            try:
                move._ml_on_invoice_posted_upload_to_meli()
            except Exception:
                _logger.exception(
                    'Error subiendo factura %s a Mercado Libre (no se revierte la confirmación)',
                    move.name or move.id,
                )
            try:
                move._ml_on_invoice_posted_reconcile_cancel()
            except Exception:
                _logger.exception(
                    'Error reconciliando NC post-cancelación para factura %s',
                    move.name or move.id,
                )
        return res

    def _ml_on_invoice_posted_reconcile_cancel(self):
        """Si la venta ML ya está cancelada, generar NC de esta factura publicada tarde."""
        self.ensure_one()
        ml_sales = self._ml_get_related_sales().filtered(
            lambda s: s.status == 'cancelled' or s.cancellation_pending
        )
        if not ml_sales:
            return False
        _logger.info(
            '🧾 Factura %s publicada con venta ML cancelada — reconciliando NC',
            self.name or self.id,
        )
        ml_sales._ml_reconcile_cancelled_invoices(post_activity=True)
        return True

    def _ml_get_related_sales(self):
        """ml.sale vinculadas a esta factura vía pedido Odoo."""
        self.ensure_one()
        SaleOrder = self.env['sale.order']
        orders = self.env['sale.order']
        if 'sale_line_ids' in self.env['account.move.line']._fields:
            orders |= self.invoice_line_ids.mapped('sale_line_ids.order_id')
        # Fallback: origen / nombre de SO en invoice_origin
        if not orders and self.invoice_origin:
            orders = SaleOrder.search([('name', '=', self.invoice_origin.strip())], limit=1)
        if not orders:
            return self.env['ml.sale']
        return self.env['ml.sale'].sudo().search([
            ('odoo_sale_order_id', 'in', orders.ids),
            ('active', '=', True),
        ])

    def _ml_render_invoice_pdf_bytes(self):
        """PDF de esta factura para adjuntar en Odoo / subir a ML."""
        self.ensure_one()
        Report = self.env['ir.actions.report'].sudo()
        for report_ref in (
            'account.account_invoices',
            'account.report_invoice_with_payments',
            'account.report_invoice',
        ):
            try:
                pdf_content, _ctype = Report._render_qweb_pdf(report_ref, res_ids=self.ids)
                if pdf_content:
                    return pdf_content
            except Exception as err:
                _logger.debug('PDF factura %s reporte %s: %s', self.id, report_ref, err)
        return False

    def _ml_on_invoice_posted_upload_to_meli(self):
        """Al confirmar factura: adjuntar PDF a ml.sale, notas, y subir a ML."""
        self.ensure_one()
        ml_sales = self._ml_get_related_sales()
        if not ml_sales:
            return False

        # Adjuntar en Odoo + notas (idempotente)
        lead = ml_sales[:1]
        sale_order = lead.odoo_sale_order_id
        lead._ml_attach_invoice_pdf_and_note(self, sale_order=sale_order)

        # Subir a Mercado Libre (una vez por pack)
        return self._ml_upload_fiscal_document_to_meli(ml_sales)

    def _ml_upload_fiscal_document_to_meli(self, ml_sales=None):
        """
        POST /packs/{pack_id}/fiscal_documents con el PDF de la factura.

        Si pack_id es null en ML, la doc indica usar el order_id manteniendo /packs.
        """
        self.ensure_one()
        if self.ml_fiscal_document_id:
            _logger.info(
                'Factura %s ya tiene fiscal_document ML %s — no se vuelve a subir',
                self.name, self.ml_fiscal_document_id,
            )
            return True

        ml_sales = ml_sales or self._ml_get_related_sales()
        if not ml_sales:
            return False

        already = ml_sales.filtered('ml_fiscal_document_id')
        if already:
            self.ml_fiscal_document_id = already[0].ml_fiscal_document_id
            _logger.info(
                'Factura %s: pack ya tiene documento ML %s',
                self.name, self.ml_fiscal_document_id,
            )
            return True

        account = ml_sales.mapped('ml_account_id')[:1]
        if not account or not account._ensure_valid_token():
            _logger.warning(
                'No se puede subir factura %s a ML: sin cuenta/token',
                self.name,
            )
            return False

        # pack_id o, si falta, order_id (doc ML)
        pack_id = False
        for sale in ml_sales:
            pack_id = sale._ml_normalize_pack_id(sale.ml_pack_id) if hasattr(sale, '_ml_normalize_pack_id') else (sale.ml_pack_id or False)
            if pack_id:
                break
        if not pack_id:
            pack_id = ml_sales[0].ml_order_id
        if not pack_id:
            _logger.warning('Factura %s: sin pack_id ni order_id ML para subir PDF', self.name)
            return False

        pdf_content = self._ml_render_invoice_pdf_bytes()
        if not pdf_content:
            _logger.warning('Factura %s: no se pudo generar PDF para subir a ML', self.name)
            return False

        max_bytes = 1024 * 1024  # 1 MB (límite ML)
        if len(pdf_content) > max_bytes:
            _logger.warning(
                'Factura %s: PDF supera 1MB (%s bytes); ML no acepta el upload',
                self.name, len(pdf_content),
            )
            return False

        invoice_name = (self.name or 'factura').replace('/', '-').replace(' ', '_')
        filename = '%s.pdf' % invoice_name
        url = 'https://api.mercadolibre.com/packs/%s/fiscal_documents' % pack_id
        headers = {'Authorization': 'Bearer %s' % account.access_token}
        files = {
            'fiscal_document': (filename, pdf_content, 'application/pdf'),
        }

        _logger.info(
            'Subiendo factura %s a ML pack=%s (%s bytes)',
            self.name, pack_id, len(pdf_content),
        )
        try:
            # No usar _ml_request_with_retry: un 403 de fulfillment no debe borrar el token.
            response = requests.post(url, headers=headers, files=files, timeout=60)
        except Exception:
            _logger.exception('Error de red subiendo factura %s a ML pack %s', self.name, pack_id)
            return False

        if response.status_code in (200, 201):
            doc_ids = []
            try:
                payload = response.json() or {}
                doc_ids = payload.get('ids') or []
            except Exception:
                pass
            doc_id = doc_ids[0] if doc_ids else 'uploaded'
            self.ml_fiscal_document_id = doc_id
            ml_sales.write({'ml_fiscal_document_id': doc_id})
            note = _('Factura cargada en Mercado Libre (pack %s, doc %s)') % (pack_id, doc_id)
            for sale in ml_sales:
                notes = (sale.notes or '').strip()
                if 'Factura cargada en Mercado Libre' not in notes:
                    sale.notes = ('%s\n%s' % (notes, note)).strip() if notes else note
            _logger.info(
                'Factura %s subida a ML pack %s → %s',
                self.name, pack_id, doc_id,
            )
            return True

        if response.status_code == 409:
            # Ya existe PDF en el pack
            _logger.info(
                'ML pack %s ya tiene factura PDF (409) para Odoo %s',
                pack_id, self.name,
            )
            self.ml_fiscal_document_id = self.ml_fiscal_document_id or 'already_on_ml'
            ml_sales.filtered(lambda s: not s.ml_fiscal_document_id).write({
                'ml_fiscal_document_id': 'already_on_ml',
            })
            return True

        _logger.warning(
            'No se pudo subir factura %s a ML pack %s: HTTP %s body=%s',
            self.name,
            pack_id,
            response.status_code,
            (response.text or '')[:500],
        )
        return False
