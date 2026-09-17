# homi_base

A Discord bot for a household that builds its own tools, plus a voice bot that shares its brain, running in
Docker on a PC in the house. This repo is the code. The tools a bot builds for its household are its **kit**, a
folder this repo ignores, so the code can be public and shared while every household's tools stay its own.

- **dev** — one small always-on Debian box (Python 3.12, Node 22, git, Claude Code). It runs the **forge**, which
  builds tools for the bot on request, and the setup page. You can also shell into it; the code is under `/bots`.
- **one container per bot** — each bot has its own folder under `bots/<name>/` with a Dockerfile, a `.env` for its
  secrets, and a `data/` folder for anything it must keep. `example-bot` is the text bot, `voice-bot` the voice one.

## Install

Needs Docker Desktop (Windows, macOS) or Docker Engine (Linux), a Discord bot of your own and an Anthropic API
key. ElevenLabs is optional.

```bash
git clone https://github.com/adiskovacevic1/homi_base.git
cd homi_base
./install.sh          # Windows: right-click install.ps1 -> Run with PowerShell
```

It builds the three images, then opens a setup page in your browser (served from the dev container on
`localhost:8792`, one-time key in the link, gone once you save). The page asks for the bot token, your API key(s),
your Discord user ID, a name, optional ElevenLabs, and a kit name, and checks each as you type: the token is
tried against Discord and shows the bot's name and whether **Message Content Intent** is on, the one step people
miss and without which the bot hears nothing; the Anthropic key is tried; an ElevenLabs key turns the voice field
into a list of your voices with previews; owner IDs resolve to usernames. Saving writes the `.env` files, shows
the invite link, and the installer starts everything. `./install.sh --terminal` (or `-Terminal` in PowerShell)
asks the same questions in the terminal instead, for a machine without a browser; there, paste with a right-click,
since Ctrl+V does not paste into a Docker terminal on Windows. The bot starts with nine generic tools (weather, web fetch, YouTube
search, memory, file handling, and Discord's own API scoped to reading, posting and the bot's own messages) from
`bots/example-bot/starter-tools/` and grows its own kit from there, so every
install becomes its household's bot rather than a copy of someone else's. Re-run the installer any time to
reconfigure. To update, in the repo folder:

```bash
git pull
docker compose up -d --build
```

That rebuilds only the images whose code changed and swaps the running containers; kits, memory, uploads and
settings are untouched because they live outside the images (see "How code gets into the containers" below).

## Daily use

```bash
docker compose up -d dev                 # start the dev box (once; it restarts with Docker)
docker compose exec dev bash             # shell in; you land in /bots
docker compose exec dev claude           # Claude Code inside the container (login persists in a volume)

docker compose up -d --build example-bot # build + run a bot
docker compose logs -f example-bot       # watch it
docker compose up -d example-bot         # after editing its .env (restart won't re-read it)
docker compose down                      # stop everything (data and volumes are kept)
```

## Making a new bot

```bash
cp -r bots/example-bot bots/my-bot
```

Edit `bots/my-bot/bot.py`, then add a service to `docker-compose.yml` by copying the `example-bot` block
and changing the three `example-bot` names. Put its secrets in `bots/my-bot/.env` (copy `.env.example`).
`docker compose up -d --build my-bot` and it is live, restarts on failure, and comes back when Docker starts.

## The template bot

`bots/example-bot/bot.py` is a Discord bot that answers when @mentioned or DMed, using Claude
(`claude-opus-5`, adaptive thinking, medium effort, server-side refusal fallbacks) with a short per-channel
memory. Without a `DISCORD_TOKEN` it idles and says so, so an empty template still starts cleanly.

```bash
docker compose run --rm example-bot python bot.py --ask "what can you do?"   # one Claude call, no Discord
```

### Attachments

Drop an image, a PDF or a file on it, with or without text. Images and PDFs are shown to Claude directly;
text-like files (`.txt`, `.md`, `.json`, `.csv`, code, logs) are read inline; anything else is saved under
`bots/example-bot/data/uploads/` and the path is passed to Claude so a tool can act on it — that is how a
`.torrent` dropped on the bot can be handed to something else. Limits: 4 images and 10 MB per attachment.
Images and PDFs are replaced by a one-line memo in the channel history after the reply, so later turns
do not resend them.

### Secrets

The bot process owns an encrypted vault (`data/secrets_manager/vault.enc`); the key is `VAULT_KEY` in `.env`, which
tools cannot read (they see only `/data`). A tool declares the secret names it needs in its spec
(`"secrets": ["XAI_API_KEY"]`, or the `secrets` argument of `create_tool`) and receives them as environment
variables when it runs. There is no way for the model to read a value back: the `secrets` tool only lists,
sets and deletes by name, and is restricted to `TOOL_CREATORS`. To load a secret without it ever appearing in
chat:

```bash
echo -n "the-value" | docker compose exec -T example-bot python bot.py --secret-set XAI_API_KEY
docker compose exec -T example-bot python bot.py --secrets      # names and notes only
```

