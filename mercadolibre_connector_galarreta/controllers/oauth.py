# -*- coding: utf-8 -*-
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import urllib.parse

from odoo import _, http
from odoo.exceptions import AccessError
from odoo.http import request

_logger = logging.getLogger(__name__)

_INSTALL_TS_TTL = 5 * 60
# grep "ML OAUTH" odoo.log
_LOG = '[ML OAUTH]'
_DISPATCHER = '19.0.1.0.59'


def _ml_oauth_state_valid(stored, incoming):
    """Comparación en tiempo constante del state OAuth."""
    if not stored or not incoming:
        return False
    if len(stored) != len(incoming):
        return False
    return secrets.compare_digest(stored, incoming)


def _oauth_html(title, heading, body, ok=True):
    color = 'green' if ok else 'red'
    return (
        "<html><head><title>%s</title></head>"
        "<body style='font-family:Arial,sans-serif;text-align:center;padding:50px;'>"
        "<h1 style='color:%s;'>%s</h1>"
        "<p>%s</p>"
        "</body></html>"
    ) % (html.escape(title), color, html.escape(heading), body)


def _clear_ml_oauth_session():
    request.session.pop('ml_account_id', None)
    request.session.pop('ml_code_verifier', None)
    request.session.pop('ml_oauth_state', None)


def _ml_local_account_for_state(state):
    """Cuenta local que inició OAuth (state en ficha o sesión)."""
    Account = request.env['ml.account'].sudo()
    state = (state or '').strip()
    if not state:
        return Account.browse()
    account = Account.search([('oauth_state_token', '=', state)], limit=1)
    if account:
        return account
    aid = request.session.get('ml_account_id')
    if not aid:
        return Account.browse()
    try:
        account = Account.browse(int(aid))
    except (TypeError, ValueError):
        return Account.browse()
    if not account.exists():
        return Account.browse()
    expected = (account.oauth_state_token or '').strip()
    sess_state = (request.session.get('ml_oauth_state') or '').strip()
    if _ml_oauth_state_valid(expected, state) or _ml_oauth_state_valid(sess_state, state):
        return account
    return Account.browse()


def _ml_handoff_tokens_to_client_tenant(account, tenant):
    """Si el vendedor ya es de un cliente, reenvía los tokens nuevos para no dejarlo sin refresh."""
    try:
        from odoo.addons.mercadolibre_oauth_hub.controllers.ml_oauth_hub import (
            MercadoLibreOAuthHubController,
        )
    except ImportError:
        return False
    hub_config = request.env['ml.oauth.hub.config'].sudo().get_config()
    if not hub_config:
        return False
    seller = request.env['ml.oauth.seller'].sudo().search([
        ('tenant_id', '=', tenant.id),
        ('meli_user_id', '=', (account.meli_user_id or '').strip()),
    ], limit=1)
    expires_in = 0
    if account.token_expiration:
        from odoo import fields as odoo_fields
        delta = account.token_expiration - odoo_fields.Datetime.now()
        expires_in = max(int(delta.total_seconds()), 0)

    class _Pending:
        tenant_account_id = seller.tenant_account_id if seller else 0

    token_data = {
        'access_token': account.access_token or '',
        'refresh_token': account.refresh_token or '',
        'expires_in': expires_in,
    }
    ok, _err = MercadoLibreOAuthHubController()._push_tokens_to_tenant(
        tenant, _Pending(), token_data, hub_config,
    )
    return ok


