# Hermes WebUI CI/CD pipeline

<a href="https://dash.elest.io/deploy?source=cicd&social=dockerCompose&url=https://github.com/elestio-examples/hermes-webui"><img src="deploy-on-elestio.png" alt="Deploy on Elest.io" width="180px" /></a>

Deploy Hermes WebUI server with CI/CD on Elestio

<img src="hermes-webui.png" style='width: 100%;'/>
<br/>
<br/>

# Once deployed ...

You can open Hermes WebUI here:

    URL: https://[CI_CD_DOMAIN]
    password: [ADMIN_PASSWORD]

After unlocking with the password, open `Settings -> Providers` and connect a model provider. Two providers can be linked via OAuth in two clicks (Nous Portal, GitHub Copilot) — no API key required. The other ten providers (Anthropic, OpenAI, Gemini, Google, Mistral, DeepSeek, Kimi/Moonshot, MiniMax, Ollama, Ollama Cloud) take a paste-in API key.

The bundled Hermes Agent runs as the chat backend (sessions, skills, memory, scheduler) and the monitoring dashboard is reachable from the VM only on port 9119.
