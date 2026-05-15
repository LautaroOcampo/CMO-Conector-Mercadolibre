# Refactorización del Sistema de Atributos de MercadoLibre

## 🎯 Objetivo

Refactorizar completamente el sistema de gestión de atributos para que sea robusto, escalable y alineado 1:1 con la API de MercadoLibre, eliminando errores como `AttributeError: 'bool' object has no attribute 'get'`.

## 🔧 Cambios Implementados

### 1. Modelo `ml.category.attribute` - Manejo Seguro de Tags

#### Problema Original
- El campo `tags` se guardaba como `fields.Text` (JSON string)
- En algunos casos se guardaba como `boolean` o se accedía directamente sin parsear
- Esto causaba `AttributeError: 'bool' object has no attribute 'get'`

#### Solución Implementada

**a) Métodos Helper Estáticos:**
```python
@staticmethod
def safe_parse_tags(tags_value):
    """
    Parsea tags de forma segura, siempre devolviendo un dict.
    Garantiza que siempre devuelve un dict, nunca None, boolean, o string.
    """
    # Maneja: dict, string JSON, boolean, None, o cualquier tipo
    # Siempre devuelve dict (nunca None)
```

**b) Campo Computed para Acceso Seguro:**
```python
tags_dict = fields.Json(
    compute="_compute_tags_dict",
    store=False,
    help="Tags parseados como diccionario. Siempre devuelve un dict."
)
```

**c) Campos Computed para Propiedades Comunes:**
- `is_required`
- `is_catalog_required`
- `is_conditional_required`
- `is_variation_attribute`
- `is_multivalued`
- `is_read_only`
- `is_hidden`
- `is_calculated`
- `is_fixed`
- `is_inferred`
- `allow_variations`

**d) Override de `write()` para Normalizar:**
- Normaliza `tags` antes de guardar
- Convierte boolean → dict vacío
- Valida y re-serializa JSON strings
- Garantiza formato consistente

### 2. Validador `ml.attribute.validator` - Método Mejorado

**Antes:**
```python
def parse_tags(tags_json):
    if isinstance(tags_json, str):
        return json.loads(tags_json)
    return tags_json or {}
```

**Después:**
```python
def parse_tags(tags_json):
    """
    Parsea tags desde JSON string, dict, boolean, o cualquier tipo.
    SIEMPRE devuelve un dict, nunca None, boolean, o string.
    """
    # Maneja todos los casos: dict, string, boolean, None, etc.
    # Siempre devuelve dict
```

### 3. Payload Builder - Uso Consistente

**Antes:**
```python
tags = cat_attr.tags  # Podía ser boolean, string, o dict
if tags.get('required'):  # ❌ Error si es boolean
```

**Después:**
```python
validator = self.env['ml.attribute.validator']
tags = validator.parse_tags(cat_attr.tags)  # ✅ Siempre dict
if tags.get('required'):  # ✅ Funciona siempre
```

### 4. Normalización en `create_from_ml_api()`

**Antes:**
```python
tags = ml_attr_data.get("tags", {})
tags_json = json.dumps(tags) if tags else None
```

**Después:**
```python
tags_raw = ml_attr_data.get("tags", {})
tags_dict = self.safe_parse_tags(tags_raw)  # ✅ Normaliza
tags_json = self.safe_serialize_tags(tags_dict)  # ✅ Serializa seguro
```

## 📍 Dónde Suele Romperse el Error

### Lugares Críticos Identificados:

1. **`ml_payload_builder.py` línea ~50, ~140**
   - Acceso directo a `cat_attr.tags` sin parsear
   - **Solución:** Usar `validator.parse_tags(cat_attr.tags)`

2. **`ml_category.py` línea ~143, ~404**
   - Parseo manual inconsistente
   - **Solución:** Usar `validator.parse_tags()`

3. **`ml_attribute_validator.py` línea ~71**
   - Acceso a `tags.get()` sin validar tipo
   - **Solución:** Método `parse_tags()` mejorado

4. **`ml_category_attribute.py` línea ~141**
   - Acceso directo a `ml_attr_data.get("tags", {})` sin validar
   - **Solución:** Usar `safe_parse_tags()`

## 🛡️ Validación Defensiva Implementada

### En Todos los Lugares:

1. **Parseo Seguro:**
   ```python
   tags = validator.parse_tags(any_value)  # ✅ Siempre dict
   ```

2. **Acceso Seguro:**
   ```python
   if tags.get('required'):  # ✅ Nunca falla
   ```

3. **Guardado Seguro:**
   ```python
   record.tags = safe_serialize_tags(tags_dict)  # ✅ Siempre JSON válido
   ```

## 🔄 Migración de Datos Existentes

### Script de Normalización (Ejecutar una vez):

```python
# En Odoo shell o método de migración
def normalize_tags_in_category_attributes():
    """Normaliza todos los tags existentes a formato JSON válido."""
    env = self.env
    cat_attrs = env['ml.category.attribute'].search([])
    
    for attr in cat_attrs:
        if attr.tags:
            # Parsear de forma segura
            tags_dict = env['ml.category.attribute'].safe_parse_tags(attr.tags)
            # Re-serializar para normalizar formato
            tags_json = env['ml.category.attribute'].safe_serialize_tags(tags_dict)
            if tags_json != attr.tags:
                attr.write({'tags': tags_json})
                _logger.info("✅ Normalizado tags para atributo %s (%s)", 
                           attr.id, attr.ml_attribute_id)
```

## ✅ Beneficios

1. **Eliminación de Errores:**
   - No más `AttributeError: 'bool' object has no attribute 'get'`
   - Manejo robusto de todos los tipos de datos

2. **Código Más Limpio:**
   - Un solo método para parsear tags
   - Campos computed para acceso fácil
   - Validación defensiva en todos los lugares

3. **Mantenibilidad:**
   - Cambios centralizados en métodos helper
   - Fácil de extender y depurar
   - Documentación clara

4. **Escalabilidad:**
   - Preparado para cambios futuros de ML
   - Fácil agregar nuevos campos computed
   - Estructura profesional

## 📝 Uso Recomendado

### Para Desarrolladores:

**✅ HACER:**
```python
# Usar el método seguro
tags = validator.parse_tags(record.tags)
if tags.get('required'):
    ...

# Usar campos computed
if record.is_required:
    ...

# Usar método de serialización
record.tags = record.safe_serialize_tags(tags_dict)
```

**❌ NO HACER:**
```python
# No acceder directamente
tags = record.tags  # ❌ Puede ser boolean
if tags.get('required'):  # ❌ Error

# No parsear manualmente
tags = json.loads(record.tags)  # ❌ Puede fallar
```

## 🚀 Próximos Pasos

1. ✅ Refactorización de `ml.category.attribute`
2. ✅ Mejora de `ml.attribute.validator`
3. ✅ Actualización de `ml.payload.builder`
4. ⏳ Migración de datos existentes (script proporcionado)
5. ⏳ Tests unitarios para validar normalización
6. ⏳ Documentación de API para desarrolladores

## 📚 Referencias

- API MercadoLibre: `/categories/{category_id}/attributes`
- Documentación Odoo: Campos computed y métodos helper
- Best Practices: Validación defensiva y manejo de errores

