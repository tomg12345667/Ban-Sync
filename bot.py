"""
Ban Sync Bot
============
When a member is banned in any linked server (by a human, AutoMod, or any other bot),
every other linked server gets an approval request with Approve / Deny buttons.
Only the server owner, administrators, or configured approver roles can decide.

All configuration (/bansync ...) is restricted to the bot owner.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from db import Database

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID") or 0) or None
EXTRA_OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "bansync.db")
# Allow bot owners to click Approve/Deny in any server (default: off, servers decide for themselves).
OWNER_CAN_APPROVE = os.getenv("OWNER_CAN_APPROVE", "false").lower() in {"1", "true", "yes"}

TAG = "[BanSync]"  # prefix on every ban/unban this bot performs, used for loop prevention
CUSTOM_ID_PREFIX = "bansync"

log = logging.getLogger("bansync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATUS_STYLE = {
    "pending":    ("⏳ Awaiting approval", 0xF1C40F),
    "processing": ("⚙️ Processing…",       0xF1C40F),
    "approved":   ("✅ Approved",           0x2ECC71),
    "denied":     ("❌ Denied",             0x95A5A6),
    "auto":       ("⚡ Auto-applied",       0x3498DB),
    "cancelled":  ("🚫 Cancelled",          0x7F8C8D),
    "failed":     ("⚠️ Failed",             0xE74C3C),
}

REQUIRED_PERMS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
    ban_members=True, view_audit_log=True,
)


# ======================================================================
# Embeds / views
# ======================================================================
def build_request_embed(r) -> discord.Embed:
    is_ban = r["kind"] == "ban"
    label, color = STATUS_STYLE.get(r["status"], (r["status"], 0x99AAB5))
    uid = r["user_id"]
    src = r["source_guild_name"] or str(r["source_guild_id"])

    if is_ban:
        title = "🔨 Ban Sync Request"
        desc = f"<@{uid}> was **banned** in **{src}**.\nShould they be banned here as well?"
    else:
        title = "🕊️ Unban Sync Request"
        desc = (f"<@{uid}> was **unbanned** in **{src}**, which is where the ban synced to this "
                f"server came from.\nShould they be unbanned here too?")

    e = discord.Embed(
        title=title, description=desc, color=color,
        timestamp=dt.datetime.fromtimestamp(r["event_created_at"], tz=dt.timezone.utc),
    )
    e.add_field(name="User", value=f"<@{uid}>\n`{r['user_tag'] or 'unknown'}`\n`{uid}`", inline=True)
    e.add_field(name="Origin server", value=src, inline=True)
    mod = f"<@{r['moderator_id']}>\n`{r['moderator_tag']}`" if r["moderator_id"] else "Unknown"
    e.add_field(name="Actioned by", value=mod, inline=True)
    e.add_field(name="Reason", value=(r["reason"] or "*No reason provided*")[:1024], inline=False)
    created = int(discord.utils.snowflake_time(uid).timestamp())
    e.add_field(name="Account created", value=f"<t:{created}:D> (<t:{created}:R>)", inline=True)

    status = label
    if r["decided_by"]:
        status += f" by <@{r['decided_by']}>"
    if r["decided_at"] and r["status"] != "pending":
        status += f" <t:{r['decided_at']}:R>"
    if r["note"]:
        status += f"\n> {r['note'][:300]}"
    e.add_field(name="Status", value=status, inline=False)

    if r["user_avatar"]:
        e.set_thumbnail(url=r["user_avatar"])
    e.set_footer(text=f"Request #{r['id']} • Approvers: server owner, administrators, approver roles")
    return e


def request_view(request_id: int, kind: str) -> discord.ui.View:
    """Buttons carry the request ID in their custom_id, so they keep working after restarts
    (handled in on_interaction, not by a stored View)."""
    v = discord.ui.View(timeout=None)
    if kind == "ban":
        v.add_item(discord.ui.Button(label="Approve Ban", emoji="🔨", style=discord.ButtonStyle.danger,
                                     custom_id=f"{CUSTOM_ID_PREFIX}:approve:{request_id}"))
        v.add_item(discord.ui.Button(label="Deny", emoji="✖️", style=discord.ButtonStyle.secondary,
                                     custom_id=f"{CUSTOM_ID_PREFIX}:deny:{request_id}"))
    else:
        v.add_item(discord.ui.Button(label="Approve Unban", emoji="🕊️", style=discord.ButtonStyle.success,
                                     custom_id=f"{CUSTOM_ID_PREFIX}:approve:{request_id}"))
        v.add_item(discord.ui.Button(label="Keep Ban", emoji="🔒", style=discord.ButtonStyle.secondary,
                                     custom_id=f"{CUSTOM_ID_PREFIX}:deny:{request_id}"))
    return v


# ======================================================================
# Bot
# ======================================================================
class BanSyncBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.moderation = True  # GUILD_BAN_ADD / GUILD_BAN_REMOVE + audit log events (not privileged)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.db = Database(DB_PATH)
        self.owner_set: set[int] = set(EXTRA_OWNER_IDS)
        # (guild_id, user_id, kind) -> expiry; actions the bot itself is about to perform
        self._self_actions: dict[tuple[int, int, str], float] = {}

    # ---------- lifecycle ----------
    async def setup_hook(self) -> None:
        await self.db.connect()
        app = await self.application_info()
        if app.team:
            self.owner_set |= {m.id for m in app.team.members}
        elif app.owner:
            self.owner_set.add(app.owner.id)
        log.info("Bot owners: %s", self.owner_set)

        group = BanSyncGroup()
        if HOME_GUILD_ID:
            home = discord.Object(HOME_GUILD_ID)
            self.tree.add_command(group, guild=home)
            await self.tree.sync(guild=home)
            log.info("Commands synced to home server %s", HOME_GUILD_ID)
        else:
            self.tree.add_command(group)
            await self.tree.sync()
            log.info("Commands synced globally (can take a few minutes to appear)")

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        msg = (f"📥 I was added to **{guild.name}** (`{guild.id}`). "
               f"Use `/bansync link` to add it to the ban sync network.")
        for oid in self.owner_set:
            try:
                await (await self.fetch_user(oid)).send(msg)
            except discord.HTTPException:
                pass

    # ---------- loop prevention ----------
    def mark_self_action(self, guild_id: int, user_id: int, kind: str) -> None:
        self._self_actions[(guild_id, user_id, kind)] = time.time() + 60

    def consume_self_action(self, guild_id: int, user_id: int, kind: str) -> bool:
        exp = self._self_actions.pop((guild_id, user_id, kind), None)
        return exp is not None and exp > time.time()

    # ---------- permissions ----------
    async def can_approve(self, member: discord.abc.User, guild: discord.Guild) -> bool:
        if OWNER_CAN_APPROVE and member.id in self.owner_set:
            return True
        if not isinstance(member, discord.Member):
            return False
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return True
        roles = set(await self.db.get_roles(guild.id))
        return any(r.id in roles for r in member.roles)

    # ---------- audit log ----------
    async def find_audit(self, guild: discord.Guild, user: discord.abc.User, kind: str):
        """Return (moderator, reason) for the most recent ban/unban of `user`.
        Works for bans by humans, other bots, AutoMod, or the API."""
        action = discord.AuditLogAction.ban if kind == "ban" else discord.AuditLogAction.unban
        for attempt in range(4):
            try:
                async for entry in guild.audit_logs(limit=15, action=action):
                    if entry.target and entry.target.id == user.id:
                        age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                        if age < 120:
                            return entry.user, entry.reason
            except discord.Forbidden:
                log.warning("Missing View Audit Log in %s", guild.name)
                break
            except discord.HTTPException:
                pass
            await asyncio.sleep(1.5 * (attempt + 1))  # audit log entries can lag behind the event
        reason = None
        if kind == "ban":
            try:
                reason = (await guild.fetch_ban(user)).reason
            except discord.HTTPException:
                pass
        return None, reason

    # ---------- events ----------
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        await self._handle_action(guild, user, "ban")

    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self._handle_action(guild, user, "unban")

    async def _handle_action(self, guild: discord.Guild, user: discord.abc.User, kind: str) -> None:
        try:
            if self.consume_self_action(guild.id, user.id, kind):
                return  # we did this ourselves as part of a sync
            cfg = await self.db.get_guild(guild.id)
            if not cfg or not cfg["linked"]:
                return
            moderator, reason = await self.find_audit(guild, user, kind)
            if moderator and moderator.id == self.user.id:
                return
            if reason and reason.startswith(TAG):
                return

            if kind == "ban":
                if not cfg["share_bans"]:
                    return
                await self.propagate_ban(guild, user, moderator, reason)
            else:
                await self.propagate_unban(guild, user, moderator, reason)
        except Exception:
            log.exception("Error handling %s of %s in %s", kind, user, guild)

    async def _new_event(self, kind, guild, user, moderator, reason) -> int:
        return await self.db.create_event(
            kind=kind,
            source_guild_id=guild.id,
            source_guild_name=guild.name,
            user_id=user.id,
            user_tag=str(user),
            user_avatar=user.display_avatar.url if user.display_avatar else None,
            moderator_id=moderator.id if moderator else None,
            moderator_tag=str(moderator) if moderator else None,
            reason=reason,
        )

    def _alert_channel(self, guild: discord.Guild, cfg) -> Optional[discord.abc.Messageable]:
        ch = guild.get_channel(cfg["alert_channel_id"]) if cfg["alert_channel_id"] else None
        if ch and ch.permissions_for(guild.me).send_messages:
            return ch
        return None

    async def _post_request(self, request_id: int, channel) -> None:
        r = await self.db.get_request(request_id)
        view = request_view(request_id, r["kind"]) if r["status"] == "pending" else None
        msg = await channel.send(embed=build_request_embed(r), view=view,
                                 allowed_mentions=discord.AllowedMentions.none())
        await self.db.set_request_message(request_id, channel.id, msg.id)

    async def refresh_request_message(self, request_id: int) -> None:
        r = await self.db.get_request(request_id)
        if not r or not r["channel_id"] or not r["message_id"]:
            return
        ch = self.get_channel(r["channel_id"])
        if not ch:
            return
        view = request_view(request_id, r["kind"]) if r["status"] == "pending" else None
        try:
            await ch.get_partial_message(r["message_id"]).edit(embed=build_request_embed(r), view=view)
        except discord.HTTPException as e:
            log.warning("Could not edit request #%s message: %s", request_id, e)

    # ---------- performing actions ----------
    def _audit_reason(self, r, approver: Optional[discord.abc.User]) -> str:
        who = f"approved by {approver} ({approver.id})" if approver else "auto-sync"
        base = f"{TAG} {r['kind']} from {r['source_guild_name']} | {who} | {r['reason'] or 'no reason'}"
        return base[:512]

    async def apply(self, target: discord.Guild, r, approver: Optional[discord.abc.User]) -> Optional[str]:
        """Ban/unban in target guild. Returns an error string or None on success."""
        uid, kind = r["user_id"], r["kind"]
        self.mark_self_action(target.id, uid, kind)
        try:
            if kind == "ban":
                await target.ban(discord.Object(uid), reason=self._audit_reason(r, approver),
                                 delete_message_seconds=0)
            else:
                await target.unban(discord.Object(uid), reason=self._audit_reason(r, approver))
            return None
        except discord.NotFound:
            self.consume_self_action(target.id, uid, kind)
            return "User not found (or not banned, for an unban)."
        except discord.Forbidden:
            self.consume_self_action(target.id, uid, kind)
            return ("Missing permission. Make sure I have **Ban Members** and my role is above "
                    "the user's highest role.")
        except discord.HTTPException as e:
            self.consume_self_action(target.id, uid, kind)
            return f"Discord error: {e.text or e}"

    async def _is_banned(self, guild: discord.Guild, user_id: int) -> Optional[bool]:
        try:
            await guild.fetch_ban(discord.Object(user_id))
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return None

    # ---------- propagation ----------
    async def propagate_ban(self, src: discord.Guild, user, moderator, reason) -> None:
        event_id = await self._new_event("ban", src, user, moderator, reason)
        log.info("Ban of %s in %s (event #%s) by %s", user, src.name, event_id, moderator)
        for cfg in await self.db.linked_guilds():
            if cfg["guild_id"] == src.id or cfg["mode"] == "off":
                continue
            target = self.get_guild(cfg["guild_id"])
            if not target:
                continue
            try:
                await self._send_to_target(event_id, target, cfg, user.id, "ban")
            except Exception:
                log.exception("Failed to propagate ban to %s", target)

    async def propagate_unban(self, src: discord.Guild, user, moderator, reason) -> None:
        # 1) Cancel pending ban requests that came from this server for this user.
        pending = await self.db.requests_for_user(user.id, src.id, "ban", ("pending",))
        for r in pending:
            if await self.db.transition(r["id"], "pending", "cancelled",
                                        note=f"User was unbanned in {src.name}."):
                await self.refresh_request_message(r["id"])

        # 2) Offer unbans where a ban from this server was applied.
        applied = await self.db.requests_for_user(user.id, src.id, "ban", ("approved", "auto"))
        targets = {r["target_guild_id"] for r in applied}
        if not targets:
            return
        event_id = await self._new_event("unban", src, user, moderator, reason)
        for gid in targets:
            cfg = await self.db.get_guild(gid)
            target = self.get_guild(gid)
            if not cfg or not cfg["linked"] or not cfg["sync_unbans"] or cfg["mode"] == "off" or not target:
                continue
            try:
                await self._send_to_target(event_id, target, cfg, user.id, "unban")
            except Exception:
                log.exception("Failed to propagate unban to %s", target)

    async def _send_to_target(self, event_id: int, target: discord.Guild, cfg, user_id: int, kind: str):
        banned = await self._is_banned(target, user_id)
        if kind == "ban" and banned:
            return  # already banned there
        if kind == "unban" and banned is False:
            return  # already unbanned there
        if await self.db.has_pending(user_id, target.id, kind):
            return  # a request for this user is already open in that server

        channel = self._alert_channel(target, cfg)
        if cfg["mode"] == "auto":
            rid = await self.db.create_request(event_id, target.id, kind, "processing")
            r = await self.db.get_request(rid)
            err = await self.apply(target, r, None)
            if err:
                # fall back to a manual request so a human can retry
                await self.db.transition(rid, "processing", "pending", note=f"Auto-sync failed: {err}")
            else:
                await self.db.transition(rid, "processing", "auto")
            if channel:
                await self._post_request(rid, channel)
            return

        if not channel:
            log.warning("No usable alert channel in %s; skipping request", target.name)
            return
        rid = await self.db.create_request(event_id, target.id, kind, "pending")
        await self._post_request(rid, channel)

    # ---------- buttons ----------
    async def on_interaction(self, inter: discord.Interaction) -> None:
        if inter.type is not discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith(CUSTOM_ID_PREFIX + ":"):
            return
        try:
            _, action, rid_s = cid.split(":")
            rid = int(rid_s)
        except ValueError:
            return
        try:
            await self._handle_button(inter, action, rid)
        except Exception:
            log.exception("Button handling failed for request #%s", rid)
            msg = "Something went wrong handling that. Check the bot logs."
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)

    async def _handle_button(self, inter: discord.Interaction, action: str, rid: int) -> None:
        r = await self.db.get_request(rid)
        if not r or not inter.guild or inter.guild.id != r["target_guild_id"]:
            return await inter.response.send_message("This request no longer exists.", ephemeral=True)
        if not await self.can_approve(inter.user, inter.guild):
            return await inter.response.send_message(
                "⛔ Only the server owner, administrators, or a designated approver role can decide this.",
                ephemeral=True)
        if r["status"] != "pending":
            await inter.response.send_message("This request has already been handled.", ephemeral=True)
            return await self.refresh_request_message(rid)

        await inter.response.defer(ephemeral=True, thinking=True)
        verb = "ban" if r["kind"] == "ban" else "unban"

        if action == "deny":
            if not await self.db.transition(rid, "pending", "denied", decided_by=inter.user.id):
                return await inter.followup.send("Someone else already handled this.", ephemeral=True)
            await self.refresh_request_message(rid)
            text = "Kept the ban." if verb == "unban" else "Denied. No ban was applied."
            return await inter.followup.send(f"✅ {text}", ephemeral=True)

        if action == "approve":
            if not await self.db.transition(rid, "pending", "processing", decided_by=inter.user.id):
                return await inter.followup.send("Someone else already handled this.", ephemeral=True)
            err = await self.apply(inter.guild, r, inter.user)
            if err:
                await self.db.transition(rid, "processing", "pending", note=f"Last attempt failed: {err}")
                await self.refresh_request_message(rid)
                return await inter.followup.send(f"⚠️ Couldn't {verb}: {err}", ephemeral=True)
            await self.db.transition(rid, "processing", "approved", decided_by=inter.user.id)
            await self.refresh_request_message(rid)
            return await inter.followup.send(f"✅ User {verb}ned (`{r['user_id']}`).", ephemeral=True)


bot = BanSyncBot()


# ======================================================================
# Owner-only slash commands
# ======================================================================
async def server_ac(inter: discord.Interaction, current: str):
    cur = current.lower()
    out = []
    for g in sorted(inter.client.guilds, key=lambda g: g.name.lower()):
        if cur in g.name.lower() or cur in str(g.id):
            out.append(app_commands.Choice(name=f"{g.name} ({g.id})"[:100], value=str(g.id)))
    return out[:25]


def _ns_guild(inter: discord.Interaction) -> Optional[discord.Guild]:
    gid = getattr(inter.namespace, "server", None)
    if gid and str(gid).isdigit():
        return inter.client.get_guild(int(gid))
    return inter.guild


async def channel_ac(inter: discord.Interaction, current: str):
    g = _ns_guild(inter)
    if not g:
        return []
    cur = current.lower()
    out = []
    for ch in g.text_channels:
        p = ch.permissions_for(g.me)
        if p.send_messages and p.embed_links and cur in ch.name.lower():
            out.append(app_commands.Choice(name=f"#{ch.name}"[:100], value=str(ch.id)))
    return out[:25]


async def role_ac(inter: discord.Interaction, current: str):
    g = _ns_guild(inter)
    if not g:
        return []
    cur = current.lower()
    return [app_commands.Choice(name=f"@{r.name}"[:100], value=str(r.id))
            for r in reversed(g.roles)
            if not r.is_default() and not r.managed and cur in r.name.lower()][:25]


def resolve_guild(inter: discord.Interaction, server: Optional[str]) -> discord.Guild:
    if server:
        if not server.isdigit() or not (g := inter.client.get_guild(int(server))):
            raise app_commands.AppCommandError("I'm not in that server (pick one from the list).")
        return g
    if not inter.guild:
        raise app_commands.AppCommandError("Pick a server.")
    return inter.guild


class BanSyncGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="bansync", description="Configure the Ban Sync network (bot owner only)",
                         guild_only=True)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id in inter.client.owner_set:
            return True
        await inter.response.send_message("⛔ Only the bot owner can configure Ban Sync.", ephemeral=True)
        return False

    # ----- link / unlink -----
    @app_commands.command(name="link", description="Add a server to the ban sync network")
    @app_commands.describe(server="Server to link", channel="Channel where ban requests are posted")
    @app_commands.autocomplete(server=server_ac, channel=channel_ac)
    async def link(self, inter: discord.Interaction, server: str, channel: str):
        g = resolve_guild(inter, server)
        ch = g.get_channel(int(channel)) if channel.isdigit() else None
        if not isinstance(ch, discord.TextChannel):
            raise app_commands.AppCommandError("That channel isn't in the selected server.")
        await inter.client.db.upsert_guild(g.id, linked=1, alert_channel_id=ch.id)
        missing = [n for n, v in REQUIRED_PERMS if v and not getattr(g.me.guild_permissions, n)]
        warn = f"\n⚠️ Missing permissions in that server: `{', '.join(missing)}`" if missing else ""
        await inter.response.send_message(
            f"✅ **{g.name}** linked. Requests will be posted in {ch.mention}.{warn}", ephemeral=True)

    @app_commands.command(name="unlink", description="Remove a server from the network")
    @app_commands.autocomplete(server=server_ac)
    async def unlink(self, inter: discord.Interaction, server: str):
        g = resolve_guild(inter, server)
        await inter.client.db.upsert_guild(g.id, linked=0)
        await inter.response.send_message(f"✅ **{g.name}** unlinked.", ephemeral=True)

    # ----- settings -----
    @app_commands.command(name="channel", description="Change where a server's ban requests are posted")
    @app_commands.autocomplete(server=server_ac, channel=channel_ac)
    async def channel(self, inter: discord.Interaction, server: str, channel: str):
        g = resolve_guild(inter, server)
        ch = g.get_channel(int(channel)) if channel.isdigit() else None
        if not isinstance(ch, discord.TextChannel):
            raise app_commands.AppCommandError("That channel isn't in the selected server.")
        await inter.client.db.upsert_guild(g.id, alert_channel_id=ch.id)
        await inter.response.send_message(f"✅ **{g.name}** requests → {ch.mention}", ephemeral=True)

    @app_commands.command(name="mode", description="How a server handles incoming bans")
    @app_commands.autocomplete(server=server_ac)
    @app_commands.choices(mode=[
        app_commands.Choice(name="Manual: staff approve or deny each one", value="manual"),
        app_commands.Choice(name="Auto: apply immediately and post a notice", value="auto"),
        app_commands.Choice(name="Off: receive nothing", value="off"),
    ])
    async def mode(self, inter: discord.Interaction, server: str, mode: app_commands.Choice[str]):
        g = resolve_guild(inter, server)
        await inter.client.db.upsert_guild(g.id, mode=mode.value)
        await inter.response.send_message(f"✅ **{g.name}** incoming mode: **{mode.value}**", ephemeral=True)

    @app_commands.command(name="share", description="Whether bans in this server are sent to the others")
    @app_commands.autocomplete(server=server_ac)
    async def share(self, inter: discord.Interaction, server: str, enabled: bool):
        g = resolve_guild(inter, server)
        await inter.client.db.upsert_guild(g.id, share_bans=int(enabled))
        await inter.response.send_message(
            f"✅ **{g.name}** outgoing bans: **{'shared' if enabled else 'not shared'}**", ephemeral=True)

    @app_commands.command(name="unbans", description="Whether a server receives unban requests")
    @app_commands.autocomplete(server=server_ac)
    async def unbans(self, inter: discord.Interaction, server: str, enabled: bool):
        g = resolve_guild(inter, server)
        await inter.client.db.upsert_guild(g.id, sync_unbans=int(enabled))
        await inter.response.send_message(
            f"✅ **{g.name}** unban sync: **{'on' if enabled else 'off'}**", ephemeral=True)

    @app_commands.command(name="approver-add", description="Let a role approve or deny requests in a server")
    @app_commands.autocomplete(server=server_ac, role=role_ac)
    async def approver_add(self, inter: discord.Interaction, server: str, role: str):
        g = resolve_guild(inter, server)
        r = g.get_role(int(role)) if role.isdigit() else None
        if not r:
            raise app_commands.AppCommandError("That role isn't in the selected server.")
        await inter.client.db.upsert_guild(g.id)
        await inter.client.db.add_role(g.id, r.id)
        await inter.response.send_message(f"✅ **@{r.name}** can now approve in **{g.name}**.", ephemeral=True)

    @app_commands.command(name="approver-remove", description="Remove an approver role")
    @app_commands.autocomplete(server=server_ac, role=role_ac)
    async def approver_remove(self, inter: discord.Interaction, server: str, role: str):
        g = resolve_guild(inter, server)
        ok = role.isdigit() and await inter.client.db.remove_role(g.id, int(role))
        await inter.response.send_message("✅ Removed." if ok else "That role wasn't an approver.", ephemeral=True)

    # ----- info -----
    @app_commands.command(name="list", description="Show the network and every server's settings")
    async def list_(self, inter: discord.Interaction):
        bot_: BanSyncBot = inter.client
        e = discord.Embed(title="🔗 Ban Sync Network", color=0x5865F2)
        linked_ids = set()
        for cfg in await bot_.db.linked_guilds():
            linked_ids.add(cfg["guild_id"])
            g = bot_.get_guild(cfg["guild_id"])
            roles = await bot_.db.get_roles(cfg["guild_id"])
            name = g.name if g else f"⚠️ Left server {cfg['guild_id']}"
            chan = f"<#{cfg['alert_channel_id']}>" if cfg["alert_channel_id"] else "❌ not set"
            e.add_field(name=name[:256], inline=False, value=(
                f"Channel: {chan}\n"
                f"Incoming: **{cfg['mode']}** • Shares bans: **{'yes' if cfg['share_bans'] else 'no'}** • "
                f"Unban sync: **{'on' if cfg['sync_unbans'] else 'off'}**\n"
                f"Approvers: owner, admins{''.join(f', <@&{r}>' for r in roles)}"
            ))
        unlinked = [g for g in bot_.guilds if g.id not in linked_ids]
        if unlinked:
            e.add_field(name="Not linked", inline=False,
                        value="\n".join(f"• {g.name} (`{g.id}`)" for g in unlinked)[:1024])
        if not e.fields:
            e.description = "No servers yet. Invite me, then use `/bansync link`."
        await inter.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="lookup", description="Show ban sync history for a user")
    @app_commands.describe(user_id="The user's ID")
    async def lookup(self, inter: discord.Interaction, user_id: str):
        if not user_id.isdigit():
            raise app_commands.AppCommandError("Give a numeric user ID.")
        bot_: BanSyncBot = inter.client
        events = await bot_.db.events_for_user(int(user_id))
        if not events:
            return await inter.response.send_message("No history for that user.", ephemeral=True)
        e = discord.Embed(title=f"History for {events[0]['user_tag']} ({user_id})", color=0x5865F2)
        for ev in events:
            reqs = await bot_.db.requests_for_event(ev["id"])
            lines = []
            for r in reqs:
                g = bot_.get_guild(r["target_guild_id"])
                lines.append(f"{STATUS_STYLE.get(r['status'], (r['status'],))[0]} — {g.name if g else r['target_guild_id']}")
            e.add_field(
                name=f"{'🔨' if ev['kind'] == 'ban' else '🕊️'} {ev['kind']} in {ev['source_guild_name']} • <t:{ev['created_at']}:d>"[:256],
                value=(f"Reason: {ev['reason'] or 'none'}\n" + "\n".join(lines or ['No other servers notified']))[:1024],
                inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="invite", description="Get the invite link with the right permissions")
    async def invite(self, inter: discord.Interaction):
        url = discord.utils.oauth_url(inter.client.user.id, permissions=REQUIRED_PERMS,
                                      scopes=("bot", "applications.commands"))
        await inter.response.send_message(f"Invite link:\n{url}", ephemeral=True)


@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure) and inter.response.is_done():
        return  # interaction_check already replied
    msg = str(error) if not isinstance(error, app_commands.CommandInvokeError) else "Something went wrong."
    if isinstance(error, app_commands.CommandInvokeError):
        log.exception("Command error", exc_info=error.original)
    if inter.response.is_done():
        await inter.followup.send(f"⚠️ {msg}", ephemeral=True)
    else:
        await inter.response.send_message(f"⚠️ {msg}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file.")
    bot.run(TOKEN, log_handler=None)
