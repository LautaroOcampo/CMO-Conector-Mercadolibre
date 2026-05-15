# -*- coding: utf-8 -*-
from odoo import models, fields, api, SUPERUSER_ID
import logging

try:
    from odoo.addons.queue_job.job import job
except ImportError:
    def job(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

_logger = logging.getLogger(__name__)

_LOG = "[ML WEBHOOK][NOTIF]"
_LOG_JOB = "[ML WEBHOOK][JOB]"


class MLWebhookNotification(models.Model):
    _name = 'ml.webhook.notification'
    _description = 'Notificación de Webhook MercadoLibre'
    _rec_name = 'order_id'
    _order = 'date_received desc'

    order_id = fields.Char(
        string='ID Orden ML',
        required=True,
        index=True,
        help='ID de la orden en MercadoLibre (extraído del resource)',
    )
    topic = fields.Char(
        string='Topic',
        required=True,
        index=True,
    )
    resource = fields.Char(string='Resource', index=True)
    user_id = fields.Char(string='User ID ML', index=True, help='meli_user_id del payload')
    status = fields.Selection(
        [
            ('pending', 'Pendiente'),
            ('processed', 'Procesada'),
            ('failed', 'Fallida'),
        ],
        string='Estado',
        required=True,
        default='pending',
        index=True,
    )
    raw_data = fields.Text(string='Datos raw')
    date_received = fields.Datetime(
        string='Fecha recepción',
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    ml_account_id = fields.Many2one(
        'ml.account',
        string='Cuenta ML',
        ondelete='set null',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )

    @api.model
    def create_notification(self, order_id, topic, resource=None, user_id=None, raw_data=None, ml_account_id=None):
        """
        Crea una notificación de webhook.
        company_id se toma de la cuenta si tiene company_id; si no, de env.company.
        """
        company_id = self.env.company.id
        vals = {
            'order_id': order_id,
            'topic': topic,
            'resource': resource or '',
            'user_id': user_id or '',
            'raw_data': raw_data,
            'ml_account_id': ml_account_id,
            'company_id': company_id,
            'status': 'pending',
        }
        notification = self.create(vals)
        _logger.info(
            "%s registro creado id=%s order_id=%s topic=%s company_id=%s ml_account_id=%s "
            "(si ml_account_id=False → job no importará: causa=config meli_user_id)",
            _LOG,
            notification.id,
            order_id,
            topic,
            company_id,
            ml_account_id or False,
        )
        return notification

    def _sudo_env_for_company(self, company):
        """Entorno superusuario acotado a la compañía de la notificación.

        Compatibilidad:
        - Odoo 15+: ``with_company`` + ``sudo`` en Environment.
        - Odoo 14: a veces sin ``with_company``; contexto multi-compañía + ``sudo``.
        - Más antiguo: sin ``env.sudo()``; se usa ``api.Environment(cr, SUPERUSER_ID, ctx)``.
        """
        self.ensure_one()
        env = self.env
        co_id = company.id
        ctx = dict(env.context)
        ctx["allowed_company_ids"] = [co_id]
        ctx["force_company"] = co_id

        if hasattr(env, "with_company"):
            e = env.with_company(company)
            if hasattr(e, "sudo"):
                return e.sudo()
            return api.Environment(env.cr, SUPERUSER_ID, ctx)

        if hasattr(env, "sudo"):
            return env.sudo().with_context(ctx)

        return api.Environment(env.cr, SUPERUSER_ID, ctx)

    @api.model
    def mark_as_processed_for_order(self, order_id, company_id):
        """Marca como procesadas las notificaciones pendientes para esta orden y compañía."""
        if not order_id or not company_id:
            return
        notifications = self.search([
            ('order_id', '=', str(order_id)),
            ('company_id', '=', company_id),
            ('status', '=', 'pending'),
        ])
        if notifications:
            notifications.write({'status': 'processed'})
            _logger.debug(
                "📬 %d notificación(es) ML marcadas como procesadas para orden %s",
                len(notifications), order_id,
            )

    def enqueue_process(self):
        """Procesa (o encola) cada notificación.

        - Si queue_job está instalado (with_delay disponible): se encola el job.
        - Si no: se procesa de forma síncrona en esta misma transacción.
        """
        for notification in self:
            if notification.status == 'processed':
                _logger.debug("%s id=%s ya procesada, skip", _LOG, notification.id)
                continue
            uses_queue = hasattr(notification, "with_delay")
            if uses_queue:
                _logger.info(
                    "%s encolando con queue_job id=%s order_id=%s — "
                    "si la venta nunca aparece: revisar worker/cola (causa=config servidor)",
                    _LOG,
                    notification.id,
                    notification.order_id,
                )
                notification.with_delay().job_process_webhook_notification()
            else:
                _logger.info(
                    "%s sin queue_job: procesamiento síncrono inmediato id=%s order_id=%s "
                    "(causa=software: módulo queue_job no instalado)",
                    _LOG,
                    notification.id,
                    notification.order_id,
                )
                notification.job_process_webhook_notification()

    @job(default_channel='root', retry_pattern={1: 60, 2: 300, 3: 900})
    def job_process_webhook_notification(self):
        """
        Job que procesa una notificación: obtiene orden de la API y crea/actualiza ml.sale.
        Idempotente (ml_order_id + company_id). Usa company_id de la notificación.
        """
        self.ensure_one()
        if self.status == 'processed':
            _logger.info(
                "%s notificación id=%s ya estaba procesada order_id=%s",
                _LOG_JOB,
                self.id,
                self.order_id,
            )
            return

        account = self.ml_account_id
        if not account or not account.exists():
            _logger.warning(
                "%s causa=config notificación id=%s order_id=%s sin ml.account en la notificación — "
                "el HTTP no enlazó user_id con meli_user_id en Odoo",
                _LOG_JOB,
                self.id,
                self.order_id,
            )
            return
        if not account.access_token:
            _logger.warning(
                "%s causa=config cuenta id=%s (%s) sin access_token — reconectar Mercado Libre",
                _LOG_JOB,
                account.id,
                account.name,
            )
            return

        company = self.company_id
        env = self._sudo_env_for_company(company)
        _logger.info(
            "%s llamando API ML order_id=%s ml.account=%s company_id=%s is_connected=%s",
            _LOG_JOB,
            self.order_id,
            account.id,
            company.id,
            getattr(account, "is_connected", None),
        )

        sale_env = env['ml.sale']
        try:
            result = sale_env.update_or_create_from_meli(
                self.order_id,
                account_id=account.id,
                create_odoo_order=True,
                update_stock=True,
                create_customer=True,
            )
        except Exception as ex:
            _logger.exception(
                "%s excepción en update_or_create_from_meli causa=software order_id=%s: %s",
                _LOG_JOB,
                self.order_id,
                ex,
            )
            return

        if result:
            env['ml.webhook.notification'].mark_as_processed_for_order(
                self.order_id,
                company.id,
            )
            self.write({'status': 'processed'})
            _logger.info(
                "%s OK ml.sale id=%s ml_order_id=%s (venta importada)",
                _LOG_JOB,
                result.id,
                self.order_id,
            )
        else:
            _logger.warning(
                "%s causa=api o config update_or_create_from_meli devolvió None order_id=%s "
                "(token inválido, orden de otro vendedor, o error API; ver logs ml.sale)",
                _LOG_JOB,
                self.order_id,
            )
