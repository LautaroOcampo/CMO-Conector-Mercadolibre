# Partes del Código que Manejan Atributos Numéricos y Booleanos

## 📍 Archivos Clave

### 1. `ml_attribute_validator.py` - Validación y Formato

**Líneas 399-422: Validación de Atributos Numéricos (`number`)**
```python
elif value_type == "number":
    # Verificar que value_number tenga un valor válido
    if publication_attr.value_number is None:
        # Fallback: intentar obtener desde value_name
        if publication_attr.value_name:
            num_value = float(publication_attr.value_name)
            publication_attr.value_number = num_value
    
    is_valid, formatted_value, warning = MLAttributeValidator.validate_number(
        publication_attr.value_number, attr_def
    )
    
    # IMPORTANTE: Atributos numéricos SIEMPRE usan value_number
    result["value_number"] = formatted_value
```

**Líneas 424-485: Validación de Atributos Numéricos con Unidad (`number_unit`)**
```python
elif value_type == "number_unit":
    # Similar a number pero también valida la unidad
    # Resultado: result["value_struct"] = {"number": ..., "unit": ...}
```

**Líneas 583-621: Validación de Atributos Booleanos**
```python
elif value_type in ["boolean", "boolean_radio"]:
    # Obtener valor booleano desde value_id, value_option_id o value_name
    boolean_value = None
    
    # 1. Desde value_id (242084=Sí, 242085=No)
    if publication_attr.value_id:
        if value_id_str in ['242084', '242085']:
            boolean_value = (value_id_str == '242084')
    
    # 2. Desde value_option_id
    # 3. Desde value_name
    
    # IMPORTANTE: Usar value_boolean en lugar de value_id
    result["value_boolean"] = boolean_value
```

**Líneas 487-516: Detección de Booleanos Disfrazados de List**
```python
elif value_type in ["list", "value_id"]:
    # Detectar si es booleano disfrazado de list (Sí/No)
    allowed_values = MLAttributeValidator.parse_allowed_values(...)
    is_boolean_list = False
    if allowed_values and len(allowed_values) == 2:
        val_names_set = set(val_names)
        if val_names_set in [{'sí', 'no'}, {'si', 'no'}, ...]:
            is_boolean_list = True
    
    if is_boolean_list:
        result["value_boolean"] = boolean_value
        return True, result, []
```

### 2. `ml_payload_builder.py` - Construcción del Payload

**Líneas 188-199: Validación y Debug**
```python
# Validar atributo usando la información del JSON importado
is_valid, ml_format_dict, validation_warnings = validator.validate_attribute(
    pub_attr, attr_def, has_variations
)

# DEBUG: Log para atributos numéricos y booleanos
if pub_attr.value_type in ['number', 'number_unit', 'boolean']:
    _logger.debug("🔍 Atributo %s: value_type=%s, value_number=%s, ...", ...)
```

**Líneas 201-360: Fallback si la Validación Falla**
```python
if not is_valid:
    # Intentar exportar de todas formas si tiene algún valor
    if has_any_value:
        ml_value_type = attr_def.get('value_type', 'string')
        fallback_dict = {"id": attr_id}
        
        if ml_value_type == "number":
            # IMPORTANTE: Atributos numéricos SIEMPRE usan value_number
            if pub_attr.value_number is not None:
                fallback_dict["value_number"] = float(pub_attr.value_number)
            elif pub_attr.value_name:
                # Fallback: convertir value_name a número
                fallback_dict["value_number"] = float(pub_attr.value_name)
        
        elif ml_value_type in ["boolean", "boolean_radio"]:
            # IMPORTANTE: Usar value_boolean en lugar de value_id
            boolean_value = ...
            fallback_dict["value_boolean"] = boolean_value
```

**Líneas 142, 158: Detección del Tipo Real de ML**
```python
# Usar attribute_type del JSON importado (tipo real de ML)
'value_type': cat_attr.attribute_type or cat_attr.value_type

# Si attribute_type está disponible, usarlo directamente
ml_value_type = pub_attr.attribute_type or pub_attr.value_type
```

### 3. `ml_publication_attribute.py` - Modelo de Atributos

**Líneas 85-88: Campo `value_number`**
```python
value_number = fields.Float(
    string="Valor Numérico",
    help="Valor numérico (si value_type es 'number' o 'number_unit')",
)
```

