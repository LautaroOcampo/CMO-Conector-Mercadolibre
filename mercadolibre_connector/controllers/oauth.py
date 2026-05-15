from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class MercadoLibreController(http.Controller):

    @http.route('/mercadolibre/oauth/callback', type='http', auth='public', csrf=False)
    def mercadolibre_callback(self, **kwargs):
        """Callback de Mercado Libre después de autorizar la app."""
        code = kwargs.get('code')
        account_id = request.session.get('ml_account_id')  # guardado al iniciar autorización
        error = kwargs.get('error')

        if error:
            return f"❌ Error de autorización: {error}"

        if not code:
            return "❌ Falta el parámetro 'code' en la respuesta."

        if not account_id:
            return "❌ No se encontró la cuenta asociada en la sesión."

        # Recuperar cuenta
        account = request.env['ml.account'].sudo().browse(account_id)
        if not account.exists():
            return f"❌ Cuenta Mercado Libre no encontrada (ID {account_id})."

        # ✅ Recuperar el verifier desde la sesión y reasignarlo por seguridad
        verifier = request.session.get('ml_code_verifier')
        if verifier:
            account.sudo().write({'code_verifier': verifier})
            _logger.info(f"✅ Reasignado code_verifier para cuenta {account.id}")

        try:
            account.exchange_code_for_token(code)
            return "✅ Autorización exitosa. Ya podés cerrar esta ventana y volver a Odoo."
        except Exception as e:
            _logger.exception("Error en callback OAuth: %s", e)
            return f"❌ Error al obtener el token: {e}"
