# Generated by Django 3.1.8 on 2021-07-13 15:49

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0018_set_os2_product_config_entry_for_pcs'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='script',
            options={'ordering': ['name']},
        ),
    ]
