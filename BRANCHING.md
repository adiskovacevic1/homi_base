# How to work on this repo

For anyone changing the code, human or agent. Several sessions often work here at once, and the code runs in people's
houses, so the rules below are about not stepping on each other and not shipping something broken to a family's bot.

## The branches

| Branch | Who runs it | May it break? |
|---|---|---|
| `feature/<name>` | you, alone | yes, that is what it is for |
| `dev` | nobody; the shared integration branch | briefly, if you fix it |
| `main` | the maintainer's own install | no; it is someone's real bot |
| `stable` | every household | never; only tried versions |

Work flows one way: `feature/…` → `dev` → `main` → `stable`. Nothing skips a step except a one-line fix the maintainer
puts straight on `main`.

```
  feature/lan-helper ──┐
  feature/voice-fix  ──┼──► dev ──────► main ──────► stable
  feature/whatever   ──┘    merge       tried on     released
                            when it     the owner's  to every
                            hangs       own install  household
                            together
```

## The loop

**1. Start from dev, never from your last branch.**

```bash
git fetch origin
git checkout -b feature/short-name origin/dev
```

Name it after the thing, not the ticket: `feature/vault-passphrase`, `feature/ssdp-discovery`.

**2. Work in small commits.** Say what changed and why it was wrong before; the next reader is usually a different
session with none of your context. End the message with your own `Co-Authored-By:` line.

**3. Test it where it runs.** Not "the code looks right" — build the image and exercise the path in the container:

```bash
docker compose build example-bot && docker compose up -d example-bot
docker compose logs --since 60s example-bot
docker compose exec -T example-bot python bot.py --ask "..."
```

A change to the setup or console page means opening it in a browser and clicking through it. A change to the forge
means letting it build one real tool.

**4. Catch up before you merge**, by rebasing onto dev, so the conflict is yours to fix and not dev's:

```bash
git fetch origin && git rebase origin/dev     # resolve here, on your branch; it is yours, rewriting it is fine
```

**5. Fast-forward dev to your branch and delete it:**

```bash
git switch dev && git pull --ff-only          # `switch`, not `checkout`: the repo has a dev/ folder too
git merge --ff-only feature/short-name
git push origin dev
git branch -d feature/short-name && git push origin --delete feature/short-name
```

History stays linear all the way up. `main` and `stable` enforce that (a merge commit cannot be pushed to them), and
because everything flows by fast-forward, `main` is always a prefix of `dev` and `stable` a prefix of `main`. If
`--ff-only` refuses, someone pushed to dev after your rebase: rebase again and retry.

**6. CI decides whether it merges.** Every push to a `feature/**` branch, `dev` or `main` runs `.github/workflows/ci.yml`:
the secrets guard, parse checks for every language in the repo, the vault and setup tests in `tests/`, a build of the
bot and dev images, and a start of the stack with no configuration. Do not merge red into `dev`, and if `dev` is red
after your merge, that is yours to fix now. Run the same checks locally before pushing: `bash scripts/check-no-secrets.sh`
and `pytest tests` (or the equivalent inside the dev image). The voice image builds nightly, and a push to `stable`
tags a release.

**7. Promoting is not your call.** `dev` → `main` is a fast-forward (`git push origin dev:main`) when a set of changes
hangs together and CI on `dev` is green; `main` → `stable` happens through `./release.sh`, which lists what would reach
every install and asks for confirmation. Do not push to `stable`, ever, and do not force-push any shared branch.

## Rules that matter more than the flow

- **Never commit a secret.** `.env` files, `data/`, `vault.enc` and the kits are git-ignored; keep them that way.
  Before `git add -A`, look at `git status`. If a secret ever lands in a commit, say so immediately: the fix is
  deleting the remote repo, not a follow-up commit.
- **Kits are households' data, not code.** `bots/*/kits/` is ignored on purpose. Never track one.
- **Another session's uncommitted work is not yours to clean up.** Check `git status` before anything destructive.
  If the tree has changes you did not make, leave them and work on your own branch.
- **Do not rewrite shared history.** Rebase your own feature branch as much as you like; never `dev`, `main` or `stable`.
- **The README is a conflict hotspot** because everyone edits it. Keep your edit to the section you actually changed.
- **If you break `dev`, fix `dev` first**, before anything else. Other sessions are branching off it.

## Releasing and rolling back

```bash
./release.sh          # main -> stable, after you have lived with it on a real install
./update.sh           # what a household runs; --check to look first
```

A release that misbehaves is undone by moving `stable` back:

```bash
git push origin <older-commit>:refs/heads/stable --force-with-lease
```

That is the one exception to never force-pushing, and only on `stable`, and only to undo.
