from django.urls import path

from . import views

urlpatterns = [
    # OAuth Clients
    path("oauth-clients/", views.OAuthClientListView.as_view(), name="oauthclient_list"),
    path("oauth-clients/add/", views.OAuthClientEditView.as_view(), name="oauthclient_add"),
    path("oauth-clients/<int:pk>/edit/", views.OAuthClientEditView.as_view(), name="oauthclient_edit"),
    path("oauth-clients/<int:pk>/delete/", views.OAuthClientDeleteView.as_view(), name="oauthclient_delete"),
    # OAuth Access Tokens (created by OAuth flow — revoke only)
    path("oauth-tokens/", views.OAuthAccessTokenListView.as_view(), name="oauthaccesstoken_list"),
    path("oauth-tokens/<int:pk>/delete/", views.OAuthAccessTokenDeleteView.as_view(), name="oauthaccesstoken_delete"),
    # MCP Tokens
    path("mcp-tokens/", views.FrisianMcpTokenListView.as_view(), name="frisianmcptoken_list"),
    path("mcp-tokens/add/", views.FrisianMcpTokenEditView.as_view(), name="frisianmcptoken_add"),
    path("mcp-tokens/<int:pk>/edit/", views.FrisianMcpTokenEditView.as_view(), name="frisianmcptoken_edit"),
    path("mcp-tokens/<int:pk>/delete/", views.FrisianMcpTokenDeleteView.as_view(), name="frisianmcptoken_delete"),
]
