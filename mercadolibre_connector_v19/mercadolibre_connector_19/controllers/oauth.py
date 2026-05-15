from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessError
import html
import logging
import secrets

_logger = logging.getLogger(__name__)


def _ml_oauth_state_valid(stored, incoming):
    """Comparación en tiempo constante del state OAuth."""
    if not stored or not incoming:
        return False
    if len(stored) != len(incoming):
        return False
    return secrets.compare_digest(stored, incoming)

class MercadoLibreController(http.Controller):

    @http.route(
        '/mercadolibre/oauth/start',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def mercadolibre_oauth_start(self, account_id=None, **kwargs):
        """
        Inicia OAuth ML en contexto HTTP (sesión disponible).
        El modelo solo devuelve la URL a esta ruta para no importar odoo.http.request en crons.
        """
        try:
            aid = int(account_id or 0)
        except (TypeError, ValueError):
            return request.not_found()
        if aid <= 0:
            return request.not_found()

        if not request.env.user.has_group('mercadolibre_connector.group_ml_admin'):
            return request.make_response(
                html.escape(
                    _('Solo los administradores de Mercado Libre pueden autorizar la cuenta.')
                ),
                status=403,
                headers=[('Content-Type', 'text/html; charset=utf-8')],
            )

        account = request.env['ml.account'].browse(aid)
        if not account.exists():
            return request.not_found()
        try:
            # Fuerza ACL + reglas de registro del usuario (sin depender de APIs internas por versión).
            account.read(['id'], limit=1)
        except AccessError:
            return request.make_response(
                html.escape(_('No tiene acceso a esta cuenta de Mercado Libre.')),
                status=403,
                headers=[('Content-Type', 'text/html; charset=utf-8')],
            )

        ml_url, verifier, oauth_state = account.sudo()._ml_build_oauth_authorization_url()
        request.session['ml_account_id'] = account.id
        request.session['ml_code_verifier'] = verifier
        request.session['ml_oauth_state'] = oauth_state
        return request.redirect(ml_url, local=False)

    @http.route('/mercadolibre/oauth/callback', type='http', auth='public', csrf=False)
    def mercadolibre_callback(self, **kwargs):
        """Callback de Mercado Libre después de autorizar la app."""
        code = kwargs.get('code')
        state = kwargs.get('state')
        account_id = request.session.get('ml_account_id')  # guardado al iniciar autorización
        error = kwargs.get('error')

        if error:
            return f"❌ Error de autorización: {html.escape(str(error))}"

        if not code:
            return "❌ Falta el parámetro 'code' en la respuesta."

        if not state:
            return (
                "❌ Falta el parámetro <code>state</code> en la respuesta OAuth. "
                "Sin <code>state</code> no se puede comprobar anti-CSRF. "
                "Iniciá la autorización con el botón «Autorizar cuenta» en Odoo y actualizá el módulo "
                "<code>mercadolibre_connector</code> si el problema continúa."
            )

        if not account_id:
            return "❌ No se encontró la cuenta asociada en la sesión."

        # Recuperar cuenta
        account = request.env['ml.account'].sudo().browse(account_id)
        if not account.exists():
            return f"❌ Cuenta Mercado Libre no encontrada (ID {html.escape(str(account_id))})."

        expected = (account.oauth_state_token or '').strip()
        incoming = state.strip()
        if not _ml_oauth_state_valid(expected, incoming):
            _logger.warning(
                "OAuth ML: state inválido o no coincide (cuenta_id=%s, sesión=%s)",
                account_id,
                bool(request.session.get('ml_oauth_state')),
            )
            account.sudo().write({'oauth_state_token': False})
            request.session.pop('ml_oauth_state', None)
            return (
                "❌ El parámetro <code>state</code> de OAuth no coincide con el iniciado en Odoo "
                "(posible ataque CSRF o sesión distinta). Cerrá esta pestaña y pulsá de nuevo "
                "«Autorizar cuenta» en la ficha de la cuenta Mercado Libre en Odoo."
            )

        # Consumir el state de un solo uso antes del intercambio de tokens
        account.sudo().write({'oauth_state_token': False})
        request.session.pop('ml_oauth_state', None)

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
            return f"❌ Error al obtener el token: {html.escape(str(e))}"
