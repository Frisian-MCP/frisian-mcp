"""
A hand-rolled ``.well-known`` URLconf that mounts ONLY the authorization-server URL.

The package ships both well-known endpoints in one ``include()``, so they
normally travel together.  A host that writes its own patterns instead can mount
one and omit the other, and this fixture is that host: it is what proves the
discovery-reachability gate reverses the endpoint it actually probes rather than
a neighbour that happens to be mounted.
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [path(".well-known/", include("tests.urls_wellknown_partial_patterns"))]
