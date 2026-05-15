# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    """Extensión de stock.quant para sincronizar stock automáticamente con MercadoLibre"""
    _inherit = 'stock.quant'

    @api.model
    def _ml_get_auto_sync_accounts(self):
        """Obtiene cuentas ML con auto-sync de stock habilitado."""
        return self.env['ml.account'].search([('auto_sync_stock_on_odoo_change', '=', True)])

    @api.model
    def _ml_to_id(self, value):
        """
        Normaliza IDs que en algunas versiones llegan como recordset.
        Ej: product_id puede venir como product.product(76,) en vez de 76.
        """
        if not value:
            return False
        # recordset
        if hasattr(value, 'id'):
            return value.id or False
        # lista/tupla
        if isinstance(value, (list, tuple)):
            return self._ml_to_id(value[0]) if value else False
        # string numérico
        if isinstance(value, str) and value.isdigit():
            return int(value)
        # int
        if isinstance(value, int):
            return value
        return False

    @api.model
    def _ml_trigger_sync_for_product_location(self, product_id, location_id=None, accounts=None, reason=None):
        """
        Dispara la sincronización ML para un producto/ubicación.
        Se usa desde write/create y también desde hooks internos (ventas/validaciones).
        """
        # Normalizar tipos (recordset -> id)
        product_id_int = self._ml_to_id(product_id)
        location_id_int = self._ml_to_id(location_id)

        if not product_id_int:
            return
        if self.env.context.get('ml_skip_stock_sync'):
            return

        accounts = accounts or self._ml_get_auto_sync_accounts()
        if not accounts:
            _logger.info(
                "ℹ️ Trigger sync ML ignorado (reason=%s): no hay cuentas con auto-sync activado (product_id=%s, location_id=%s)",
                reason or 'N/A', product_id_int, location_id_int or 'N/A'
            )
            return

        try:
            # Evitar display_name (puede disparar computes y queries) y usar name/id simple
            product = self.env['product.product'].browse(product_id_int)
            product_name = product.name if product and product.exists() else 'N/A'
            location = self.env['stock.location'].browse(location_id_int) if location_id_int else self.env['stock.location']
            location_name = location.complete_name if location_id_int and location and location.exists() else 'N/A'
            _logger.info(
                "🔁 Trigger sync ML (reason=%s): producto=%s (ID=%s) | ubicación=%s (ID=%s) | cuentas=%d",
                reason or 'N/A',
                product_name,
                product_id_int,
                location_name if location_id_int else 'N/A',
                location_id_int or 'N/A',
                len(accounts),
            )
            # Evitar loops/reentradas por escrituras internas durante la sync
            self.with_context(ml_skip_stock_sync=True)._sync_stock_to_mercadolibre(product_id_int, location_id_int, accounts)
        except Exception as e:
            _logger.exception("❌ Error en trigger de sync ML (reason=%s): %s", reason or 'N/A', e)

    def write(self, vals):
        """
        Sobrescribir write para sincronizar stock automáticamente a MercadoLibre cuando cambia en Odoo.
        """
        # Guardar info previa (write puede venir con múltiples quants)
        prev_info = []
        if 'quantity' in vals:
            for rec in self:
                prev_info.append((rec.product_id.id, rec.location_id.id, rec.quantity or 0))

        result = super(StockQuant, self).write(vals)
        
        # Verificar si se modificó la cantidad (stock)
        if 'quantity' in vals:
            # Log por cada quant afectado (sin spamear demasiado)
            for rec in self:
                old_qty = None
                for p_id, l_id, q_old in prev_info:
                    if p_id == rec.product_id.id and l_id == rec.location_id.id:
                        old_qty = q_old
                        break
                _logger.info(
                    "📦 Cambio de stock detectado: producto_id=%s, ubicación_id=%s, cantidad_anterior=%s, cantidad_nueva=%s",
                    rec.product_id.id if rec.product_id else 'N/A',
                    rec.location_id.id if rec.location_id else 'N/A',
                    old_qty if old_qty is not None else 'N/A',
                    rec.quantity if rec.quantity is not None else 'N/A'
                )
            
            # Verificar si hay alguna cuenta con sincronización automática activada
            accounts = self._ml_get_auto_sync_accounts()
            
            if accounts:
                # Sincronizar SOLO los productos/ubicaciones impactados
                seen = set()
                for rec in self:
                    if not rec.product_id:
                        continue
                    key = (rec.product_id.id, rec.location_id.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    self._ml_trigger_sync_for_product_location(
                        rec.product_id.id,
                        rec.location_id.id,
                        accounts=accounts,
                        reason='quant.write(quantity)',
                    )
        
        return result
    
    @api.model
    def create(self, vals_list):
        """
        Sobrescribir create para sincronizar stock automáticamente a MercadoLibre cuando se crea en Odoo.
        """
        result = super(StockQuant, self).create(vals_list)
        
        # Verificar si hay alguna cuenta con sincronización automática activada
        accounts = self._ml_get_auto_sync_accounts()
        
        if accounts:
            # Para cada registro creado, sincronizar si tiene cantidad
            for record in result:
                # Importante: también sincronizar si quantity queda en 0 (puede disparar regla de pausa)
                if record.product_id and record.quantity is not None:
                    self._ml_trigger_sync_for_product_location(
                        record.product_id.id,
                        record.location_id.id,
                        accounts=accounts,
                        reason='quant.create',
                    )
        
        return result

    # =====================================================
    # Hooks internos de Odoo (ventas/validaciones/reservas)
    # =====================================================
    @api.model
    def _update_available_quantity(self, product_id, location_id, quantity=0.0, *args, **kwargs):
        """
        Hook más confiable: Odoo lo usa cuando realmente impacta stock disponible (picking done, ajustes, etc.).
        Esto cubre casos donde no pasa por write() directo en quants.
        """
        _logger.info(
            "📌 Odoo _update_available_quantity: product_id=%s location_id=%s delta=%s kwargs=%s",
            product_id, location_id, quantity, {k: kwargs.get(k) for k in ('reserved_quantity', 'lot_id', 'package_id', 'owner_id', 'in_date') if k in kwargs}
        )
        # IMPORTANTE: Odoo cambia la firma entre versiones (ej: reserved_quantity). Aceptar kwargs evita romper el flujo.
        res = super()._update_available_quantity(product_id, location_id, quantity, *args, **kwargs)
        try:
            self._ml_trigger_sync_for_product_location(product_id, location_id, reason='quant._update_available_quantity')
        except Exception:
            # ya loguea internamente
            pass
        return res

    @api.model
    def _update_reserved_quantity(self, product_id, location_id, quantity=0.0, *args, **kwargs):
        """
        Hook para reservas (afecta virtual_available / expected). Útil si la cuenta usa stock esperado.
        """
        _logger.info(
            "📌 Odoo _update_reserved_quantity: product_id=%s location_id=%s delta=%s kwargs=%s",
            product_id, location_id, quantity, {k: kwargs.get(k) for k in ('strict', 'lot_id', 'package_id', 'owner_id') if k in kwargs}
        )
        res = super()._update_reserved_quantity(product_id, location_id, quantity, *args, **kwargs)
        try:
            self._ml_trigger_sync_for_product_location(product_id, location_id, reason='quant._update_reserved_quantity')
        except Exception:
            pass
        return res
    
    @api.model
    def _sync_stock_to_mercadolibre(self, product_id, location_id=None, accounts=None):
        """
        Sincroniza el stock de un producto a MercadoLibre cuando cambia en Odoo.
        
        Args:
            product_id: int - ID del producto cuyo stock cambió
            location_id: int - ID de la ubicación donde cambió el stock (opcional)
            accounts: recordset - Cuentas de MercadoLibre con sincronización activada (opcional)
        """
        try:
            # Obtener el producto
            product = self.env['product.product'].browse(product_id)
            if not product.exists():
                _logger.warning("⚠️ Producto ID %s no existe", product_id)
                return
            
            location = None
            if location_id:
                location = self.env['stock.location'].browse(location_id)
                if not location.exists():
                    location = None
            
            # Si no se pasaron cuentas, buscarlas
            if not accounts:
                accounts = self.env['ml.account'].search([
                    ('auto_sync_stock_on_odoo_change', '=', True)
                ])
            
            if not accounts:
                _logger.info("ℹ️ Sincronización automática de stock desde Odoo desactivada. No se sincroniza.")
                return
            
            _logger.info("=" * 80)
            _logger.info("🔄 INICIO: Sincronización automática de stock a MercadoLibre")
            _logger.info("📦 Producto: %s (ID: %s)", product.name, product.id)
            _logger.info("📍 Ubicación: %s (ID: %s)", location.display_name if location else 'N/A', location.id if location else 'N/A')
            
            # Obtener el template del producto
            product_template = product.product_tmpl_id
            
            # Para cada cuenta con sincronización activada
            for account in accounts:
                try:
                    _logger.info("👤 Cuenta ML: %s (ID: %s) | warehouse=%s", account.name, account.id, account.warehouse_id.name if account.warehouse_id else 'N/A')
                    # Verificar si hay warehouse configurado
                    stock_location = None
                    if account.warehouse_id:
                        stock_location = account.warehouse_id.lot_stock_id
                        _logger.info("🏭 Usando almacén configurado: %s (ID: %s)", account.warehouse_id.name, account.warehouse_id.id)
                        _logger.info("📍 Ubicación de stock del almacén: %s (ID: %s)", stock_location.name if stock_location else 'N/A', stock_location.id if stock_location else 'N/A')
                    
                    if stock_location:
                        if location:
                            # Verificar si la ubicación es la configurada o es hija de ella
                            location_ids = self.env['stock.location'].search([
                                ('id', 'child_of', stock_location.id)
                            ]).ids
                            
                            _logger.info("🔍 Verificando ubicación: %s (ID: %s) contra almacén: %s (ID: %s)", 
                                       location.name, location.id, stock_location.name, stock_location.id)
                            _logger.info("   Ubicaciones válidas (hijas del almacén): %s", location_ids)
                            
                            if location.id not in location_ids:
                                _logger.warning("⚠️ La ubicación %s (ID: %s) NO corresponde al almacén configurado %s (ID: %s).", 
                                               location.name, location.id, stock_location.name, stock_location.id)
                                _logger.warning("   ⚠️ NO se sincronizará el stock para este cambio.")
                                continue
                            else:
                                _logger.info("✅ Ubicación %s corresponde al almacén configurado %s", 
                                           location.name, stock_location.name)
                        else:
                            _logger.info("ℹ️ No se recibió location_id (se sincroniza de todas formas).")
                    else:
                        _logger.info("ℹ️ No hay almacén configurado en la cuenta %s. Sincronizando de todas formas.", account.name)
                    
                    # Buscar publicaciones de MercadoLibre relacionadas con este producto
                    publications = self.env['ml.publication'].search([
                        ('product_tmpl_id', '=', product_template.id),
                        ('ml_item_id', '!=', False),  # Solo publicaciones ya sincronizadas
                        ('ml_account_id', '=', account.id),
                    ])
                    
                    if not publications:
                        _logger.warning("⚠️ No se encontraron publicaciones de MercadoLibre para producto %s (Template ID: %s) en cuenta %s", 
                                       product.name, product_template.id, account.name)
                        continue
                    
                    _logger.info("📦 Publicaciones encontradas: %d", len(publications))
                    for pub in publications[:5]:
                        _logger.info("   - Pub: %s | ML Item=%s | status=%s | producto_tmpl=%s",
                                     pub.title, pub.ml_item_id, pub.ml_status, pub.product_tmpl_id.display_name if pub.product_tmpl_id else 'N/A')
                    if len(publications) > 5:
                        _logger.info("   ... (%d más)", len(publications) - 5)
                    
                    # Para cada publicación, actualizar el stock
                    for publication in publications:
                        try:
                            _logger.info("🔄 Sincronizando stock para publicación: %s (ID ML: %s)", 
                                       publication.title, publication.ml_item_id)
                            
                            # IMPORTANTE: Replicar el stock directamente desde Odoo (SIN cálculos)
                            # El stock se copia directamente (no se suma ni resta)
                            # Obtener stock directamente según el tipo configurado
                            stock_type = publication.ml_account_id.stock_type if publication.ml_account_id else 'available'
                            use_expected = (stock_type == 'expected')
                            
                            # Obtener el producto variant
                            product_variant = publication.product_tmpl_id.product_variant_id
                            
                            if not product_variant:
                                _logger.warning("⚠️ Publicación %s no tiene variante del producto, omitiendo", publication.title)
                                continue
                            
                            # Obtener stock directamente según el tipo configurado
                            # IMPORTANTE: usar contexto de almacén/ubicación configurado en la cuenta,
                            # para que el stock copiado sea el del warehouse de ML (no el global).
                            product_variant_ctx = product_variant
                            try:
                                wh = account.warehouse_id
                                if wh and wh.lot_stock_id:
                                    product_variant_ctx = product_variant.with_context(location=wh.lot_stock_id.id)
                            except Exception:
                                product_variant_ctx = product_variant

                            if use_expected:
                                # Stock esperado: usar virtual_available directamente
                                current_stock = int(product_variant_ctx.virtual_available or 0)
                                _logger.info("📊 [1] Obteniendo stock esperado (virtual_available) directamente: %s → %d", 
                                           publication.product_tmpl_id.name if publication.product_tmpl_id else 'N/A',
                                           current_stock)
                            else:
                                # Stock disponible: usar qty_available directamente
                                current_stock = int(product_variant_ctx.qty_available or 0)
                                _logger.info("📊 [1] Obteniendo stock disponible (qty_available) directamente: %s → %d", 
                                           publication.product_tmpl_id.name if publication.product_tmpl_id else 'N/A',
                                           current_stock)
                            
                            # Asegurar que no sea negativo
                            current_stock = max(0, current_stock)
                            
                            _logger.info("   ℹ️ Este es el valor ABSOLUTO que se copiará a MercadoLibre (no se suma ni resta)")
                            
                            # Luego, actualizar solo el stock en MercadoLibre (no el precio)
                            # Pasar el stock_value como parámetro para asegurar que se use el valor directo
                            result = publication._force_update_stock_in_ml(stock_value=current_stock)
                            
                            _logger.info("✅ [2] Resultado sincronización: %s | Stock enviado: %d", 
                                       "Éxito" if result else "Falló", current_stock)
                            
                            # Verificar reglas de stock y pausar/activar automáticamente si está configurado
                            # IMPORTANTE: Solo se ejecuta para este producto específico que cambió
                            try:
                                _logger.info("🔍 Verificando reglas de stock para producto %s (solo este producto)", product.name)
                                publication._check_stock_rules_and_update_status()
                            except Exception as e:
                                _logger.warning("⚠️ Error verificando reglas de stock para publicación %s: %s", publication.title, e)
                            
                        except Exception as e:
                            _logger.exception("❌ Error sincronizando publicación %s a MercadoLibre: %s", publication.title, e)
                            # Continuar con otras publicaciones aunque una falle
                            continue
                    
                except Exception as e:
                    _logger.exception("❌ Error procesando cuenta %s: %s", account.name, e)
                    # Continuar con otras cuentas aunque una falle
                    continue
            
            _logger.info("=" * 80)
            
        except Exception as e:
            _logger.exception("❌ Error en sincronización automática de stock a MercadoLibre: %s", e)

