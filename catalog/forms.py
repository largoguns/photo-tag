from django import forms

from catalog.models import CatalogSettings, EventType, Person


class EventTypeForm(forms.ModelForm):
    class Meta:
        model = EventType
        fields = ["name", "order", "active"]


class ThresholdsForm(forms.ModelForm):
    class Meta:
        model = CatalogSettings
        fields = ["vote_min_approvals", "vote_approval_ratio"]
        help_texts = {
            "vote_min_approvals": (
                "Votos a favor necesarios para aceptar (o en contra para rechazar)."
            ),
            "vote_approval_ratio": "Proporción mínima de votos a favor, entre 0 y 1.",
        }

    def clean_vote_min_approvals(self):
        value = self.cleaned_data["vote_min_approvals"]
        if value < 1:
            raise forms.ValidationError("Debe ser al menos 1.")
        return value

    def clean_vote_approval_ratio(self):
        value = self.cleaned_data["vote_approval_ratio"]
        if not 0 < value <= 1:
            raise forms.ValidationError("Debe estar entre 0 y 1.")
        return value


class MergePersonsForm(forms.Form):
    source = forms.ModelChoiceField(
        label="Persona a fusionar (desaparece)", queryset=Person.objects.none()
    )
    target = forms.ModelChoiceField(
        label="Fusionar en (se conserva)", queryset=Person.objects.none()
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        people = Person.objects.active().order_by("display_name")
        self.fields["source"].queryset = people
        self.fields["target"].queryset = people

    def clean(self):
        data = super().clean()
        if data.get("source") and data.get("source") == data.get("target"):
            raise forms.ValidationError("Elige dos personas distintas.")
        return data
