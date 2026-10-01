# -*- coding: utf-8 -*-
"""Quita el campo allowed_sku_ids de vistas guardadas antes de validar el esquema."""
import json
import logging
import re

_logger = logging.getLogger(__name__)

_FIELD_RE = re.compile(
    r'<field[^>]*name="allowed_sku_ids"[^>]*/>'
    r'|<field[^>]*name="allowed_sku_ids"[^>]*>\s*</field>',
    re.IGNORECASE,
)


def _strip_field(arch):
    if not isinstance(arch, str) or 'allowed_sku_ids' not in arch:
        return arch
    return _FIELD_RE.sub('', arch)


def migrate(cr, version):
    cr.execute(
        """
        SELECT id, arch_db
          FROM ir_ui_view
         WHERE arch_db::text LIKE %s
        """,
        ('%allowed_sku_ids%',),
    )
    rows = cr.fetchall()
    for view_id, arch_db in rows:
        if isinstance(arch_db, str):
            try:
                data = json.loads(arch_db)
            except json.JSONDecodeError:
                data = arch_db
        else:
            data = arch_db

        if isinstance(data, dict):
            cleaned = {
                lang: _strip_field(arch) if isinstance(arch, str) else arch
                for lang, arch in data.items()
            }
        else:
            cleaned = _strip_field(data)

        cr.execute(
            "UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s",
            (json.dumps(cleaned, ensure_ascii=False), view_id),
        )
    if rows:
        _logger.info('Vistas actualizadas sin allowed_sku_ids: %s', len(rows))
