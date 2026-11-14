from django import forms
from django.conf import settings
from django.contrib.auth.forms import SetPasswordForm

from accounts.models import Role, User


class NewPasswordForm(SetPasswordForm):
    """Password change that refuses to keep the generic initial password."""

    def clean_new_password1(self):
        password = self.cleaned_data.get("new_password1")
        if (
            password
            and settings.DEFAULT_USER_PASSWORD
            and password == settings.DEFAULT_USER_PASSWORD
        ):
            raise forms.ValidationError("La nueva contraseña no puede ser la contraseña genérica.")
        return password


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["display_name"]


class UserCreateForm(forms.Form):
    username = forms.RegexField(
        label="Nombre de usuario",
        max_length=150,
        regex=r"^[\w.@+-]+$",
        error_messages={"invalid": "Solo letras, números y los caracteres @ . + - _"},
    )
    display_name = forms.CharField(label="Nombre a mostrar", max_length=150, required=False)
    role = forms.ChoiceField(label="Rol", choices=Role.choices, initial=Role.COLLABORATOR)
    password = forms.CharField(
        label="Contraseña inicial",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Déjala vacía para usar la contraseña genérica.",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe un usuario con ese nombre.")
        return username


class UserEditForm(forms.Form):
    display_name = forms.CharField(label="Nombre a mostrar", max_length=150, required=False)
    role = forms.ChoiceField(label="Rol", choices=Role.choices)
    person = forms.ModelChoiceField(
        label="Persona vinculada",
        queryset=None,
        required=False,
        empty_label="— Ninguna —",
        help_text="«Este usuario soy yo en las fotos».",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from catalog.models import Person

        self.fields["person"].queryset = Person.objects.active().order_by("display_name")
