# Security Policy

## Supported versions

Versions below receive security updates.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| 0.x.x   | :x:                |

Run the `prod` channel (`IMAGE_TAG=prod`, the default) or the newest `vX.Y.Z`.
A server pinned to an older version or date tag doesn't get the fixes below.

## Reporting a vulnerability

Report it privately: **Security → Report a vulnerability** on this repository.
Please don't open a public issue for a vulnerability.

## How security fixes ship

There are three kinds, and each is shipped differently.

**1. A Debian package fix (most of them).** No release is made. The images are
rebuilt on a fresh Debian base, nightly for `dev` and weekly for `prod`, and
are pushed only when something changed. Each rebuild also gets a
`vX.Y.Z-YYYYMMDD` tag, which records which build has the fix. The published
images are scanned daily for vulnerabilities that have a fix available.

**2. A fix that needs a new version** (a Dockerfile or configuration change).
A patch release, `vX.Y.Z`, with a **Security** section at the top of its
release notes: what was fixed, the CVEs, whether a running server is exposed,
and how soon to update. The release name has no suffix, because any
`vX.Y.Z-<suffix>` tag is a pre-release and only reaches `dev`.

**3. A vulnerability in this project's own code or configuration.** The patch
release as in 2, plus a GitHub security advisory on this repository (Security
→ Advisories), which gets a GHSA ID and, where it applies, a CVE.

## After a security release

A server's update timer (`scripts/update.sh install-timer`) runs once a month,
or weekly with `--weekly`. Don't wait for it after a security release; update
now:

```bash
sudo scripts/update.sh run
```

## Getting notified

- **Watch** this repository with **Custom → Releases** and **Security alerts**, or
- subscribe to the release feed: `https://github.com/RyanSawyer6003/PrintServer/releases.atom`
