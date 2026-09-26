# Egress proxy

Sandboxed code runs with `--network none` by default (`DockerSandbox`, final
draft §12.2 M8, §13.1 #2). The only way a task gets any network access at all
is when the controller explicitly calls `sandbox.exec(..., network=True)`; in
that case the container joins the `factory-egress` docker network instead of
`none`, and its `HTTP_PROXY`/`HTTPS_PROXY` point at this proxy. The proxy is
the only thing standing between agent-authored code and the internet, so it
allows exactly two kinds of destination:

1. the LLM gateway (LiteLLM), for tool calls that go through it, and
2. a package mirror, for dependency installs.

Nothing else. This is what makes prompt injection in an issue body harmless
even if an agent were tricked into trying to exfiltrate data (final draft
§13.1 #6, walkthrough §19.3 #11): there is no path out except these two
allowed hosts, and neither of them is a place to exfiltrate secrets to.

## Files

- `tinyproxy.conf`: tinyproxy configured with `FilterDefaultDeny Yes`, so only
  hostnames in `filter.allowlist` are reachable.
- `filter.allowlist`: the allowed FQDNs, one per line. **Edit this before
  deploying** — the shipped list is a placeholder (`litellm.internal`, plus
  common Python/Node package mirrors). Keep it as short as your products
  actually need; every extra entry widens what a compromised sandbox can
  reach.

## Running it

```bash
docker network create factory-egress
docker run -d --name factory-egress-proxy --network factory-egress \
  -v "$(pwd)/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro" \
  -v "$(pwd)/filter.allowlist:/etc/tinyproxy/filter.allowlist:ro" \
  vimagick/tinyproxy
```

Point `FACTORY_EGRESS_PROXY_URL` at `http://factory-egress-proxy:8888` and
construct `DockerSandbox(egress_network="factory-egress", egress_proxy_url=...)`
from it (see `src/factory/sandbox/docker.py`).

## Squid alternative

Any allowlisting forward proxy works as long as it default-denies and can
match a request's destination FQDN; Squid's `http_access` with a
`dstdomain` ACL does the same job as tinyproxy's filter here. Swap the
container image and translate `filter.allowlist` into an ACL file if your
environment already standardizes on Squid.
