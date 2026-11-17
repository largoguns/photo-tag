# Relationships, honorifics and nicknames are saved directly, without voting.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0014_honorifics_nicknames'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(
            model_name='relationship',
            name='accepted_proposal',
        ),
        migrations.DeleteModel(
            name='PersonDetailVote',
        ),
        migrations.DeleteModel(
            name='PersonDetailProposal',
        ),
        migrations.DeleteModel(
            name='RelationshipVote',
        ),
        migrations.DeleteModel(
            name='RelationshipProposal',
        ),
        migrations.AddField(
            model_name='personnickname',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_nicknames', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='relationship',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_relationships', to=settings.AUTH_USER_MODEL),
        ),
    ]
