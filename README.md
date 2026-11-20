# RobloxCycler

RobloxCycler equips outfits you already saved on your own account, one after another, so your public avatar keeps changing while the program is running.

It signs in with your browser cookie, reads your saved Avatar outfits, and calls Roblox’s avatar API. If Roblox answers `429 Too Many Requests`, it stops hammering, waits, and makes the gap between changes longer.

The configured gap is **10 seconds**, and each change is one wear request. Five seconds is the fastest the program will attempt. That pace sends about 12 requests a minute, which is over the roughly 10 a minute these outfit routes have been reported to allow. If Roblox answers `429`, the gap doubles so the account is less likely to get restricted. If Roblox blocks the account from the API, the program stops.

## What you need

- Python 3.11 or newer
- At least two outfits saved on the account you want to change
- That account’s `.ROBLOSECURITY` cookie, copied from a browser you are already logged into

The cookie is a full login. It can spend Robux and take items. Keep it in `.env` only. Do not commit it, do not send it to anyone, and do not paste it into a chat.

## Set it up

Open PowerShell in this folder.

```powershell
cd C:\Users\laugh\Documents\RobloxCycler
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

If Windows blocks the activate script, run the tool with the venv Python directly:

```powershell
.venv\Scripts\python.exe main.py outfits
```

### Put the cookie in `.env`

1. Log into [roblox.com](https://www.roblox.com) in your browser.
2. Press F12, open **Application** (Chrome) or **Storage** (Firefox).
3. Open **Cookies** for `https://www.roblox.com`.
4. Copy the value of `.ROBLOSECURITY`.
5. Paste it after the equals sign in `.env`:

```text
ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS...
```

The value usually starts with `_|WARNING`. Paste the whole value. Do not add quotes.

### Save two outfits

On the Roblox website, open your avatar, dress it, and save the outfit. Change the look and save a second outfit. Those are the looks the cycler will swap.

## Check that it works

```powershell
python main.py whoami
python main.py outfits
python main.py once
```

`whoami` prints the username the cookie belongs to. If it is the wrong account, stop and replace the cookie.

`outfits` writes `outfits.json` and prints which looks are in the cycle. Set `"enabled": false` on any outfit you want to leave alone, then run `outfits` again to confirm.

`once` equips the next outfit a single time. Hard-refresh your profile. The 3D avatar updates first. The flat thumbnail image can stay on the old picture for a while. That delay is Roblox’s image cache, not a failed equip.

## Leave it running

```powershell
python main.py run
```

Leave that window open. Ctrl+C stops it. Closing the laptop or shutting down the PC stops it too.

Each successful line looks like this:

```text
Equipped "Red jacket" (123456789). Next change in 60s.
```

The same history is appended to `logs/cycler.log`. `state.json` remembers which outfit comes next, so a restart continues the rotation instead of starting over.

### Run it when you sign in to Windows

1. Open **Task Scheduler** and create a basic task.
2. Trigger: **When I log on**.
3. Action: **Start a program**.
4. Program: `C:\Users\laugh\Documents\RobloxCycler\.venv\Scripts\python.exe`
5. Arguments: `main.py run`
6. Start in: `C:\Users\laugh\Documents\RobloxCycler`

### Run it on another computer

A free cloud server is optional. The program is a normal Python process, so any machine that stays on can run `python main.py run` after you copy the repo and create `.env` there. Do not put the cookie in the Docker image or in git.

```powershell
docker build -t robloxcycler .
docker run --rm -it --env-file .env -v "${PWD}:/app" robloxcycler
```

Oracle Cloud’s always-free VM is the usual way to keep a small Python process up when this PC is off. A laptop that sleeps will not keep cycling.

## When Roblox tells you to slow down

A `429` is a rate limit. The cycler:

1. Stops equipping immediately.
2. Waits for the `Retry-After` time, or a backoff if that header is missing.
3. Doubles the gap between outfits, up to `max_interval_seconds` (300 by default). At 5 seconds that becomes 10, then 20, then 40.
4. Saves that slower gap in `state.json`.
5. Continues.

A `403` that is not the normal login-token check, or a message that the account is banned, moderated, or restricted, stops the program. Wait until that block lifts, then start it again.

`python main.py reset` clears the automatic slowdown and goes back to the interval in `config.json`.

If the cookie starts returning `401`, it expired. Copy a new one into `.env`. Log out of that browser session when you are done if you do not want the old cookie to keep working.

## How fast to go

| Interval | What to expect |
| --- | --- |
| 10 seconds | Current setting. About 6 equips a minute, under the reported limit. |
| 5 seconds | Fastest this program allows. About 12 equips a minute, which is easy to get limited at. |
| After a 429 | The gap doubles: 10s, then 20s, then 40s, up to 300s. |
| Under 5 seconds | Raised back to 5 seconds. |

Developers have reported roughly 10 requests a minute on related outfit routes. Roblox also says extra, unpublished limits can apply, and the response headers are what you should obey. This tool reads `Retry-After` and the `x-ratelimit-*` headers and backs off. It does not try to get around a limit.

Change the pace in `config.json`:

```json
{
  "interval_seconds": 10
}
```

## Settings

`config.json`

| Field | Meaning |
| --- | --- |
| `interval_seconds` | Seconds between successful equips. This project uses 10. Values under 5 are raised to 5. |
| `max_interval_seconds` | Ceiling after automatic slowdowns. Default 300. |
| `slowdown_factor` | How much the gap grows after a 429. Default 2, so 10 becomes 20. |
| `on_rate_limit` | `pause` waits, lengthens the gap, and keeps going. `stop` exits on a 429 instead. An API block exits either way. |
| `request_timeout_seconds` | How long one Roblox request may hang. |
| `outfit_ids` | Leave `[]` to use every enabled outfit in `outfits.json`. Or list ids, in cycle order: `[111, 222]`. |

`outfits.json` is recreated from your account each run. Names are refreshed. `"enabled"` flags you already set are kept.

## Commands

| Command | What it does |
| --- | --- |
| `python main.py whoami` | Prints the account the cookie belongs to. |
| `python main.py outfits` | Lists saved outfits and writes `outfits.json`. |
| `python main.py once` | Equips the next outfit one time. |
| `python main.py run` | Keeps cycling until you stop it. |
| `python main.py reset` | Forgets the rate-limit slowdown. |

## If something looks wrong

| What you see | What to do |
| --- | --- |
| `No cookie found` | Create `.env` from `.env.example` and paste the cookie value. |
| `Roblox rejected the cookie` | Copy `.ROBLOSECURITY` again. Cookies die when you log out or change your password. |
| `You need at least two saved Avatar outfits` | Save two outfits in the avatar editor on the website. |
| The profile picture did not change | Hard-refresh the profile. Confirm `once` printed `Equipped`. The 2D image can lag behind the 3D avatar. |
| `Rate limited` | Leave the default `pause` behavior, or set a higher `interval_seconds` and run `reset`. |
| Every outfit failed | An item in those outfits may no longer be owned. Check `logs/cycler.log`, then disable that outfit. |

Roblox is removing a batch of older avatar routes on December 11, 2026. Equipping still uses the outfit wear route today. If that route disappears, the cycler reads the outfit and sends it through `PATCH /v4/avatar` instead.

## Layout

```text
main.py              commands
robloxcycler/        Roblox calls and the timing rules
config.json          how fast to cycle, and what to do on a 429
.env                 your cookie, not committed
outfits.json         created on first run, not committed
state.json           rotation and slowdown, not committed
logs/cycler.log      one line per equip, not committed
```
