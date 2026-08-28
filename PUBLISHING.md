# Publishing Guide

How Starr's images and releases are published. Relevant if you maintain this
repo or run a fork — not needed to *use* Starr (see the [README](README.md)).

---

## Cutting a release

Releases are fully automatic. Open a PR that:

1. Flips `CHANGELOG.md`'s `[Unreleased]` section to `[X.Y.Z] — YYYY-MM-DD`
2. Bumps the version banners in `app/server.py` and `app/templates/index.html`
3. Bumps the image-tag pin in `README.md`

Merge it. CI detects the version flip and — in the same run — publishes the
images, pushes the `vX.Y.Z` git tag, and creates the GitHub Release using that
CHANGELOG section as the body. Nothing to push from your machine.

## Tagging policy

Defined in `.github/workflows/docker-publish.yml`:

| Trigger | Images | Git tag | GitHub Release |
|---|---|---|---|
| Plain merge to `main` (CHANGELOG top is `[Unreleased]`) | `edge` | — | — |
| **Release-PR merge** (CHANGELOG top is `[X.Y.Z] — DATE`) | `edge` + `X.Y.Z`, `X.Y`, `X`, `latest` | `vX.Y.Z` (auto) | auto |
| Manual `git push origin vX.Y.Z` | `X.Y.Z`, `X.Y`, `X`, `latest` | (already pushed) | auto |

So `latest` is always the newest **released** version, and `edge` tracks the tip
of `main` for testing ahead of a release. Pushing a tag by hand still works —
useful for re-running the pipeline on an already-released version.

---

## Fork setup

Publishing from a fork needs two GitHub secrets
(**Settings → Secrets and variables → Actions**):

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token with Read/Write/Delete |

Create the token under Docker Hub → Account Settings → Security. GHCR needs no
secret — the workflow uses the built-in `GITHUB_TOKEN`.

---

## Unraid Community Apps

To get the template indexed:

1. Fork <https://github.com/selfhosters/unRAID-CA-templates>
2. Copy `templates/unraid.xml` into the fork
3. Open a PR

Or host your own template repo and add it in Unraid under
**Apps → Settings → Add templates repository URL**.
