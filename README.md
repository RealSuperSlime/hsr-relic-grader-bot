# HSR Relic Grader — Discord bot

Grades relic screenshots in Discord with the same local OCR and Fribbels-exact scoring
as [hsr-relic-grader](https://github.com/RealSuperSlime/hsr-relic-grader), the engine
this bot is a front end for. Nothing is uploaded anywhere — the image is downloaded to
your PC and processed in this process.

**Needs:** Python 3.10+ and the grader package, which `requirements-bot.txt` installs
straight from GitHub.

## One-time setup

0. **Install** — `start_bot.bat` does all of this for you on first run. By hand it is:

   ```bat
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements-bot.txt
   .venv\Scripts\hsr-relic-grader-tesseract.exe     :: fetches Tesseract, once
   ```

   If you already have the grader repo checked out in the **parent** folder, the bot
   imports the engine from there instead of the installed copy — handy while editing it.

1. **Create the bot app** — https://discord.com/developers/applications → *New Application* →
   **Bot** tab → *Reset Token* → copy the token.
2. **Turn on the message intent** — same **Bot** tab → *Privileged Gateway Intents* →
   enable **Message Content Intent**. Without this `!grade` never fires.
3. **Put the token in `.env`** — copy `.env.example` to `.env` in this folder and paste
   your token after `DISCORD_BOT_TOKEN=` (no `Bot ` prefix, no quotes). `.env` is
   git-ignored, so the token never leaves your machine; `.env.example` is the shareable
   version and holds no real value. You can also export `DISCORD_BOT_TOKEN` instead —
   an environment variable wins over the file.
4. **Invite it** — *OAuth2 → URL Generator* → scopes `bot` + `applications.commands`,
   permissions **`View Channels`, `Send Messages`, `Embed Links`** (that is the whole
   minimum; the permission integer is `19456`) → open the generated URL and pick your
   server. Add `Send Messages in Threads` only if you want `!grade` to work inside threads.
5. **Start it** — double-click **`start_bot.bat`**. Leave the window open; the bot stops
   when you close it.

### If a slash command doesn't show up

Discord keeps two separate slash-command lists, and the bot needs to be **restarted**
after its commands change:

* **Restart the bot** — `bot.tree.sync()` runs on startup, so a command added while it was
  running doesn't exist yet. The console prints exactly what registered:
  `Synced 2 slash command(s) globally: grade, uid`.
* **Global commands lag.** New global commands can take up to an hour to appear, and the
  Discord client caches the list — press **Ctrl+R** to refresh it.
* **Make it instant:** turn on *Developer Mode* (Discord → User Settings → Advanced),
  right-click your server → *Copy Server ID*, and paste it into **`guild.txt`**. Restart
  the bot and the commands are published to that server immediately.
* Still nothing? Run **`!sync`** in the server (bot owner only) to force a re-sync, and
  check the console — if it says the bot is missing the `applications.commands` scope,
  re-invite it with scopes `bot` + `applications.commands`.

`start_bot.bat` creates the Python environment on first run and installs
`requirements-bot.txt`. If a `.venv` exists in the parent folder it reuses that one
instead of making a second copy.

## Using it

| command | what it does |
|---|---|
| `!grade` | shows a paged character list (25 per page, ◀ ▶ buttons), then asks for the screenshot |
| `!grade Acheron` | skips the picker — name is fuzzy (`acheron`, `dan heng imbibitor`, `1308`) |
| `!uid 101000000` | look up a player's showcase by UID and grade **every** character on it |
| `!uid 101000000 herta` | same, filtered to characters whose name matches |
| `!characters` | how many characters are loaded, with examples |
| `!graderhelp` | short help embed |
| `/grade` | slash-command version of `!grade` — the character name autocompletes |
| `/uid` | slash-command version of `!uid` |

Flow: `!grade` → pick the character from the dropdown → attach the relic screenshot to
your next message in that channel → the bot replies with an embed:

```
Eagle's Quilted Puttees
Eagle of Twilight Line · Feet · 5★ +15
Score      Main stat        Scored for
63.3       SPD 25           Boothill
SS

Substats · card · weight · points
HP%        7.3   w —      → 0.00
CRIT Rate  6.1   w 0.25   → 3.08
Effect RES 3.4   w —      → 0.00
Break Effect 24.6 w 1.00  → 24.62
Breakdown: substat points 27.70 / ideal 43.74
```

### Looking up a player (`!uid`)

`!uid <uid>` pulls the player's public **Showcase** and grades every character on it —
one embed per character, with the character's total grade in the title and each of the
six equipped pieces underneath (piece score, letter grade, main stat, and every substat
with its roll badge, weight and points):

```
Gilgamesh — 69.9 SS+          Phainon — 62.8 SS
Lv 80 · E2 · average of 6     Lv 80 · average of 6 pieces
Head    85.5 WTF+             Head    62.7 SS
Hands   77.9 SSS+             Hands   80.0 WTF
Body    76.2 SSS+             Body    68.4 SS+
Feet    53.0 S                Feet    67.6 SS+
Sphere  68.3 SS+              Sphere  37.8 B+
Rope    58.3 S+               Rope    60.2 SS
```

* The character grade is the average of the six piece scores, same as Fribbels.
* Relic stats come from the API's numeric affix ids, converted with the same
  `relic_main_affixes.json` / `relic_sub_affixes.json` tables the optimizer uses, then
  scored by the same engine as the screenshot path — so a piece scores the same either way.
* Only what the player put on showcase is visible. If they have it turned off you get a
  clear message instead of an empty embed.
* Results are cached for 15 minutes, and the service is shared, so don't hammer it.
* Characters newer than the bundled weights are listed but shown as "not graded".

Notes:

* One image per request, 3 minutes to upload; the OCR runs on the CPU and takes a few
  seconds, so the bot posts "Reading the screenshot…" first.
* If a stat is misread the embed lists what it was unsure about under **⚠ OCR warnings** —
  check that before trusting the grade.
* The token in `.env` is a password. Don't commit it or paste it in chat; if it ever
  leaks, hit *Reset Token* in the developer portal.
* `DISCORD_TOKEN` in the environment overrides the file if you prefer that.
