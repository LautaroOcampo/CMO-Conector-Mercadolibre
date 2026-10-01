# -*- coding: utf-8 -*-
import json
import logging
import threading

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ML scan: limit máx. 100; scroll_id expira ~5 min → listamos todo el catálogo primero.
ML_ITEMS_SCAN_PAGE_LIMIT = 100
# pg_advisory_lock de dos enteros: evita que el hilo del botón y el cron
# importen la misma cuenta a la vez.
ML_IMPORT_LOCK_NS = 87421001


class MLAccountImportBatch(models.Model):
    _inherit = 'ml.account'

    def _ml_publication_import_context(self):
        return {
            'ml_import_silent': True,
            'ml_import_download_images': self.import_download_images,
        }

    def _ml_import_api_headers(self):
        self.ensure_one()
        self._ensure_valid_token()
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }

    def _get_existing_ml_item_ids(self):
        self.ensure_one()
        pubs = self.env['ml.publication'].search([
            ('ml_account_id', '=', self.id),
            ('ml_item_id', '!=', False),
        ])
        return set(pubs.mapped('ml_item_id'))

    def _resolve_meli_user_id_for_import(self, headers=None):
        self.ensure_one()
        if self.meli_user_id:
            return str(self.meli_user_id)
        user_response = self._ml_request_with_retry(
            'GET',
            'https://api.mercadolibre.com/users/me',
            headers=headers,
            timeout=15,
        )
        if not user_response.ok:
            raise UserError(_('Error al obtener información del usuario: %s') % user_response.text)
        user_id = user_response.json().get('id')
        if not user_id:
            raise UserError(_('No se pudo obtener el ID del usuario desde MercadoLibre.'))
        return str(user_id)

    def _fetch_ml_item_ids_scan_page(self, user_id, headers, limit, scroll_id=None):
        """
        Una página de /users/{id}/items/search?search_type=scan (soporta >1000 ítems).
        Sin offset: se pagina con scroll_id.
        """
        items_url = f'https://api.mercadolibre.com/users/{user_id}/items/search'
        params = {
            'search_type': 'scan',
            'limit': min(max(int(limit or ML_ITEMS_SCAN_PAGE_LIMIT), 1), ML_ITEMS_SCAN_PAGE_LIMIT),
        }
        if scroll_id:
            params['scroll_id'] = scroll_id
        items_response = self._ml_request_with_retry(
            'GET',
            items_url,
            headers=headers,
            params=params,
            timeout=60,
        )
        if not items_response.ok:
            raise UserError(_('Error al obtener items de MercadoLibre: %s') % items_response.text)
        items_data = items_response.json() if items_response.text else {}
        if items_data is None:
            return {'item_ids': [], 'total': 0, 'scroll_id': False, 'last_page': True}
        paging = items_data.get('paging', {}) or {}
        item_ids = items_data.get('results') or []
        if not isinstance(item_ids, list):
            item_ids = []
        next_scroll = items_data.get('scroll_id') or False
        return {
            'item_ids': [str(i) for i in item_ids if i],
            'total': int(paging.get('total') or 0),
            'scroll_id': next_scroll,
            'last_page': not item_ids,
        }

    def _ml_scan_collect_all_item_ids(self, user_id, headers):
        """
        Recorre todo el catálogo con search_type=scan (rápido) y devuelve la lista de IDs.
        Debe completarse dentro de la ventana de ~5 min del scroll_id.
        """
        self.ensure_one()
        all_ids = []
        seen = set()
        scroll_id = None
        total = 0
        page_n = 0
        _logger.info(
            '📥 Scan ML [%s] id=%s: inicio user_id=%s',
            self.name, self.id, user_id,
        )
        while True:
            page_n += 1
            page = self._fetch_ml_item_ids_scan_page(
                user_id,
                headers,
                ML_ITEMS_SCAN_PAGE_LIMIT,
                scroll_id=scroll_id,
            )
            if page['total']:
                total = page['total']
            batch = page['item_ids']
            if not batch:
                _logger.info(
                    '📥 Scan ML [%s] user_id=%s: página %d sin ítems '
                    '(total_ml=%s tiene_scroll=%s). Fin del listado.',
                    self.name,
                    user_id,
                    page_n,
                    total,
                    bool(page.get('scroll_id')),
                )
                break
            for item_id in batch:
                if item_id not in seen:
                    seen.add(item_id)
                    all_ids.append(item_id)
            _logger.info(
                '📥 Scan ML [%s]: página %d — +%d (acum %d%s)',
                self.name,
                page_n,
                len(batch),
                len(all_ids),
                f'/{total}' if total else '',
            )
            scroll_id = page.get('scroll_id')
            if page['last_page'] or not scroll_id:
                break
            if total and len(all_ids) >= total:
                break
        return all_ids, total or len(all_ids)

    def _ml_import_queue_load(self):
        """Lista de ítems en cola (JSON)."""
        self.ensure_one()
        raw = (self.import_publications_item_ids or '').strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        return [str(i) for i in data if i]

    def _ml_import_queue_save(self, item_ids, total=None):
        self.ensure_one()
        self.write({
            'import_publications_item_ids': json.dumps(item_ids),
            'import_publications_total': int(total if total is not None else len(item_ids)),
            'import_publications_offset': 0,
        })

    def _ml_import_queue_clear(self):
        self.ensure_one()
        self.write({
            'import_publications_active': False,
            'import_publications_offset': 0,
            'import_publications_total': 0,
            'import_publications_item_ids': False,
        })

    def _import_single_ml_item(self, item_id, headers, skip_existing=False):
        """Importa un ítem ML (y sus variaciones). Retorna contadores del lote."""
        self.ensure_one()
        imported_count = 0
        updated_count = 0
        errors = []
        import_ctx = self._ml_publication_import_context()

        if skip_existing and item_id in self._get_existing_ml_item_ids():
            _logger.info(
                '📥 Importación ML [%s]: omitido ítem %s (ya en Odoo)',
                self.name,
                item_id,
            )
            return {
                'imported_count': 0,
                'updated_count': 0,
                'skipped': True,
                'errors': [],
            }

        item_response = self._ml_request_with_retry(
            'GET',
            f'https://api.mercadolibre.com/items/{item_id}',
            headers=headers,
            timeout=30,
        )
        if not item_response.ok:
            error_msg = _('Error obteniendo datos del item %s: %s') % (item_id, item_response.text)
            _logger.error('❌ %s', error_msg)
            return {
                'imported_count': 0,
                'updated_count': 0,
                'skipped': False,
                'errors': [error_msg],
            }

        item_data = item_response.json()
        variations = item_data.get('variations') or []
        item_title = item_data.get('title', 'Item')
        Publication = self.env['ml.publication']

        if variations:
            variations_by_id = {}
            try:
                var_url = f'https://api.mercadolibre.com/items/{item_id}/variations'
                var_response = self._ml_request_with_retry(
                    'GET',
                    var_url,
                    headers=headers,
                    timeout=30,
                )
                if var_response.ok:
                    var_data = var_response.json()
                    raw_list = (
                        var_data.get('variations')
                        if isinstance(var_data, dict)
                        else (var_data if isinstance(var_data, list) else [])
                    )
                    for v in (raw_list or []):
                        vid = str(v.get('id') or '')
                        if vid:
                            variations_by_id[vid] = dict(v)
                    for vid in list(variations_by_id.keys()):
                        try:
                            one_resp = self._ml_request_with_retry(
                                'GET',
                                f'https://api.mercadolibre.com/items/{item_id}/variations/{vid}',
                                headers=headers,
                                timeout=15,
                            )
                            if one_resp.ok:
                                one_var = one_resp.json()
                                if isinstance(one_var, dict):
                                    variations_by_id[vid].update(one_var)
                        except Exception as e:
                            _logger.debug('GET /items/%s/variations/%s: %s', item_id, vid, e)
            except Exception as e:
                _logger.warning('⚠️ Error obteniendo /items/%s/variations: %s', item_id, e)

            existing_by_var = {
                p.ml_variation_id: p
                for p in Publication.search([
                    ('ml_item_id', '=', item_id),
                    ('ml_account_id', '=', self.id),
                ])
                if p.ml_variation_id
            }
            for var in variations:
                var_id = str(var.get('id') or '')
                full_var = variations_by_id.get(var_id) or var
                combo = full_var.get('attribute_combinations') or var.get('attribute_combinations') or []
                parts = [
                    str(ac.get('value_name') or ac.get('value_id', '')).strip()
                    for ac in combo
                    if ac.get('value_name') or ac.get('value_id')
                ]
                variant_label = (
                    ', '.join(parts)
                    if parts
                    else (full_var.get('seller_custom_field') or '').strip()
                    or _('Variante %s') % var_id
                    or str(len(existing_by_var) + 1)
                )
                sku_var = self._ml_seller_sku_from_variation_payload(full_var)
                name_title = f'{item_title} | {variant_label}'
                pub_vals = {
                    'name': name_title,
                    'title': name_title,
                    'ml_item_id': item_id,
                    'ml_variation_id': var_id,
                    'ml_account_id': self.id,
                    'is_full': self._meli_item_data_is_full(item_data),
                    'ml_status': item_data.get('status', 'inactive'),
                    'ml_sub_status': Publication._ml_format_sub_status(item_data) or False,
                    'permalink': item_data.get('permalink', ''),
                    'current_price_ml': float(full_var.get('price') or var.get('price') or 0),
                    'current_stock_ml': int(full_var.get('available_quantity') or var.get('available_quantity') or 0),
                    'seller_sku': sku_var or '',
                    'category_id': item_data.get('category_id') or '',
                }
                existing = existing_by_var.get(var_id)
                try:
                    if existing:
                        existing.write(pub_vals)
                        existing.with_context(**import_ctx).action_import_from_ml()
                        updated_count += 1
                    else:
                        pub = Publication.create(pub_vals)
                        pub.with_context(**import_ctx).action_import_from_ml()
                        imported_count += 1
                except Exception as e:
                    errors.append(
                        _('Error importando variación %s del item %s: %s') % (variant_label, item_id, str(e))
                    )
        else:
            existing_publication = Publication.search([
                ('ml_item_id', '=', item_id),
                ('ml_account_id', '=', self.id),
            ], limit=1)
            if existing_publication:
                try:
                    if existing_publication.ml_account_id.id != self.id:
                        existing_publication.ml_account_id = self.id
                    existing_publication.with_context(**import_ctx).action_import_from_ml()
                    updated_count += 1
                except Exception as e:
                    errors.append(_('Error actualizando publicación %s: %s') % (item_id, str(e)))
            else:
                seller_sku = self._ml_seller_sku_from_item_payload(item_data)
                publication_vals = {
                    'name': item_title,
                    'title': item_title,
                    'ml_item_id': item_id,
                    'ml_account_id': self.id,
                    'is_full': self._meli_item_data_is_full(item_data),
                    'ml_status': item_data.get('status', 'inactive'),
                    'ml_sub_status': Publication._ml_format_sub_status(item_data) or False,
                    'permalink': item_data.get('permalink', ''),
                    'current_price_ml': item_data.get('price', 0.0),
                    'current_stock_ml': item_data.get('available_quantity', 0),
                    'seller_sku': seller_sku,
                }
                if item_data.get('category_id'):
                    publication_vals['category_id'] = item_data['category_id']
                try:
                    publication = Publication.create(publication_vals)
                    publication.with_context(**import_ctx).action_import_from_ml()
                    imported_count += 1
                except Exception as e:
                    errors.append(_('Error importando publicación %s: %s') % (item_id, str(e)))

        return {
            'imported_count': imported_count,
            'updated_count': updated_count,
            'skipped': False,
            'errors': errors,
        }

    def action_import_all_publications_from_ml(self):
        """Encola importación masiva en segundo plano (cron, commit por ítem)."""
        self.ensure_one()
        if not self.access_token:
            raise UserError(_(
                'La cuenta de MercadoLibre no tiene token de acceso configurado. '
                'Por favor, autorice la cuenta primero.'
            ))
        if self.import_publications_active:
            raise UserError(_(
                'Ya hay una importación en curso (offset %d). '
                'Espere a que termine o use «Detener importación».'
            ) % (self.import_publications_offset or 0))

        batch_size = self.import_publications_batch_size or 25
        if batch_size < 5 or batch_size > 50:
            raise UserError(_('Ítems por lote debe estar entre 5 y 50.'))

        self.write({
            'import_publications_active': True,
            'import_publications_offset': 0,
            'import_publications_total': 0,
            'import_publications_item_ids': False,
        })
        # El hilo abre otro cursor: tiene que ver la bandera ya confirmada.
        self.env.cr.commit()

        cron_xmlid = '%s.ir_cron_import_publications_ml' % self._module
        cron = self.env.ref(cron_xmlid, raise_if_not_found=False)
        _logger.info(
            '📥 Importación ML [%s] id=%s: ARRANQUE por botón. '
            'meli_user_id=%s batch=%s imágenes=%s cron=%s',
            self.name,
            self.id,
            self.meli_user_id or '(vacío, se resuelve con /users/me)',
            batch_size,
            bool(self.import_download_images),
            cron_xmlid if cron else 'NO ENCONTRADO %s' % cron_xmlid,
        )
        self._ml_import_spawn()
        if cron:
            cron._trigger()
            _logger.info(
                '📥 Importación ML [%s]: hilo lanzado y cron de respaldo id=%s name=%s',
                self.name, cron.id, cron.name,
            )
        else:
            _logger.warning(
                '📥 Importación ML [%s]: cron %s no existe. '
                'La importación sigue en el hilo de este proceso.',
                self.name, cron_xmlid,
            )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Importación iniciada'),
                'message': _(
                    'La importación arranca ahora en este servidor (~%(batch)d ítems por lote). '
                    'Primero lista el catálogo y después crea las publicaciones. '
                    'Progreso en logs y Publicaciones ML. Imágenes: %(images)s.'
                ) % {
                    'batch': batch_size,
                    'images': _('sí') if self.import_download_images else _('no (solo IDs ML)'),
                },
                'type': 'success',
                'sticky': True,
            },
        }

    def action_stop_import_publications(self):
        self.ensure_one()
        if not self.import_publications_active:
            raise UserError(_('No hay importación en curso.'))
        offset = self.import_publications_offset or 0
        _logger.info(
            '📥 Importación ML [%s] id=%s: DETENIDA por botón en offset %s / %s',
            self.name, self.id, offset, self.import_publications_total or 0,
        )
        self._ml_import_queue_clear()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Importación detenida'),
                'message': _(
                    'Se detuvo en offset %d. Los ítems ya importados permanecen en Odoo. '
                    'Puede reanudar con «Importar Todas las Publicaciones» (omitirá existentes).'
                ) % offset,
                'type': 'warning',
                'sticky': False,
            },
        }

    @api.model
    def cron_import_publications_batch(self):
        accounts = self.search([
            ('import_publications_active', '=', True),
        ])
        if accounts:
            _logger.info(
                '📥 Importación ML: cron tick. cuentas_activas=%s ids=%s',
                len(accounts),
                accounts.ids,
            )
        else:
            _logger.debug('📥 Importación ML: cron tick sin cuentas activas.')
        for account in accounts:
            if not account._ml_import_try_lock():
                _logger.info(
                    '📥 Importación ML [%s] id=%s: cron salta, '
                    'otro proceso ya está importando.',
                    account.name, account.id,
                )
                continue
            try:
                account._run_import_publications_batch()
            except Exception as e:
                _logger.exception(
                    '❌ Cron importación publicaciones ML falló (%s): %s',
                    account.display_name,
                    e,
                )
            finally:
                account._ml_import_unlock()

    def _ml_import_try_lock(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT pg_try_advisory_lock(%s, %s)',
            (ML_IMPORT_LOCK_NS, self.id),
        )
        return bool(self.env.cr.fetchone()[0])

    def _ml_import_unlock(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT pg_advisory_unlock(%s, %s)',
            (ML_IMPORT_LOCK_NS, self.id),
        )

    def _ml_import_spawn(self):
        """Corre todos los lotes en este proceso, sin esperar al worker de crons."""
        self.ensure_one()
        account_id = self.id
        dbname = self.env.cr.dbname
        registry = self.env.registry

        def _target():
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    account = env['ml.account'].browse(account_id)
                    if not account.exists():
                        return
                    account._ml_import_run_all_batches()
            except Exception:
                _logger.exception(
                    '📥 Importación ML id=%s db=%s: el hilo murió',
                    account_id, dbname,
                )

        threading.Thread(
            target=_target,
            name='ml-import-%s-%s' % (dbname, account_id),
            daemon=True,
        ).start()
        _logger.info(
            '📥 Importación ML [%s] id=%s: hilo ml-import-%s-%s iniciado',
            self.name, self.id, dbname, account_id,
        )

    def _ml_import_run_all_batches(self):
        """Lista el catálogo e importa lote tras lote hasta que la bandera se apaga."""
        self.ensure_one()
        if not self._ml_import_try_lock():
            _logger.info(
                '📥 Importación ML [%s] id=%s: hilo no entra, lock ocupado.',
                self.name, self.id,
            )
            return
        try:
            _logger.info(
                '📥 Importación ML [%s] id=%s: hilo en marcha (no depende del cron).',
                self.name, self.id,
            )
            while True:
                self.invalidate_recordset()
                if not self.exists() or not self.import_publications_active:
                    _logger.info(
                        '📥 Importación ML [%s] id=%s: hilo termina, bandera apagada.',
                        self.name, self.id,
                    )
                    break
                self._run_import_publications_batch()
                self.invalidate_recordset()
                if not self.import_publications_active:
                    break
        finally:
            self._ml_import_unlock()

    def _run_import_publications_batch(self):
        self.ensure_one()
        if not self.access_token:
            _logger.warning(
                '📥 Importación ML [%s] id=%s: sin access_token. Se cancela.',
                self.name, self.id,
            )
            self._ml_import_queue_clear()
            return

        headers = self._ml_import_api_headers()
        user_id = self._resolve_meli_user_id_for_import(headers)
        batch_size = max(5, min(self.import_publications_batch_size or 25, 50))
        existing_ids = self._get_existing_ml_item_ids()
        _logger.info(
            '📥 Importación ML [%s] id=%s: lote arranca. user_id=%s '
            'ya_en_odoo=%s batch=%s offset_guardado=%s',
            self.name,
            self.id,
            user_id,
            len(existing_ids),
            batch_size,
            self.import_publications_offset or 0,
        )

        queue = self._ml_import_queue_load()
        if not queue:
            _logger.info(
                '📥 Importación ML [%s] user_id=%s: listando catálogo '
                '(search_type=scan, sin filtro de estado)…',
                self.name,
                user_id,
            )
            queue, total = self._ml_scan_collect_all_item_ids(user_id, headers)
            if not queue:
                self._ml_import_queue_clear()
                _logger.info(
                    '📥 Importación ML [%s] user_id=%s: catálogo vacío. '
                    'ML no devolvió ítems. Se apaga la importación.',
                    self.name,
                    user_id,
                )
                return
            self._ml_import_queue_save(queue, total=total)
            self.env.cr.commit()
            _logger.info(
                '📥 Importación ML [%s]: cola lista con %d ítems. Iniciando lotes…',
                self.name,
                len(queue),
            )
            offset = 0
        else:
            offset = self.import_publications_offset or 0

        total = len(queue) or self.import_publications_total or 0
        if total and not self.import_publications_total:
            self.import_publications_total = total

        if offset >= len(queue):
            self._ml_import_queue_clear()
            _logger.info('📥 Importación ML [%s]: catálogo completado.', self.name)
            return

        item_ids = queue[offset:offset + batch_size]
        if not item_ids:
            self._ml_import_queue_clear()
            _logger.info('📥 Importación ML [%s]: catálogo completado.', self.name)
            return

        imported_batch = 0
        updated_batch = 0
        skipped_batch = 0
        error_count = 0

        for index, item_id in enumerate(item_ids, start=1):
            if item_id in existing_ids:
                skipped_batch += 1
                continue
            pct = ((offset + index) * 100.0 / total) if total else 0
            _logger.info(
                '📥 Importación ML [%s]: %.1f%% (offset %d, ítem %d/%d del lote) — %s',
                self.name,
                pct,
                offset,
                index,
                len(item_ids),
                item_id,
            )
            try:
                result = self._import_single_ml_item(
                    item_id,
                    headers,
                    skip_existing=False,
                )
                imported_batch += result.get('imported_count', 0)
                updated_batch += result.get('updated_count', 0)
                if result.get('skipped'):
                    skipped_batch += 1
                error_count += len(result.get('errors') or [])
                for err in result.get('errors') or []:
                    _logger.error('❌ %s', err)
                self.env.cr.commit()
                existing_ids.add(item_id)
            except Exception as e:
                error_count += 1
                _logger.exception('❌ Error importando ítem %s: %s', item_id, e)
                self.env.cr.rollback()

        new_offset = offset + len(item_ids)
        is_last = new_offset >= len(queue)
        _logger.info(
            '📥 Importación ML [%s] lote offset %d: +%d importadas, +%d actualizadas, '
            '%d omitidas, %d errores (nuevo offset %d / %d)',
            self.name,
            offset,
            imported_batch,
            updated_batch,
            skipped_batch,
            error_count,
            new_offset,
            len(queue),
        )

        if is_last:
            self._ml_import_queue_clear()
            _logger.info('📥 Importación ML [%s]: catálogo completado.', self.name)
        else:
            self.write({'import_publications_offset': new_offset})

    def _ml_discover_new_ml_item_ids(self, user_id, headers, existing_ids):
        """
        Scan completo del catálogo ML y diff contra ml_item_id ya presentes en Odoo.
        Retorna lista ordenada de MLA nuevos (solo IDs, sin importar aún).
        """
        self.ensure_one()
        scan_ids, total = self._ml_scan_collect_all_item_ids(user_id, headers)
        new_ids = [item_id for item_id in scan_ids if item_id not in existing_ids]
        _logger.info(
            '🔍 Scan novedades ML [%s]: %d ítems en ML, %d ya en Odoo, %d nuevos%s',
            self.name,
            len(scan_ids),
            len(existing_ids),
            len(new_ids),
            f' (total ML reportado: {total})' if total else '',
        )
        return new_ids

    def _run_scan_new_publications_daily(self):
        """
        Cron diario: scan liviano de todo el catálogo + importación de MLA nuevos solamente.
        Reutiliza _import_single_ml_item (importa todos los ítems nuevos).
        """
        self.ensure_one()
        if not self.access_token:
            _logger.info(
                '🔍 Scan novedades ML [%s]: omitido (sin access_token)',
                self.name,
            )
            return
        if self.import_publications_active:
            _logger.info(
                '🔍 Scan novedades ML [%s]: omitido (importación masiva en curso)',
                self.name,
            )
            return
        if not self.scan_new_publications_enabled:
            return

        import_limit = max(1, min(int(self.scan_new_publications_import_limit or 50), 200))
        headers = self._ml_import_api_headers()
        user_id = self._resolve_meli_user_id_for_import(headers)
        existing_ids = self._get_existing_ml_item_ids()

        new_ids = self._ml_discover_new_ml_item_ids(user_id, headers, existing_ids)
        imported_total = 0
        skipped_total = 0
        error_total = 0

        if new_ids:
            batch = new_ids[:import_limit]
            if len(new_ids) > import_limit:
                _logger.info(
                    '🔍 Scan novedades ML [%s]: importando %d de %d nuevos hoy (límite diario %d)',
                    self.name,
                    len(batch),
                    len(new_ids),
                    import_limit,
                )
            for index, item_id in enumerate(batch, start=1):
                _logger.info(
                    '🔍 Scan novedades ML [%s]: nuevo %d/%d — %s',
                    self.name,
                    index,
                    len(batch),
                    item_id,
                )
                try:
                    result = self._import_single_ml_item(
                        item_id,
                        headers,
                        skip_existing=True,
                    )
                    imported_total += result.get('imported_count', 0)
                    if result.get('skipped'):
                        skipped_total += 1
                    error_total += len(result.get('errors') or [])
                    for err in result.get('errors') or []:
                        _logger.error('❌ Scan novedades: %s', err)
                    self.env.cr.commit()
                    existing_ids.add(item_id)
                except Exception as e:
                    error_total += 1
                    _logger.exception(
                        '❌ Scan novedades ML [%s]: error importando %s: %s',
                        self.name,
                        item_id,
                        e,
                    )
                    self.env.cr.rollback()

        self.write({
            'scan_new_publications_last_run': fields.Datetime.now(),
            'scan_new_publications_last_imported': imported_total,
        })
        _logger.info(
            '🔍 Scan novedades ML [%s]: fin — %d nuevos detectados, %d publicaciones creadas, '
            '%d omitidos (SKU/existente), %d errores',
            self.name,
            len(new_ids),
            imported_total,
            skipped_total,
            error_total,
        )

    @api.model
    def cron_scan_new_ml_publications(self):
        """Cron diario: detecta e importa publicaciones ML que aún no están en Odoo."""
        accounts = self.search([
            ('access_token', '!=', False),
            ('scan_new_publications_enabled', '=', True),
            ('import_publications_active', '=', False),
        ])
        for account in accounts:
            try:
                account._run_scan_new_publications_daily()
            except Exception as e:
                _logger.exception(
                    '❌ Cron scan novedades ML falló (%s): %s',
                    account.display_name,
                    e,
                )
