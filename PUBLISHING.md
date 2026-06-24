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

Releases are fully automatic — merging a release PR is enough.

Tagging policy (`.github/workflows/docker-publish.yml`):

| Trigger | Images produced | Git tag | GitHub Release |
|---|---|---|---|
| plain merge to `main` (CHANGELOG top is `[Unreleased]`) | `edge` | — | — |
| **release-PR merge** (CHANGELOG top is `[X.Y.Z] — DATE`) | `edge` **+** `X.Y.Z`, `X.Y`, `X`, **`latest`** | `vX.Y.Z` (auto) | created automatically |
| manual `git push origin vX.Y.Z` | `X.Y.Z`, `X.Y`, `X`, **`latest`** | (already pushed) | created/updated automatically |

So `latest` always points at the newest **released version** — running
`docker pull krippler52/starr` (no tag) on a container host gets the
newest release with zero config; users who want to pin pick `X.Y.Z`
instead. `edge` tracks the tip of `main` for testing ahead of a release.

To cut a release:

1. Open a release PR that:
   - flips `CHANGELOG.md`'s `[Unreleased]` section to `[X.Y.Z] — YYYY-MM-DD`,
   - bumps the version banners in `app/server.py` and `app/templates/index.html`,
   - bumps the README image-tag pin.
2. Merge it.

That's it. CI detects the version flip, publishes the image set, pushes
the `vX.Y.Z` git tag, and creates the matching GitHub Release with the
CHANGELOG section as the body — all in the same workflow run, no need
to push a tag from your machine.

(Manually pushing a `vX.Y.Z` tag still works — useful for re-running the
release pipeline on an already-released version.)

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
2. Merge it.

CI detects the CHANGELOG version flip, publishes the version images
(`X.Y.Z` / `X.Y` / `X`), moves **`latest`**, pushes the `vX.Y.Z` git
tag, and creates the matching GitHub Release — all automatically in
the same workflow run.
