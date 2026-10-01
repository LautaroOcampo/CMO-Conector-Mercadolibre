# -*- coding: utf-8 -*-
"""
Validador genérico de atributos de Mercado Libre.

Este módulo proporciona validación robusta y general para cualquier tipo de atributo,
sin hardcodear casos específicos. Funciona para cualquier categoría y cualquier atributo.
"""
from odoo import models, api, _
import json
import logging
import re

_logger = logging.getLogger(__name__)


class MLAttributeValidator(models.AbstractModel):
    """
    Validador genérico de atributos de Mercado Libre.
    
    Proporciona métodos estáticos para validar y convertir atributos según su value_type,
    tags y reglas de Mercado Libre.
    """
    _name = "ml.attribute.validator"
    _description = "Validador de Atributos Mercado Libre"

    @staticmethod
    def parse_tags(tags_json):
        """
        Parsea tags desde JSON string, dict, boolean, o cualquier tipo.
        SIEMPRE devuelve un dict, nunca None, boolean, o string.
        
        Este método es compatible con el método safe_parse_tags de ml.category.attribute.
        
        :param tags_json: Puede ser dict, string JSON, boolean, None, o cualquier cosa
        :return: dict (nunca None)
        """
        if not tags_json:
            return {}
        
        # Si ya es dict, devolverlo
        if isinstance(tags_json, dict):
            return tags_json
        
        # Si es boolean, convertir a dict vacío (error de datos)
        if isinstance(tags_json, bool):
            _logger.warning("⚠️ tags es boolean en lugar de dict. Normalizando a dict vacío.")
            return {}
        
        # Si es string, intentar parsear JSON
        if isinstance(tags_json, str):
            try:
                parsed = json.loads(tags_json)
                # Asegurar que sea dict
                if isinstance(parsed, dict):
                    return parsed
                else:
                    _logger.warning("⚠️ tags parseado no es dict, es %s. Normalizando a dict vacío.", type(parsed))
                    return {}
            except (json.JSONDecodeError, ValueError) as e:
                _logger.warning("⚠️ Error parseando tags JSON: %s. Normalizando a dict vacío.", e)
                return {}
        
        # Cualquier otro tipo, normalizar a dict vacío
        _logger.warning("⚠️ tags es de tipo inesperado: %s. Normalizando a dict vacío.", type(tags_json))
        return {}

    @staticmethod
    def parse_allowed_values(allowed_values_json):
        """Parsea valores permitidos desde JSON string o list."""
        if not allowed_values_json:
            return []
        if isinstance(allowed_values_json, str):
            try:
                return json.loads(allowed_values_json)
            except Exception:
                return []
        return allowed_values_json or []

    @staticmethod
    def parse_allowed_units(allowed_units_json):
        """Parsea unidades permitidas desde JSON string o list."""
        if not allowed_units_json:
            return []
        if isinstance(allowed_units_json, str):
            try:
                return json.loads(allowed_units_json)
            except Exception:
                return []
        return allowed_units_json or []

    @staticmethod
    def should_exclude_attribute(attr_def, has_variations=False):
        """
        Determina si un atributo debe excluirse del payload según sus tags.
        
        :param attr_def: Diccionario con definición del atributo (desde ml.category.attribute)
        :param has_variations: Si el item tiene variaciones
        :return: (should_exclude, reason)
        """
        tags = MLAttributeValidator.parse_tags(attr_def.get('tags'))
        
        # hidden → NO ENVIAR
        if tags.get('hidden'):
            return True, "hidden"
        
        # read_only → NO ENVIAR
        if tags.get('read_only'):
            return True, "read_only"
        
        # variation_attribute → no aplicar si el ítem no tiene variaciones
        if tags.get('variation_attribute') and not has_variations:
            return True, "variation_attribute (sin variaciones)"
        
        return False, None

    @staticmethod
    def validate_string(attr_value, attr_def):
        """
        Valida atributo de tipo string.
        
        :param attr_value: Valor del atributo (value_name)
        :param attr_def: Definición del atributo desde ml.category.attribute
        :return: (is_valid, formatted_value, warning)
        """
        if not attr_value or not str(attr_value).strip():
            return False, None, "Valor vacío"
        
        value_str = str(attr_value).strip()
        max_length = attr_def.get('value_max_length', 255)
        
        if len(value_str) > max_length:
            return False, None, f"Valor excede longitud máxima ({max_length})"
        
        return True, value_str, None

    @staticmethod
    def validate_number(attr_value, attr_def):
        """
        Valida atributo de tipo number.
        
        :param attr_value: Valor numérico (value_number)
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_value, warning)
        """
        if attr_value is None:
            return False, None, "Valor numérico es None"
        
        try:
            num_value = float(attr_value)
            if num_value <= 0:
                return False, None, "Valor numérico debe ser mayor que 0"
            return True, num_value, None
        except (ValueError, TypeError):
            return False, None, "Valor no es numérico válido"

    @staticmethod
    def validate_number_unit(attr_value, attr_unit, attr_def):
        """
        Valida atributo de tipo number_unit.
        
        :param attr_value: Valor numérico (value_number)
        :param attr_unit: Unidad (value_unit)
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_value, formatted_unit, warning)
        """
        # Validar número
        is_valid_num, num_value, num_warning = MLAttributeValidator.validate_number(attr_value, attr_def)
        if not is_valid_num:
            return False, None, None, num_warning or "Valor numérico inválido"
        
        # Validar unidad
        if not attr_unit:
            # Intentar usar default_unit
            default_unit = attr_def.get('default_unit')
            if default_unit:
                attr_unit = default_unit
            else:
                return False, None, None, "Unidad requerida y no especificada"
        
        unit_str = str(attr_unit).strip()
        if not unit_str:
            return False, None, None, "Unidad vacía"
        
        # Validar contra allowed_units si están disponibles
        allowed_units = MLAttributeValidator.parse_allowed_units(attr_def.get('allowed_units'))
        if allowed_units:
            unit_ids = [u.get('id') for u in allowed_units if u.get('id')]
            if unit_str not in unit_ids:
                # Intentar buscar por nombre
                unit_found = False
                for u in allowed_units:
                    if u.get('id') == unit_str or u.get('name') == unit_str or u.get('symbol') == unit_str:
                        unit_str = u.get('id') or unit_str
                        unit_found = True
                        break
                if not unit_found:
                    return False, None, None, f"Unidad '{unit_str}' no está en allowed_units"
        
        return True, num_value, unit_str, None

    @staticmethod
    def validate_list(attr_value_id, attr_value_name, attr_def):
        """
        Valida atributo de tipo list (value_id).
        
        :param attr_value_id: ID del valor (value_id)
        :param attr_value_name: Nombre del valor (value_name) - opcional, para búsqueda
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_value_id, warning)
        """
        allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
        
        if not allowed_values:
            # Si no hay valores permitidos definidos, aceptar cualquier value_id
            if attr_value_id:
                return True, str(attr_value_id), None
            return False, None, "value_id requerido para atributo tipo list"
        
        # Si hay value_id, validar que esté en allowed_values
        if attr_value_id:
            value_id_str = str(attr_value_id)
            for val in allowed_values:
                if str(val.get('id')) == value_id_str:
                    return True, value_id_str, None
            return False, None, f"value_id '{value_id_str}' no está en valores permitidos"
        
        # Si no hay value_id pero hay value_name, intentar buscar por nombre
        if attr_value_name:
            value_name_str = str(attr_value_name).strip()
            for val in allowed_values:
                val_name = val.get('name', '').strip()
                if val_name.lower() == value_name_str.lower():
                    return True, str(val.get('id')), None
            return False, None, f"Valor '{value_name_str}' no encontrado en valores permitidos"
        
        return False, None, "value_id o value_name requerido para atributo tipo list"

    @staticmethod
    def validate_boolean(attr_value_id, attr_def):
        """
        Valida atributo de tipo boolean.
        
        :param attr_value_id: ID del valor booleano
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_value_id, warning)
        """
        if not attr_value_id:
            return False, None, "value_id requerido para atributo tipo boolean"
        
        # Validar contra allowed_values si están disponibles
        allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
        if allowed_values:
            value_id_str = str(attr_value_id)
            for val in allowed_values:
                if str(val.get('id')) == value_id_str:
                    return True, value_id_str, None
            return False, None, f"value_id '{value_id_str}' no está en valores permitidos"
        
        # Si no hay valores permitidos, aceptar cualquier value_id
        return True, str(attr_value_id), None

    @staticmethod
    def validate_picture(attr_value_id, attr_value_name, attr_def):
        """
        Valida atributo de tipo picture.
        
        IMPORTANTE: Solo acepta picture_id válido (hash de ML), nunca texto libre.
        
        :param attr_value_id: ID de la imagen (picture_id de ML)
        :param attr_value_name: Nombre del valor (NO DEBE USARSE para picture)
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_value_id, warning)
        """
        if not attr_value_id:
            return False, None, "value_id (picture_id) requerido para atributo tipo picture"
        
        value_id_str = str(attr_value_id).strip()
        
        # Validar formato de picture_id (debe ser un hash válido de ML)
        # Los picture_id de ML tienen formato como: "720203-MLA99258242646_112025"
        # o simplemente números/hashes
        if not value_id_str:
            return False, None, "picture_id vacío"
        
        # Rechazar valores comunes que NO son picture_id válidos
        invalid_values = ["sí", "si", "no", "a", "b", "archivo", "imagen", "file", "picture"]
        if value_id_str.lower() in invalid_values:
            return False, None, f"Valor '{value_id_str}' no es un picture_id válido (es texto común)"
        
        # Validar que no sea solo texto sin formato de ID
        # Los picture_id suelen tener números, guiones, o formato específico
        if re.match(r'^[a-zA-Z]+$', value_id_str) and len(value_id_str) < 10:
            return False, None, f"Valor '{value_id_str}' parece texto, no picture_id válido"
        
        return True, value_id_str, None

    @staticmethod
    def validate_multivalued(attr_values, attr_def):
        """
        Valida atributo multivalued.
        
        :param attr_values: Lista de valores (puede ser lista de IDs o lista de dicts)
        :param attr_def: Definición del atributo
        :return: (is_valid, formatted_values, warning)
        """
        if not attr_values:
            return False, None, "Valores requeridos para atributo multivalued"
        
        if not isinstance(attr_values, list):
            return False, None, "Atributo multivalued requiere lista de valores"
        
        if len(attr_values) == 0:
            return False, None, "Lista de valores vacía"
        
        # Formatear valores según el tipo
        formatted_values = []
        for val in attr_values:
            if isinstance(val, dict):
                # Si es dict, extraer value_id o value_name según corresponda
                if 'value_id' in val:
                    formatted_values.append(str(val['value_id']))
                elif 'value_name' in val:
                    formatted_values.append(str(val['value_name']).strip())
            else:
                formatted_values.append(str(val).strip())
        
        return True, formatted_values, None

    @staticmethod
    def validate_attribute(publication_attr, category_attr_def, has_variations=False):
        """
        Valida un atributo de publicación contra su definición de categoría.
        
        Este es el método principal que debe usarse para validar atributos.
        
        :param publication_attr: Record de ml.publication.attribute
        :param category_attr_def: Record de ml.category.attribute o dict con definición
        :param has_variations: Si el item tiene variaciones
        :return: (is_valid, ml_format_dict, warnings)
        """
        warnings = []
        
        # Convertir category_attr_def a dict si es record
        if hasattr(category_attr_def, 'ml_attribute_id'):
            attr_def = {
                'ml_attribute_id': category_attr_def.ml_attribute_id,
                'value_type': category_attr_def.attribute_type or category_attr_def.value_type,
                'tags': category_attr_def.tags,
                'allowed_values': category_attr_def.allowed_values,
                'allowed_units': category_attr_def.allowed_units,
                'default_unit': category_attr_def.default_unit if hasattr(category_attr_def, 'default_unit') else None,
                'value_max_length': getattr(category_attr_def, 'value_max_length', 255),
            }
        else:
            attr_def = category_attr_def
        
        # Verificar si debe excluirse según tags
        should_exclude, exclude_reason = MLAttributeValidator.should_exclude_attribute(
            attr_def, has_variations
        )
        if should_exclude:
            return False, None, [f"Atributo excluido: {exclude_reason}"]
        
        ml_attribute_id = attr_def.get('ml_attribute_id') or publication_attr.ml_attribute_id
        
        # IMPORTANTE: Usar attribute_type del JSON importado si está disponible
        # attribute_type es el value_type real de ML (string, number, list, boolean, number_unit, etc.)
        # Si no está disponible, usar value_type de la definición o del atributo
        value_type = attr_def.get('value_type')
        
        # Si no hay value_type en attr_def, intentar obtenerlo del publication_attr
        if not value_type:
            # Priorizar attribute_type del JSON importado sobre value_type de Odoo
            if hasattr(publication_attr, 'attribute_type') and publication_attr.attribute_type:
                value_type = publication_attr.attribute_type
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
                value_type = odoo_to_ml_type.get(publication_attr.value_type, 'string')
        
        result = {"id": ml_attribute_id}
        
        # Validar según value_type (ahora usando el tipo real de ML del JSON)
        if value_type in ["string", "value_name"]:
            is_valid, formatted_value, warning = MLAttributeValidator.validate_string(
                publication_attr.value_name, attr_def
            )
            if not is_valid:
                return False, None, [warning or "Valor string inválido"]
            result["value_name"] = formatted_value
            
        elif value_type == "number":
            # IMPORTANTE: Verificar que value_number tenga un valor válido
            if publication_attr.value_number is None:
                # Si no hay value_number, intentar obtener desde value_name como fallback
                if publication_attr.value_name:
                    try:
                        num_value = float(publication_attr.value_name)
                        # Actualizar value_number para futuras referencias
                        publication_attr.value_number = num_value
                        _logger.debug("✅ Convertido value_name '%s' a value_number %s para atributo %s", 
                                    publication_attr.value_name, num_value, ml_attribute_id)
                    except (ValueError, TypeError):
                        return False, None, [f"Valor numérico inválido: '{publication_attr.value_name}' no es un número válido"]
                else:
                    return False, None, ["Valor numérico requerido (value_number o value_name numérico)"]
            
            is_valid, formatted_value, warning = MLAttributeValidator.validate_number(
                publication_attr.value_number, attr_def
            )
            if not is_valid:
                return False, None, [warning or "Valor numérico inválido"]
            
            # IMPORTANTE: Recrear dict limpio para evitar campos residuales que ML ignora
            result = {
                "id": ml_attribute_id,
                "value_number": formatted_value
            }
            
        elif value_type == "number_unit":
            # IMPORTANTE: Verificar que value_number tenga un valor válido
            if publication_attr.value_number is None:
                # Si no hay value_number, intentar obtener desde value_name como fallback
                if publication_attr.value_name:
                    try:
                        # Intentar extraer número desde value_name (puede ser "10 kg" o "10")
                        import re
                        num_match = re.search(r'(\d+\.?\d*)', str(publication_attr.value_name))
                        if num_match:
                            num_value = float(num_match.group(1))
                            publication_attr.value_number = num_value
                            _logger.debug("✅ Extraído número %s desde value_name '%s' para atributo %s", 
                                        num_value, publication_attr.value_name, ml_attribute_id)
                        else:
                            return False, None, [f"Valor numérico inválido: '{publication_attr.value_name}' no contiene un número válido"]
                    except (ValueError, TypeError):
                        return False, None, [f"Valor numérico inválido: '{publication_attr.value_name}' no es un número válido"]
                else:
                    return False, None, ["Valor numérico requerido (value_number o value_name con número)"]
            
            # Obtener unidad desde value_unit
            unit_value = None
            if publication_attr.value_unit:
                unit_value = publication_attr.value_unit
            else:
                # Intentar extraer unidad desde value_name si no está en value_unit
                if publication_attr.value_name:
                    import re
                    # Buscar unidad al final del string (ej: "10 kg", "5 cm")
                    unit_match = re.search(r'\d+\.?\d*\s*([a-zA-Z]+)$', str(publication_attr.value_name))
                    if unit_match:
                        unit_value = unit_match.group(1).strip()
                        _logger.debug("✅ Extraída unidad '%s' desde value_name para atributo %s", 
                                    unit_value, ml_attribute_id)
            
            is_valid, formatted_num, formatted_unit, warning = MLAttributeValidator.validate_number_unit(
                publication_attr.value_number, unit_value, attr_def
            )
            if not is_valid:
                return False, None, [warning or "Valor number_unit inválido"]
            
            # IMPORTANTE: MercadoLibre NO soporta value_struct, usar value_number + value_unit separados
            # Recrear dict limpio para evitar campos residuales que ML ignora
            result = {
                "id": ml_attribute_id,
                "value_number": formatted_num,
                "value_unit": formatted_unit
            }
                
        elif value_type in ["list", "value_id"]:
            # IMPORTANTE: Detectar si es booleano disfrazado de list (Sí/No)
            allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
            is_boolean_list = False
            if allowed_values and len(allowed_values) == 2:
                # Verificar si solo tiene Sí y No
                val_names = [str(v.get('name', '')).lower().strip() for v in allowed_values]
                val_names_set = set(val_names)
                if val_names_set in [{'sí', 'no'}, {'si', 'no'}, {'yes', 'no'}, {'true', 'false'}]:
                    is_boolean_list = True
            
            # Si es booleano disfrazado de list, tratarlo como booleano
            if is_boolean_list:
                boolean_value = None
                if publication_attr.value_id:
                    # Buscar el valor en allowed_values
                    for val in allowed_values:
                        if str(val.get('id')) == str(publication_attr.value_id):
                            val_name = str(val.get('name', '')).lower().strip()
                            boolean_value = val_name in ['sí', 'si', 'yes', 'true', '1']
                            break
                elif publication_attr.value_name:
                    value_name_lower = str(publication_attr.value_name).lower().strip()
                    boolean_value = value_name_lower in ['sí', 'si', 'yes', 'true', '1']
                
                if boolean_value is None:
                    return False, None, ["Valor booleano requerido: debe seleccionar 'Sí' o 'No'"]
                
                # IMPORTANTE: Recrear dict limpio SOLO con value_boolean, sin value_id ni value_name
                result = {
                    "id": ml_attribute_id,
                    "value_boolean": boolean_value
                }
                return True, result, []
            
            # Atributo tipo list normal (valores predefinidos)
            # Para SIZE_GRID_ID y otros atributos de lista, validar estrictamente
            is_valid, formatted_value_id, warning = MLAttributeValidator.validate_list(
                publication_attr.value_id, publication_attr.value_name, attr_def
            )
            if not is_valid:
                # Log detallado para debugging
                allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
                _logger.warning("⚠️ Atributo %s (%s) inválido: value_id=%s, value_name=%s, warning=%s", 
                              ml_attribute_id, publication_attr.name, 
                              publication_attr.value_id, publication_attr.value_name, warning)
                
                # Construir mensaje detallado con valores permitidos
                error_msg = warning or "Valor list inválido"
                if allowed_values:
                    # Mostrar primeros 15 valores permitidos para ayudar al usuario
                    valid_values_info = []
                    for val in allowed_values[:15]:
                        val_id = str(val.get('id', ''))
                        val_name = val.get('name', '')
                        if val_name:
                            valid_values_info.append(f"{val_name} (ID: {val_id})")
                        else:
                            valid_values_info.append(f"ID: {val_id}")
                    
                    if len(allowed_values) > 15:
                        valid_values_info.append(f"... y {len(allowed_values) - 15} más")
                    
                    error_msg = (
                        f"El valor seleccionado para '{publication_attr.name or ml_attribute_id}' no es válido. "
                        f"Valor actual: ID={publication_attr.value_id}, Nombre={publication_attr.value_name or 'N/A'}. "
                        f"Valores permitidos: {', '.join(valid_values_info)}. "
                        f"Por favor, seleccione un valor de la lista desplegable."
                    )
                else:
                    # Si no hay allowed_values, puede ser que no estén cargados
                    error_msg = (
                        f"El valor seleccionado para '{publication_attr.name or ml_attribute_id}' no es válido. "
                        f"Valor actual: ID={publication_attr.value_id}, Nombre={publication_attr.value_name or 'N/A'}. "
                        f"Nota: Los valores permitidos no están cargados. Por favor, actualice los atributos de la categoría."
                    )
                
                return False, None, [error_msg]
            
            # Validación adicional: asegurar que el value_id esté en los valores permitidos
            allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
            if allowed_values:
                value_id_str = str(formatted_value_id)
                value_found = False
                for val in allowed_values:
                    if str(val.get('id')) == value_id_str:
                        value_found = True
                        break
                if not value_found:
                    valid_ids = [str(v.get('id')) for v in allowed_values[:10]]
                    _logger.error("❌ value_id '%s' no está en valores permitidos para %s. Valores permitidos: %s", 
                                value_id_str, ml_attribute_id, valid_ids)
                    return False, None, [
                        f"El valor seleccionado (ID: {value_id_str}) no es válido para esta categoría. "
                        f"Valores permitidos (IDs): {', '.join(valid_ids)}. "
                        f"Por favor, seleccione un valor de la lista desplegable."
                    ]
            
            result["value_id"] = formatted_value_id
            
        elif value_type in ["boolean", "boolean_radio"]:
            # IMPORTANTE: Detectar si es booleano y convertir a value_boolean
            # Obtener valor booleano desde value_id o value_name
            boolean_value = None
            
            # 1. Intentar desde value_id (IDs de ML: 242084=Sí, 242085=No)
            if publication_attr.value_id:
                value_id_str = str(publication_attr.value_id).strip()
                if value_id_str in ['242084', '242085']:
                    boolean_value = (value_id_str == '242084')
                else:
                    # Validar contra allowed_values si está disponible
                    allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
                    if allowed_values:
                        for val in allowed_values:
                            if str(val.get('id')) == value_id_str:
                                val_name = str(val.get('name', '')).lower().strip()
                                boolean_value = val_name in ['sí', 'si', 'yes', 'true', '1']
                                break
            
            # 2. Intentar desde value_name
            if boolean_value is None and publication_attr.value_name:
                value_name_lower = str(publication_attr.value_name).lower().strip()
                boolean_value = value_name_lower in ['sí', 'si', 'yes', 'true', '1']
            
            if boolean_value is None:
                return False, None, ["Valor booleano requerido: debe seleccionar 'Sí' o 'No'"]
            
            # IMPORTANTE: Recrear dict limpio SOLO con value_boolean, sin value_id ni value_name
            result = {
                "id": ml_attribute_id,
                "value_boolean": boolean_value
            }
            
        elif value_type in ["picture", "file"]:
            is_valid, formatted_value_id, warning = MLAttributeValidator.validate_picture(
                publication_attr.value_id, publication_attr.value_name, attr_def
            )
            if not is_valid:
                return False, None, [warning or "Valor picture inválido"]
            result["value_id"] = formatted_value_id
            
        else:
            # Tipo desconocido - intentar como string
            _logger.warning("⚠️ Tipo de atributo desconocido: %s. Usando validación string.", value_type)
            is_valid, formatted_value, warning = MLAttributeValidator.validate_string(
                publication_attr.value_name, attr_def
            )
            if not is_valid:
                return False, None, [warning or f"Valor inválido para tipo desconocido: {value_type}"]
            result["value_name"] = formatted_value
        
        # Verificar si es multivalued
        tags = MLAttributeValidator.parse_tags(attr_def.get('tags'))
        if tags.get('multivalued'):
            # Si es multivalued, el resultado debe ser una lista
            # Por ahora, enviamos el valor como está (ML maneja multivalued de forma especial)
            pass
        
        return True, result, warnings

