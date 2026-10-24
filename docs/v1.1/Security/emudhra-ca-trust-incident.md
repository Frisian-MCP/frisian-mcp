# eMudhra certificate incident: security update available

**Status:** fixed in frisian-mcp 1.1.1, released 2026-10-23. The hosted
Frisian-MCP service is remediated and verified for the components listed under
**Status** below; the remaining items are named there. Last updated 2026-10-24.

---

## Summary

A root certificate operated by eMudhra, **emSign ECC Root CA - G3**, is no longer
trusted following a certificate authority incident. Software that still trusts
it may accept TLS certificates it should reject.

- **Advisories:** [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
  (Python), CVE-2026-111943 / GO-2026-6809 (Go), Red Hat RHSB-2026-012.
- **frisian-mcp 1.1.0 installed with the optional `usage` extra** pulls in a
  `certifi` CA bundle that still trusts the affected root.
- **frisian-mcp 1.1.1 requires `certifi` 2026.10.19 or later** for the `usage`
  extra, so a fresh install or upgrade no longer resolves an affected bundle.
- **Self-hosted operators** should also update their Python CA bundles and
  their operating system and custom trust stores, then rebuild and restart
  affected workloads. See **What to do now** below.
- **External MCP clients** that connect to a frisian-mcp server need their own
  updates. This release doesn't change them.
- **The hosted Frisian-MCP service:** the MCP API service and the Slack bot run
  on frisian-mcp 1.1.1 with updated CA bundles, and the hosts' operating system
  trust stores are updated, verified on 2026-10-23. Some host-level components
  are still pending; see **Status** below.
  Remediating the hosted service doesn't update self-hosted installations.

---

## Who is affected

| Installation | Affected through frisian-mcp? |
|---|---|
| frisian-mcp 1.1.0 with the `usage` extra (`frisian-mcp[usage]`) | **Yes**, when the environment has `certifi` older than 2026.10.18 |
| frisian-mcp 1.1.0 without the `usage` extra | No dependency path through frisian-mcp, but see the note below |
| frisian-mcp 1.0.x | No dependency path through frisian-mcp: 1.0.x has no `usage` extra |
| frisian-mcp 1.1.1 or later | No: the `usage` extra requires `certifi` 2026.10.19 or later |

The `usage` extra installs `tiktoken`, which depends on `requests`, which
depends on `certifi`. `certifi` releases before 2026.10.18 include the affected
root; 2026.10.19 is the release frisian-mcp 1.1.1 requires. Other emSign roots
remain in `certifi` and are not affected by this incident.

**Most Python environments contain `certifi` through some other package**, so
not depending on it through frisian-mcp doesn't mean your environment is clean.
Check the installed version whatever your frisian-mcp version is. Upgrading an
existing environment doesn't always replace a `certifi` that's already
installed, which is why step 2 below upgrades it explicitly.

frisian-mcp has no Go code and ships no certificate files of its own.

### Where frisian-mcp makes outbound TLS connections

- **Token usage reporting** (the `usage` extra). On first use, `tiktoken` may
  download its vocabulary file over HTTPS when it isn't already cached. That
  connection uses `requests`, so it trusts the `certifi` bundle.
- **`mcp_doctor`'s OAuth URL check** uses the Python standard library HTTP
  client. It trusts **Python's default CA file and directory**, not the
  `certifi` package in your environment. On most Linux systems that default is
  the operating system store. On macOS installations from python.org it is
  usually a `certifi` bundle inside the Python framework, linked there by the
  installer's *Install Certificates* step, and upgrading `certifi` in a virtual
  environment doesn't change it. Neither frisian-mcp 1.1.1 nor a `certifi`
  upgrade in your environment covers this check; step 4 below shows how to find
  and update the store it uses.

Your own application code and other dependencies may make TLS connections too;
they use one of these same trust stores.

---

## What to do now

1. **Upgrade frisian-mcp to 1.1.1 or later** if you install the `usage` extra:

   ```bash
   python -m pip install --upgrade "frisian-mcp[usage]>=1.1.1"
   ```

   Step 2 protects you on any frisian-mcp version, including when 1.1.1 hasn't
   reached your package mirror yet.

2. **Upgrade `certifi` explicitly in every environment that runs frisian-mcp:**

   ```bash
   python -m pip install --upgrade "certifi>=2026.10.19"
   ```

   With uv, add `certifi>=2026.10.19` to your project's dependencies or
   constraints and re-lock, so the next build can't resolve an older version.

3. **Check the version that actually loads:**

   ```bash
   python -c "import certifi; print(certifi.__version__, certifi.where())"
   ```

   Run it with the same interpreter and virtual environment as the service.

4. **Update the store Python trusts by default.** Find it with the same
   interpreter as the service:

   ```bash
   python -c "import ssl; print(ssl.get_default_verify_paths())"
   ```

   `cafile` and `capath` show the CA file and directory in use (`None` means
   that one isn't present). Update whatever they point to:

   - **An operating system store** (for example under `/etc/ssl`): install your
     OS vendor's latest CA certificates package (for example `ca-certificates`
     on Debian, Ubuntu and Alpine, or the vendor's update for RHEL). Follow your
     vendor's guidance, since some distributions also handle cross-signed paths
     to the affected root.
   - **A `certifi` bundle inside a python.org macOS installation:** run
     *Install Certificates.command* from that Python version's folder in
     Applications. It upgrades the framework's own `certifi` and relinks the
     default CA file to it.
   - **A bundle from another package manager** (for example Homebrew or conda):
     upgrade that package manager's `ca-certificates` package.

5. **Check for overridden trust stores.** If you set `SSL_CERT_FILE`,
   `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE`, or mount a custom
   CA bundle, make sure that bundle no longer contains the affected root.

6. **Rebuild container images** from updated build inputs (dependency pins and
   base images) rather than patching a running container, so the fix survives
   the next deploy.

7. **Restart long-running processes** after updating, because they keep the
   trust store they loaded at startup.

8. **Update your MCP clients.** Agents and other MCP clients that connect to
   your server have their own trust stores; follow each client's vendor
   guidance.

**Don't turn off certificate verification as a workaround.** That removes the
protection this update restores.

---

## Status

### frisian-mcp package

| Release | Change | Published |
|---|---|---|
| frisian-mcp 1.1.1 | `usage` extra requires `certifi>=2026.10.19` | 2026-10-23 15:47 UTC, PyPI |

### Hosted Frisian-MCP service

Each component below was checked for the affected root by certificate
fingerprint in every trust store it uses. The checks finished on
2026-10-23 at 23:54 UTC.

| Component | Change | Completed (UTC) |
|---|---|---|
| MCP API service | Redeployed on frisian-mcp 1.1.1, `certifi` 2026.10.19 and updated OS CA certificates | 2026-10-23 20:52 |
| Slack bot | Redeployed with `certifi` 2026.10.23 and updated OS CA certificates | 2026-10-23 21:31 |
| Database host OS trust store | Vendor CA certificates update | 2026-10-23 20:19 |
| Application host OS trust store | Vendor CA certificates update | 2026-10-21 06:20 |

**Still pending:**

- **Container runtime and system agent services on the hosts.** These
  long-running services loaded their trust store before it was updated and
  need a restart or upgrade to pick up the change.
- **The web proxy and cache containers** in front of the service. They have
  not been updated yet.

This page will be updated when these items are complete.
