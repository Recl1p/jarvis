# Connecting JARVIS to models

JARVIS needs a **model provider** before it can answer. This guide covers every way to connect
one: which command to run, which environment variable holds the key, and what to do when
something does not work. The in-app setup guide (the **GUIDE** button) covers the same ground in the browser,
with no commands to type, then offers optional voice and Telegram steps (see INSTALL.md).

Facts here were checked against the Hermes source shipped in this project (Hermes Agent
v0.21.0). Anything that could not be confirmed by reading code or running a read-only command
is marked **(not verified end to end)**.

Contents

1. [How the pieces fit](#1-how-the-pieces-fit)
2. [Running commands: always as the `hermes` user](#2-running-commands-always-as-the-hermes-user)
3. [Check what is connected](#3-check-what-is-connected)
4. [Choose the main model](#4-choose-the-main-model)
5. [Free setup (OpenRouter)](#5-free-setup-openrouter)
6. [Connect a provider](#6-connect-a-provider)
7. [Where credentials live](#7-where-credentials-live)
8. [Applying changes: when to restart](#8-applying-changes-when-to-restart)
9. [Fallbacks and per-agent models](#9-fallbacks-and-per-agent-models)
10. [Local models with Ollama](#10-local-models-with-ollama)
11. [Troubleshooting](#11-troubleshooting)
12. [Reopening the tour](#12-reopening-the-tour)

---

## 1. How the pieces fit

| Piece | What it does | Where its model is set |
| --- | --- | --- |
| **Hermes** | The assistant: chat, memory, skills, tools, scheduled tasks, messaging. Installed by JARVIS into its data volume, or your own. | The free setup in the tour, `hermes model` (the default), or the model button in the dashboard (this chat session only). |
| **JARVIS** | The dashboard, voice and browser desktop, talking to Hermes through its connector plugin. | Nothing to set: it uses Hermes's model. |
| **Ollama** | Optional local model engine (service `ollama`, profile `local-model`). | Point Hermes at it with a custom endpoint ([section 10](#10-local-models-with-ollama)). |

**Hermes has a default model and a per-session model.** `hermes model` and the free setup write
the default into `config.yaml` (`model.provider` and `model.default`). The model button in the
dashboard switches only the current chat session (the dashboard calls
`POST /api/sessions/{id}/model` and says "This session will use it from here on").

---

## 2. Running commands: always as the `hermes` user

Hermes runs inside the container as the unprivileged `hermes` user (uid 10001). Every command
that touches its data must run as that user, otherwise files such as `/opt/data/auth.json` can be
left owned by root, the gateway cannot read them, and you see a misleading "Provider
authentication failed" error even though your credentials are fine.

When JARVIS installed Hermes, Hermes runs inside the JARVIS container. Use the form that matches yours, and **keep the
user flag**. (Your own Hermes: just run `hermes ...` where it is installed.)

| Your setup | Command prefix (interactive) | Command prefix (non-interactive) |
| --- | --- | --- |
| Compose, from the project folder (service `jarvis`) | `docker compose exec -u hermes jarvis hermes` | same |
| The container by name (`jarvis` by default) | `docker exec -it --user hermes jarvis hermes` | `docker exec --user hermes jarvis hermes` |

`-it` gives `docker exec` a terminal; the interactive pickers and login prompts need one.
`docker compose exec` allocates a terminal by default.

In the rest of this guide, `hermes <subcommand>` means "the prefix above, then the subcommand".
For example `hermes model` is:

```sh
docker compose exec -u hermes jarvis hermes model      # Compose
docker exec -it --user hermes jarvis hermes model      # named container
```

Notes:

- `hermes` is the official install in the data volume (`/opt/data/.local/bin/hermes`, on the container's PATH).
- If a credential file did become root-owned, see [Troubleshooting](#11-troubleshooting).

---

## 3. Check what is connected

**In the dashboard.** The **Telemetry** panel has a **RUN DIAGNOSTICS** button that runs
`hermes doctor` and shows the result. The model button in the top bar lists every connected
provider and model. A provider counts as connected when it is authenticated **and** lists at
least one model.

**From a terminal** (all read-only):

```sh
hermes status        # components, API keys (redacted) and login state
hermes doctor        # configuration and dependency checks
hermes auth list     # stored logins and keys, by label (no secrets printed)
hermes config get model.provider
hermes config get model.default
hermes config env-path      # prints /opt/data/.env inside the container
```

---

## 4. Choose the main model

- **First start:** `hermes setup` runs the full wizard, including provider and model. The README's
  flow for a brand-new install is `docker compose run --rm --no-deps hermes setup`, then
  `docker compose up -d`. To run just the model section later: `hermes setup model`.
- **Any time after:** `hermes model`. It lists providers, asks for the credential if one is
  missing, shows the models, and writes your choice into `config.yaml`.
- **Scripted:** `hermes config set model.provider <provider-id>` and
  `hermes config set model.default <model-id>`. Provider ids are in [section 6](#6-connect-a-provider).

What the `hermes model` picker shows (the labels you will see):

| You see | Provider id | Credential |
| --- | --- | --- |
| Nous Portal | `nous` | Browser/device login |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY`, or a Claude subscription token |
| OpenAI ▸ ChatGPT or Codex Subscription | `openai-codex` | ChatGPT login (device code) |
| OpenAI ▸ OpenAI API | `openai-api` | `OPENAI_API_KEY` |
| Google AI Studio | `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| Custom endpoint (enter URL manually) | `custom` | Optional key for the server |
| ...and more (about forty providers in all) | see [6.7](#67-other-providers) | |

`hermes model --refresh` clears the picker's cached model lists and refetches them.

---

## 5. Free setup (OpenRouter)

[OpenRouter](https://openrouter.ai) is free to sign up for and offers free models with daily
request limits.

In the tour, open **Connect a model**, stay on the **Free** tab and:

1. Create a key at <https://openrouter.ai/keys> (the link opens in a new tab) and paste it into
   the key field. If a key is already saved, the field can stay empty.
2. Keep the recommended model, or pick another free one from the list.
3. Press **Connect**. The tour confirms `Connected: <model>` and states the current limits, the
   model list refreshes, and this chat switches to the new model.

The key is sent once to the gateway, stored in `/opt/data/.env` as `OPENROUTER_API_KEY`, and
cleared from the page. The default model in `config.yaml` is set to the chosen free model. Free
models are rate limited; for heavier use, add credit on OpenRouter or connect another provider
([section 6](#6-connect-a-provider)).

The tour uses three gateway routes, all behind the dashboard key (`Authorization: Bearer`):

| Route | Purpose |
| --- | --- |
| `GET /vesper/setup/openrouter-free/models` | Free model list and the recommended one. |
| `POST /vesper/setup/openrouter-free` | Body `{"api_key"?, "model"?}`. Saves the key, sets the model, returns `{"ok": true, "provider", "model", "limits"}`. Errors return `{"error": {"code", "message"}}`, for example `invalid_key`, `invalid_model` or `upstream_unavailable`. |
| `GET /vesper/setup/status` | `{"provider", "model", "openrouter_key_set"}`. |

---

## 6. Connect a provider

Every provider follows the same pattern: get a credential, run `hermes model`, pick the
provider, paste the credential when asked, pick a model. The sections below give the exact
details. Credentials entered this way are saved for you (see [section 7](#7-where-credentials-live)).

### 6.1 Anthropic (Claude)

**API key (pay per token).** Variable: `ANTHROPIC_API_KEY`.

1. Create a key at <https://platform.claude.com/settings/keys>. It starts with `sk-ant-`.
2. Run `hermes model`, choose **Anthropic**, then **2. Anthropic API key**, paste the key, and
   pick a model.

**Claude Pro / Max subscription login.**

1. Run `hermes auth add anthropic`. (OAuth is the default for this provider.) It prints a
   `claude.ai` link. Open it in any browser, approve access, and paste the code it shows back
   into the terminal. No browser is needed inside the container.
2. Make Claude the default model:

   ```sh
   hermes config set model.provider anthropic
   hermes config set model.default claude-sonnet-5
   ```

   Or skip this and choose a Claude model with the dashboard model button; the login makes
   Anthropic appear in that list immediately.

Why not `hermes model` for the subscription? Its option "1. Claude Pro/Max subscription (OAuth
login)" shells out to the `claude` command-line tool, which this container does not include. If
you do have a token from `claude setup-token` (it starts with `sk-ant-oat`), the picker accepts
it pasted in and stores it as `ANTHROPIC_TOKEN`. Also, from reading the code, `hermes model`
looks for `ANTHROPIC_*` variables and Claude Code's own files, not for a login added with
`hermes auth add`, so it may ask for credentials again **(not verified end to end)**.

> **Subscription caveat.** Anthropic can bill third-party use of a subscription as "extra
> usage". Hermes contains workarounds for this, but if chats fail with "You're out of extra
> usage", switch to an API key.

Other Anthropic variables Hermes recognises: `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`
(an `sk-ant-oat…` token, not an API key), and `ANTHROPIC_BASE_URL`.

Model ids come from the picker. At the time of writing the built-in list starts with
`claude-fable-5` and `claude-sonnet-5`, followed by the 4.x Opus, Sonnet and Haiku ids.

### 6.2 OpenAI

**API key (pay per token).** Variable: `OPENAI_API_KEY` (provider id `openai-api`).

1. Create a key at <https://platform.openai.com/api-keys>.
2. Run `hermes model`, choose **OpenAI ▸**, then **OpenAI API**, paste the key, pick a model.

**ChatGPT or Codex subscription.** Provider id `openai-codex`.

1. Run `hermes model`, choose **OpenAI ▸**, then **ChatGPT or Codex Subscription**.
2. It prints a code. Open <https://auth.openai.com/codex/device> in any browser, enter the code
   and sign in. The terminal waits and continues on its own. Then pick a model.

To add the login without changing the default model, run `hermes auth add openai-codex`.

An API key is billed separately from a ChatGPT plan; having one does not give you the other.
`OPENAI_BASE_URL` overrides the endpoint; leave it unset for `api.openai.com`.

### 6.3 OpenRouter

One key reaches hundreds of models. Variable: `OPENROUTER_API_KEY`. For the free models, use the
[free setup](#5-free-setup-openrouter) instead.

1. Create a key at <https://openrouter.ai/keys>. Keys start with `sk-or-`; Hermes ignores a key in
   `.env` with another prefix (and logs a warning) so a truncated or wrong paste cannot shadow a
   good credential.
2. Run `hermes model`, choose **OpenRouter**, paste the key, pick a model (prices are shown).

Alternative that stores the key in the credential pool instead of `.env`:
`hermes auth add openrouter --type api-key`.

### 6.4 Google Gemini

Provider id `gemini` (picker label **Google AI Studio**). Variables: `GOOGLE_API_KEY` or
`GEMINI_API_KEY`; either name works, and Hermes checks `GOOGLE_API_KEY` first.

1. Create a key at <https://aistudio.google.com/app/apikey>.
2. Run `hermes model`, choose **Google AI Studio**, paste the key, pick a model.

> **Free-tier keys are refused.** `hermes model` probes the key, and if it is on the free tier
> (about 250 requests a day) it prints "Not saving Gemini as the default provider": an agent
> makes several calls per message and runs out within a few messages. Enable billing on the
> Google Cloud project and create the key in that project. `GEMINI_BASE_URL` overrides the
> endpoint. **Google Vertex AI** (provider `vertex`) is separate and uses a service account:
> `VERTEX_CREDENTIALS_PATH`, then `GOOGLE_APPLICATION_CREDENTIALS`, then Application Default
> Credentials.

### 6.5 Nous Portal

Provider id `nous`. There is no API-key variable; it uses a device login.

```sh
hermes portal
```

It prints a link and a code. Open the link in any browser, enter the code and approve. Then it
asks you to pick a model, sets Nous as the provider, and offers the Tool Gateway (bundled web
search, image generation and similar tools) for paid subscribers. The same login is available as
**Nous Portal** inside `hermes model`, as `hermes setup --portal`, and as
`hermes auth add nous --type oauth`. Free accounts see only the free models; paid accounts see
the full list. `hermes portal info` shows login and Tool Gateway state.

### 6.6 Custom endpoint (any OpenAI-compatible server)

Use this for Ollama, vLLM, llama.cpp, a company gateway and so on. Run `hermes model` and choose
**Custom endpoint (enter URL manually)**. It asks for:

1. **API base URL** (for example `http://ollama:11434/v1`). For a local server it offers to add
   `/v1` if you left it off.
2. **API key**, optional for servers that do not check one.
3. It then probes `/models`, asks for the API mode, lets you pick a detected model, and asks for a
   context length (leave blank to auto-detect) and a display name.

The key is stored as `HERMES_CUSTOM_<HOST>_<PORT>_API_KEY` in `.env`, for example
`HERMES_CUSTOM_OLLAMA_11434_API_KEY`, and the endpoint is remembered as a saved custom provider
that appears in the picker next time.

### 6.7 Other providers

`hermes model` lists them all and asks for the key by variable name. Recognised variables:

| Provider | Provider id | Variable(s) |
| --- | --- | --- |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| xAI Grok (API) | `xai` | `XAI_API_KEY` |
| xAI Grok (SuperGrok / Premium+ login) | `xai-oauth` | device login |
| Z.AI / GLM | `zai` | `GLM_API_KEY` (aliases `ZAI_API_KEY`, `Z_AI_API_KEY`) |
| Kimi / Moonshot | `kimi-coding` | `KIMI_API_KEY` (`KIMI_CN_API_KEY` for `kimi-coding-cn`) |
| MiniMax | `minimax` | `MINIMAX_API_KEY` (`MINIMAX_CN_API_KEY` for `minimax-cn`) |
| Qwen Cloud (DashScope) | `alibaba` | `DASHSCOPE_API_KEY` |
| NVIDIA NIM | `nvidia` | `NVIDIA_API_KEY` |
| Hugging Face | `huggingface` | `HF_TOKEN` |
| Fireworks AI | `fireworks` | `FIREWORKS_API_KEY` |
| Vercel AI Gateway | `ai-gateway` | `AI_GATEWAY_API_KEY` |
| GitHub Copilot | `copilot` | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` |
| LM Studio | `lmstudio` | `LM_API_KEY` (optional), `LM_BASE_URL` |
| Ollama Cloud | `ollama-cloud` | `OLLAMA_API_KEY` |
| Azure Foundry | `azure-foundry` | `AZURE_FOUNDRY_API_KEY`, `AZURE_FOUNDRY_BASE_URL` |
| AWS Bedrock | `bedrock` | AWS credentials (IAM or API key) |

Provider ids are what `hermes config set model.provider` and `hermes auth add` accept. Aliases
such as `claude`, `google`, `or` (OpenRouter) and `grok` also work in many places.

---

## 7. Where credentials live

Inside the container Hermes' home is `/opt/data` (`HERMES_HOME`; `hermes config env-path` prints
`/opt/data/.env`). It is the `hermes-data` volume, so it survives restarts and rebuilds.

| File | Holds | Written by |
| --- | --- | --- |
| `/opt/data/.env` | API keys (secrets only) | `hermes model`, `hermes setup`, `hermes config set <KEY> ...` |
| `/opt/data/auth.json` | Logins (OAuth tokens) and the credential pool | `hermes auth add`, `hermes portal`, the OAuth flows in `hermes model` |
| `/opt/data/config.yaml` | Settings, including `model.provider` and `model.default` | `hermes model`, `hermes config set` |

Keys and logins written by the commands above go to these files automatically, owned by the
`hermes` user (the live files are mode `0600`). You never need to edit them by hand.

**Setting a key without the picker.** `hermes config set OPENROUTER_API_KEY <key>` writes to
`.env` (names ending in `_API_KEY`, `_TOKEN` or `_SECRET` are routed there), and
`hermes config unset OPENROUTER_API_KEY` removes it. The value then sits in your shell history,
so the interactive picker is the safer route.

**Shareable Compose setup: a second place.** `compose.yaml` passes the project's `.env` file into
the container as environment variables (`env_file`). `.env.example` lists the provider
variables it documents. Two things to know:

- Changing that `.env` needs a container re-create: `docker compose up -d`.
- If a variable is set non-empty in both places, the one inside the data volume
  (`/opt/data/.env`) wins, because Hermes loads its own `.env` over the environment.

---

## 8. Applying changes: when to restart

- **Logins** (`hermes auth add`, `hermes portal`) are read from `auth.json` on every request, so
  the provider appears in the dashboard model list right away.
- **API keys in `.env`** are re-read by the gateway at the start of every chat turn, so your next
  message already uses a new key. The dashboard's provider list, however, is built from the
  gateway process's environment, which only refreshes on the next turn or after a restart. So if
  a new key does not show up in the model button, restart the gateway:

  ```sh
  hermes gateway restart
  ```

  The container runs the gateway under a supervisor, so this restarts only the gateway, not the
  container; the dashboard reconnects in a few seconds. (`docker compose restart hermes`
  restarts the whole container and also works.)
- **Config changes** (`hermes config set model.provider ...`) are read when the dashboard builds
  its model list; restart if anything looks stale.

After restarting, press **Refresh models** in the tour's Cloud tab (or reload the page) so the
model button picks up the new provider.

---

## 9. Fallbacks and per-agent models

- **Fallback providers.** If the main model fails with a rate limit, overload or connection
  error, Hermes can try other providers in order. Manage the chain with `hermes fallback list`,
  `hermes fallback add` (same picker as `hermes model`), `hermes fallback remove` and
  `hermes fallback clear`.
- **Per-agent model.** In **Agent Control** (the Hermes scheduler panel), the agent form's
  **Model** menu offers "Hermes default" plus every connected provider and model. Pick one to
  pin that scheduled agent to a model.

---

## 10. Local models with Ollama

Ollama runs open models on your own hardware. It is optional and off by default.

**1. Start the service.** From the project folder:

```sh
docker compose --profile local-model up -d
```

(`docker compose up -d ollama` also works.) No port is published; Hermes reaches it on the
Compose network at `http://ollama:11434`. If `ollama` does not resolve, the service is not
running. CPU inference is the default; GPU access needs a host-specific Compose override.

**2. Use it for Hermes' main chat.** Run `hermes model`, choose **Custom endpoint (enter URL
manually)** and enter:

| Prompt | Value |
| --- | --- |
| API base URL | `http://ollama:11434/v1` |
| API key | `ollama` (optional; some clients require a non-empty value) |
| Model | the detected model you downloaded |
| Context length | leave blank to auto-detect |

Download the model first, for example `docker compose exec ollama ollama pull <model>`. With your own Hermes, run Ollama
where it can reach it instead and use that address.

**Context window.** Hermes refuses models whose context window is smaller than **64,000
tokens**, so choose a model that supports at least that. Ollama defaults to 2,048 tokens per
request unless asked, so Hermes detects the model's maximum window and requests it
(`ollama_num_ctx`); that needs enough RAM or VRAM. To cap it, set `model.context_length` in
`config.yaml` (`hermes config set model.context_length <tokens>`). The `ollama` hostname counts as
a local endpoint, so local-endpoint timeouts apply.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Provider authentication failed" or "No Anthropic credentials found" although you just logged in | A command ran as root and left `auth.json` (or `.env`, `config.yaml`) owned by root, so the gateway cannot read it. | Repair ownership, then restart: `docker exec --user root jarvis chown hermes:hermes /opt/data/auth.json` (Compose: `docker compose exec -u root jarvis chown hermes:hermes /opt/data/auth.json`), then `hermes gateway restart`. This is the one place root is appropriate. Use `--user hermes` for everything else. |
| The dashboard cannot reach the gateway | Container stopped or still starting. | `docker compose ps` / `docker ps`; read the log with `docker compose logs --tail=100 jarvis` or `docker logs --tail 100 jarvis`; the setup screen at `/jarvis/` has **Test connection**. |
| A request fails with HTTP 401 | The dashboard key was rejected, for example from a stale page. | Reload `/vesper` and try again. |
| Provider added but the model button is empty | A key in `.env` is not yet in the gateway's environment. | `hermes gateway restart`, then **Refresh models** ([section 8](#8-applying-changes-when-to-restart)). |
| Free setup says "That key was not accepted" | The key is wrong or truncated. | Copy it again from <https://openrouter.ai/keys> and paste it fresh. |
| Free setup says OpenRouter is not reachable | OpenRouter, or the container's internet access, is down. | Try again in a minute. |
| Gemini key refused during `hermes model` | The key is on the free tier. | Enable billing and create the key in that Google Cloud project. |
| OpenRouter key rejected | The key does not start with `sk-or-`. | Copy the key again from <https://openrouter.ai/keys>. |
| Claude subscription chat fails with "out of extra usage" | Anthropic bills third-party subscription use as extra usage. | Use `ANTHROPIC_API_KEY` instead. |
| `hermes model` asks for Anthropic credentials again after `hermes auth add anthropic` | The picker checks `ANTHROPIC_*` variables and Claude Code files, not the credential pool **(not verified end to end)**. | Set the model with `hermes config set model.provider anthropic` and `model.default <id>`, or use the dashboard model button. |
| Custom endpoint cannot be verified | URL missing `/v1`, or the server is not running. | Use `http://ollama:11434/v1`; start the service ([section 10](#10-local-models-with-ollama)). |
| Model "below the minimum 64K context" | The local model's window is too small. | Choose a model with at least 64K context. |
| Same variable in the project `.env` and the volume gives the wrong key | The volume's `/opt/data/.env` wins. | Change it with `hermes config set <KEY> <value>` or remove it with `hermes config unset <KEY>`. |
| Everything looks right but chat fails | A stale default model or a provider without credit. | Telemetry, **RUN DIAGNOSTICS** (or `hermes doctor`); confirm the provider has credit and model access. |

---

## 12. Reopening the tour

Click **GUIDE** in the top bar. When no model is connected the dashboard opens the tour on the
connect step (it sends the `no-model` reason described below). Closing the tour stores
`vesperTourDone = 1` in the browser's `localStorage`, which tells the dashboard you have already
seen it; remove that key to get the automatic behaviour back. To open it by hand from the browser
console:

```js
window.dispatchEvent(new CustomEvent('vesper:tour'))
// jump to a step: {detail: {step: 'connect'}}  (welcome, connect, voice, telegram, model, around, done)
// jump to a connect tab: {detail: {step: 'free'}}  (or 'login', 'key', 'local'); the ElevenLabs tab: 'elevenlabs'
// the "no model yet" variant: {detail: {reason: 'no-model'}}
```
