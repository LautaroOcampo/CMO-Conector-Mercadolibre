# controllers/ml_webhook.py
"""
Webhooks HTTP de Mercado Libre.

Seguridad:
- POST /ml/notification: exige `application_id` en el JSON coincidente con `client_id` de alguna
  `ml.account`. Para `orders` / `orders_v2`, exige además `user_id` coincidente con `meli_user_id`
  de alguna cuenta con ese `client_id`.
- Opcional: parámetro `mercadolibre_connector.webhook_ingress_secret`. Si está definido, la
  petición debe enviar el mismo valor en cabecera `X-ML-Webhook-Secret` o en query `ml_webhook_token`
  (útil si registrás la URL en ML con `?ml_webhook_token=...`).
- GET /ml/webhook/test: solo usuarios internos, grupo Administrador ML, y
  `mercadolibre_connector.webhook_test_enabled` = True.
"""
import json
import logging
import secrets
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Prefijo único para grep en odoo.log:  grep "ML WEBHOOK" odoo.log
_LOG = "[ML WEBHOOK][HTTP]"

ICP_WEBHOOK_INGRESS_SECRET = "mercadolibre_connector.webhook_ingress_secret"
ICP_WEBHOOK_TEST_ENABLED = "mercadolibre_connector.webhook_test_enabled"


def _ml_json_error(message, status=400):
    return request.make_response(
        json.dumps({"status": "error", "message": message}),
        headers={"Content-Type": "application/json"},
        status=status,
    )


def _ml_json_ok(payload=None, status=200):
    body = {"status": "ok"}
    if payload:
        body.update(payload)
    return request.make_response(
        json.dumps(body),
        headers={"Content-Type": "application/json"},
        status=status,
    )


def _ml_webhook_ingress_secret():
    return (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param(ICP_WEBHOOK_INGRESS_SECRET, "")
        .strip()
    )


def _ml_validate_ingress_secret():
    """
    Si hay secreto en ICP, exige el mismo valor en cabecera o query (ML suele conservar query en POST).
    """
    secret = _ml_webhook_ingress_secret()
    if not secret:
        return True, None
    hdr = (
        request.httprequest.headers.get("X-ML-Webhook-Secret")
        or request.httprequest.headers.get("X-Webhook-Secret")
        or ""
    ).strip()
    qry = (
        request.httprequest.args.get("ml_webhook_token")
        or request.httprequest.args.get("token")
        or ""
    ).strip()
    candidate = hdr or qry
    if not candidate:
        return False, "missing_ingress_secret"
    if len(candidate) != len(secret):
        return False, "ingress_secret_mismatch"
    if not secrets.compare_digest(secret, candidate):
        return False, "ingress_secret_mismatch"
    return True, None


