from django import forms

from notifications.models import NotificationSettings


class NotificationSettingsForm(forms.ModelForm):
    class Meta:
        model = NotificationSettings
        fields = ["new_photos", "pending_votes", "resolved"]
