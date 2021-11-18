# Generated by Django 3.1.9 on 2021-11-18 14:31

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0037_auto_20211118_1248'),
    ]

    operations = [
        migrations.AlterField(
            model_name='site',
            name='user_login_duration',
            field=models.DurationField(blank=True, default=datetime.timedelta(seconds=3600), help_text='Login duration when integrating with library login', null=True, verbose_name='Login duration'),
        ),
        migrations.AlterField(
            model_name='site',
            name='user_quarantine_duration',
            field=models.DurationField(blank=True, default=datetime.timedelta(seconds=14400), help_text='Quarantine period when integrating with library login', null=True, verbose_name='Quarantine duration'),
        ),
    ]
