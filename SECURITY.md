# Security policy

## Supported versions

nanosdk is pre-1.0: security fixes go into the latest release only. Upgrade to the newest
version on [PyPI](https://pypi.org/project/nanosdk/) to get them.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub instead:
[Report a vulnerability](https://github.com/bjfoster/nanosdk/security/advisories/new)
(the repository's **Security** tab → **Report a vulnerability**).

Include what you found, how to reproduce it, and the versions of nanosdk, Python, httpx and
pydantic you used. You should hear back within a week. Once a fix is released, the
advisory is published with credit to you, unless you'd rather stay anonymous.

## Verifying releases

Every release is built in GitHub Actions from its tag and signed with Sigstore. To check
that a file came from this repository's release workflow:

```bash
gh attestation verify nanosdk-X.Y.Z-py3-none-any.whl --repo bjfoster/nanosdk
```