### It writes its own tools

When a question needs something the bot can't do, it writes a Python tool for itself, calls it, and keeps
it — so the kit grows with use instead of being designed up front. Ask it for the SHA-256 of a string and
it writes `sha256` once; the next person to ask just gets an answer. It gets three built-in tools to manage
the rest: `create_tool`, `read_tool` and `delete_tool`.

Each tool is a pair of files in the household's **kit**, `bots/example-bot/kits/<name>/`, which compose mounts at
`/data/tools` inside the container (the name comes from `KIT=` in the root `.env`, written by setup). This repo
ignores the kits folder entirely: a kit is the household's data, not the project's code, so it never travels
with a `git pull` and never ends up in someone else's clone. Setup makes the kit a small git repo of its own, so
every tool the forge builds is a commit, and if you give setup a URL (a private repo of yours) the kit has
somewhere to push for backup. The files are plain and readable in any editor, and deleting a pair removes the tool:

```
bots/example-bot/kits/<name>/sha256.json   the spec the model sees
bots/example-bot/kits/<name>/sha256.py     the code, defining run(**kwargs)
docker compose exec example-bot python bot.py --tools   # list what it has written
cd bots/example-bot/kits/<name> && git push -u origin main   # back the kit up (origin set by setup)
```

The bot process never imports generated code. Every call — and one self-test at creation — runs in a
separate `python`, so a crash, a hang or an infinite loop costs a subprocess instead of the bot. A tool
that won't import is rejected and the model is told why, which it can usually fix on the next turn;
replacing a working tool with a broken one keeps the version that worked. Tools get the standard library,
`httpx` and `anthropic`, a `TOOL_TIMEOUT` (30s) per call, and no access to the conversation.

### It runs its own machine

It is root in its own container and has two more tools: `shell` runs any bash command, and `install` adds
software. Both persist properly, which is the part that needs care — a container is rebuilt often (every
`.env` edit), and anything installed by hand at a shell is lost when that happens:

- **pip** installs into `/data/pylibs` via `PYTHONUSERBASE`, so it is on the volume and simply survives.
- **apt** can't live on the volume, so `install` records the package in `/data/apt-packages.txt` and the
  bot reinstalls anything missing at startup. You'll see `reinstalling apt packages after rebuild: …`.

So `install` is durable and `shell` is not, and the bot is told that.

### It can have tools built for it

`create_tool` is the model writing code in one shot, with one import check. For anything that has to talk to a
device or a service, needs auth, or should be tested against the real thing, the bot calls `request_tool` instead
and hands a brief to the **forge** on the dev box (`dev/forge.py`, port 8791, compose network only). The forge
starts a headless Claude Code in a fresh directory under `/root/work` on the dev box's volume, where it reads the
existing tools for style, writes the pair, runs it through a harness that behaves exactly like the bot (fresh
python, 30s), tests it against the real target on the LAN, and iterates. The forge then checks the result the way
`create_tool` would, moves the two files into the kit with one rename each (the bot rescans that folder
every round, so it never sees a half-written tool), commits the kit, and returns a summary.

The bot does not wait for that. `request_tool` starts the build in the background and returns at once, so the
model tells the person "having that built, I'll follow up here" and ends its turn; other questions carry on
meanwhile. When the forge answers, the bot wakes the model in the same channel with a follow-up turn that carries
the tool's status, the forge's notes on using it, and the person's original message. The model then calls the new
tool and posts the answer as a reply to that message, pinging the person, or explains what went wrong if the build
failed. By voice the same happens, with the follow-up posted to the voice session's text channel. A build that is
still running when the bot restarts still lands, but the follow-up for it is lost.

Claude Code runs there without a permission bypass: an allowlist lets it read anywhere, write only inside its work
directory, and run exactly two commands, the harness and `pip install --user` (into the bot's `/data/pylibs`, so
the bot gets the package too). It has no other shell; it probes the world through the tool it is writing.
`request_tool` is restricted to `TOOL_CREATORS` like `create_tool`.

Setup: the forge reads `INTERNAL_TOKEN` from `bots/example-bot/.env` (the one the voice bot already shares), and
Claude Code there uses the bot's API key unless someone has logged in with `docker compose exec dev claude`. The bot
offers `request_tool` when `INTERNAL_TOKEN` is set; `FORGE_URL=` in `.env` turns it off and `FORGE_TIMEOUT` (480s)
is how long the bot waits. A build costs roughly what a few minutes of a coding agent costs, about a dollar.

```bash
docker compose up -d --build dev                        # the dev box now runs the forge; a shell into it still works
docker compose exec dev curl -s localhost:8791/health   # {"ok": true, "busy": false, ...}
docker compose exec dev ls /root/work                   # one folder per build: brief, prompt, output, RESULT.json
docker compose logs -f dev
```

Work folders are kept a week. If a tool comes out wrong, `RESULT.json` says what was tested, `claude-output.json`
is the full run, and `previous/` holds the version a rebuilt tool replaced.

