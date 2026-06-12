# Release Automation

Memplex publishes a Python package and an npm wrapper. The npm wrapper is what
enables:

```bash
npx memplex setup
```

The repository now has a GitHub Actions workflow at
`.github/workflows/npm-release.yml` that publishes the npm package from
`npm/memplex` when a `v*` tag is pushed, or when the workflow is run manually.

## Do Not Commit Tokens

Never paste a real npm token into the repository. For local development,
`npm login` stores authentication in your user-level npm config, normally
`~/.npmrc`. That is useful for one-off local checks, but release publishing
should use a CI secret instead.

For CI, create a granular npm access token on npmjs.com:

1. Open your npm profile menu and go to Access Tokens.
2. Generate a new granular token.
3. Give it read and write access to the `memplex` package.
4. If npm 2FA blocks automated publishing, enable bypass 2FA for this token.
5. Use a short expiration date and rotate it regularly.

Then save the token in GitHub:

```text
Repository Settings -> Secrets and variables -> Actions -> New repository secret
Name: NPM_TOKEN
Value: npm_xxx...
```

Only the secret store contains the real token. The workflow exposes it to npm as
`NODE_AUTH_TOKEN`.

## Publish

For a normal release:

```bash
git tag v3.2.7
git push origin v3.2.7
```

The release workflow will:

1. Check that `pyproject.toml` and `npm/memplex/package.json` have the same
   version.
2. Run `npm pack --dry-run` to verify the npm package contents.
3. Skip publishing if that exact npm version already exists.
4. Run `npm publish --access public`.

For a manual npm publish after `NPM_TOKEN` is configured and the workflow is on
`main`, run the workflow from GitHub Actions or from the CLI:

```bash
gh workflow run npm-release.yml --ref main
```

## Stronger Option: Trusted Publishing

npm recommends trusted publishing for GitHub Actions because it uses OIDC rather
than a long-lived token. The current workflow intentionally uses `NPM_TOKEN` so
releases work before trusted publishing is configured. Once the npm package is
configured for trusted publishing on npmjs.com, switch the workflow back to
`id-token: write` plus `npm publish --provenance`.