def complete_local_ml_oauth_callback(code, state):
    """Termina OAuth directo en esta base. None si el state no es de una cuenta local."""
    account = _ml_local_account_for_state(state)
    if not account:
        return None

    expected = (account.oauth_state_token or '').strip()
    if not _ml_oauth_state_valid(expected, (state or '').strip()):
        _logger.warning(
            '%s /callback local state inválido cuenta_id=%s has_expected=%s',
            _LOG, account.id, bool(expected),
        )
        account.write({'oauth_state_token': False})
        _clear_ml_oauth_session()
        return _oauth_html(
            'Error',
            'Error',
            html.escape(
                'El parámetro state de OAuth no coincide. Cerrá esta pestaña y '
                'autorizá de nuevo desde Odoo.'
            ),
            ok=False,
        )

    account.write({'oauth_state_token': False})
    verifier = request.session.get('ml_code_verifier')
    if verifier:
        account.write({'code_verifier': verifier})
    account._ensure_hub_app_credentials_on_account()

    try:
        account.exchange_code_for_token(code)
        uid = (account.meli_user_id or '').strip()
        if uid and 'ml.oauth.seller' in request.env:
            tenant, _seller = request.env['ml.oauth.seller'].sudo()._find_tenant_for_user(uid)
            if tenant and not tenant._is_this_odoo():
                _logger.warning(
                    '%s /callback local: meli_user_id=%s ya está en cliente %s — '
                    'se reenvían tokens al cliente y no se guardan acá',
                    _LOG, uid, tenant.code,
                )
                _ml_handoff_tokens_to_client_tenant(account, tenant)
                account.write({
                    'access_token': False,
                    'refresh_token': False,
                    'token_expiration': False,
                    'is_connected': False,
                    'meli_user_id': False,
                    'meli_nickname': False,
                })
                _clear_ml_oauth_session()
                return _oauth_html(
                    'Error',
                    'Error',
                    html.escape(
                        'Esa cuenta de Mercado Libre ya está autorizada en el cliente '
                        '%s. Usá otro usuario (por ejemplo un usuario test), no el '
                        'mismo vendedor de un cliente.'
                        % tenant.name
                    ),
                    ok=False,
                )
        _logger.info('%s /callback local OK tokens account_id=%s', _LOG, account.id)
        _clear_ml_oauth_session()
        return _oauth_html(
            'Autorización exitosa',
            'Autorización exitosa',
            'Ya podés cerrar esta ventana y volver a Odoo.',
        )
    except Exception as e:
        _logger.exception('%s /callback local error al obtener token: %s', _LOG, e)
        _clear_ml_oauth_session()
        return _oauth_html(
            'Error',
            'Error',
            html.escape('Error al obtener el token: %s' % e),
            ok=False,
        )


