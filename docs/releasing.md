# Releasing

How to publish a release to PyPI. Publishing uses GitHub Actions
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) — no API
token is ever stored in the repo. The workflow is
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

## One-time setup (before the first publish)

Do this once. It needs the PyPI account that will own the project.

1. **PyPI pending publisher** — on [pypi.org](https://pypi.org) → your account →
   *Publishing* → *Add a pending publisher*:
   - PyPI Project Name: `hermes-inbox-organizer`
   - Owner: `Northbound-Run`
   - Repository: `hermes-inbox-organizer`
   - Workflow: `release.yml`
   - Environment: `pypi`
2. **TestPyPI pending publisher** — repeat the same on
   [test.pypi.org](https://test.pypi.org) with environment `testpypi`.
3. **GitHub environments** — repo → Settings → Environments → create `pypi` and
   `testpypi`. Optionally add yourself as a required reviewer on `pypi` so every
   production publish needs a manual click.

## Dry run on TestPyPI (recommended)

1. GitHub → Actions → **Release** → *Run workflow* → target = `testpypi`.
2. When it's green, confirm the artifact installs from TestPyPI:
   ```sh
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ hermes-inbox-organizer
   ```
   (The extra index lets dependencies resolve from real PyPI.)

## Cut a release

1. Make sure `main` is green (CI: lint + tests on 3.11–3.13) and the build is
   clean locally:
   ```sh
   uv build && uvx twine check --strict dist/*
   ```
2. Set the version in `pyproject.toml` (`project.version`). The release workflow
   **fails if the git tag doesn't match this**, so they must agree.
3. In `CHANGELOG.md`, move items out of `## [Unreleased]` into a dated
   `## [X.Y.Z] - YYYY-MM-DD` section and update the compare links at the bottom.
4. Commit, then tag and push the tag:
   ```sh
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
   Pushing the tag triggers the build → publish-to-PyPI job. (If you put a
   reviewer on the `pypi` environment, approve the run in the Actions tab.)
5. Confirm it's live:
   ```sh
   pip install "hermes-inbox-organizer[live]==X.Y.Z"
   ```
6. Cut a **GitHub Release** for the tag with the changelog section as the notes:
   ```sh
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
   ```
   or create it from the Releases UI and paste the changelog section.

## After the first publish

- Optionally point the repo homepage at the PyPI page:
  `gh repo edit --homepage https://pypi.org/project/hermes-inbox-organizer/`.
- The pending publishers from setup become regular trusted publishers
  automatically after the first successful upload.
