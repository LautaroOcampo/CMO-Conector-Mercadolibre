# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import json
import logging

_logger = logging.getLogger(__name__)


class MLPublicationAttribute(models.Model):
    _name = "ml.publication.attribute"
    _description = "Atributo de Publicación Mercado Libre"
    _order = "sequence, id"

    publication_id = fields.Many2one(
        "ml.publication",
        string="Publicación",
        required=True,
        ondelete="cascade",
    )
    
    ml_attribute_id = fields.Char(
        string="ID Atributo ML",
        required=True,
        help="ID del atributo en Mercado Libre (ej: BRAND, MODEL, etc.)",
    )
    
    name = fields.Char(
        string="Nombre del Atributo",
        required=True,
        help="Nombre legible del atributo",
    )
    
    name_display = fields.Char(
        string="Nombre del Atributo (Mostrar)",
        compute="_compute_name_display",
        store=False,
        help="Nombre del atributo para mostrar en la vista (no se guarda)",
    )
    
    @api.depends('name')
    def _compute_name_display(self):
        """Calcula el nombre para mostrar (solo lectura)."""
        for record in self:
            record.name_display = record.name or record.ml_attribute_id or ""
    
    value_type = fields.Selection([
        ('value_id', 'Valor Predefinido'),
        ('value_name', 'Texto Libre'),
        ('boolean', 'Booleano'),
        ('number', 'Número'),
        ('number_unit', 'Número con Unidad'),
        ('picture', 'Imagen/Archivo'),
    ], string="Tipo de Valor", default='value_name')
    
    value_id = fields.Char(
        string="ID Valor",
        help="ID del valor predefinido (si value_type es 'value_id')",
    )

    value_id_display = fields.Char(
        string="Valor Seleccionado",
        compute="_compute_value_id_display",
        store=False,
        help="Nombre del valor seleccionado",
    )
    
    value_name = fields.Char(
        string="Valor",
        help="Valor del atributo (texto libre o nombre del valor predefinido)",
        required=False,  # Se validará dinámicamente según required del atributo
    )
    
    value_number = fields.Float(
        string="Valor Numérico",
        help="Valor numérico (si value_type es 'number' o 'number_unit')",
    )
    
    value_unit = fields.Char(
        string="Unidad",
        help="Unidad del valor numérico (ej: kg, cm, etc.)",
    )

    value_file = fields.Binary(
        string="Archivo/Imagen",
        help="Archivo o imagen para atributos de tipo picture/file",
    )
    
    value_file_name = fields.Char(
        string="Nombre del archivo",
        help="Nombre del archivo subido",
    )
    
    required = fields.Boolean(
        string="Obligatorio",
        default=False,
        help="Indica si este atributo es obligatorio para la categoría",
    )
    
    is_common = fields.Boolean(
        string="Atributo Común",
        default=False,
        help="Indica si este es un atributo común a múltiples categorías",
    )
    
    sequence = fields.Integer(
        string="Secuencia",
        default=10,
        help="Orden de visualización",
    )
    
    # Campos para almacenar información de la API
    allowed_values = fields.Text(
        string="Valores Permitidos (JSON)",
        help="Valores permitidos para este atributo (formato JSON)",
    )
    
    allowed_units = fields.Text(
        string="Unidades Permitidas (JSON)",
        help="Unidades permitidas para este atributo (formato JSON)",
    )
    
    has_allowed_units = fields.Boolean(
        string="Tiene unidades permitidas",
        compute="_compute_has_allowed_units",
        store=False,
    )
    
    attribute_type = fields.Char(
        string="Tipo de Atributo",
        help="Tipo del atributo según ML (string, number, boolean, list, etc.)",
    )

    has_allowed_values = fields.Boolean(
        string="Tiene valores permitidos",
        compute="_compute_has_allowed_values",
        store=False,
    )
    
    @api.depends('allowed_values', 'value_type', 'ml_attribute_id')
    def _compute_has_allowed_values(self):
        """Calcula si el atributo tiene valores permitidos para seleccionar."""
        for record in self:
            # Solo tiene valores permitidos si es tipo value_id o boolean Y tiene allowed_values
            has_allowed = bool(record.allowed_values)
            is_list_type = record.value_type in ['value_id', 'boolean']
            record.has_allowed_values = has_allowed and is_list_type
    
    valid_values_help = fields.Text(
        string="Valores Válidos",
        compute="_compute_valid_values_help",
        store=False,
        help="Lista de valores válidos para este atributo",
    )
    
    
    @api.depends("value_id", "allowed_values")
    def _compute_value_id_display(self):
        """Calcula el nombre del valor seleccionado desde allowed_values."""
        for attr in self:
            if attr.value_id and attr.allowed_values:
                try:
                    values = json.loads(attr.allowed_values)
                    selected = next((v for v in values if str(v.get("id")) == str(attr.value_id)), None)
                    attr.value_id_display = selected.get("name", "") if selected else ""
                except Exception:
                    attr.value_id_display = ""
            else:
                attr.value_id_display = ""

    def write(self, vals):
        """Sobrescribir write para asegurar que ml_attribute_id no se pierda al actualizar y proteger campos importantes."""
        # IMPORTANTE: Proteger campos importantes que no deben perderse al guardar
        
        # 1. Proteger el campo 'name' si tiene un valor personalizado
        if 'name' in vals:
            new_name = (vals.get('name') or '').strip()
            protected_records = []
            records_to_update = []
            
            for record in self:
                current_name = (record.name or '').strip()
                ml_attr_id = (record.ml_attribute_id or '').strip()
                
                # Si el nombre actual es personalizado (diferente de ml_attribute_id) y el nuevo es ml_attribute_id,
                # NO actualizar el name - preservar el nombre personalizado
                if current_name and current_name != ml_attr_id:
                    if new_name == ml_attr_id or not new_name:
                        _logger.debug("🔒 Protegiendo name personalizado '%s' (intento de cambio a '%s') para atributo %s", 
                                    current_name, new_name, record.id)
                        protected_records.append(record)
                        continue
                
                # Si el nombre actual es igual a ml_attribute_id o está vacío, permitir la actualización
                records_to_update.append(record)
            
            # Si hay registros protegidos, escribir sin el name para ellos
            if protected_records:
                protected_vals = vals.copy()
                protected_vals.pop('name', None)
                if protected_vals:  # Solo escribir si hay otros campos para actualizar
                    protected_records.write(protected_vals)
                elif len(protected_records) == len(self):
                    # Si todos los registros están protegidos y no hay otros campos, no hacer nada
                    return protected_records
            
            # Si todos los registros están protegidos, no continuar
            if not records_to_update:
                return protected_records if protected_records else self
            
            # Actualizar solo los registros que no están protegidos
            if len(records_to_update) < len(self):
                # Hay algunos registros protegidos, actualizar solo los no protegidos
                records_to_update.write(vals)
                return protected_records + records_to_update
            else:
                # Todos los registros pueden actualizarse, pero eliminar name de vals si todos están protegidos
                # (esto no debería pasar porque ya los filtramos arriba)
                pass
        
        # 2. Proteger allowed_values si ya tiene valores (preservar opciones del multiple choice)
        # Si se intenta limpiar allowed_values pero el registro ya tiene valores, preservar
        if 'allowed_values' in vals:
            new_allowed_values = vals.get('allowed_values')
            if not new_allowed_values:
                # Se está intentando limpiar allowed_values - verificar si hay registros que deben protegerse
                records_with_allowed = self.filtered(lambda r: r.allowed_values)
                if records_with_allowed:
                    _logger.debug("🔒 Protegiendo allowed_values para %d atributos (intento de limpiar)", 
                                len(records_with_allowed))
                    # Eliminar allowed_values de vals para preservar los valores existentes
                    vals = vals.copy()
                    vals.pop('allowed_values', None)
        
        # 3. Proteger required si ya está configurado (preservar configuración del usuario)
        # Si se intenta desmarcar required pero el registro ya está marcado, preservar
        if 'required' in vals:
            new_required = vals.get('required')
            if new_required is False:
                # Se está intentando desmarcar required - verificar si hay registros que deben protegerse
                records_required = self.filtered(lambda r: r.required is True)
                if records_required:
                    _logger.debug("🔒 Protegiendo required=True para %d atributos (intento de desmarcar)", 
                                len(records_required))
                    # Eliminar required de vals para preservar la configuración existente
                    vals = vals.copy()
                    vals.pop('required', None)
        
        # Si se está actualizando y no se está pasando ml_attribute_id, preservarlo del registro existente
        if 'ml_attribute_id' not in vals:
            # Obtener ml_attribute_id del registro existente si no está en vals
            for record in self:
                if record.ml_attribute_id:
                    # Preservar ml_attribute_id desde el registro existente
                    if 'ml_attribute_id' not in vals:
                        vals = vals.copy()
                    # Si vals es un diccionario compartido, necesitamos crear una copia por registro
                    # Pero como write puede recibir múltiples registros, necesitamos manejar esto correctamente
                    _logger.debug("🔍 Preservando ml_attribute_id=%s para atributo %s", 
                                record.ml_attribute_id, record.id)
        
        # Si hay múltiples registros, necesitamos preservar ml_attribute_id para cada uno
        # Odoo maneja esto automáticamente si no incluimos ml_attribute_id en vals
        # Pero para asegurarnos, vamos a verificar que todos los registros tengan ml_attribute_id
        if 'ml_attribute_id' not in vals:
            # Verificar que todos los registros tengan ml_attribute_id
            records_without_id = self.filtered(lambda r: not r.ml_attribute_id)
            if records_without_id:
                _logger.warning("⚠️ Intentando actualizar %d atributos sin ml_attribute_id. IDs: %s", 
                              len(records_without_id), records_without_id.ids)
        
        res = super().write(vals)
        return res
    
    @api.model_create_multi
    def create(self, vals_list):
        """Sobrescribir create para validar que ml_attribute_id esté presente y recuperarlo desde la categoría si falta."""
        # Validar y limpiar valores antes de crear
        validated_vals_list = []
        for vals in vals_list:
            # Validar que ml_attribute_id esté presente y no sea vacío
            ml_attribute_id = vals.get('ml_attribute_id')
            publication_id = vals.get('publication_id')
            sequence = vals.get('sequence')
            name = vals.get('name')
            
            # Log para debugging
            _logger.debug("🔍 Validando atributo: ml_attribute_id=%s, name=%s, sequence=%s, publication_id=%s", 
                         ml_attribute_id, name, sequence, publication_id)
            
            # Si no tiene ml_attribute_id, intentar recuperarlo desde la categoría de la publicación
            if not ml_attribute_id or not str(ml_attribute_id).strip():
                if publication_id and sequence is not None:
                    _logger.info("🔍 Intentando recuperar ml_attribute_id desde categoría: publication_id=%s, sequence=%s", 
                               publication_id, sequence)
                    try:
                        publication = self.env['ml.publication'].browse(publication_id)
                        if publication.exists() and publication.ml_attribute_ids:
                            same_seq = publication.ml_attribute_ids.filtered(lambda a: a.sequence == sequence)
                            if same_seq and same_seq[0].ml_attribute_id:
                                ml_attribute_id = same_seq[0].ml_attribute_id
                                if not name and same_seq[0].name:
                                    name = same_seq[0].name
                    except Exception as e:
                        _logger.warning("⚠️ Error recuperando ml_attribute_id: %s", e)
            
            # Si aún no tiene ml_attribute_id, omitir este atributo
            if not ml_attribute_id or not str(ml_attribute_id).strip():
                _logger.warning("⚠️ Atributo sin ml_attribute_id. Omitiendo: %s", name or 'Sin nombre')
                continue
            
            # Convertir a string y limpiar
            ml_attribute_id_str = str(ml_attribute_id).strip()
            if not ml_attribute_id_str:
                _logger.warning("⚠️ Atributo con ml_attribute_id vacío después de limpiar. Omitiendo: %s", name or 'Sin nombre')
                continue
            
            # Asegurar que ml_attribute_id sea string
            vals['ml_attribute_id'] = ml_attribute_id_str
            
            # Asegurar que name esté presente
            if not vals.get('name'):
                vals['name'] = name or ml_attribute_id_str
            
            validated_vals_list.append(vals)
        
        if not validated_vals_list:
            _logger.error("❌ No se pudo crear ningún atributo: todos fueron rechazados por falta de ml_attribute_id")
            _logger.error("❌ Valores recibidos: %s", vals_list)
            # No lanzar error, solo loguear - Odoo manejará el error de campo requerido
            return self.browse()
        
        records = super().create(validated_vals_list)
        
        # Preseleccionar unidad automáticamente para atributos number_unit si hay solo una unidad disponible
        # Y crear valores "Sí"/"No" para atributos boolean si no existen
        for record in records:
            # Crear valores "Sí" y "No" para atributos boolean si no existen
            if record.value_type == "boolean" and not record.allowed_values:
                boolean_values = [
                    {"id": "242084", "name": "Sí"},
                    {"id": "242085", "name": "No"}
                ]
                record.allowed_values = json.dumps(boolean_values)
                _logger.info("✅ Valores boolean (Sí/No) creados automáticamente para %s (%s)", 
                           record.name, record.ml_attribute_id)
            
            # Preseleccionar unidad para number_unit (solo desde allowed_units JSON)
            if record.value_type == "number_unit" and not record.value_unit:
                allowed_units = None
                if record.allowed_units:
                    try:
                        allowed_units = json.loads(record.allowed_units) if isinstance(record.allowed_units, str) else record.allowed_units
                    except Exception:
                        pass
                if allowed_units and len(allowed_units) == 1:
                    first_unit = allowed_units[0]
                    unit_code = first_unit.get("id") or first_unit.get("symbol") or first_unit.get("name")
                    if unit_code:
                        record.value_unit = unit_code
                        _logger.info("✅ Unidad preseleccionada automáticamente para %s (%s): %s", 
                                   record.name, record.ml_attribute_id, unit_code)
        
        return records
    
    @api.model
    def create_from_ml_attribute(self, publication_id, ml_attr_data):
        """
        Crea un registro de atributo desde los datos de la API de ML.
        
        :param publication_id: ID de la publicación
        :param ml_attr_data: Diccionario con los datos del atributo desde ML API
        :return: Recordset del atributo creado
        """
        import json
        
        attr_id = ml_attr_data.get("id")
        if not attr_id:
            raise UserError(_("El atributo de ML debe tener un ID válido."))
        
        attr_name = ml_attr_data.get("name", "")
        required = ml_attr_data.get("tags", {}).get("required", False)
        values = ml_attr_data.get("values", [])
        value_type_ml = ml_attr_data.get("value_type", "string")
        
        # Determinar el tipo de valor en Odoo
        if value_type_ml in ["boolean", "boolean_radio"]:
            value_type = "boolean"
            # Para boolean, crear valores "Sí" y "No" si no vienen en values
            if not values:
                values = [
                    {"id": "242084", "name": "Sí"},
                    {"id": "242085", "name": "No"}
                ]
        elif value_type_ml in ["number", "number_unit"]:
            value_type = "number" if value_type_ml == "number" else "number_unit"
        elif value_type_ml in ["picture", "file"]:
            value_type = "picture"  # Imagen o archivo
        elif values:
            value_type = "value_id"  # Tiene valores predefinidos (list)
        else:
            value_type = "value_name"  # Texto libre (string)
        
        # Guardar valores permitidos como JSON
        allowed_values_json = json.dumps(values) if values else None
        allowed_units = ml_attr_data.get("allowed_units") or []
        allowed_units_json = json.dumps(allowed_units) if allowed_units else None
        
        # Crear el atributo
        attr_record = self.create({
            "publication_id": publication_id,
            "ml_attribute_id": str(attr_id),
            "name": attr_name or str(attr_id),
            "value_type": value_type,
            "required": required,
            "allowed_values": allowed_values_json,
            "allowed_units": allowed_units_json,
            "attribute_type": value_type_ml,
        })
        
        return attr_record
    
    def to_ml_format(self, category_attr_def=None, has_variations=False):
        """
        Convierte el atributo al formato esperado por la API de Mercado Libre.
        
        Usa el validador genérico para validar y formatear el atributo.
        IMPORTANTE: Usa la información del JSON importado (attribute_type) para exportar correctamente.
        
        :param category_attr_def: Definición del atributo desde ml.category.attribute (opcional)
        :param has_variations: Si el item tiene variaciones
        :return: Diccionario con el formato correcto o None si no es válido
        """
        self.ensure_one()
        
        # Construir definición desde el propio atributo de publicación si no se proporciona
        if not category_attr_def:
            # Usar attribute_type del JSON importado si está disponible
            ml_value_type = None
            if hasattr(self, 'attribute_type') and self.attribute_type:
                ml_value_type = self.attribute_type
            else:
                # Mapear value_type de Odoo a value_type de ML
                odoo_to_ml_type = {
                    'value_name': 'string',
                    'value_id': 'list',
                    'boolean': 'boolean',
                    'number': 'number',
                    'number_unit': 'number_unit',
                    'picture': 'picture',
                }
                ml_value_type = odoo_to_ml_type.get(self.value_type, 'string')
            
            category_attr_def = {
                'ml_attribute_id': self.ml_attribute_id,
                'value_type': ml_value_type,
                'tags': {},
                'allowed_values': self.allowed_values,
                'allowed_units': self.allowed_units,
                'default_unit': None,
                'value_max_length': 255,
            }
        
        # Usar validador genérico
        validator = self.env['ml.attribute.validator']
        is_valid, ml_format_dict, warnings = validator.validate_attribute(
            self, category_attr_def, has_variations
        )
        
        if not is_valid:
            if warnings:
                _logger.warning("⚠️ Atributo %s (%s) inválido: %s", 
                              self.ml_attribute_id, self.name, warnings[0] if warnings else "Sin razón")
            return None
        
        if warnings:
            _logger.debug("⚠️ Atributo %s (%s) tiene warnings: %s", 
                         self.ml_attribute_id, self.name, warnings)
        
        return ml_format_dict

    
    def _compute_has_allowed_units(self):
        for record in self:
            record.has_allowed_units = bool(record.allowed_units)
    
    @api.depends("allowed_values", "value_type", "ml_attribute_id")
    def _compute_valid_values_help(self):
        """Calcula la ayuda con valores válidos disponibles."""
        for attr in self:
            help_text = ""
            if attr.value_type == "value_id" and attr.allowed_values:
                try:
                    values = json.loads(attr.allowed_values)
                    if values:
                        help_text = "Valores permitidos:\n"
                        for val in values[:10]:  # Mostrar máximo 10 valores
                            help_text += f"  • {val.get('name', '')} (ID: {val.get('id', '')})\n"
                        if len(values) > 10:
                            help_text += f"  ... y {len(values) - 10} más"
                except Exception:
                    help_text = "Error al leer valores permitidos"
            elif attr.value_type in ["number", "number_unit"]:
                help_text = "Ingrese un valor numérico"
                if attr.value_type == "number_unit":
                    help_text += " y una unidad"
                    try:
                        if attr.allowed_units:
                            units = json.loads(attr.allowed_units)
                            if units:
                                unit_names = ", ".join(u.get("name") or u.get("id") for u in units[:5])
                                help_text += f" (unidades válidas: {unit_names}"
                                if len(units) > 5:
                                    help_text += f", ... +{len(units) - 5} más"
                                help_text += ")"
                    except Exception:
                        pass
            elif attr.value_type == "value_name":
                help_text = "Ingrese un valor de texto libre"
            elif attr.value_type == "boolean":
                help_text = "Seleccione Sí o No"
            
            attr.valid_values_help = help_text
    
    @api.onchange("value_unit")
    def _onchange_value_unit(self):
        pass

    @api.model
    def action_delete_all_attributes(self):
        """Elimina todos los atributos de publicación."""
        try:
            all_attributes = self.search([])
            count = len(all_attributes)
            
            if count == 0:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Eliminar atributos",
                        "message": "No hay atributos de publicación para eliminar.",
                        "type": "info",
                        "sticky": False,
                    }
                }
            
            all_attributes.unlink()
            _logger.info("🧹 Eliminados %d atributos de publicación", count)
            
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Atributos eliminados",
                    "message": f"Se eliminaron {count} atributos de publicación correctamente.",
                    "type": "success",
                    "sticky": False,
                }
            }
        except Exception as e:
            _logger.exception("❌ Error eliminando atributos de publicación: %s", e)
            raise UserError(_("Error al eliminar atributos de publicación: %s") % str(e))

