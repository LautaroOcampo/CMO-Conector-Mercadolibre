# -*- coding: utf-8 -*-
import hashlib
import logging

import requests
from markupsafe import Markup, escape

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import UserError

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


def _ml_webhook_order_lock_key(company_id, order_id):
    """Clave estable para pg_advisory_xact_lock (orden + compañía).

    Misma clave que ``ml.sale._ml_advisory_lock_order`` para serializar
    webhook + polling + import sobre la misma orden.
    """
    raw = 'ml.order:%s:%s' % (company_id or 0, str(order_id or '').strip())
    return int(hashlib.md5(raw.encode('utf-8')).hexdigest()[:15], 16)


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
    retry_count = fields.Integer(
        string='Reintentos fallidos',
        default=0,
        readonly=True,
        help='Solo cuenta errores de procesamiento en Odoo; no incluye timeouts, 429 ni errores 5xx de ML.',
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

    @staticmethod
    def _is_transient_meli_request_error(exc):
        """Errores que debe reintentar queue_job sin consumir retry_count."""
        if isinstance(exc, requests.exceptions.Timeout):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = getattr(exc, 'response', None)
            if resp is not None:
                code = resp.status_code
                if code == 429:
                    return True
                if 500 <= code <= 599:
                    return True
        return False

    def _notify_failure_to_responsible(self, error_message):
        """Actividad de aviso en ml.account (sin email). Evita duplicados por orden."""
        self.ensure_one()
        account = self.ml_account_id
        if not account or not account.exists():
            return
        activity_type = self.env.ref('mail.mail_activity_data_warning', raise_if_not_found=False)
        if not activity_type:
            _logger.warning('%s mail.mail_activity_data_warning no disponible', _LOG_JOB)
            return
        assignee = account.create_uid
        if not assignee:
            assignee = self.env.user

        summary = _('Venta #%s no se pudo importar') % self.order_id
        Activity = self.env['mail.activity'].sudo()
        duplicate = Activity.search([
            ('res_model', '=', 'ml.account'),
            ('res_id', '=', account.id),
            ('activity_type_id', '=', activity_type.id),
            ('summary', '=', summary),
        ], limit=1)
        if duplicate:
            return

        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        link = f"{base}/web#id={self.id}&model=ml.webhook.notification&view_type=form"
        note_lines = [
            _('Error: %s') % (error_message or ''),
            _('Topic: %s') % (self.topic or ''),
            _('Resource: %s') % (self.resource or ''),
            _('Notificación: %s') % link,
        ]
        body = '\n'.join(note_lines)
        note_html = Markup('<p style="white-space:pre-wrap;">{}</p>').format(escape(body))

        Activity.create({
            'activity_type_id': activity_type.id,
            'summary': summary,
            'note': note_html,
            'res_model': 'ml.account',
            'res_id': account.id,
            'user_id': assignee.id,
            'date_deadline': fields.Date.context_today(self),
            'company_id': self.company_id.id,
        })
        _logger.info(
            '%s actividad de fallo webhook creada para user_id=%s ml.account=%s order_id=%s',
            _LOG_JOB,
            assignee.id,
            account.id,
            self.order_id,
        )

    def _mark_failed_processing(self, error_message):
        """Incrementa retry_count, marca failed y notifica al 3.er fallo de procesamiento."""
        self.ensure_one()
        new_retry = (self.retry_count or 0) + 1
        if new_retry >= 3:
            self._notify_failure_to_responsible(error_message)
        self.write({
            'status': 'failed',
            'retry_count': new_retry,
        })

    def action_retry_webhook_notification(self):
        """Vuelve a pendiente y encola procesamiento (solo desde estado fallida)."""
        failed = self.filtered(lambda n: n.status == 'failed')
        other = self - failed
        if other:
            raise UserError(_('Solo se pueden reintentar notificaciones en estado Fallida.'))
        failed.write({'status': 'pending'})
        failed.with_context(webhook_bypass_go_live_cutoff=True).enqueue_process()
        return True

    def _ml_advisory_lock_order(self, order_id=None, company_id=None):
        """Serializa el procesamiento de webhooks de la misma orden en la misma compañía."""
        self.ensure_one()
        order_id = str(order_id or self.order_id or '').strip()
        company_id = company_id or self.company_id.id
        if not order_id or not company_id:
            return
        key = _ml_webhook_order_lock_key(company_id, order_id)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
        _logger.debug(
            "%s advisory lock order_id=%s company_id=%s key=%s",
            _LOG_JOB, order_id, company_id, key,
        )

    @api.model
    def mark_as_processed_for_order(self, order_id, company_id, statuses=('pending', 'failed'), max_id=None):
        """Marca como procesadas las notificaciones hermanas (no más nuevas que max_id)."""
        if not order_id or not company_id:
            return
        domain = [
            ('order_id', '=', str(order_id)),
            ('company_id', '=', company_id),
            ('status', 'in', list(statuses)),
        ]
        if max_id:
            domain.append(('id', '<=', max_id))
        notifications = self.search(domain)
        if notifications:
            notifications.write({'status': 'processed'})
            _logger.info(
                "📬 %d notificación(es) ML marcadas como procesadas para orden %s (max_id=%s)",
                len(notifications), order_id, max_id,
            )

    def enqueue_process(self):
        """Procesa notificaciones serializadas por order_id (lock advisory).

        Síncrono para no depender de workers de queue_job. Varias notificaciones
        del mismo order_id en milisegundos se serializan: una importa, el resto
        queda coalescida o reintenta con el estado ya actualizado.
        """
        # Una sola pasada por (company, order): la de mayor id (más reciente).
        by_key = {}
        for notification in self:
            if notification.status == 'processed':
                continue
            key = (notification.company_id.id, str(notification.order_id or ''))
            prev = by_key.get(key)
            if not prev or notification.id > prev.id:
                by_key[key] = notification

        for notification in by_key.values():
            _logger.info(
                "%s procesamiento síncrono id=%s order_id=%s",
                _LOG,
                notification.id,
                notification.order_id,
            )
            try:
                notification.job_process_webhook_notification()
            except Exception:
                _logger.exception(
                    "%s fallo procesando notificación id=%s order_id=%s",
                    _LOG,
                    notification.id,
                    notification.order_id,
                )

    @api.model
    def cron_process_pending_webhook_notifications(self):
        """Respaldo: una notificación por order_id (la más reciente) para evitar carreras."""
        self.env.cr.execute("""
            SELECT DISTINCT ON (company_id, order_id) id
            FROM ml_webhook_notification
            WHERE status = 'pending'
            ORDER BY company_id, order_id, id DESC
            LIMIT 40
        """)
        ids = [row[0] for row in self.env.cr.fetchall()]
        if not ids:
            return True
        pending = self.browse(ids)
        _logger.info(
            "%s cron: %d orden(es) con notificación pending a procesar",
            _LOG,
            len(pending),
        )
        for notification in pending:
            try:
                notification.job_process_webhook_notification()
            except Exception:
                _logger.exception(
                    "%s cron: fallo id=%s order_id=%s",
                    _LOG,
                    notification.id,
                    notification.order_id,
                )
        return True

    @job(default_channel='root', retry_pattern={1: 60, 2: 300, 3: 900})
    def job_process_webhook_notification(self):
        """
        Procesa una notificación: API ML → create/update ml.sale.

        Serializado por (company_id, order_id) con advisory lock. Idempotente.
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

        # Serializa concurrentes del mismo order_id (packs / ráfagas de webhooks).
        self._ml_advisory_lock_order()
        self.invalidate_recordset(['status'])
        if self.status == 'processed':
            _logger.info(
                "%s notificación id=%s ya procesada tras lock order_id=%s",
                _LOG_JOB,
                self.id,
                self.order_id,
            )
            return

        # Coalesce: si hay una pending más nueva, dejar que esa importe el estado final.
        newer = self.search([
            ('order_id', '=', self.order_id),
            ('company_id', '=', self.company_id.id),
            ('status', '=', 'pending'),
            ('id', '>', self.id),
        ], limit=1)
        if newer:
            self.write({'status': 'processed'})
            _logger.info(
                "%s id=%s coalescida: hay pending más nueva id=%s order_id=%s",
                _LOG_JOB, self.id, newer.id, self.order_id,
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

        # No cortar solo por access_token vacío: puede renovarse con refresh_token.
        # (Antes el job devolvía acá y nunca llamaba al refresh lazy.)
        account = account.sudo()
        if not account._ensure_valid_token():
            _logger.warning(
                "%s causa=config cuenta id=%s (%s) sin token válido "
                "(access_token/refresh fallidos) — reconectar Mercado Libre",
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
        apply_go_live_cutoff = not self.env.context.get('webhook_bypass_go_live_cutoff')
        import_opts = account._ml_sale_import_options()
        try:
            result = sale_env.with_context(
                from_ml_webhook=apply_go_live_cutoff,
            ).update_or_create_from_meli(
                self.order_id,
                account_id=account.id,
                create_odoo_order=import_opts['create_odoo_order'],
                update_stock=import_opts['update_stock'],
                create_customer=import_opts['create_customer'],
            )
        except Exception as ex:
            _logger.exception(
                "%s excepción en update_or_create_from_meli causa=software order_id=%s: %s",
                _LOG_JOB,
                self.order_id,
                ex,
            )
            if self._is_transient_meli_request_error(ex):
                _logger.warning(
                    '%s error transitorio (timeout/429/5xx), reintento queue_job sin incrementar retry_count: %s',
                    _LOG_JOB,
                    ex,
                )
                raise
            self._mark_failed_processing(str(ex))
            account.record_webhook_health(
                'error',
                f'Orden {self.order_id}: {str(ex)[:200]}',
            )
            return

        if result is None:
            msg = (
                'update_or_create_from_meli devolvió None (token inválido, orden de otro vendedor, '
                'o error API; ver logs ml.sale)'
            )
            _logger.warning(
                "%s causa=api o config %s order_id=%s",
                _LOG_JOB,
                msg,
                self.order_id,
            )
            self._mark_failed_processing(msg)
            account.record_webhook_health('error', f'Orden {self.order_id}: {msg[:200]}')
            return

        # Hermanas pending/failed del mismo order con id <= esta → processed.
        # Las más nuevas quedan pending (pueden traer cancelación / estado final).
        env['ml.webhook.notification'].mark_as_processed_for_order(
            self.order_id,
            company.id,
            max_id=self.id,
        )
        if self.status != 'processed':
            self.write({'status': 'processed'})

        # Si llegó otra pending durante el import, procesarla en esta misma tx (mismo lock).
        newer = self.search([
            ('order_id', '=', self.order_id),
            ('company_id', '=', company.id),
            ('status', '=', 'pending'),
            ('id', '>', self.id),
        ], order='id desc', limit=1)
        if newer:
            _logger.info(
                "%s order_id=%s: pendiente más nueva id=%s tras import — reencolando",
                _LOG_JOB, self.order_id, newer.id,
            )
            newer.job_process_webhook_notification()
            return

        if not result:
            account.record_webhook_health(
                'ignored',
                f'Orden {self.order_id} omitida (go-live o aún no pagada)',
            )
            _logger.info(
                "%s orden %s omitida (go-live webhook_active_since=%s, o status distinto de paid)",
                _LOG_JOB,
                self.order_id,
                account.webhook_active_since,
            )
            return

        account.record_webhook_health(
            'ok',
            f'Orden {self.order_id} importada (ml.sale id={result.id})',
        )
        _logger.info(
            "%s OK ml.sale id=%s ml_order_id=%s (venta importada)",
            _LOG_JOB,
            result.id,
            self.order_id,
        )
