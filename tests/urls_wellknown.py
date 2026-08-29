"""
Root URLconf that mounts the OAuth ``.well-known`` endpoints.

``tests.urls`` deliberately does not mount them, so a doctor test running under
it exercises a host where a client could not reach the metadata endpoint at all.
The discovery-reachability check reads exactly that distinction, so the tests
that assert it must run against a URLconf where the endpoints resolve — and
``tests.urls_wellknown`` is that posture.

The companion posture (URLs absent) is covered by pointing a test at
``frisian_mcp._ci_doctor_urls``, whose ``urlpatterns`` are empty.
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path(".well-known/", include("frisian_mcp.contrib.oauth.wellknown_urls")),
]
