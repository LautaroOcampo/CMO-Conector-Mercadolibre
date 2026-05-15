# -*- coding: utf-8 -*-
"""
Builder robusto de payload para publicaciones de Mercado Libre.

Este módulo construye el payload de atributos de forma inteligente,
manejando errores sin bloquear la publicación y validando todos los atributos.
"""
from odoo import models, api, _
import logging

_logger = logging.getLogger(__name__)


class MLPayloadBuilder(models.AbstractModel):
    """
    Builder robusto de payload para Mercado Libre.
    
    Construye el payload de atributos validando cada uno, excluyendo inválidos
    y registrando warnings sin bloquear la publicación.
    """
    _name = "ml.payload.builder"
    _description = "Builder de Payload Mercado Libre"

    @api.model
    def build_attributes_payload(self, publication, category_attrs_def=None, include_variation_attrs=False):
        """
        Construye el payload de atributos para una publicación.
        
        :param publication: Record de ml.publication
        :param category_attrs_def: Dict con definiciones de atributos de categoría (opcional)
        :param include_variation_attrs: Si incluir atributos de variación en item.attributes
        :return: (attributes_list, warnings_dict, errors_list)
            - attributes_list: Lista de atributos válidos en formato ML
            - warnings_dict: Dict con warnings por atributo {attr_id: [warnings]}
            - errors_list: Lista de errores críticos que deberían bloquear la publicación
        """
        validator = self.env['ml.attribute.validator']
        attributes_list = []
        warnings_dict = {}
        errors_list = []
        
        if not category_attrs_def:
            category_attrs_def = {}
            for pub_attr in publication.ml_attribute_ids:
                if pub_attr.ml_attribute_id:
                    category_attrs_def[pub_attr.ml_attribute_id] = {
                        'ml_attribute_id': pub_attr.ml_attribute_id,
                        'value_type': pub_attr.attribute_type or pub_attr.value_type,
                        'tags': getattr(pub_attr, 'tags', {}),
                        'allowed_values': pub_attr.allowed_values,
                        'allowed_units': pub_attr.allowed_units,
                        'default_unit': getattr(pub_attr, 'default_unit', None),
                        'value_max_length': getattr(pub_attr, 'value_max_length', 255),
                        'required': pub_attr.required,
                        'catalog_required': self._is_catalog_required(getattr(pub_attr, 'tags', {})),
                        'conditional_required': self._is_conditional_required(getattr(pub_attr, 'tags', {})),
                    }
        
        # Obtener atributos de variación para excluirlos de item.attributes
        # IMPORTANTE: Los atributos con tag "variation_attribute" o "allow_variations" deben ir en variations, no en attributes
        variation_attr_ids = set()
        
        # 1. Obtener atributos de variación desde las variantes existentes
        if publication.ml_variant_ids:
            for variant in publication.ml_variant_ids:
                for attr_comb in variant.attribute_combination_ids:
                    if attr_comb.ml_attribute_id:
                        variation_attr_ids.add(attr_comb.ml_attribute_id)
        
        # 2. Atributos de variación desde publicación (sin sección categorías)
        validator = self.env.get('ml.attribute.validator')
        if validator:
            for pub_attr in publication.ml_attribute_ids:
                if not pub_attr.ml_attribute_id:
                    continue
                tags = validator.parse_tags(getattr(pub_attr, 'tags', {})) if hasattr(pub_attr, 'tags') else {}
                if tags.get('variation_attribute') or tags.get('allow_variations'):
                    variation_attr_ids.add(pub_attr.ml_attribute_id)
        
        # 3. Atributos comunes que siempre son de variación cuando hay variantes
        # (solo si realmente hay variantes)
        if publication.ml_variant_ids:
            common_variation_attrs = {"COLOR", "SIZE"}
            variation_attr_ids.update(common_variation_attrs)
        
        has_variations = bool(publication.ml_variant_ids)
        
        # Procesar cada atributo de la publicación
        for pub_attr in publication.ml_attribute_ids:
            attr_id = pub_attr.ml_attribute_id
            if not attr_id:
                continue
            
            # Excluir atributos de variación si no se deben incluir
            if not include_variation_attrs and attr_id in variation_attr_ids:
                _logger.debug("⏭️ Atributo %s excluido (es de variación)", attr_id)
                continue
            
            # VALIDACIÓN: Verificar que el atributo pertenezca a la categoría
            # IMPORTANTE: No rechazar atributos que no están importados en Odoo
            # Si un atributo existe en ML pero no está importado, ML lo validará
            # Solo filtrar atributos de sistema que claramente no deben enviarse
            system_attrs = {'PACKAGE_HEIGHT', 'PACKAGE_WIDTH', 'PACKAGE_LENGTH', 'PACKAGE_WEIGHT',
                           'SELLER_PACKAGE_HEIGHT', 'SELLER_PACKAGE_WIDTH', 'SELLER_PACKAGE_LENGTH', 
                           'SELLER_PACKAGE_WEIGHT', 'SELLER_PACKAGE_TYPE', 'SELLER_PACKAGE_DATA_SOURCE',
                           'PACKAGE_DATA_SOURCE', 'IS_FLAMMABLE', 'PRODUCT_FEATURES', 'DESCRIPTIVE_TAGS',
                           'PRODUCT_CHEMICAL_FEATURES', 'FOODS_AND_DRINKS', 'MEDICINES', 'BATTERIES_FEATURES',
                           'SHIPMENT_PACKING', 'ADDITIONAL_INFO_REQUIRED', 'EXCLUDED_PLATFORMS',
                           'IS_SUITABLE_FOR_SHIPMENT', 'PRODUCT_DATA_SOURCE', 'LIMITED_MARKETPLACE_VISIBILITY_REASONS',
                           'HAS_COMPATIBILITIES', 'CATALOG_TITLE', 'SEARCH_ENHANCEMENT_FIELDS', 'IS_NEW_OFFER',
                           'SYI_PYMES_ID', 'WITH_POSITIVE_IMPACT', 'HAZMAT_TRANSPORTABILITY', 'IS_KIT'}
            
            # Solo filtrar atributos de sistema
            if attr_id in system_attrs:
                warning_msg = f"El atributo '{pub_attr.name or attr_id}' ({attr_id}) es un atributo de sistema y será omitido del payload."
                if attr_id not in warnings_dict:
                    warnings_dict[attr_id] = []
                warnings_dict[attr_id].append(warning_msg)
                _logger.debug("⏭️ %s", warning_msg)
                continue  # Omitir este atributo
            
            # Obtener definición del atributo desde la categoría importada
            attr_def = category_attrs_def.get(attr_id)
            
            # Si no hay definición, construir una desde pub_attr
            if not attr_def:
                # IMPORTANTE: Usar attribute_type del JSON importado si está disponible
                # attribute_type es el value_type original de ML (string, number, list, boolean, etc.)
                # value_type es el mapeo a Odoo (value_name, value_id, number, number_unit, boolean)
                ml_value_type = pub_attr.attribute_type or pub_attr.value_type
                
                # Si attribute_type está disponible, usarlo directamente (es el tipo real de ML)
                # Si no, inferir desde value_type de Odoo
                if not pub_attr.attribute_type:
                    # Mapear value_type de Odoo a value_type de ML
                    odoo_to_ml_type = {
                        'value_name': 'string',
                        'value_id': 'list',
                        'boolean': 'boolean',
                        'number': 'number',
                        'number_unit': 'number_unit',
                        'picture': 'picture',
                    }
                    ml_value_type = odoo_to_ml_type.get(pub_attr.value_type, 'string')
                
                attr_def = {
                    'ml_attribute_id': attr_id,
                    'value_type': ml_value_type,  # Usar el tipo real de ML del JSON
                    'tags': {},
                    'allowed_values': pub_attr.allowed_values,
                    'allowed_units': pub_attr.allowed_units,
                    'default_unit': None,
                    'value_max_length': 255,
                    'required': pub_attr.required,
                    'catalog_required': False,
                    'conditional_required': False,
                }
            
            # Validar atributo usando la información del JSON importado
            is_valid, ml_format_dict, validation_warnings = validator.validate_attribute(
                pub_attr, attr_def, has_variations
            )
            
            # DEBUG: Log para atributos numéricos y booleanos
            if pub_attr.value_type in ['number', 'number_unit', 'boolean']:
                _logger.debug("🔍 Atributo %s (%s): value_type=%s, value_number=%s, value_id=%s, value_name=%s, value_unit=%s, attribute_type=%s", 
                            attr_id, pub_attr.name, pub_attr.value_type, 
                            pub_attr.value_number, pub_attr.value_id, pub_attr.value_name, 
                            pub_attr.value_unit, pub_attr.attribute_type)
                _logger.debug("🔍 attr_def value_type=%s, is_valid=%s, ml_format_dict=%s", 
                            attr_def.get('value_type'), is_valid, ml_format_dict)
            
            if not is_valid:
                # IMPORTANTE: Intentar exportar de todas formas si tiene algún valor
                # Esto asegura que todos los atributos importados se exporten usando la info del JSON
                has_any_value = (
                    pub_attr.value_id or 
                    pub_attr.value_name or 
                    pub_attr.value_number is not None or
                    (pub_attr.value_type == 'boolean' and pub_attr.value_id)
                )
                
                if has_any_value:
                    # Intentar construir el formato ML manualmente usando attribute_type del JSON
                    _logger.debug("⚠️ Atributo %s (%s) no pasó validación estricta, intentando exportar con formato del JSON...", 
                                  attr_id, pub_attr.name)
                    
                    # Usar attribute_type del JSON importado (tipo real de ML)
                    ml_value_type = attr_def.get('value_type', 'string')
                    fallback_dict = {"id": attr_id}
                    
                    try:
                        if ml_value_type in ["string", "value_name"]:
                            if pub_attr.value_name:
                                fallback_dict["value_name"] = str(pub_attr.value_name).strip()
                            else:
                                continue
                        
                        elif ml_value_type == "number":
                            # IMPORTANTE: Atributos numéricos SIEMPRE usan value_number
                            num_value = None
                            if pub_attr.value_number is not None and pub_attr.value_number > 0:
                                num_value = float(pub_attr.value_number)
                            elif pub_attr.value_name:
                                # Fallback: intentar convertir value_name a número
                                try:
                                    num_value = float(pub_attr.value_name)
                                    if num_value <= 0:
                                        continue
                                except (ValueError, TypeError):
                                    continue
                            else:
                                continue
                            
                            # Recrear dict limpio SOLO con id y value_number
                            fallback_dict = {
                                "id": attr_id,
                                "value_number": num_value
                            }
                        
                        elif ml_value_type == "number_unit":
                            num_value = None
                            unit_value = None
                            
                            if pub_attr.value_number is not None and pub_attr.value_number > 0:
                                num_value = float(pub_attr.value_number)
                                unit_value = pub_attr.value_unit
                            
                            if not num_value or not unit_value:
                                continue
                            
                            # IMPORTANTE: MercadoLibre NO soporta value_struct, usar value_number + value_unit separados
                            # Recrear dict limpio SOLO con id, value_number y value_unit
                            fallback_dict = {
                                "id": attr_id,
                                "value_number": num_value,
                                "value_unit": str(unit_value).strip()
                            }
                        
                        elif ml_value_type in ["list", "value_id"]:
                            # IMPORTANTE: Detectar si es booleano disfrazado de list
                            from .ml_attribute_validator import MLAttributeValidator
                            allowed_values = MLAttributeValidator.parse_allowed_values(attr_def.get('allowed_values'))
                            is_boolean_list = False
                            if allowed_values and len(allowed_values) == 2:
                                val_names = [str(v.get('name', '')).lower().strip() for v in allowed_values]
                                val_names_set = set(val_names)
                                if val_names_set in [{'sí', 'no'}, {'si', 'no'}, {'yes', 'no'}, {'true', 'false'}]:
                                    is_boolean_list = True
                            
                            if is_boolean_list:
                                # Tratar como booleano
                                boolean_value = None
                                if pub_attr.value_id:
                                    for val in allowed_values:
                                        if str(val.get('id')) == str(pub_attr.value_id):
                                            val_name = str(val.get('name', '')).lower().strip()
                                            boolean_value = val_name in ['sí', 'si', 'yes', 'true', '1']
                                            break
                                elif pub_attr.value_name:
                                    value_name_lower = str(pub_attr.value_name).lower().strip()
                                    boolean_value = value_name_lower in ['sí', 'si', 'yes', 'true', '1']
                                
                                if boolean_value is not None:
                                    # IMPORTANTE: Recrear dict limpio SOLO con value_boolean, sin value_id ni value_name
                                    fallback_dict = {
                                        "id": attr_id,
                                        "value_boolean": boolean_value
                                    }
                                else:
                                    continue
                            else:
                                # Lista normal
                                if pub_attr.value_id:
                                    fallback_dict["value_id"] = str(pub_attr.value_id)
                                elif pub_attr.value_name:
                                    # Intentar buscar el ID desde allowed_values
                                    if allowed_values:
                                        for val in allowed_values:
                                            if str(val.get('name', '')).lower() == str(pub_attr.value_name).lower():
                                                fallback_dict["value_id"] = str(val.get('id'))
                                                break
                                        if "value_id" not in fallback_dict:
                                            continue
                                    else:
                                        continue
                                else:
                                    continue
                        
                        elif ml_value_type in ["boolean", "boolean_radio"]:
                            # IMPORTANTE: Usar value_boolean en lugar de value_id
                            boolean_value = None
                            if pub_attr.value_id:
                                # Mapear IDs de ML a boolean
                                if str(pub_attr.value_id) == '242084':
                                    boolean_value = True
                                elif str(pub_attr.value_id) == '242085':
                                    boolean_value = False
                            elif pub_attr.value_name:
                                value_name_lower = str(pub_attr.value_name).lower().strip()
                                boolean_value = value_name_lower in ['sí', 'si', 'yes', 'true', '1']
                            
                            if boolean_value is not None:
                                # IMPORTANTE: Recrear dict limpio SOLO con value_boolean, sin value_id ni value_name
                                fallback_dict = {
                                    "id": attr_id,
                                    "value_boolean": boolean_value
                                }
                            else:
                                continue
                        
                        elif ml_value_type in ["picture", "file"]:
                            if pub_attr.value_id:
                                fallback_dict["value_id"] = str(pub_attr.value_id)
                            else:
                                continue
                        
                        else:
                            # Tipo desconocido, intentar con value_name
                            if pub_attr.value_name:
                                fallback_dict["value_name"] = str(pub_attr.value_name).strip()
                            else:
                                continue
                        
                        # Si llegamos aquí, tenemos un formato válido
                        ml_format_dict = fallback_dict
                        if validation_warnings:
                            warnings_dict[attr_id] = validation_warnings
                        _logger.debug("   ✅ Atributo %s exportado usando formato del JSON", attr_id)
                    
                    except Exception as e:
                        _logger.debug("   ❌ Error construyendo formato para %s: %s", attr_id, str(e))
                        # Si es requerido, registrar error crítico
                        if attr_def.get('required') or attr_def.get('catalog_required'):
                            errors_list.append({
                                'attr_id': attr_id,
                                'attr_name': pub_attr.name,
                                'reason': validation_warnings[0] if validation_warnings else "Valor inválido",
                                'is_required': True,
                            })
                        continue
                else:
                    # No tiene valor - solo registrar error si es requerido
                    if attr_def.get('required') or attr_def.get('catalog_required'):
                        errors_list.append({
                            'attr_id': attr_id,
                            'attr_name': pub_attr.name,
                            'reason': validation_warnings[0] if validation_warnings else "Valor inválido",
                            'is_required': True,
                        })
                    continue
            
            # Atributo válido - agregar al payload
            attributes_list.append(ml_format_dict)
            if validation_warnings:
                warnings_dict[attr_id] = validation_warnings
        
        return attributes_list, warnings_dict, errors_list

    @api.model
    def _is_catalog_required(self, tags_value):
        """
        Verifica si un atributo es catalog_required según sus tags.
        
        :param tags_value: Puede ser dict, string JSON, boolean, None, etc.
        :return: bool
        """
        validator = self.env['ml.attribute.validator']
        tags = validator.parse_tags(tags_value)
        return tags.get('catalog_required', False) or False

    @api.model
    def _is_conditional_required(self, tags_value):
        """
        Verifica si un atributo es conditional_required según sus tags.
        
        :param tags_value: Puede ser dict, string JSON, boolean, None, etc.
        :return: bool
        """
        validator = self.env['ml.attribute.validator']
        tags = validator.parse_tags(tags_value)
        return tags.get('conditional_required', False) or False

    @api.model
    def handle_ml_response_errors(self, ml_response, publication):
        """
        Maneja errores de respuesta de ML de forma inteligente.
        
        Analiza los errores/warnings de ML y determina si deben bloquear la publicación
        o solo registrar warnings. Genera mensajes claros y accionables para el usuario.
        
        :param ml_response: Respuesta de ML (dict con 'cause' o string JSON)
        :param publication: Record de ml.publication
        :return: (should_retry, cleaned_attributes, warnings, errors)
        """
        import json
        import re
        
        # Parsear respuesta
        if isinstance(ml_response, str):
            try:
                response_data = json.loads(ml_response)
            except Exception:
                response_data = {"message": ml_response}
        else:
            response_data = ml_response
        
        causes = response_data.get('cause', [])
        if not causes:
            return False, None, [], []
        
        warnings = []
        errors = []
        attributes_to_remove = set()
        
        # Diccionario de traducción de códigos de error a mensajes claros
        error_translations = {
            "invalid.fashion_grid.grid_id.values": {
                "title": "Atributo de Talla Inválido",
                "message": "El atributo SIZE_GRID_ID (Talla) tiene un valor que no está permitido para esta categoría específica. El valor seleccionado no coincide con los valores permitidos por Mercado Libre para esta categoría.",
                "solution": "1. Vaya a la sección 'Atributos de Mercado Libre' en la publicación.\n2. Busque el atributo 'SIZE_GRID_ID' (Talla).\n3. Elimine el valor actual y seleccione uno nuevo de la lista desplegable.\n4. Si no hay valores disponibles en la lista, actualice los atributos de la categoría desde 'Mercado Libre > Categorías'.\n5. Si el problema persiste después de actualizar, el atributo puede no ser válido para esta categoría específica - elimínelo temporalmente.",
                "is_blocking": True,
            },
            "delete.item.sale_terms.manufacturing_time": {
                "title": "Término de Venta No Permitido",
                "message": "El término de venta 'Tiempo de fabricación' (MANUFACTURING_TIME) no está permitido para productos usados.",
                "solution": "Si el producto está marcado como 'Usado', elimine cualquier término de venta relacionado con tiempo de fabricación.",
                "is_blocking": False,
            },
            "item.attribute.dropped": {
                "title": "Atributo Eliminado",
                "message": "Un atributo fue eliminado porque su valor es inválido o no aplica para esta categoría.",
                "solution": "Revise los atributos de la publicación y elimine los que tengan valores inválidos.",
                "is_blocking": False,
            },
            "item.attribute.value_name.invalid": {
                "title": "Valor de Atributo Inválido",
                "message": "Un atributo tiene un valor que no es válido para esta categoría.",
                "solution": "Revise los atributos y seleccione valores válidos de las listas desplegables.",
                "is_blocking": True,
            },
            "item.attribute.value_id.invalid": {
                "title": "Valor de Atributo Inválido",
                "message": "Un atributo tiene un valor ID que no es válido para esta categoría.",
                "solution": "Revise los atributos y seleccione valores válidos de las listas desplegables.",
                "is_blocking": True,
            },
            "item.attribute.invalid": {
                "title": "Atributo Inválido",
                "message": "Un atributo no es válido para esta categoría.",
                "solution": "Revise los atributos y elimine los que no correspondan a esta categoría.",
                "is_blocking": True,
            },
            "item.attributes.missing_required": {
                "title": "Atributos Requeridos Faltantes",
                "message": "Faltan atributos obligatorios para esta categoría.",
                "solution": "Complete todos los atributos marcados como requeridos en la sección 'Atributos de Mercado Libre'.",
                "is_blocking": True,
            },
            "item.attributes.invalid": {
                "title": "Atributos Inválidos",
                "message": "Hay atributos con valores inválidos o que no aplican para esta categoría.",
                "solution": "Revise todos los atributos y asegúrese de que los valores sean válidos para la categoría seleccionada.",
                "is_blocking": True,
            },
            "shipping.lost_me1_by_user": {
                "title": "Modo de Envío No Disponible",
                "message": "El modo de envío 'me1' no está disponible para su cuenta.",
                "solution": "Configure los modos de envío disponibles en su cuenta de Mercado Libre.",
                "is_blocking": False,
            },
        }
        
        for cause in causes:
            code = cause.get('code', '')
            message = cause.get('message', '')
            attr_id = None
            attr_name = None
            
            # =====================================================
            # MEJORADA: Extracción del atributo desde múltiples fuentes
            # =====================================================
            
            # 1. Intentar extraer desde references
            references = cause.get('references', [])
            for ref in references:
                if isinstance(ref, dict):
                    # Buscar en diferentes campos de references
                    if 'attribute_id' in ref:
                        attr_id = ref.get('attribute_id')
                    elif 'id' in ref and ('attribute' in str(ref).lower() or 'attr' in str(ref).lower()):
                        attr_id = ref.get('id')
                    elif 'field' in ref and 'attribute' in str(ref.get('field', '')).lower():
                        attr_id = ref.get('field')
                elif isinstance(ref, str):
                    # Si es string, puede ser el ID directamente
                    if ref and len(ref) < 50:  # IDs de atributos suelen ser cortos
                        attr_id = ref
            
            # 2. Intentar extraer desde el mensaje (formato [ATTR_ID])
            if not attr_id and '[' in message and ']' in message:
                match = re.search(r'\[([^\]]+)\]', message)
                if match:
                    potential_attr_id = match.group(1).strip()
                    # Validar que parezca un ID de atributo (no muy largo, sin espacios)
                    if potential_attr_id and len(potential_attr_id) < 50 and ' ' not in potential_attr_id:
                        attr_id = potential_attr_id
            
            # 3. Intentar extraer desde el mensaje (buscar IDs conocidos de atributos)
            if not attr_id:
                # Buscar patrones comunes en mensajes de ML
                # Ejemplo: "attribute BRAND is invalid" o "BRAND value is invalid"
                attr_patterns = [
                    r'attribute\s+([A-Z_][A-Z0-9_]*)\s+',
                    r'([A-Z_][A-Z0-9_]*)\s+value',
                    r'([A-Z_][A-Z0-9_]*)\s+is\s+invalid',
                    r'invalid\s+([A-Z_][A-Z0-9_]*)',
                ]
                for pattern in attr_patterns:
                    match = re.search(pattern, message, re.IGNORECASE)
                    if match:
                        potential_attr_id = match.group(1).strip()
                        # Verificar que existe en la publicación
                        if publication.ml_attribute_ids.filtered(lambda a: a.ml_attribute_id == potential_attr_id):
                            attr_id = potential_attr_id
                            break
            
            # 4. Si aún no tenemos attr_id, buscar en todos los atributos de la publicación
            # y ver cuál tiene valores que podrían ser inválidos
            if not attr_id and code in ['item.attribute.value_name.invalid', 'item.attribute.value_id.invalid', 'item.attribute.invalid']:
                # Buscar atributos con valores que podrían ser problemáticos
                for pub_attr in publication.ml_attribute_ids:
                    # Si tiene valor pero no está en allowed_values, podría ser el problema
                    if pub_attr.value_id or pub_attr.value_name:
                        # Verificar si el atributo tiene allowed_values pero el valor no está en la lista
                        if pub_attr.has_allowed_values and pub_attr.allowed_values:
                            import json
                            try:
                                allowed_vals = json.loads(pub_attr.allowed_values)
                                if pub_attr.value_id:
                                    # Verificar si el value_id está en allowed_values
                                    value_found = any(str(v.get('id')) == str(pub_attr.value_id) for v in allowed_vals)
                                    if not value_found:
                                        attr_id = pub_attr.ml_attribute_id
                                        break
                            except Exception:
                                pass
            
            # Si encontramos un attr_id, buscar su nombre en la publicación
            if attr_id:
                pub_attr = publication.ml_attribute_ids.filtered(
                    lambda a: a.ml_attribute_id == attr_id
                )
                if pub_attr:
                    attr_name = pub_attr[0].name or attr_id
                    # Agregar información del valor actual si está disponible
                    value_info = []
                    if pub_attr[0].value_id:
                        value_info.append(f"Valor ID: {pub_attr[0].value_id}")
                    if pub_attr[0].value_name:
                        value_info.append(f"Valor: {pub_attr[0].value_name}")
                    if value_info:
                        attr_name += f" ({', '.join(value_info)})"
                else:
                    attr_name = attr_id
            
            # Obtener traducción del error
            error_info = error_translations.get(code, {})
            
            if error_info:
                is_blocking = error_info.get('is_blocking', True)
                
                # Mejorar el mensaje si tenemos información del atributo
                error_message = error_info.get('message', message)
                if attr_name:
                    # Personalizar el mensaje con el nombre del atributo
                    if 'Un atributo' in error_message or 'un atributo' in error_message:
                        error_message = error_message.replace('Un atributo', f"El atributo '{attr_name}'")
                        error_message = error_message.replace('un atributo', f"el atributo '{attr_name}'")
                
                error_dict = {
                    'code': code,
                    'title': error_info.get('title', 'Error'),
                    'message': error_message,
                    'solution': error_info.get('solution', 'Revise la configuración de la publicación.'),
                    'attr_id': attr_id,
                    'attr_name': attr_name,
                    'original_message': message,
                }
                
                if is_blocking:
                    errors.append(error_dict)
                else:
                    warnings.append(error_dict)
                    if attr_id:
                        attributes_to_remove.add(attr_id)
            else:
                # Código desconocido - intentar extraer información útil
                error_dict = {
                    'code': code,
                    'title': 'Error de Validación',
                    'message': message,
                    'solution': 'Revise la configuración de la publicación y los atributos.',
                    'attr_id': attr_id,
                    'attr_name': attr_name,
                    'original_message': message,
                }
                
                # Por defecto, tratar como error bloqueante si contiene "invalid" o "missing"
                if 'invalid' in code.lower() or 'missing' in code.lower() or 'required' in code.lower():
                    errors.append(error_dict)
                else:
                    warnings.append(error_dict)
                    if attr_id:
                        attributes_to_remove.add(attr_id)
        
        # Si hay errores bloqueantes, no retry
        should_retry = len(errors) == 0
        
        return should_retry, list(attributes_to_remove), warnings, errors

