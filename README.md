<p align="center">
  <img src="docs/logo.svg" width="88" height="88" alt="evren-bridge logo">
</p>
<h1 align="center">evren-bridge</h1>
<p align="center">
  Keep each coding-agent session on one API key, and watch every agent live in your terminal.
</p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-65d6ce?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependency-rich-f1b66b?style=flat-square" alt="Dependency: rich">
  <img src="https://img.shields.io/badge/OpenAI-Chat_Completions-64748b?style=flat-square&amp;logo=openai&amp;logoColor=white" alt="OpenAI Chat Completions compatible"><br>
  English · <a href="README.tr.md">Türkçe</a>
</p>

Prompt caches are often kept per account: when an agent's requests move to another key, its cached input is lost. evren-bridge keeps each agent session on one key and shows what every agent is doing, how fast it answers and how much of its input came from cache.

Built for [EVREN](https://evren.ssyz.org.tr), it works with any OpenAI Chat Completions provider. Not an official EVREN project.

![evren-bridge in Windows Terminal](docs/screens/terminal.png)

## Quick start

Requires Python 3.10+ on Windows, macOS or Linux (use `python3` where `python` is not found).

```sh
git clone https://github.com/Kscl1/evren-bridge
cd evren-bridge
python -m pip install -r requirements.txt
```

Create `~/.evren/keys.txt` (Windows: `%USERPROFILE%\.evren\keys.txt`), including its parent directory, with your API key:

```text
main=sk-...
```

```sh
python evren_bridge.py
```

This starts the bridge for EVREN. For another provider, add `--upstream URL` with its root URL, without `/v1`.

Set your agent's OpenAI-compatible base URL to `http://127.0.0.1:8787/v1` and its API key to `unused`; the bridge substitutes your key.

Try the panel without an API key: `python docs/demo.py`.

## Clients

Use each client's Chat Completions / OpenAI-compatible provider with the base URL above.

| Client | Session pinning setup |
|---|---|
| <img src="https://github.com/earendil-works.png?size=40" width="20" height="20" alt=""> [Pi](https://github.com/earendil-works/pi) | `sendSessionAffinityHeaders` on each model, see below |
| <img src="https://github.com/anomalyco.png?size=40" width="20" height="20" alt=""> [OpenCode](https://github.com/anomalyco/opencode) | Automatic |
| <img src="https://github.com/Kilo-Org.png?size=40" width="20" height="20" alt=""> [Kilo Code](https://github.com/Kilo-Org/kilocode) (OpenCode-based) | Automatic |
| <img src="https://github.com/charmbracelet.png?size=40" width="20" height="20" alt=""> [Crush](https://github.com/charmbracelet/crush) | Not tested yet |
| <img src="https://github.com/aaif-goose.png?size=40" width="20" height="20" alt=""> [Goose](https://github.com/aaif-goose/goose) | Not tested yet |

<img src="https://github.com/cline.png?size=40" width="20" height="20" alt=""> [Cline](https://github.com/cline/cline), <img src="https://github.com/RooCodeInc.png?size=40" width="20" height="20" alt=""> [Roo Code](https://github.com/RooCodeInc/Roo-Code), <img src="https://github.com/continuedev.png?size=40" width="20" height="20" alt=""> [Continue](https://github.com/continuedev/continue), <img src="https://github.com/Aider-AI.png?size=40" width="20" height="20" alt=""> [Aider](https://github.com/Aider-AI/aider), <img src="https://github.com/zed-industries.png?size=40" width="20" height="20" alt=""> [Zed](https://github.com/zed-industries/zed) and <img src="https://github.com/QwenLM.png?size=40" width="20" height="20" alt=""> [Qwen Code](https://github.com/QwenLM/qwen-code) work, but send no session id, so they are not kept on one key.

<details>
<summary>Pi configuration</summary>

`~/.pi/agent/models.json`:

```json
{
  "providers": {
    "evren-bridge": {
      "baseUrl": "http://127.0.0.1:8787/v1",
      "api": "openai-completions",
      "apiKey": "unused",
      "models": [
        { "id": "glm-5.3", "compat": { "sendSessionAffinityHeaders": true } }
      ]
    }
  }
}
```

```sh
pi --provider evren-bridge --model glm-5.3
```

</details>

Custom clients can send `X-Session-Affinity`, `X-Session-Id` or `Agent-Session-Id` (first non-empty wins). Codex CLI and Claude Code speak other APIs and are not supported.

## Configuration

| Option | Default | Purpose |
|---|---|---|
| `--upstream URL` / `EVREN_BRIDGE_UPSTREAM` | `https://evren-llmapi.ssyz.org.tr` | Root URL; request paths are appended |
| `--profile evren\|none` | `evren` without an upstream override, otherwise `none` | Provider-specific rules |
| `--active-cap N` | `20` | Placement threshold for active sessions per key |
| `--lang en\|tr` | `en` | Panel language |
| `--no-panel` | Off | Request log output instead of the panel |
| `EVREN_KEYS_FILE` | `~/.evren/keys.txt` | Keys file |
| `EVREN_BRIDGE_PORT` | `8787` | Local port |

`GET /bridge/quota` returns key labels and routing state, plus quota data when a profile supplies it.

## How it works

- Sessions are **active** during requests and tool execution (up to 10 minutes per tool call); an ended turn is **idle**.
- For multiple keys, add one `label=key` per line for the same upstream. New sessions use the first available key with fewer than 20 active sessions (`--active-cap`), otherwise the available key with the fewest. Idle sessions do not count.
- Session assignments stay in memory for 60 minutes after their last activity. Returning sessions keep their key, even above the cap, unless it is parked. Load never moves a session; the cap is not a concurrency limit.
- Without a provider profile, upstream response bodies and error statuses pass through unchanged.

## EVREN profile

Enabled by default unless you override the upstream; use `--profile evren` to enable it explicitly.

| Condition | Behavior |
|---|---|
| Per-minute 429 | Pass through; add `Retry-After: 60` if missing |
| Daily-limit 429 | Park the key until reset; retry on another available key |
| All keys parked | Return 429 with `Retry-After` until the earliest reset |
| Masked error in the first stream data chunk | Return 503 for the client to retry |

The profile adds per-minute, 5-minute and daily quota metrics to the panel.

## Panel

| Key | Action |
|---|---|
| `←` / `→` or `1` / `2` / `3` | Switch tabs |
| `Tab` | Live: switch active / idle agents |
| `↑` / `↓`, `PgUp` / `PgDn` | Live: page agents; Requests: scroll; Statistics: change range |
| `Home` | Live: first page; Requests: newest request; Statistics: first range |
| `L` | Switch English / Turkish |
| `q` | Quit |

### Live

![Live](docs/screens/live.svg)

### Requests

![Requests](docs/screens/requests.svg)

### Statistics

![Statistics](docs/screens/stats.svg)

## Privacy

The bridge binds only to `127.0.0.1`. Any local program can use it, so run it on a trusted machine.
`logs/bridge.log` records request metadata, key labels and the last eight characters of session IDs, never API keys, message content or tool arguments. Statistics are loaded from this log; session assignments are lost on restart. Cache hits depend on the provider.

## Development

```sh
python -m unittest
```

## License

[MIT](LICENSE)
