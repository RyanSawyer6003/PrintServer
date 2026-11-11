# Contributing

Thanks for helping with PrintServer. This guide covers how to report problems, how to propose a change, and what a pull request needs before it can be merged.

## Reporting problems and asking for features

- **Bugs and feature requests:** open an issue with the **Bug report** or **Feature request** template.
- **Security problems:** don't open a public issue. Report them privately under **Security → Report a vulnerability**. See [SECURITY.md](SECURITY.md).

For anything larger than a small fix, open an issue first so the approach can be agreed before you write the code.

## The one rule: no site values

This repository is public and has to work for any district or organization, so it never holds real deployment values. That means no real:

- IP addresses or subnets
- host names, server names or domains
- NIC or interface names
- passwords, tokens or certificates

The rule covers commit messages and commit author identities as well as files.

- Everything site-specific goes in `.env`, which is gitignored. If you add a setting, add it to `.env.example` with a placeholder value and to the configuration table in the README.
- In docs, examples and tests, use the placeholder ranges `10.10`, `10.20`, `10.30` and `10.40`, and the domain `example.org`.
- `cupsd.conf.template` and `snmp.conf.template` hold placeholders only. Their values are filled in from `.env` when the container starts.
- CI runs the site-value scan on every pull request. Run it yourself before you push:

  ```bash
  scripts/ci/check-site-values.sh                 # tracked files
  scripts/ci/check-site-values.sh origin/dev..HEAD   # your commits too
  ```

  If the scan flags a public name that is fine (a project's website, for example), add it to `scripts/ci/allowed-domains.txt` in the same pull request. Never add a site's own names.

## Branches

| Branch | What it's for |
|---|---|
| `dev` (default) | Work in progress. **Open pull requests against `dev`.** |
| `main` | Stable code. Only promotions from `dev` and hotfixes are merged here. |

- **Normal changes:** branch from `dev`, then open a pull request into `dev`.
- **Hotfixes:** branch from `main`, then open a pull request into `main`. After it is merged, `main` is merged back into `dev`.

Releases, channels and promotion are covered in [Releases and updates](README.md#releases-and-updates).

## Before you open a pull request

CI must pass. You can run the same checks locally:

```bash
# Shell scripts
git ls-files -z '*.sh' | xargs -0 shellcheck -S warning

# Build both images and smoke-test them (includes the unit tests)
docker build --pull -t printserver-cups:ci .
docker build --pull -t printserver-usage:ci ./usage
scripts/ci/smoke-test.sh cups  printserver-cups:ci
scripts/ci/smoke-test.sh usage printserver-usage:ci
```

To run just the unit tests: `python3 -m unittest discover -s status` and `python3 -m unittest discover -s usage`. Both use the Python standard library only, so don't add pip packages.

The smoke test doesn't cover macvlan networking, real printing or the volumes. If your change touches any of those, test it on a real server and say so in the pull request.

## What a pull request should include

- **What changed and why**, and the issue it closes, if there is one.
- **How you tested it:** what you ran, and on what. Say clearly what you couldn't test.
- **Docs that match the change.** Update the README, `.env.example` and the guides in `docs/` (`deploy.md`, `runbook.md`, `client-setup.md`, `backup.md`) wherever they describe what you changed.
- **Upgrade notes** if a running server needs anything beyond `scripts/update.sh run`, such as a new `.env` value, a changed volume or a firewall change.

Keep each pull request to one change. Smaller pull requests are reviewed faster.

## Style

- **Shell:** `bash` with `set -euo pipefail`, clean under shellcheck. Explain *why* in comments, not *what*.
- **Python:** standard library only, with tests next to the code (`test_*.py`).
- **Containers:** keep them least-privilege. Don't add capabilities, the Docker socket, network access for the `usage` container, or new listening ports unless the issue agreed on it.
- **Settings:** server settings change through `.env` or the templates, never through the CUPS web interface (it is locked read-only on purpose).

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE), the same license as the rest of the project (see section 5 of the license).
