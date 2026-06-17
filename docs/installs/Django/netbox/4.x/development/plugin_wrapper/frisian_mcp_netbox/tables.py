import django_tables2 as tables
from netbox.tables import BaseTable

from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient
from frisian_mcp.contrib.tokens.models import FrisianMcpToken

_EDIT_DELETE = (
    '<a href="{edit_url}" class="btn btn-sm btn-warning" title="Edit">'
    '<i class="mdi mdi-pencil"></i></a> '
    '<a href="{delete_url}" class="btn btn-sm btn-danger" title="Delete">'
    '<i class="mdi mdi-trash-can-outline"></i></a>'
)

_DELETE_ONLY = (
    '<a href="{delete_url}" class="btn btn-sm btn-danger" title="Revoke">'
    '<i class="mdi mdi-trash-can-outline"></i></a>'
)

_CLIENT_ACTIONS = """
{% load static %}
<a href="{% url 'plugins:frisian_mcp_netbox:oauthclient_edit' record.pk %}"
   class="btn btn-sm btn-warning" title="Edit">
  <i class="mdi mdi-pencil"></i></a>
<a href="{% url 'plugins:frisian_mcp_netbox:oauthclient_delete' record.pk %}"
   class="btn btn-sm btn-danger" title="Delete">
  <i class="mdi mdi-trash-can-outline"></i></a>
"""

_TOKEN_ACTIONS = """
{% load static %}
<a href="{% url 'plugins:frisian_mcp_netbox:oauthaccesstoken_delete' record.pk %}"
   class="btn btn-sm btn-danger" title="Revoke">
  <i class="mdi mdi-trash-can-outline"></i></a>
"""

_MCP_TOKEN_ACTIONS = """
{% load static %}
<a href="{% url 'plugins:frisian_mcp_netbox:frisianmcptoken_edit' record.pk %}"
   class="btn btn-sm btn-warning" title="Edit">
  <i class="mdi mdi-pencil"></i></a>
<a href="{% url 'plugins:frisian_mcp_netbox:frisianmcptoken_delete' record.pk %}"
   class="btn btn-sm btn-danger" title="Delete">
  <i class="mdi mdi-trash-can-outline"></i></a>
"""

_ACTION_ATTRS = {"td": {"class": "text-end text-nowrap noprint p-1"}}


class OAuthClientTable(BaseTable):
    name = tables.Column(verbose_name="Name")
    client_id = tables.Column(verbose_name="Client ID")
    permission = tables.Column(verbose_name="Permission")
    is_active = tables.BooleanColumn(verbose_name="Active", yesno="Yes,No")
    created_at = tables.DateTimeColumn(verbose_name="Created", format="Y-m-d H:i")
    actions = tables.TemplateColumn(
        template_code=_CLIENT_ACTIONS,
        orderable=False,
        verbose_name="",
        attrs=_ACTION_ATTRS,
    )

    class Meta(BaseTable.Meta):
        model = OAuthClient
        fields = ("name", "client_id", "permission", "is_active", "created_at", "actions")
        default_columns = ("name", "client_id", "permission", "is_active", "created_at", "actions")


class OAuthAccessTokenTable(BaseTable):
    client = tables.Column(verbose_name="Client")
    permission = tables.Column(verbose_name="Permission")
    expires_at = tables.DateTimeColumn(verbose_name="Expires", format="Y-m-d H:i")
    last_used_at = tables.DateTimeColumn(verbose_name="Last Used", format="Y-m-d H:i")
    created_at = tables.DateTimeColumn(verbose_name="Issued", format="Y-m-d H:i")
    actions = tables.TemplateColumn(
        template_code=_TOKEN_ACTIONS,
        orderable=False,
        verbose_name="",
        attrs=_ACTION_ATTRS,
    )

    class Meta(BaseTable.Meta):
        model = OAuthAccessToken
        fields = ("client", "permission", "expires_at", "last_used_at", "created_at", "actions")
        default_columns = ("client", "permission", "expires_at", "last_used_at", "created_at", "actions")


class FrisianMcpTokenTable(BaseTable):
    name = tables.Column(verbose_name="Name")
    permission = tables.Column(verbose_name="Permission")
    is_active = tables.BooleanColumn(verbose_name="Active", yesno="Yes,No")
    user = tables.Column(verbose_name="User")
    created_at = tables.DateTimeColumn(verbose_name="Created", format="Y-m-d H:i")
    last_used_at = tables.DateTimeColumn(verbose_name="Last Used", format="Y-m-d H:i")
    actions = tables.TemplateColumn(
        template_code=_MCP_TOKEN_ACTIONS,
        orderable=False,
        verbose_name="",
        attrs=_ACTION_ATTRS,
    )

    class Meta(BaseTable.Meta):
        model = FrisianMcpToken
        fields = ("name", "permission", "is_active", "user", "created_at", "last_used_at", "actions")
        default_columns = ("name", "permission", "is_active", "user", "created_at", "last_used_at", "actions")
