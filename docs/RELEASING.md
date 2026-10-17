# Releasing mendwright

Releases go to PyPI from GitHub Actions through trusted publishing, so no API token is stored in the repository. The workflow is `.github/workflows/release.yml`. It does two things:

- Pushing a tag such as `v0.3.0` builds and tests the sdist and wheel, then uploads them to PyPI once you approve the `pypi` environment.
- Running it by hand from the Actions tab does the same build and test and uploads nothing. Use this as a rehearsal.

Three things to know before the first release:

- PyPI never accepts the same version twice. A file name stays taken even after you delete the release. If anything goes wrong after an upload, the fix is a new version number.
- 0.1.0 and 0.2.0 were developed privately under the name selfheal and never published. The first upload to PyPI is 0.3.0.
- A pending publisher does **not** reserve the name. Until the first upload, anyone can register `mendwright` on PyPI, and that invalidates your pending publisher ([PyPI docs](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)). Do the 0.3.0 release on the same day the repository goes public. If you can't, claim the name by hand first (see [Claiming the name by hand](#claiming-the-name-by-hand)).

## One-time setup

1. Create an account on [pypi.org](https://pypi.org) and turn on two-factor authentication. PyPI requires 2FA for publishing.
2. On PyPI, open [Publishing](https://pypi.org/manage/account/publishing/), pick the GitHub tab and add a pending publisher:
   - PyPI project name: `mendwright`
   - Owner: `saadbuilt`
   - Repository name: `mendwright`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. Create the repository `saadbuilt/mendwright` on GitHub with `main` as the default branch, and push the code to `main`.
4. Make the repository public (Settings, General, Danger Zone, Change visibility). The links on the PyPI page point at GitHub, and GitHub Free only offers environments on public repositories.
5. Turn on private vulnerability reporting (Settings, Advanced Security, then Enable next to Private vulnerability reporting). `SECURITY.md` and the bug report template send people there.
6. In Settings, Environments, create `pypi`:
   - Turn on Required reviewers and add yourself.
   - Leave Prevent self-review off while you are the only maintainer. Otherwise nobody can approve your releases.
   - Under Deployment branches and tags, choose Selected branches and tags, then add a rule with ref type Tag and the pattern `v*`.
7. Optional: add a tag ruleset for `v*` (Settings, Rules, Rulesets) so only maintainers can create release tags.
8. Set the description, website and topics, and turn off the empty Wiki and Projects tabs:

    ```
    gh repo edit saadbuilt/mendwright --description "Self-healing selectors for Playwright and Selenium. A model proposes a new XPath and mendwright checks it against the live page before using it." --homepage https://pypi.org/project/mendwright/ --enable-wiki=false --enable-projects=false --add-topic playwright,selenium,python,xpath,selectors,locators,self-healing,self-healing-tests,test-automation,browser-automation,web-automation,e2e-testing,ui-testing,flaky-tests,pytest,pytest-plugin,playwright-python,selenium-python,qa-automation,llm
    ```

    Then open Settings, General, Social preview and upload `docs/assets/social-preview.png`. GitHub doesn't read it from the repository, so this step is by hand.

Then, on the same day, release 0.3.0 as described below.

## Rehearse

In the Actions tab, open Release, click Run workflow and choose `main`. It builds the sdist and wheel, installs the wheel into a fresh virtualenv, runs the command and the tests, and uploads nothing.

## Each release

1. Set the version in four places, all to the same value:
   - `version` in `pyproject.toml`
   - both `__version__ = '...'` fallback lines at the bottom of `mendwright/__init__.py`. The GitHub Action runs mendwright from its own folder without installing it, so it reports this literal; `tests/test_action_yml.py` checks both.
   - every `uses: saadbuilt/mendwright@vX.Y.Z` in `docs/guide.md`. `tests/test_action_yml.py` checks it too.
2. In `CHANGELOG.md`, change `## [X.Y.Z] - Unreleased` to `## [X.Y.Z] - YYYY-MM-DD`, and check that the link line `[X.Y.Z]: https://github.com/saadbuilt/mendwright/releases/tag/vX.Y.Z` is at the bottom.
3. If `README.md` or the code changed since the wheel was last measured, measure it again. The wheel embeds the README, so its size changes. Build it from a clean copy with LF line endings, as the release workflow does: `git -c core.autocrlf=false archive HEAD` into a scratch folder, then `python -m build --wheel` there (on Windows, an archive with CRLF endings gives a wheel about 1.7 KB larger). After the release, replace the figure with the size of the wheel PyPI serves. Then put the new byte counts in `download.rows[0]` and the Linux total in `infra.rows[0]` of `docs/charts/data.json`, and run `python docs/charts/build_charts.py`. Then update the figures in `docs/comparison.md`: the download and infra tables, the chart descriptions and the Methodology bullets.
4. Reinstall and run the tests:

   ```
   python -m pip install -e ".[dev]"
   python -m pytest -q
   ```

   The reinstall matters. `mendwright.__version__` is read from the installed package metadata, so an old editable install still reports the previous version and `tests/test_version.py` fails.
5. Open a pull request, wait for CI to pass, and merge it to `main`.
   The same tag releases the GitHub Action (`action.yml` at the root). Its pinned actions must be the ones `ci.yml` and `release.yml` use, which `tests/test_action_yml.py` checks. Before tagging a release that changes `action.yml` or `action/`, run the action once against a scratch repository with a real token, through a run that creates the pull request, one that updates it, one that leaves it unchanged and one that closes it: the offline tests push over the file protocol and never reach GitHub's HTTP authentication or its API. Give one of those runs a `webhook-url` secret for a test channel and check that the alert arrives and that the job log shows the URL as `***`: the offline tests post to a local server, not to a real webhook.
6. Tag the merge commit and push the tag:

   ```
   git checkout main
   git pull
   git tag -a vX.Y.Z -m "mendwright X.Y.Z"
   git push origin vX.Y.Z
   ```

   The build job stops if the tag doesn't match `version` in `pyproject.toml`.
7. In the Actions tab, open the Release run. After the build job passes, the PyPI job waits for approval. Click Review deployments and approve `pypi`.
8. Create the GitHub release from the files the run built. Put the CHANGELOG section for this version in `notes.md` first.

   ```
   gh run download <run-id> -n python-package-distributions -D dist
   gh release create vX.Y.Z dist/* --title "mendwright X.Y.Z" --notes-file notes.md
   ```

9. Check the real install from a fresh virtualenv:

   ```
   python -m pip install mendwright==X.Y.Z
   mendwright --version
   ```

## Claiming the name by hand

Use this only if the first trusted-publishing release can't happen on the day the repository goes public.
It uploads 0.3.0 from your own machine with an API token, then hands later releases to the workflow.

1. On PyPI, open Account settings, API tokens, and add a token with the scope "Entire account (all projects)". A token scoped to one project can only be made once the project exists.
2. Do steps 1 to 5 of Each release first, so `main` has version 0.3.0 and the CHANGELOG heading carries the release date. Then build, check and upload from `main`:

   ```
   git checkout main
   git pull
   python -m pip install -e ".[dev]"
   python -m pytest -q
   python -m build
   python -m twine check --strict dist/*
   python -m twine upload dist/*
   ```

   twine asks for a username and a password. Enter `__token__` as the username and the token as the password.
3. Open https://pypi.org/project/mendwright/ and check the page.
4. Open the project's Manage page on PyPI, then Publishing. If the GitHub publisher from setup step 2 isn't listed, add it there with the same values.
5. Delete the account-wide token (Account settings, API tokens).
6. Tag `v0.3.0` and push it, as in step 6 of Each release. The build job runs as usual. When the PyPI job asks for approval, reject it: PyPI already has 0.3.0, and that job doesn't skip existing files, so it would fail.
7. Create the GitHub release from your local `dist/`, which holds the files you uploaded, rather than from the run's artifact:

   ```
   gh release create v0.3.0 dist/* --title "mendwright 0.3.0" --notes-file notes.md
   ```

From 0.3.1 on, follow Each release.

## When something fails

- The build job failed: nothing was uploaded. Fix the problem on `main`, delete the tag (`git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`), and tag the fixed commit.
- The PyPI upload failed partway, or a broken release went out: release X.Y.Z+1. Deleting the tag doesn't help, because PyPI won't take X.Y.Z again. You can yank the broken version on PyPI (Manage, Releases, Options, Yank). Yanking hides it from new installs but keeps it available to anyone who pinned it.

## The npm package

The TypeScript package in `js/` goes to npm from `.github/workflows/release-npm.yml`, through npm's trusted publishing, so no npm token is stored either. Nothing triggers that workflow yet: it runs only by hand (Actions, Release npm, Run workflow).
The Python sdist and wheel never hold `js/`: the wheel has only the `mendwright` package, and `MANIFEST.in` prunes `js` last (`tests/test_package_files.py` checks both).

- With publish off, it builds and tests `js/` and packs it, and uploads nothing. Use this as a rehearsal.
- With publish on, it does the same, then publishes the packed file once you approve the `npm` environment. The package gets npm provenance: a signed statement, shown on its npm page, that this workflow built it from this commit.

The name `mendwright` is reserved on npm with a 0.0.1 placeholder. Before the first upload from the workflow:

1. On npmjs.com, open the settings of the package `mendwright`, and add a trusted publisher: GitHub Actions, owner `saadbuilt`, repository `mendwright`, workflow `release-npm.yml`, environment `npm`.
2. In the repository's Settings, Environments, create `npm` with Required reviewers, as for `pypi`.

Each release:

1. Set the version in `js/package.json` and in `version` in `js/src/index.ts` (the unit suite checks they match), and date its section in `js/CHANGELOG.md`.
2. In `js/`, run `npm install` (so `package-lock.json` carries the version), `npm test` and `npm run test:browser`.
3. Merge to `main`. Run Release npm from `main` with publish on, and approve `npm` when the publish job asks.

npm never takes the same version twice, so if anything goes wrong after an upload, the fix is a new version number. The workflow stops before building if the version is on npm already.