**Líneas 146-149: Campo `attribute_type`**
```python
attribute_type = fields.Char(
    string="Tipo de Atributo",
    help="Tipo del atributo según ML (string, number, boolean, list, etc.)",
)
```

**Líneas 50-57: Campo `value_type` (mapeo a Odoo)**
```python
value_type = fields.Selection([
    ('value_id', 'Valor Predefinido'),
    ('value_name', 'Texto Libre'),
    ('boolean', 'Booleano'),
    ('number', 'Número'),
    ('number_unit', 'Número con Unidad'),
    ('picture', 'Imagen/Archivo'),
], string="Tipo de Valor", default='value_name')
```

**Líneas 462-514: Creación desde API de ML**
```python
def create_from_ml_attribute(self, publication_id, ml_attr_data):
    value_type_ml = ml_attr_data.get("value_type", "string")
    
    # Determinar el tipo de valor en Odoo
    if value_type_ml in ["boolean", "boolean_radio"]:
        value_type = "boolean"
    elif value_type_ml in ["number", "number_unit"]:
        value_type = "number" if value_type_ml == "number" else "number_unit"
    
    # Guardar attribute_type (tipo real de ML)
    "attribute_type": value_type_ml,
```

### 4. `ml_publication.py` - Importación desde ML

**Líneas 4292-4313: Extracción de Valores desde ML**
```python
def _extract_attribute_value_from_ml(self, ml_attr, value_type, ...):
    if value_type == 'boolean':
        # IMPORTANTE: Leer value_boolean si está disponible
        value_boolean = ml_attr.get('value_boolean')
        if value_boolean is not None:
            result['value_boolean'] = bool(value_boolean)
            result['value_id'] = '242084' if value_boolean else '242085'
    
    elif value_type == 'number':
        value_number = ml_attr.get('value_number')
        if value_number is not None:
            result['value_number'] = float(value_number)
```

## 🔍 Puntos Críticos a Verificar

### 1. **Detección del Tipo (`value_type` vs `attribute_type`)**
- `attribute_type`: Tipo real de ML (string, number, boolean, list, number_unit)
- `value_type`: Mapeo a Odoo (value_name, value_id, number, number_unit, boolean)
- **Problema potencial**: Si `attribute_type` no está guardado, el sistema podría usar `value_type` incorrectamente

### 2. **Guardado de Valores**
- **Numéricos**: Deben guardarse en `value_number` (Float)
- **Booleanos**: Deben guardarse en `value_id` (242084/242085) o `value_option_id`, pero exportarse como `value_boolean`
- **Problema potencial**: Si el usuario ingresa valores en `value_name` en lugar de `value_number`, el sistema intenta convertirlos pero podría fallar

### 3. **Validación**
- La validación en `validate_attribute()` debería detectar correctamente el tipo
- Si falla, el fallback en `build_attributes_payload()` intenta construir el payload manualmente
- **Problema potencial**: Si ambos fallan, el atributo no se exporta

## 🐛 Posibles Causas del Problema

1. **`attribute_type` no está guardado**: Los atributos no tienen `attribute_type` cuando se crean, entonces el sistema usa `value_type` de Odoo que podría ser incorrecto.

2. **Valores no se guardan en campos correctos**: 
   - Numéricos: Usuario ingresa en `value_name` pero no en `value_number`
   - Booleanos: Usuario selecciona `value_option_id` pero `value_id` no se sincroniza

3. **Detección de tipo falla**: El sistema no detecta correctamente que un atributo es `number` o `boolean`, entonces lo trata como `string` o `list`.

4. **Validación estricta falla**: La validación es muy estricta y rechaza valores válidos, entonces cae en el fallback que también podría fallar.

## ✅ Solución Recomendada

1. **Verificar que `attribute_type` se guarde correctamente** cuando se importan atributos desde ML
2. **Asegurar que los valores se guarden en los campos correctos**:
   - Numéricos: `value_number` (no `value_name`)
   - Booleanos: `value_id` o `value_option_id` (no `value_name`)
3. **Mejorar la detección de tipo** usando `attribute_type` en lugar de `value_type` cuando esté disponible
4. **Agregar logs detallados** para ver qué está pasando durante la validación y construcción del payload

