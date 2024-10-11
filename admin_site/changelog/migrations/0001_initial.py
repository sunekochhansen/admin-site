# Generated by Django 4.2.1 on 2023-08-24 16:19

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import markdownx.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Changelog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("title", models.CharField(max_length=100, verbose_name="title")),
                (
                    "description",
                    models.TextField(max_length=240, verbose_name="description"),
                ),
                ("content", markdownx.models.MarkdownxField(verbose_name="content")),
                (
                    "created",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="created"
                    ),
                ),
                (
                    "updated",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="updated"
                    ),
                ),
                ("author", models.CharField(max_length=255, verbose_name="author")),
                ("version", models.CharField(max_length=255, verbose_name="version")),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
        migrations.CreateModel(
            name="ChangelogTag",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255, verbose_name="name")),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="ChangelogComment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("content", models.TextField(max_length=240, verbose_name="content")),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "changelog",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="changelog.changelog",
                    ),
                ),
                (
                    "parent_comment",
                    models.ForeignKey(
                        blank=True,
                        default=None,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comment_children",
                        to="changelog.changelogcomment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["created"],
            },
        ),
        migrations.AddField(
            model_name="changelog",
            name="tags",
            field=models.ManyToManyField(
                blank=True, related_name="changelogs", to="changelog.changelogtag"
            ),
        ),
    ]