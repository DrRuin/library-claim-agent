# web

The page the policyholder opens on their phone. Plain HTML and JavaScript, served by `app/main.py`.

| File | What it does |
|---|---|
| `index.html` | The sweep screen: camera view, **Start sweep** and **I'm done**, live counts (books, identified, priced, other items), the logged lines and the conversation |
| `sweep.js` | Opens the `/ws/sweep` WebSocket, sends a 1024 px frame to the agent and a full-resolution frame with the compass heading once a second, plays the agent's voice and shows its updates |
| `audio.js` | `AudioWorklet` that captures the microphone as 16 kHz PCM16 |

The camera needs HTTPS on a phone. `cloudflared tunnel --url http://127.0.0.1:8300` gives an HTTPS address without an account.
