"""
TV Stack Discord control bot.

Commands (prefix: !):
  !help                   - show this list
  !status                 - all container states
  !restart <service>      - restart one container
  !restart all            - restart every stack container (needs confirmation)
  !up                     - docker compose up -d (starts/creates all containers)
  !down                   - docker compose down (stops + removes, needs confirmation)
  !logs <service>         - last 30 lines of logs
"""

import asyncio
import os
import subprocess

import discord

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CONTROL_CHANNEL"])
ALLOWED_USER_ID = int(os.environ.get("DISCORD_ALLOWED_USER_ID", "0"))
COMPOSE_DIR = "/compose"

STACK_SERVICES = [
    "gluetun", "qbittorrent", "flaresolverr", "prowlarr", "sabnzbd",
    "radarr", "sonarr", "lidarr", "bazarr", "seerr", "tautulli",
    "homepage", "tailscale", "watchtower", "deunhealth", "media-check",
    "discord-bot",
]

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def shell(args: list[str], timeout: int = 120) -> tuple[str, int]:
    """Run a command and return (stdout+stderr, returncode)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=COMPOSE_DIR,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace"), proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            return "Command timed out.", 1
    except Exception as exc:
        return str(exc), 1


def status_emoji(status: str) -> str:
    s = status.lower()
    if s.startswith("up") and "unhealthy" not in s:
        return "🟢"
    if "unhealthy" in s:
        return "🟡"
    return "🔴"


async def get_status_lines() -> str:
    out, _ = await shell(["docker", "ps", "-a",
                          "--format", "{{.Names}}\t{{.Status}}",
                          "--filter", "name=" + "|".join(STACK_SERVICES)])
    rows = []
    seen = set()
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        seen.add(name)
        rows.append(f"{status_emoji(status)} **{name}** — {status}")
    # Show missing expected containers
    for svc in STACK_SERVICES:
        if svc not in seen:
            rows.append(f"⚫ **{svc}** — not found")
    return "\n".join(rows) or "No containers found."


async def ask_confirm(message: discord.Message, prompt: str) -> bool:
    """Ask for ✅ reaction confirmation. Returns True if confirmed within 15s."""
    confirm_msg = await message.channel.send(prompt)
    await confirm_msg.add_reaction("✅")
    await confirm_msg.add_reaction("❌")

    def check(reaction: discord.Reaction, user: discord.User) -> bool:
        return (
            user == message.author
            and reaction.message.id == confirm_msg.id
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _ = await client.wait_for("reaction_add", timeout=15.0, check=check)
        return str(reaction.emoji) == "✅"
    except asyncio.TimeoutError:
        await message.channel.send("Timed out — cancelled.")
        return False


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message) -> None:
    if message.channel.id != CHANNEL_ID:
        return
    if message.author.bot:
        return
    if ALLOWED_USER_ID and message.author.id != ALLOWED_USER_ID:
        return

    content = message.content.strip()
    if not content.startswith("!"):
        return

    parts = content.split()
    cmd = parts[0].lower()
    args = parts[1:]

    # ── !help ─────────────────────────────────────────────────────────────────
    if cmd == "!help":
        await message.channel.send(
            "**TV Stack Control**\n"
            "`!status` — all container states\n"
            "`!restart <service>` — restart one container\n"
            "`!restart all` — restart every container *(confirms first)*\n"
            "`!up` — `docker compose up -d` (starts/recreates all containers)\n"
            "`!down` — `docker compose down` *(confirms first)*\n"
            "`!logs <service>` — last 30 lines of container logs\n"
            "`!services` — list valid service names"
        )

    # ── !services ─────────────────────────────────────────────────────────────
    elif cmd == "!services":
        await message.channel.send(
            "**Services:** " + ", ".join(f"`{s}`" for s in STACK_SERVICES)
        )

    # ── !status ───────────────────────────────────────────────────────────────
    elif cmd == "!status":
        async with message.channel.typing():
            body = await get_status_lines()
        await message.channel.send(body)

    # ── !logs <service> ───────────────────────────────────────────────────────
    elif cmd == "!logs":
        if not args:
            await message.channel.send("Usage: `!logs <service>`")
            return
        svc = args[0]
        if svc not in STACK_SERVICES:
            await message.channel.send(
                f"Unknown service `{svc}`. Use `!services` to list valid names."
            )
            return
        async with message.channel.typing():
            out, _ = await shell(["docker", "logs", "--tail=30", svc])
        if not out.strip():
            out = "(no output)"
        # Trim to fit Discord's 2000-char message limit
        if len(out) > 1850:
            out = "…(truncated)\n" + out[-1850:]
        await message.channel.send(f"```\n{out}\n```")

    # ── !restart <service | all> ──────────────────────────────────────────────
    elif cmd == "!restart":
        if not args:
            await message.channel.send("Usage: `!restart <service>` or `!restart all`")
            return
        target = args[0].lower()

        if target == "all":
            confirmed = await ask_confirm(
                message, "⚠️ Restart **all** containers? React ✅ to confirm."
            )
            if not confirmed:
                return
            async with message.channel.typing():
                out, code = await shell(
                    ["docker", "compose", "restart"], timeout=180
                )
            icon = "✅" if code == 0 else "❌"
            summary = out.strip()[-600:] or "(no output)"
            await message.channel.send(f"{icon} Restarted all containers.\n```\n{summary}\n```")

        elif target in STACK_SERVICES:
            async with message.channel.typing():
                out, code = await shell(["docker", "restart", target])
            icon = "✅" if code == 0 else "❌"
            await message.channel.send(f"{icon} `{target}` restarted.")

        else:
            await message.channel.send(
                f"Unknown service `{target}`. Use `!services` to list valid names."
            )

    # ── !up ───────────────────────────────────────────────────────────────────
    elif cmd == "!up":
        await message.channel.send("🚀 Running `docker compose up -d`…")
        async with message.channel.typing():
            out, code = await shell(
                ["docker", "compose", "up", "-d"], timeout=300
            )
        icon = "✅" if code == 0 else "❌"
        summary = out.strip()[-800:] or "(no output)"
        await message.channel.send(f"{icon} Stack started.\n```\n{summary}\n```")

    # ── !down ─────────────────────────────────────────────────────────────────
    elif cmd == "!down":
        confirmed = await ask_confirm(
            message,
            "⚠️ This runs `docker compose down` — stops **and removes** all containers. React ✅ to confirm.",
        )
        if not confirmed:
            return
        await message.channel.send("🛑 Running `docker compose down`…")
        async with message.channel.typing():
            out, code = await shell(["docker", "compose", "down"], timeout=300)
        icon = "✅" if code == 0 else "❌"
        summary = out.strip()[-800:] or "(no output)"
        await message.channel.send(f"{icon} Stack stopped.\n```\n{summary}\n```")

    # ── unknown ───────────────────────────────────────────────────────────────
    else:
        await message.channel.send("Unknown command. Try `!help`.")


client.run(TOKEN)
