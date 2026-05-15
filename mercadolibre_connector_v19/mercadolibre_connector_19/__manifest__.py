# -*- coding: utf-8 -*-
{
    'name': "Conector Mercado Libre",

    'summary': "Sincroniza publicaciones, ventas y stock entre Odoo y Mercado Libre",

    'description': """
Conector Mercado Libre (Meli Connector)
========================================

Integración con la API de Mercado Libre para:

* Gestionar cuentas y autorización OAuth 2.0
* Importar y publicar productos en Mercado Libre
* Sincronizar precios y stock (manual o automático)
* Recibir ventas en tiempo real vía webhook e importar órdenes
* Reglas automáticas de pausa/activación por stock
* Facturación automática de ventas ML
* Matcheo de publicaciones con productos Odoo por SKU
    """,

    'author': "Galarreta",
    'website': "https://galarreta.co",

    'category': 'Sales',
    'version': '19.0.1.0.0',

    'depends': ['base', 'mail', 'product', 'sale', 'sale_channel_origin', 'stock', 'account'],

    'data': [
        'security/ml_security.xml',
        'security/ir.model.access.csv',
        'data/cron_data.xml',
        'views/ml_wizard_views.xml',
        'views/ml_publication_values_wizard_views.xml',
        'views/ml_sale_views.xml',
        'views/menu_views.xml',
        'views/ml_account_views.xml',
        'views/ml_test_user_views.xml',
        'views/product_template_views.xml',
        'views/ml_sync_log_views.xml',
        'views/ml_publication.xml',
        'views/ml_import_orders_wizard_views.xml',
        'views/ml_webhook_notification_views.xml',
        'views/sale_order_views.xml',
    ],

    'demo': [
        'demo/demo.xml',
    ],

    'installable': True,
    'application': True,
    'license': 'LGPL-3',

    'post_init_hook': 'post_init_hook',
}