### It proposes its own next tools

Once a day (`IDEAS_AT`, default 09:00 in the container's `TZ`; empty turns it off) the bot reads the last day of
every channel it can see, looks at its kit, and posts one to three concrete tool ideas grounded in what people
asked for, tried, or hit a wall on, each with the moment that suggested it. It posts to `IDEAS_CHANNEL` or, unset,
to the day's busiest channel, and the post joins that channel's memory, so replying **build 2** has the forge
build that one. A quiet day produces nothing. `docker compose exec example-bot python bot.py --ideas --dry` runs
the review now and prints it instead of posting.

### What it can reach

The container runs as root, and Docker's network isolation is not a boundary against your LAN. From inside
it the bot can reach the internet, your router's admin page, this PC's file shares, and every device on the
network, plus anything the PC reaches over a VPN. That is what makes it useful for a household, lights, TVs,
a pool controller, and it means a tool the bot writes runs with all of that reach.

`TOOL_CREATORS` is therefore the real security boundary, not a nicety. The tools that touch the
machine — `create_tool`, `request_tool`, `delete_tool`, `shell`, `install`, `secrets` — are refused for anyone not on that list;
everyone else can still talk to the bot and use the tools that already exist. Leaving it empty means
anyone who can message the bot gets root in a container that can see your whole network, so the bot warns
at startup when it is unset.

## The voice bot

`bots/voice-bot` joins a Discord voice channel, listens, answers with Claude and talks back. With an
`ELEVENLABS_API_KEY` in its `.env` it hears through ElevenLabs Scribe and speaks with Flash v2.5 in the voice
set by `ELEVEN_VOICE_ID`; without one, speech is entirely local, faster-whisper (CPU, `base.en`) for hearing and
Piper for speaking, and only the Claude call leaves the machine. The local engines stay in the image as the
fallback: an ElevenLabs call that fails is retried locally, so a bad key or an outage costs a second, not the
answer. It logs in with the same Discord token as example-bot, so it is the same bot from Discord's point of
view — the two just answer different things (example-bot ignores `!` commands).

```bash
docker compose up -d --build voice-bot
docker compose exec voice-bot node index.js --selftest   # tts -> stt -> claude -> tts, no Discord; prints which engines
docker compose exec voice-bot node index.js --voices     # the ElevenLabs voices on the account, to pick ELEVEN_VOICE_ID
docker compose logs -f voice-bot
```

In Discord it comes by itself: when someone enters a voice channel listed in `AUTO_JOIN` (default `general`),
it joins and posts what it hears and says in that channel's own text chat. It leaves when the channel has been
empty for `EMPTY_LEAVE_MS` (15 s), when someone tells it to ("hey bot, bye", "hey bot, leave", "go away"), or
on `!leave`. `!join` in a text channel still pulls it into whatever channel you are in, and `!voice` shows
status. Say the wake word first — "hey bot, …" (list in `WAKE_WORDS`; empty = answer everything). Round trip is
a few seconds: silence detection, transcription, Claude at low effort, synthesis. The bot needs Connect and
Speak permission in the voice channel, and View Channel on it to see who is there.

**One brain.** With `BRAIN_URL` and a shared `INTERNAL_TOKEN` set, the voice bot sends what it hears to
the text bot's internal `/ask` endpoint (port 8790, compose network only) and speaks the answer, so voice gets
the same tools and memory as text — "hey bot, what's the pool status" actually checks the pool. If the text bot
is down it falls back to answering with Claude directly, without tools.

It hears everyone in the channel, so tell people it is there; the wake word keeps casual chat out of the
model. Utterances shorter than `MIN_UTTERANCE_MS` (600) are dropped as noise.

## How code gets into the containers

An image is a snapshot of the code taken at build time; a container is that snapshot running. Editing a file on
disk changes nothing until `docker compose up -d --build` takes a new snapshot and swaps the container (a few
seconds of downtime; the layer cache makes the rebuild quick). A few folders are instead **mounted** straight
from disk, so both sides see the same files live: the kit at `/data/tools`, the bot's `data/`, and the whole
repo in the dev box. That is why a tool the forge writes is usable seconds later with no restart, and why
rebuilding never loses a tool. Settings in `.env` files are read when a container is created, so after editing
one run `docker compose up -d` (a plain `restart` does not re-read them).

## Notes

- Images are Debian slim, not Alpine: Python wheels install without compiling and the size difference is
  small where it matters (a bot image is ~270 MB, the voice image ~2 GB with its speech model, the dev box
  ~1.3 GB because of Node and Claude Code).
- Bot services have a memory cap in the compose file: 512 MB is plenty for a plain bot; `example-bot` gets
  2 GB because it installs packages itself, `voice-bot` 3 GB for the local speech model.
- Local model inference is not attempted; the bots call hosted models. The PC only needs to run Docker.
- The compose project is named after the folder and containers after the project, so two checkouts (say, a
  test install) run side by side on one PC without colliding.
