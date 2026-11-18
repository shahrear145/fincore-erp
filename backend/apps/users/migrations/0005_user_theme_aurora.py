# Users who never explicitly picked a theme just carry the old "premium"
# default. Move them onto the new "aurora" default so the redesigned UI is
# what they see. Anyone who later picks a theme from the picker keeps it.
from django.db import migrations, models


def forwards(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.filter(theme="premium").update(theme="aurora")


def backwards(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.filter(theme="aurora").update(theme="premium")


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_user_theme"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="theme",
            field=models.CharField(max_length=32, default="aurora", db_index=True),
        ),
        migrations.RunPython(forwards, backwards),
    ]
