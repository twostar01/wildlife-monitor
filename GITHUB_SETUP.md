# GitHub Setup

Instructions for putting Wildlife Monitor under version control and publishing a release.

---

## First-time setup on your machine

### 1. Install git if needed

```bash
sudo apt-get install -y git
```

### 2. Configure git identity

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

### 3. Initialise the repository

```bash
cd ~/wildlife_monitor
git init
git add .
git commit -m "Initial release — Wildlife Monitor v1.0.0"
```

### 4. Create the GitHub repository

Go to https://github.com/new and create a new repository named `wildlife-monitor`.
Leave it empty (no README, no .gitignore — you already have those).

### 5. Push to GitHub

```bash
git remote add origin https://github.com/<your-username>/wildlife-monitor.git
git branch -M main
git push -u origin main
```

---

## Tagging a release

```bash
git tag -a v1.0.0 -m "Wildlife Monitor v1.0.0 — initial release"
git push origin v1.0.0
```

Then on GitHub: Releases → Draft a new release → choose tag v1.0.0 → paste the
relevant section from CHANGELOG.md → attach the wildlife_monitor_v1.0.0.zip file.

---

## Day-to-day workflow

After making changes to any file:

```bash
cd ~/wildlife_monitor
git add -A
git commit -m "Brief description of what changed"
git push
```

### Useful commands

```bash
git status              # see what's changed
git diff                # see exact changes
git log --oneline       # history
git checkout -- <file>  # discard changes to a file
```

---

## What is and isn't tracked

**Tracked (committed to git):**
- All Python source files (`*.py`)
- Shell scripts (`*.sh`)
- The web UI (`static/index.html`)
- Documentation (`README.md`, `CHANGELOG.md`)
- Systemd unit templates (`systemd/`)
- `.gitignore`, `VERSION`, `CHANGELOG.md`

**Not tracked (in .gitignore):**
- `data/` — database, crops, thumbnails, logs (machine-specific, potentially large)
- `local_videos/` — staging directory, always temporary
- `wildlife_env/` — Python virtual environment (recreated by `setup.sh`)
- NAS credentials (`~/.config/wildlife_monitor/`)

---

## Updating from GitHub on another machine

```bash
cd ~/wildlife_monitor
git pull
sudo systemctl restart wildlife-monitor
```

If `database.py` changed, the migrations run automatically on next startup.
If `setup.sh` changed with new dependencies, re-run it:

```bash
./setup.sh
```
