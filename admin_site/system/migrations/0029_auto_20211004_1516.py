# Generated by Django 3.1.9 on 2021-10-04 15:16

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("system", "0028_auto_20210930_1146"),
    ]

    operations = [
        migrations.RenameField(
            model_name="pc",
            old_name="is_active",
            new_name="is_activated",
        ),
    ]
