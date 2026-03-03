# Publishing Guide

Step-by-step for first-time Docker Hub + GitHub setup.

---

## 1. GitHub Repository

```bash
# Init and push
git init
git add .
git commit -m "chore: initial commit"
gh repo create Krippler/Starr --public --push --source=.
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
| `Krippler` | Your GitHub username |
| `yourdockerhubuser` | Your Docker Hub username |
| `YOUR_NAME` | Your name (in LICENSE) |
| `your@email.com` | Your email (in Dockerfile LABEL) |

Quick replace:
```bash
grep -rl 'Krippler'   . | xargs sed -i 's/Krippler/mygithubuser/g'
grep -rl 'yourdockerhubuser'  . | xargs sed -i 's/yourdockerhubuser/mydockerhubuser/g'
```

---

## 5. First release

```bash
git tag v1.0.0
git push origin main --tags
```

The GitHub Actions workflow will:
1. Run tests
2. Build `linux/amd64` + `linux/arm64` images
3. Push to Docker Hub with tags: `1.0.0`, `1.0`, `1`, `latest`
4. Push to GitHub Container Registry (`ghcr.io`)
5. Run Trivy security scan
6. Update Docker Hub README

---

## 6. Unraid Community Apps

To have your template indexed in the Unraid Community Apps plugin:

1. Fork https://github.com/selfhosters/unRAID-CA-templates
2. Copy `templates/unraid.xml` into the fork
3. Submit a PR with your template

Or host your own template repository and add it in Unraid under:
**Apps → Settings → Add templates repository URL**

---

## 7. Release workflow (subsequent releases)

```bash
# Bump version in README badges if needed
git add .
git commit -m "feat: description of changes"
git tag v1.1.0
git push origin main --tags
```

Docker Hub tags updated automatically via CI.
