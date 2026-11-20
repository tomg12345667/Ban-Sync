# Ban Sync Bot

Links several Discord servers. When someone is banned in one of them, whether by a moderator, AutoMod, another bot (Dyno, Carl, Wick…) or the API, every other linked server gets an approval card:

> 🔨 **Ban Sync Request**: `@user` was banned in **Server A**. Should they be banned here as well?
> User · Origin server · Actioned by · Reason · Account age
> **[🔨 Approve Ban]  [✖️ Deny]**

Only the **server owner**, **administrators** or a **designated approver role** in *that* server can press the buttons. All setup is done by the **bot owner only**.

## Features
- Catches every ban and unban through Discord's ban events, and gets the moderator and reason from the audit log.
- Per-server **incoming mode**: `manual` (approve or deny), `auto` (apply right away and post a notice), `off`.
- Per-server **share** toggle, which decides whether that server's own bans are sent out.
- **Unban sync:** if the origin server unbans someone, open requests are cancelled. Servers that applied the ban get an *Approve Unban / Keep Ban* card.
- Buttons keep working after the bot restarts. All state is stored in SQLite.
- **No loops:** bans the bot carries out are tagged `[BanSync]` and ignored, so they don't spread again.
- No duplicates: a server isn't asked again if the user is already banned there or a request is already open.
- If two moderators press the button at the same moment, only one action happens.
- If the bot lacks permission or its role is too low, the request stays open and the card shows the error.

## Setup
1. **Create the bot** at <https://discord.com/developers/applications> → New Application → Bot → copy the token.
   No privileged intents are needed.
2. **Install** (Python 3.10+):
   ```bash
   pip install -r requirements.txt
   cp .env.example .env    # then fill in DISCORD_TOKEN and HOME_GUILD_ID
   python bot.py
   ```
3. **Invite** the bot to each server. Run `/bansync invite` to get a link with the right permissions:
   View Channels, Send Messages, Embed Links, Read Message History, **Ban Members**, **View Audit Log**.
4. In each server, **drag the bot's role above** the roles of the people it may need to ban.
5. Link the servers (from your home server; each command has a server picker):
   ```
   /bansync link server:<Server A> channel:#ban-requests
   /bansync link server:<Server B> channel:#mod-queue
   /bansync approver-add server:<Server B> role:@Moderators
   /bansync mode server:<Server C> mode:Auto
   ```

## Commands (bot owner only)
| Command | What it does |
|---|---|
| `/bansync link server channel` | Add a server to the network and set its request channel |
| `/bansync unlink server` | Remove a server from the network |
| `/bansync channel server channel` | Change the request channel |
| `/bansync mode server manual\|auto\|off` | How incoming bans are handled |
| `/bansync share server true\|false` | Whether this server's bans go out to the others |
| `/bansync unbans server true\|false` | Whether this server gets unban requests |
| `/bansync approver-add / approver-remove server role` | Roles that can press Approve/Deny |
| `/bansync list` | Network overview and every server's settings |
| `/bansync lookup user_id` | A user's ban sync history across the network |
| `/bansync invite` | Invite link |

"Bot owner" means the application owner, every member of the developer team if the app belongs to one, and any IDs listed in `OWNER_IDS`.

## Notes
- Bans made while the bot is offline are not caught. Discord doesn't replay those events.
- The bot bans by user ID, so it works even if the user isn't in the target server (a pre-emptive ban).
- To run it 24/7, use any VPS, a Raspberry Pi, or a host such as Railway or fly.io. Keep `bansync.db` on persistent storage.