def _ml_normalize_application_id(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _ml_accounts_for_application(app_id_norm):
    return request.env["ml.account"].sudo().search([("client_id", "=", app_id_norm)])


def _ml_validate_application_and_user(data, topics_requiring_user=("orders", "orders_v2")):
    """
    Devuelve (ok, accounts, app_id_norm, error_code, error_message).
    `accounts`: recordset de ml.account con client_id = application_id del payload.
    """
    app_id_norm = _ml_normalize_application_id(data.get("application_id"))
    if not app_id_norm:
        return False, request.env["ml.account"].browse(), None, "missing_application_id", (
            "Missing or empty 'application_id' (must match the Mercado Libre App Client ID in Odoo)."
        )
    accounts = _ml_accounts_for_application(app_id_norm)
    if not accounts:
        _logger.warning(
            "%s rechazado: application_id=%s no coincide con ningún ml.account.client_id",
            _LOG,
            app_id_norm,
        )
        return (
            False,
            accounts,
            app_id_norm,
            "unknown_application_id",
            "Unknown application_id for this Odoo instance.",
        )

    topic = data.get("topic")
    if topic in topics_requiring_user:
        user_id = data.get("user_id")
        if user_id is None or str(user_id).strip() == "":
            return (
                False,
                accounts,
                app_id_norm,
                "missing_user_id",
                "Missing 'user_id' for order notification (required for verification).",
            )
        uid = str(user_id).strip()
        matched = accounts.filtered(lambda a: (a.meli_user_id or "").strip() == uid)
        if not matched:
            _logger.warning(
                "%s rechazado: user_id=%s no coincide con meli_user_id de cuentas "
                "con client_id=%s (cuentas candidatas=%s)",
                _LOG,
                uid,
                app_id_norm,
                accounts.ids,
            )
            return (
                False,
                accounts,
                app_id_norm,
                "user_application_mismatch",
                "user_id does not match any Mercado Libre account linked to this application in Odoo.",
            )
        return True, matched, app_id_norm, None, None

    return True, accounts, app_id_norm, None, None


class MLWebhookController(http.Controller):

    @http.route("/ml/notification", type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def ml_notification(self, **kwargs):
        _logger.debug(
            "%s request: method=%s ip=%s",
            _LOG,
            request.httprequest.method,
            request.httprequest.remote_addr,
        )

        ok_ingress, ingress_err = _ml_validate_ingress_secret()
        if not ok_ingress:
            _logger.warning(
                "%s acceso denegado (ingress): %s ip=%s",
                _LOG,
                ingress_err,
                request.httprequest.remote_addr,
            )
            return _ml_json_error("Forbidden: invalid or missing webhook ingress secret.", status=403)

        try:
            if request.httprequest.method == "GET":
                _logger.info(
                    "%s GET /ml/notification — prueba de vida (no importa ventas)",
                    _LOG,
                )
                return _ml_json_ok(
                    {"message": "Webhook endpoint is active"},
                    status=200,
                )

            ct = (request.httprequest.content_type or "") or ""
            try:
                raw_bytes = request.httprequest.get_data(cache=False, as_text=False) or b""
            except TypeError:
                raw_bytes = request.httprequest.get_data(cache=False) or b""
            raw_data = raw_bytes.decode("utf-8") if raw_bytes else ""
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
                    return _ml_json_error(f"Invalid JSON: {str(e)}", status=400)
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
                return _ml_json_error("No data received", status=400)

            ok_app, accounts, _app_id, err_code, err_msg = _ml_validate_application_and_user(data)
            if not ok_app:
                _logger.warning(
                    "%s validación application/user falló: %s ip=%s",
                    _LOG,
                    err_code,
                    request.httprequest.remote_addr,
                )
                return _ml_json_error(err_msg, status=403)

            topic = data.get("topic")
            resource = data.get("resource")
            user_id = data.get("user_id")

            _logger.info(
                "%s payload OK: topic=%s resource=%s user_id=%s application_id=%s",
                _LOG,
                topic,
                resource,
                user_id,
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
                return _ml_json_error("Missing 'topic' field", status=400)

            if topic in ("orders", "orders_v2"):
                if not resource:
                    _logger.error(
                        "%s topic=%s pero falta 'resource' causa=software/ml",
                        _LOG,
                        topic,
                    )
                    return _ml_json_error("Missing 'resource' field", status=400)

                order_id = resource.split("/")[-1]
                uid = str(user_id).strip() if user_id is not None and user_id != "" else ""
                account = accounts[:1]
                account_id = account.id if account else None
                if not account:
                    _logger.error(
                        "%s inconsistencia: validación pasó pero no hay cuenta para user_id=%s",
                        _LOG,
                        uid,
                    )
                    return _ml_json_error("Internal validation error.", status=500)

                _logger.info(
                    "%s cuenta ml.account encontrada id=%s name=%s meli_user_id=%s process_webhook_sales=%s",
                    _LOG,
                    account.id,
                    account.name,
                    account.meli_user_id,
                    account.process_webhook_sales,
                )

                if account and not account.process_webhook_sales:
                    _logger.warning(
                        "%s causa=config cuenta id=%s (%s): process_webhook_sales=False — "
                        "activar en la ficha o el webhook no importa ventas",
                        _LOG,
                        account.id,
                        account.name,
                    )
                    return _ml_json_ok(
                        {
                            "message": "Webhook received but processing is disabled",
                        },
                        status=200,
                    )

                notification = request.env[
                    "ml.webhook.notification"
                ].sudo().create_notification(
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
                    notification.ml_account_id.id
                    if notification.ml_account_id
                    else False,
                )

            elif topic == "items":
                item_id = resource.split("/")[-1] if resource else None
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

            return _ml_json_ok(status=200)

        except Exception as e:
            _logger.error(
                "%s excepción no controlada causa=software: %s",
                _LOG,
                str(e),
                exc_info=True,
            )
            return request.make_response(
                json.dumps({"status": "error", "message": str(e)}),
                headers={"Content-Type": "application/json"},
                status=200,
            )

    @http.route("/ml/webhook/test", type="http", auth="user", methods=["GET", "POST"], csrf=False)
    def ml_webhook_test(self, **kwargs):
        """
        Endpoint de prueba: no público. Requiere usuario interno, grupo Administrador ML y
        parámetro de sistema mercadolibre_connector.webhook_test_enabled = True.
        """
        if not request.env.user.has_group("mercadolibre_connector.group_ml_admin"):
            return _ml_json_error("Forbidden: Mercado Libre administrator group required.", status=403)

        icp = request.env["ir.config_parameter"].sudo()
        if icp.get_param(ICP_WEBHOOK_TEST_ENABLED) != "True":
            return _ml_json_error(
                "Test endpoint disabled. Set system parameter "
                f"{ICP_WEBHOOK_TEST_ENABLED} to True (Technical settings).",
                status=403,
            )

        _logger.debug("Webhook ML test: method=%s user=%s", request.httprequest.method, request.env.user.login)
        return _ml_json_ok(
            {
                "message": "Webhook test endpoint is working",
                "method": request.httprequest.method,
                "timestamp": datetime.now().isoformat(),
            },
            status=200,
        )
