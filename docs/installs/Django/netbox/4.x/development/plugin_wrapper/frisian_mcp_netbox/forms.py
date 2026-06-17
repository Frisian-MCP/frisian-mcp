from django import forms

from frisian_mcp.contrib.oauth.models import OAuthClient
from frisian_mcp.contrib.tokens.models import FrisianMcpToken


class OAuthClientForm(forms.ModelForm):
    # Shown as readonly on edit — client_id is editable=False on the model so
    # it never appears in ModelForm fields automatically.
    client_id_display = forms.CharField(
        label="Client ID",
        required=False,
        disabled=True,
        help_text="Public identifier — auto-generated at creation, never changes.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance and instance.pk:
            self.fields["client_id_display"].initial = instance.client_id
        else:
            # Hide on the create form — client_id doesn't exist yet
            self.fields.pop("client_id_display")

    class Meta:
        model = OAuthClient
        fields = ("name", "is_active", "permission", "user", "redirect_uris", "grant_types")
        help_texts = {
            "redirect_uris": (
                'JSON list of callback URLs. '
                'Example: ["https://claude.ai/api/mcp/auth_callback"]'
            ),
            "grant_types": (
                'JSON list of allowed grant types. Leave empty to allow all. '
                'Example: ["authorization_code"] or ["client_credentials"]'
            ),
        }
        widgets = {
            "redirect_uris": forms.Textarea(attrs={"rows": 3}),
            "grant_types": forms.Textarea(attrs={"rows": 2}),
        }


class FrisianMcpTokenForm(forms.ModelForm):
    class Meta:
        model = FrisianMcpToken
        fields = ("name", "is_active", "permission", "user")
