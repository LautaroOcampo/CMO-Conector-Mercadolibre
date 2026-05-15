# -*- coding: utf-8 -*-
"""
Herramienta para analizar atributos de Mercado Libre y generar una tabla
con únicamente los atributos que el usuario puede modificar directamente.
"""

import json


def analyze_customizable_attributes(attributes_json):
    """
    Analiza un JSON de atributos de Mercado Libre y genera una tabla/lista
    con únicamente los atributos que el usuario puede modificar directamente.
    
    :param attributes_json: Lista de atributos en formato JSON de ML API (puede ser string o list)
    :return: Lista de diccionarios con información formateada de atributos modificables
    """
    # Parsear JSON si es string
    if isinstance(attributes_json, str):
        try:
            attributes_json = json.loads(attributes_json)
        except Exception:
            raise ValueError("El JSON proporcionado no es válido")
    
    if not isinstance(attributes_json, list):
        raise ValueError("El JSON debe ser una lista de atributos")
    
    customizable_attributes = []
    
    for attr in attributes_json:
        attr_id = attr.get('id')
        if not attr_id:
            continue
        
        # Obtener tags
        tags = attr.get('tags', {})
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = {}
        
        # EXCLUIR si tiene read_only o hidden
        if tags.get('read_only', False) or tags.get('hidden', False):
            continue
        
        # Obtener información básica
        attr_name = attr.get('name', attr_id)
        value_type = attr.get('value_type', 'string')
        
        # Determinar si es requerido
        is_required = tags.get('required', False) or tags.get('catalog_required', False)
        is_conditional = tags.get('conditional_required', False)
        
        requerido = "No"
        if is_required:
            requerido = "Sí"
        elif is_conditional:
            requerido = "Condicional"
        
        # Preparar información adicional según el tipo
        info_adicional = ""
        
        # Para number_unit: mostrar unidades permitidas
        if value_type == 'number_unit':
            allowed_units = attr.get('allowed_units', [])
            if allowed_units:
                unit_names = [u.get('name', u.get('id', '')) for u in allowed_units]
                info_adicional = f"Unidades permitidas: {', '.join(unit_names)}"
            default_unit = attr.get('default_unit')
            if default_unit:
                if info_adicional:
                    info_adicional += f" | Unidad por defecto: {default_unit}"
                else:
                    info_adicional = f"Unidad por defecto: {default_unit}"
        
        # Para list: mostrar opciones disponibles
        elif value_type in ['list', 'value_id']:
            values = attr.get('values', [])
            if values:
                # Mostrar primeros 5 valores como ejemplo
                value_names = [v.get('name', v.get('id', '')) for v in values[:5]]
                info_adicional = f"Opciones disponibles: {', '.join(value_names)}"
                if len(values) > 5:
                    info_adicional += f" ... y {len(values) - 5} más"
        
        # Para GTIN: indicar que es multivalued
        if attr_id == 'GTIN' and tags.get('multivalued', False):
            if info_adicional:
                info_adicional += " | "
            info_adicional += "Acepta múltiples valores"
        
        # Agregar a la lista
        customizable_attributes.append({
            'id': attr_id,
            'nombre': attr_name,
            'requerido': requerido,
            'tipo': value_type,
            'info_adicional': info_adicional,
        })
    
    return customizable_attributes


def format_as_table(customizable_attributes):
    """
    Formatea la lista de atributos como una tabla legible.
    
    :param customizable_attributes: Lista de diccionarios con atributos
    :return: String formateado como tabla
    """
    if not customizable_attributes:
        return "No se encontraron atributos personalizables."
    
    # Agrupar por requerido
    requeridos = [a for a in customizable_attributes if a['requerido'] == 'Sí']
    condicionales = [a for a in customizable_attributes if a['requerido'] == 'Condicional']
    opcionales = [a for a in customizable_attributes if a['requerido'] == 'No']
    
    output = []
    output.append("=" * 100)
    output.append("ATRIBUTOS PERSONALIZABLES DE MERCADO LIBRE")
    output.append("=" * 100)
    output.append("")
    
    if requeridos:
        output.append("🔴 ATRIBUTOS REQUERIDOS:")
        output.append("-" * 100)
        for attr in requeridos:
            output.append(f"  • {attr['nombre']} ({attr['id']})")
            output.append(f"    Tipo: {attr['tipo']}")
            if attr['info_adicional']:
                output.append(f"    {attr['info_adicional']}")
            output.append("")
    
    if condicionales:
        output.append("🟡 ATRIBUTOS CONDICIONALMENTE REQUERIDOS:")
        output.append("-" * 100)
        for attr in condicionales:
            output.append(f"  • {attr['nombre']} ({attr['id']})")
            output.append(f"    Tipo: {attr['tipo']}")
            if attr['info_adicional']:
                output.append(f"    {attr['info_adicional']}")
            output.append("")
    
    if opcionales:
        output.append("🟢 ATRIBUTOS OPCIONALES:")
        output.append("-" * 100)
        for attr in opcionales:
            output.append(f"  • {attr['nombre']} ({attr['id']})")
            output.append(f"    Tipo: {attr['tipo']}")
            if attr['info_adicional']:
                output.append(f"    {attr['info_adicional']}")
            output.append("")
    
    output.append("")
    output.append(f"📊 RESUMEN:")
    output.append(f"   Total: {len(customizable_attributes)} atributos personalizables")
    output.append(f"   - Requeridos: {len(requeridos)}")
    output.append(f"   - Condicionales: {len(condicionales)}")
    output.append(f"   - Opcionales: {len(opcionales)}")
    output.append("=" * 100)
    
    return "\n".join(output)


if __name__ == "__main__":
    # Ejemplo de uso
    import sys
    
    if len(sys.argv) > 1:
        # Leer JSON desde archivo
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            attributes_json = json.load(f)
    else:
        # Ejemplo con JSON inline
        attributes_json = []
        print("Por favor, proporcione un archivo JSON o use el método desde Odoo")
        sys.exit(1)
    
    # Analizar
    customizable = analyze_customizable_attributes(attributes_json)
    
    # Formatear y mostrar
    print(format_as_table(customizable))

