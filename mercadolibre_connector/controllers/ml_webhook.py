# controllers/ml_webhook.py
from odoo import http
from odoo.http import request
import json
import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

# Prefijo único para grep en odoo.log:  grep "ML WEBHOOK" odoo.log
_LOG = "[ML WEBHOOK][HTTP]"


class MLWebhookController(http.Controller):

    @http.route('/ml/notification', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def ml_notification(self, **kwargs):
        _logger.debug(
            "%s request: method=%s ip=%s",
            _LOG,
            request.httprequest.method,
            request.httprequest.remote_addr,
        )

        try:
            # Manejar GET (verificación del webhook por parte de MercadoLibre)
            if request.httprequest.method == 'GET':
                _logger.info(
                    "%s GET /ml/notification — endpoint alcanzado (solo prueba de vida, no importa ventas)",
                    _LOG,
                )
                return request.make_response(
                    json.dumps({"status": "ok", "message": "Webhook endpoint is active"}),
                    headers={'Content-Type': 'application/json'},
                    status=200
                )

            # Manejar POST (notificaciones reales)
            # Importante: en rutas type='http', el cuerpo JSON debe leerse del body.
            # request.jsonrequest no siempre está relleno según versión de Odoo / Content-Type;
            # priorizamos siempre json.loads del body crudo (patrón recomendado para webhooks).
            ct = (request.httprequest.content_type or "") or ""
            try:
                raw_bytes = request.httprequest.get_data(cache=False, as_text=False) or b''
            except TypeError:
                raw_bytes = request.httprequest.get_data(cache=False) or b''
            raw_data = raw_bytes.decode('utf-8') if raw_bytes else ''
            _logger.info(
                "%s POST recibido causa=software content_type=%s body_len=%d ip=%s",
                _LOG,
                ct,
                len(raw_bytes),
                request.httprequest.remote_addr,
            )
            data = {}
            if raw_data.strip():
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError as e:
                    _logger.error(
                        "%s JSON inválido causa=software (cuerpo no es JSON): %s",
                        _LOG,
                        str(e),
                    )
                    return request.make_response(
                        json.dumps({"status": "error", "message": f"Invalid JSON: {str(e)}"}),
                        headers={'Content-Type': 'application/json'},
                        status=400
                    )
            else:
                try:
                    jr = request.jsonrequest
                    if isinstance(jr, dict):
                        data = jr
                        _logger.info(
                            "%s JSON tomado de request.jsonrequest (body vacío) causa=software",
                            _LOG,
                        )
                except Exception as ex:
                    _logger.debug("%s request.jsonrequest no disponible: %s", _LOG, ex)

            if not data:
                _logger.warning(
                    "%s POST sin payload parseable causa=software "
                    "(revisar proxy/nginx que no consuma el body, o Content-Type)",
                    _LOG,
                )
                return request.make_response(
                    json.dumps({"status": "error", "message": "No data received"}),
                    headers={'Content-Type': 'application/json'},
                    status=400
                )

            topic = data.get('topic')
            resource = data.get('resource')
            _logger.info(
                "%s payload OK: topic=%s resource=%s user_id=%s application_id=%s",
                _LOG,
                topic,
                resource,
                data.get("user_id"),
                data.get("application_id"),
            )

            noisy_topics = {"stock-locations", "user-products-families"}
            if topic in noisy_topics:
                _logger.debug("Notificación ML (topic ruidoso): %s", topic)

            if not topic:
                _logger.warning(
                    "%s falta 'topic' en JSON causa=software o payload ML inesperado keys=%s",
                    _LOG,
                    list(data.keys())[:20],
                )
                return request.make_response(
                    json.dumps({"status": "error", "message": "Missing 'topic' field"}),
                    headers={'Content-Type': 'application/json'},
                    status=400
                )

            if topic in ('orders', 'orders_v2'):
                if not resource:
                    _logger.error(
                        "%s topic=%s pero falta 'resource' causa=software/ml",
                        _LOG,
                        topic,
                    )
                    return request.make_response(
                        json.dumps({"status": "error", "message": "Missing 'resource' field"}),
                        headers={'Content-Type': 'application/json'},
                        status=400
                    )
                
                order_id = resource.split('/')[-1]
                user_id = data.get('user_id')
                account_id = None
                if user_id is None or user_id == '':
                    _logger.warning(
                        "%s orden ML id=%s sin user_id en payload causa=config "
                        "(ML debería enviar user_id del vendedor; revisar app/topic)",
                        _LOG,
                        order_id,
                    )
                if user_id is not None and user_id != '':
                    uid = str(user_id).strip()
                    account = request.env['ml.account'].sudo().search([('meli_user_id', '=', uid)], limit=1)
                    if account:
                        account_id = account.id
                        _logger.info(
                            "%s cuenta ml.account encontrada id=%s name=%s meli_user_id=%s process_webhook_sales=%s",
                            _LOG,
                            account.id,
                            account.name,
                            account.meli_user_id,
                            account.process_webhook_sales,
                        )
                    else:
                        n_accounts = request.env["ml.account"].sudo().search_count([])
                        _logger.warning(
                            "%s causa=config ninguna ml.account con meli_user_id=%s "
                            "(hay %s cuenta(s) ML en Odoo; conectar OAuth o igualar ML User ID en la ficha)",
                            _LOG,
                            uid,
                            n_accounts,
                        )

                account = None
                if account_id:
                    account = request.env['ml.account'].sudo().browse(account_id)
                elif user_id is not None and user_id != '':
                    account = request.env['ml.account'].sudo().search(
                        [('meli_user_id', '=', str(user_id).strip())], limit=1
                    )

                if account and not account.process_webhook_sales:
                    _logger.warning(
                        "%s causa=config cuenta id=%s (%s): process_webhook_sales=False — "
                        "activar en la ficha o el webhook no importa ventas",
                        _LOG,
                        account.id,
                        account.name,
                    )
                    return request.make_response(
                        json.dumps({"status": "ok", "message": "Webhook received but processing is disabled"}),
                        headers={'Content-Type': 'application/json'},
                        status=200
                    )

                if not account_id:
                    _logger.warning(
                        "%s se guardará notificación sin ml_account_id order_id=%s — "
                        "el [JOB] no importará hasta que meli_user_id en Odoo = user_id del payload",
                        _LOG,
                        order_id,
                    )

                # Guardar notificación y encolar job (sin procesar inline)
                notification = request.env['ml.webhook.notification'].sudo().create_notification(
                    order_id=order_id,
                    topic=topic,
                    resource=resource,
                    user_id=str(user_id) if user_id else None,
                    raw_data=raw_data,
                    ml_account_id=account_id,
                )
                notification.enqueue_process()
                _logger.info(
                    "%s notificación guardada id=%s order_id=%s ml_account_id=%s — "
                    "siguiente paso: cola o job (ver [ML WEBHOOK][NOTIF]/[JOB])",
                    _LOG,
                    notification.id,
                    order_id,
                    notification.ml_account_id.id if notification.ml_account_id else False,
                )
            
            elif topic == 'items':
                item_id = resource.split('/')[-1] if resource else None
                _logger.info(
                    "%s topic=items (no crea venta; solo ítem) item_id=%s",
                    _LOG,
                    item_id,
                )
            else:
                _logger.info(
                    "%s topic=%s ignorado por este conector (no es orders)",
                    _LOG,
                    topic,
                )

            return request.make_response(
                json.dumps({"status": "ok"}),
                headers={'Content-Type': 'application/json'},
                status=200
            )

        except Exception as e:
            _logger.error(
                "%s excepción no controlada causa=software: %s",
                _LOG,
                str(e),
                exc_info=True,
            )
            # Retornar 200 para evitar que MercadoLibre reenvíe el webhook en caso de errores internos
            return request.make_response(
                json.dumps({"status": "error", "message": str(e)}),
                headers={'Content-Type': 'application/json'},
                status=200
            )
    
    @http.route('/ml/webhook/test', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def ml_webhook_test(self, **kwargs):
        """Endpoint de prueba para verificar que el webhook esté funcionando."""
        _logger.debug("Webhook ML test: method=%s", request.httprequest.method)
        return request.make_response(
            json.dumps({
                "status": "ok",
                "message": "Webhook endpoint is working",
                "method": request.httprequest.method,
                "timestamp": datetime.now().isoformat()
            }),
            headers={'Content-Type': 'application/json'},
            status=200
        )


