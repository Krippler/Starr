# Publishing Guide

Step-by-step for first-time Docker Hub + GitHub setup.

---

## 1. GitHub Repository

```bash
# Init and push
git init
git add .
git commit -m "chore: initial commit"
gh repo create YOUR_GITHUB_USER/Starr --public --push --source=.
```

---

## 2. Docker Hub

1. Create a free account at https://hub.docker.com
2. Create a new repository named `Starr`
3. Create an **Access Token**: Account Settings → Security → New Access Token
   - Name: `github-actions`
   - Permissions: Read, Write, Delete

---

## 3. GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New secret**

| Secret name | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | The access token from step 2 |

---

## 4. Find & Replace placeholders

In the project files, replace:

| Placeholder | Replace with |
|---|---|
| `YOUR_GITHUB_USER` | Your GitHub username |
| `yourdockerhubuser` | Your Docker Hub username |
| `YOUR_NAME` | Your name (in LICENSE) |
| `your@email.com` | Your email (in Dockerfile LABEL) |

Quick replace:
```bash
grep -rl 'YOUR_GITHUB_USER'   . | xargs sed -i 's/YOUR_GITHUB_USER/mygithubuser/g'
grep -rl 'yourdockerhubuser'  . | xargs sed -i 's/yourdockerhubuser/mydockerhubuser/g'
```

---

## 5. Releasing

Tagging policy (`.github/workflows/docker-publish.yml`):

| You push… | Images produced | GitHub Release |
|---|---|---|
| a merge to `main` | `edge` | — |
| a git tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `X`, **`latest`** | **created automatically** |

So `latest` always points at the newest **released version**, and `edge`
tracks the tip of `main` for testing ahead of a release.

To cut a release:

```bash
# 1. Land your changes on main (via PR), and make sure CHANGELOG.md has a
#    "## [X.Y.Z] — DATE" section — its body becomes the GitHub Release notes.
git checkout main && git pull

# 2. Tag and push the tag.
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The workflow then:
1. Builds the `linux/amd64` image
2. Pushes to **Docker Hub** and **GHCR** as `X.Y.Z`, `X.Y`, `X`, and `latest`
3. **Signs** every tag with cosign (keyless / Sigstore)
4. **Creates a GitHub Release** named `vX.Y.Z`, with notes pulled from the
   matching `CHANGELOG.md` section plus the pull commands

---

## 6. Unraid Community Apps

To have your template indexed in the Unraid Community Apps plugin:

1. Fork https://github.com/selfhosters/unRAID-CA-templates
2. Copy `templates/unraid.xml` into the fork
3. Submit a PR with your template

Or host your own template repository and add it in Unraid under:
**Apps → Settings → Add templates repository URL**

---

## 7. Subsequent releases

1. Open a release PR that bumps the version banners (header subtitle +
   repair-log greeting in `index.html`, the log banner in `server.py`), the
   README image-tag pin, and flips the `CHANGELOG.md` `[Unreleased]` section
   to `[X.Y.Z] — DATE`.
2. Merge it (this updates `edge`).
3. Tag and push:

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Version images, `latest`, and the GitHub Release are all produced automatically
(see section 5).
