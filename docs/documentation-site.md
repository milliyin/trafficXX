# Documentation site

The documentation is built from the repository's `docs/` directory with MkDocs
and Material for MkDocs. The intended GitHub Pages address is
[milliyin.github.io/trafficXX](https://milliyin.github.io/trafficXX/).

## Local preview

Use a separate Python environment for documentation dependencies:

```bash
python -m venv .venv-docs
```

Activate it using the command appropriate to your shell, then run:

```bash
python -m pip install -r requirements-docs.txt
python -m mkdocs serve
```

Open the localhost address printed by MkDocs. The application and Azure do not
need to be running. No cloud login is required to build the static documentation.

## Validate a change

```bash
python -m mkdocs build --strict
```

The generated output goes to ignored `test-results/docs-site`. The navigation
lives in `mkdocs.yml`; Markdown guides remain the source of truth. Keep historical
experiment results labelled as dated evidence, and use the progress page for
the current verified checkpoint.

## GitHub deployment

The `Documentation` workflow builds relevant pull requests without publishing.
Changes to documentation, configuration, dependency pins, or the workflow on
`main` build and publish the static artifact to GitHub Pages. It can also be
started with `workflow_dispatch`.

GitHub Pages must use **GitHub Actions** as its publishing source. Only the
generated site artifact is uploaded. Application files, root credential files,
Terraform state, local test results, and the session learning log are outside
the documentation source directory and are not copied into the site.

The deployment job uses GitHub's Pages environment, `pages: write`, and
`id-token: write`. It does not use Azure credentials or deploy the application.
Dependencies and action revisions are pinned separately from the application.

After deployment, verify the home page, a nested guide, site search, and the
light/dark theme toggle. The source repository and edit links remain available
from the site navigation.
