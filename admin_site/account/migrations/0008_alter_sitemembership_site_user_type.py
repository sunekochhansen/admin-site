# Generated by Django 4.2.11 on 2024-05-29 14:40

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0007_userprofile_is_hidden"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sitemembership",
            name="site_user_type",
            field=models.IntegerField(
                choices=[(1, "Site User"), (2, "Site Admin"), (3, "Customer Admin")],
                default=1,
            ),
        ),
    ]