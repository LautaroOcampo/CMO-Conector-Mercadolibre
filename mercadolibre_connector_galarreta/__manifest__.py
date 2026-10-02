# -*- coding: utf-8 -*-
{
    'name': "Conector Mercado Libre",

    'summary': "Sync listings, sales and stock between Odoo and Mercado Libre",

    'description': """
Mercado Libre Connector
=======================

Connect Odoo with the Mercado Libre API.

Requires an active Mercado Libre developer application and seller account
(external service). Authorization uses OAuth 2.0; order, product and stock
data are exchanged with Mercado Libre servers.

Features:

* Manage Mercado Libre accounts and OAuth 2.0 authorization
* Import and publish products on Mercado Libre
* Sync prices and stock (manual or automatic)
* Receive sales in real time via webhooks and import orders
* Automatic pause/activation rules based on stock
* Automatic invoicing of Mercado Libre sales
* Match listings with Odoo products by SKU
* Register Mercado Libre selling fees as a separate accounting entry
    """,

    'author': "Galarreta",
    'website': "https://galarreta.co",
    'support': "alvaro@galarreta.co",

    'category': 'Sales',
    'version': '19.0.1.0.68',

    'depends': ['base', 'mail', 'product', 'sale', 'stock', 'account'],

    'data': [
        'security/ml_security.xml',
        'security/ir.model.access.csv',
        'security/ml_retire_manager.xml',
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
        'views/ml_cleanup_unpaid_sales_wizard_views.xml',
        'views/ml_webhook_notification_views.xml',
        'views/sale_order_views.xml',
        'views/account_move_views.xml',
        'views/report_invoice_templates.xml',
    ],

    'demo': [
        'demo/demo.xml',
    ],

    'images': [
        'static/description/cover_screenshot.png',
    ],
    'price': 250.00,
    'currency': 'USD',

    'installable': True,
    'application': True,
    'license': 'LGPL-3',

    'post_init_hook': 'post_init_hook',
}