class MercadoLibreController(http.Controller):
    """OAuth en el Odoo cliente (v19).

    - /mercadolibre/oauth/start → directo a ML o redirige al hub externo
    - /mercadolibre/oauth/callback → solo modo directo (hub externo tiene su propio callback)
    - /mercadolibre/oauth/install → recibe tokens del hub (POST firmado)
    - /mercadolibre/oauth/done → página de éxito tras volver del hub
    """

    def _page_error(self, message, title='Error'):
        _logger.warning('%s page_error title=%s message=%s', _LOG, title, message)
        return _oauth_html(title, title, html.escape(str(message)), ok=False)

    def _redirect_tenant_to_hub(self, account):
        Account = request.env['ml.account'].sudo()
        hub = Account._oauth_hub_url()
        secret = Account._oauth_hub_secret()
        code = request.env.cr.dbname
        web_base = Account._get_web_base_url()
        host_url = (request.httprequest.host_url or '').rstrip('/')
        _logger.info(
            '%s hub_redirect begin db=%s account_id=%s hub_url=%s '
            'web.base.url=%s host_url=%s secret_set=%s country=%s',
            _LOG,
            code,
            account.id,
            hub or '(vacío)',
            web_base or '(vacío)',
            host_url or '(vacío)',
            bool(secret),
            account.country_id.code if account.country_id else None,
        )
        if not hub or not secret:
            _logger.error(
                '%s hub_redirect ABORT: falta hub_url=%s o secret (set=%s)',
                _LOG, bool(hub), bool(secret),
            )
            return self._page_error(
                'Falta la URL o el secreto del hub OAuth (parámetros de sistema).'
            )
        return_url = host_url
        ts = str(int(time.time()))
        account_id = str(account.id)
        msg = '%s|%s|%s|%s' % (code, return_url, account_id, ts)
        sig = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        url = '%s/mercadolibre/oauth/hub/start?%s' % (
            hub,
            urllib.parse.urlencode({
                'tenant': code,
                'return_url': return_url,
                'account_id': account_id,
                'country': (account.country_id.code or 'AR'),
                'ts': ts,
                'sig': sig,
            }),
        )
        _logger.info(
            '%s hub_redirect OK → %s (si ves 404 en el navegador, el módulo '
            'mercadolibre_oauth_hub no está instalado/publicado en ese host)',
            _LOG,
            url,
        )
        return request.redirect(url, local=False)

    @http.route(
        '/mercadolibre/oauth/start',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def mercadolibre_oauth_start(self, account_id=None, **kwargs):
        """Inicia OAuth ML (directo o vía hub externo)."""
        _logger.info(
            '%s /start hit account_id_param=%s user=%s db=%s ip=%s',
            _LOG,
            account_id,
            request.env.user.login,
            request.env.cr.dbname,
            request.httprequest.remote_addr,
        )
        try:
            aid = int(account_id or 0)
        except (TypeError, ValueError):
            _logger.warning('%s /start account_id inválido: %r', _LOG, account_id)
            return request.not_found()
        if aid <= 0:
            _logger.warning('%s /start account_id <= 0', _LOG)
            return request.not_found()

        if not request.env.user.has_group('mercadolibre_connector_galarreta.group_ml_admin'):
            _logger.warning(
                '%s /start DENEGADO: user=%s sin group_ml_admin',
                _LOG, request.env.user.login,
            )
            return request.make_response(
                html.escape(
                    _('Solo los administradores de Mercado Libre pueden autorizar la cuenta.')
                ),
                status=403,
                headers=[('Content-Type', 'text/html; charset=utf-8')],
            )

        account = request.env['ml.account'].browse(aid)
        if not account.exists():
            _logger.warning('%s /start cuenta id=%s no existe', _LOG, aid)
            return request.not_found()
        try:
            account[:1].read(['id'])
        except AccessError:
            _logger.warning(
                '%s /start AccessError user=%s account_id=%s',
                _LOG, request.env.user.login, aid,
            )
            return request.make_response(
                html.escape(_('No tiene acceso a esta cuenta de Mercado Libre.')),
                status=403,
                headers=[('Content-Type', 'text/html; charset=utf-8')],
            )

        sec = account.sudo()
        via = sec._oauth_via_hub_enabled()
        is_hub = sec._oauth_this_is_hub()
        same_host = sec._oauth_same_host(sec._oauth_hub_url(), request.httprequest.host_url)
        use_hub = via and not is_hub
        if use_hub and same_host:
            _logger.warning(
                '%s /start hub_url apunta a este mismo host → forzando modo directo',
                _LOG,
            )
            use_hub = False
        _logger.info(
            '%s /start modo: via_hub=%s is_hub=%s same_host=%s → use_hub=%s',
            _LOG, via, is_hub, same_host, use_hub,
        )
        if use_hub:
            return self._redirect_tenant_to_hub(sec)

        try:
            redirect_uri = sec._validate_oauth_configuration()
        except Exception as e:
            _logger.exception('%s /start validación OAuth falló: %s', _LOG, e)
            return self._page_error(str(e), title='Error de Configuración')

        _logger.info(
            '%s /start modo DIRECTO cuenta_id=%s país=%s redirect_uri=%s',
            _LOG,
            account.id,
            account.country_id.code if account.country_id else None,
            redirect_uri,
        )
        ml_url, verifier, oauth_state = sec._ml_build_oauth_authorization_url()
        request.session['ml_account_id'] = account.id
        request.session['ml_code_verifier'] = verifier
        request.session['ml_oauth_state'] = oauth_state
        _logger.info('%s /start redirect a Mercado Libre OK', _LOG)
        return request.redirect(ml_url, local=False)

    @http.route('/mercadolibre/oauth/callback', type='http', auth='public', csrf=False)
    def mercadolibre_callback(self, **kwargs):
        """Callback único: si el hub está instalado, él decide cliente vs local."""
        _logger.info(
            '%s /callback dispatcher=%s pending_model=%s session_account_id=%s has_code=%s',
            _LOG,
            _DISPATCHER,
            'ml.oauth.pending' in request.env,
            request.session.get('ml_account_id'),
            bool(kwargs.get('code')),
        )
        if 'ml.oauth.pending' in request.env:
            from odoo.addons.mercadolibre_oauth_hub.controllers.ml_oauth_hub import (
                MercadoLibreOAuthHubController,
            )
            return MercadoLibreOAuthHubController().callback(**kwargs)

        code = kwargs.get('code')
        state = kwargs.get('state')
        error = kwargs.get('error')
        account_id = request.session.get('ml_account_id')
        _logger.info(
            '%s /callback hit error=%s has_code=%s has_state=%s session_account_id=%s',
            _LOG, error, bool(code), bool(state), account_id,
        )

        if error:
            _clear_ml_oauth_session()
            return self._page_error('Error de autorización: %s' % error)

        if not code:
            _clear_ml_oauth_session()
            return self._page_error("Falta el parámetro 'code' en la respuesta.")

        if not state:
            _clear_ml_oauth_session()
            return self._page_error(
                'Falta el parámetro state en la respuesta OAuth. '
                'Iniciá la autorización con el botón «Autorizar cuenta» en Odoo.'
            )

        local = complete_local_ml_oauth_callback(code, state)
        if local is not None:
            return local
        _clear_ml_oauth_session()
        return self._page_error('No se encontró la cuenta asociada en la sesión.')

    @http.route('/mercadolibre/oauth/install', type='http', auth='public', csrf=False, methods=['POST'])
    def install_from_hub(self, **kwargs):
        """El hub POSTea los tokens acá. No van por querystring ni por el browser."""
        raw = request.httprequest.data or b''
        signature = request.httprequest.headers.get('X-ML-OAuth-Signature') or ''
        Account = request.env['ml.account'].sudo()
        _logger.info(
            '%s /install hit db=%s body_len=%s has_sig=%s via_hub=%s is_hub=%s ip=%s',
            _LOG,
            request.env.cr.dbname,
            len(raw or b''),
            bool(signature),
            Account._oauth_via_hub_enabled(),
            Account._oauth_this_is_hub(),
            request.httprequest.remote_addr,
        )

        def _json(payload, status=200):
            if status >= 400:
                _logger.warning('%s /install response status=%s payload=%s', _LOG, status, payload)
            return request.make_response(
                json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                status=status,
            )

        if not Account._oauth_via_hub_enabled() or Account._oauth_this_is_hub():
            return _json({'ok': False, 'error': 'not_store'}, status=400)
        if not Account._verify_oauth_install_signature(raw, signature):
            _logger.warning('%s /install firma HMAC inválida', _LOG)
            return _json({'ok': False, 'error': 'bad_signature'}, status=403)
        try:
            data = json.loads(raw.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return _json({'ok': False, 'error': 'bad_json'}, status=400)
        try:
            ts = int(data.get('ts') or 0)
        except (TypeError, ValueError):
            ts = 0
        age = abs(time.time() - ts) if ts else None
        if abs(time.time() - ts) > _INSTALL_TS_TTL:
            _logger.warning('%s /install payload expirado ts=%s age=%s', _LOG, ts, age)
            return _json({'ok': False, 'error': 'expired'}, status=403)
        if not data.get('access_token'):
            return _json({'ok': False, 'error': 'no_token'}, status=400)
        try:
            account_id = int(data.get('account_id') or 0)
        except (TypeError, ValueError):
            account_id = 0
        account = Account.browse(account_id)
        if not account.exists():
            _logger.warning('%s /install account_id=%s no existe', _LOG, account_id)
            return _json({'ok': False, 'error': 'no_account'}, status=404)
        try:
            account._apply_tokens_from_hub(data)
        except Exception as e:
            _logger.exception('%s /install _apply_tokens_from_hub falló: %s', _LOG, e)
            return _json({'ok': False, 'error': 'apply_failed', 'detail': str(e)[:200]}, status=500)
        _logger.info(
            '%s /install OK account_id=%s connected=%s meli_user_id=%s has_client_id=%s',
            _LOG,
            account.id,
            account.is_connected,
            account.meli_user_id or '',
            bool(account.client_id),
        )
        return _json({'ok': True, 'account_id': account.id})

    @http.route('/mercadolibre/oauth/done', type='http', auth='public', csrf=False)
    def oauth_done(self, **kwargs):
        _logger.info('%s /done hit db=%s', _LOG, request.env.cr.dbname)
        return _oauth_html(
            'Autorización exitosa',
            'Autorización exitosa',
            'La cuenta quedó conectada con Odoo. Podés cerrar esta ventana.',
        )
