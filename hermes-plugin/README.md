# kifu for Hermes

Brings [kifu](../README.md) into [Hermes Agent](https://github.com/NousResearch/hermes-agent): the ideas
left behind in your Claude Code sessions, where your agent and your dashboard can see them.

- **An Ideas tab** in the dashboard: open ideas with their loose ends, what git shows happened to their
  files since, the trail through sessions with resume commands, Done and Dismiss, and a glance at how you work.
- **Three agent tools** — ask "what did I leave unfinished in tidepool?" or "where did the offline idea
  start?":
  `kifu_ideas` (search open ideas), `kifu_idea` (one idea's trail), `kifu_mark` (only when you say so).
- **`/kifu`** in any chat: the top open ideas, or `/kifu <words>`, or `/kifu digest`. No model call.
- **A weekly digest** of ideas that went quiet, as a `no_agent` cron job: no model call, and nothing is
  sent when nothing is quiet.

![The Ideas tab](../docs/hermes-ideas.png)

The plugin holds no data and needs nothing beyond the standard library: it calls a running kifu service.

## Install

```bash
pip install "kifu[web] @ git+https://github.com/gweber/kifu" && kifu serve          # or run it as a service, see ../examples
hermes plugins install gweber/kifu/hermes-plugin
hermes plugins enable kifu
systemctl restart hermes-dashboard             # the tab's routes mount at dashboard start
```

Working from a clone instead: `ln -s "$PWD/hermes-plugin" ~/.hermes/plugins/kifu`, then enable it.

The tools and `/kifu` load with the next agent session; for the gateway (Telegram and other platforms)
that means a gateway restart.

## Settings

In `config.yaml`, under `plugins.entries.kifu.settings`:

| Key | Default | |
|---|---|---|
| `url` | `http://127.0.0.1:8765` | where `kifu serve` listens (`$KIFU_URL` wins) |
| `digest_deliver` | `local` | cron delivery target, e.g. `telegram:<chat_id>` |
| `digest_schedule` | `0 18 * * 0` | cron schedule for the digest |
| `digest_quiet_days` | `21` | an open idea joins the digest after this many quiet days |

```bash
hermes kifu status             # is kifu reachable, what does it hold
hermes kifu digest             # the digest, now
hermes kifu setup              # create or update the weekly digest job (--dry-run shows what it would do)
```

## Keeping the prompt small

Every enabled tool costs prompt tokens on every turn. Two settings keep kifu's cost at zero until a
question needs it:

```yaml
tools:
  tool_search:
    force_deferrable: [kifu_ideas, kifu_idea, kifu_mark]    # loaded on demand

platform_toolsets:              # only where you talk to the agent yourself
  cli: [..., kifu]
  telegram: [..., kifu]
```

A new plugin toolset is enabled on every platform whose tool selection Hermes has not saved yet. To keep
it off elsewhere, list `kifu` under `known_plugin_toolsets` for those platforms (known and not selected
means off), or pick it in `hermes tools`.

## Security

The dashboard's authentication is the perimeter: wherever the dashboard is reachable, the Ideas tab is
too, including its Pull and Run buttons. The browser only talks to the dashboard, which forwards to kifu
on loopback. See [../SECURITY.md](../SECURITY.md).
