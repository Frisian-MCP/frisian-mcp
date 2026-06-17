#!/bin/bash
set -e

# ---------------------------------------------------------------------------
# Package manager detection
#
# The official netbox-docker image (netboxcommunity/netbox) uses Python 3.14
# and ships uv (/usr/local/bin/uv) as the package manager. The venv is owned
# by root, so installations must run as root (docker-compose user: "0:0") or
# via sudo. Custom builds using python:3.12-slim use pip as normal.
#
# This script detects which is available and uses it accordingly.
# ---------------------------------------------------------------------------
VENV=/opt/netbox/venv
export VIRTUAL_ENV=$VENV
export PATH="$VENV/bin:$PATH"

if [ -x "$VENV/bin/pip" ]; then
    PIP="$VENV/bin/pip"
    INSTALL_CMD="$PIP install -q"
    INSTALL_EDITABLE="$PIP install -q --no-build-isolation -e"
elif [ -x /usr/local/bin/uv ]; then
    # Official netbox-docker image: pip is absent, uv is present.
    # setuptools must be installed first — frisian-mcp uses the setuptools
    # build backend and uv does not bundle it.
    /usr/local/bin/uv pip install setuptools -q
    INSTALL_CMD="/usr/local/bin/uv pip install -q"
    INSTALL_EDITABLE="/usr/local/bin/uv pip install -q --no-build-isolation -e"
else
    echo "ERROR: neither pip nor uv found in the container" >&2
    exit 1
fi

# Install frisian-mcp from the mounted local source (editable, no network).
$INSTALL_EDITABLE /opt/frisian-mcp

# Install the dev harness plugin wrapper so NetBox can load frisian-mcp via PLUGINS.
# Copy to /tmp first — the source mount is read-only so the package manager can't
# write egg-info/dist-info there.
# rm -rf ensures a clean destination on container restarts (cp -r into an existing
# dir would nest plugin_wrapper/ inside, confusing setuptools package discovery).
rm -rf /tmp/frisian_mcp_plugin_wrapper
cp -r /opt/netbox/development/plugin_wrapper/. /tmp/frisian_mcp_plugin_wrapper
$INSTALL_CMD /tmp/frisian_mcp_plugin_wrapper

# Run database migrations.
python manage.py migrate --no-input

# Create superuser if it doesn't exist.
python manage.py shell -c "
from django.contrib.auth import get_user_model
import os
User = get_user_model()
username = os.getenv('NETBOX_SUPERUSER_NAME', 'admin')
email    = os.getenv('NETBOX_SUPERUSER_EMAIL', 'admin@example.com')
password = os.getenv('NETBOX_SUPERUSER_PASSWORD', 'admin')
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print(f'Created superuser: {username}')
"

# Create a predictable dev API token (v1, plaintext) if it doesn't exist.
# v1 tokens use 'Authorization: Token <key>' — they bypass the v2 HMAC/pepper
# system so the token value is predictable across container restarts.
#
# NetBox v4.x ships a v2 token system (HMAC-keyed, format: Bearer nbt_<key>.<plain>).
# v1 tokens are created by setting version=1 on the Token model and assigning
# token.token directly. Use 'Authorization: Token <key>' in API requests.
python manage.py shell -c "
from django.contrib.auth import get_user_model
from users.models import Token
import os
User = get_user_model()
username  = os.getenv('NETBOX_SUPERUSER_NAME', 'admin')
token_key = os.getenv('NETBOX_SUPERUSER_API_TOKEN', '0123456789abcdef0123456789abcdef01234567')
user = User.objects.get(username=username)
if not Token.objects.filter(user=user, plaintext=token_key).exists():
    t = Token(user=user, version=1)
    t.token = token_key
    t.save()
    print(f'Created API token: {token_key}')
"

exec "$@"
