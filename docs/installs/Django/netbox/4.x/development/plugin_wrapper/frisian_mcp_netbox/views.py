from django.contrib import messages
from django.shortcuts import redirect, render
from netbox import object_actions
from netbox.views import generic

from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient
from frisian_mcp.contrib.tokens.models import FrisianMcpToken

from . import forms, tables


class _NoRestrictMixin:
    """Skip NetBox's restrict() call — frisian-mcp models use plain Django managers."""

    def has_permission(self):
        # ObjectPermissionRequiredMixin.has_permission() calls queryset.restrict() which does
        # not exist on plain Django managers.  Login requirement enforces access control here.
        return self.request.user.is_authenticated


# ---------------------------------------------------------------------------
# OAuth Clients
# ---------------------------------------------------------------------------

class OAuthClientListView(_NoRestrictMixin, generic.ObjectListView):
    queryset = OAuthClient.objects.all()
    table = tables.OAuthClientTable
    actions = (object_actions.AddObject,)


class OAuthClientEditView(_NoRestrictMixin, generic.ObjectEditView):
    queryset = OAuthClient.objects.all()
    form = forms.OAuthClientForm
    default_return_url = "plugins:frisian_mcp_netbox:oauthclient_list"

    def post(self, request, *args, **kwargs):
        obj = self.get_object(**kwargs)
        if obj.pk:
            # Editing existing client — default handling, no plaintext to capture
            return super().post(request, *args, **kwargs)
        # Creating new client — save ourselves to capture the one-time plaintext secret
        form = forms.OAuthClientForm(data=request.POST, files=request.FILES, instance=obj)
        if form.is_valid():
            saved = form.save()
            plaintext = getattr(saved, "plaintext_client_secret", None)
            if plaintext:
                messages.success(
                    request,
                    f"OAuth client '{saved.name}' created. "
                    f"Client ID: {saved.client_id} — "
                    f"Client secret (copy now, shown once): {plaintext}",
                )
            return redirect(self.get_return_url(request, saved))
        return render(request, self.template_name, {
            "model": self.queryset.model,
            "object": obj,
            "form": form,
            "return_url": self.get_return_url(request, obj),
        })


class OAuthClientDeleteView(_NoRestrictMixin, generic.ObjectDeleteView):
    queryset = OAuthClient.objects.all()
    default_return_url = "plugins:frisian_mcp_netbox:oauthclient_list"


# ---------------------------------------------------------------------------
# OAuth Access Tokens (created by OAuth flow — read-only list, revoke only)
# ---------------------------------------------------------------------------

class OAuthAccessTokenListView(_NoRestrictMixin, generic.ObjectListView):
    queryset = OAuthAccessToken.objects.select_related("client").all()
    table = tables.OAuthAccessTokenTable
    actions = ()


class OAuthAccessTokenDeleteView(_NoRestrictMixin, generic.ObjectDeleteView):
    queryset = OAuthAccessToken.objects.all()
    default_return_url = "plugins:frisian_mcp_netbox:oauthaccesstoken_list"


# ---------------------------------------------------------------------------
# MCP Tokens
# ---------------------------------------------------------------------------

class FrisianMcpTokenListView(_NoRestrictMixin, generic.ObjectListView):
    queryset = FrisianMcpToken.objects.all()
    table = tables.FrisianMcpTokenTable
    actions = (object_actions.AddObject,)


class FrisianMcpTokenEditView(_NoRestrictMixin, generic.ObjectEditView):
    queryset = FrisianMcpToken.objects.all()
    form = forms.FrisianMcpTokenForm
    default_return_url = "plugins:frisian_mcp_netbox:frisianmcptoken_list"

    def post(self, request, *args, **kwargs):
        obj = self.get_object(**kwargs)
        if obj.pk:
            # Editing existing token — default handling, no plaintext to capture
            return super().post(request, *args, **kwargs)
        # Creating new token — save ourselves to capture the one-time Bearer value
        form = forms.FrisianMcpTokenForm(data=request.POST, files=request.FILES, instance=obj)
        if form.is_valid():
            saved = form.save()
            plaintext = getattr(saved, "plaintext_token", None)
            if plaintext:
                messages.success(
                    request,
                    f"Token '{saved.name}' created. "
                    f"Bearer token (copy now, shown once): {plaintext}",
                )
            return redirect(self.get_return_url(request, saved))
        return render(request, self.template_name, {
            "model": self.queryset.model,
            "object": obj,
            "form": form,
            "return_url": self.get_return_url(request, obj),
        })


class FrisianMcpTokenDeleteView(_NoRestrictMixin, generic.ObjectDeleteView):
    queryset = FrisianMcpToken.objects.all()
    default_return_url = "plugins:frisian_mcp_netbox:frisianmcptoken_list"
