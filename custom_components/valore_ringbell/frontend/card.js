import { DoorbellAudio } from "./audio.js";
import { DoorbellTalk } from "./talk.js";

class ValoreDoorbellCard extends HTMLElement {
  setConfig(config) {
    if (!/^event\.[a-z0-9_]+$/.test(config.event_entity ?? "") ||
        !/^camera\.[a-z0-9_]+$/.test(config.camera ?? "")) {
      throw new Error("Vyber entitu zazvonění a kameru Valore.");
    }
    this.closeVideo();
    this.config = config;
    this.lastRing = undefined;
    this.render();
  }

  getCardSize() {
    return 5;
  }

  set hass(hass) {
    this.homeAssistant = hass;
    if (!this.config || !this.isConnected) return;
    const target = new URL(window.location.href);
    if (target.searchParams.get("valore_answer") === "1" &&
        hass.states[this.config.camera]?.attributes.access_token) {
      target.searchParams.delete("valore_answer");
      history.replaceState(history.state, "", target);
      this.openVideo();
    }
    const state = hass.states[this.config.event_entity];
    if (!state) return;
    const when = new Date(state.state);
    if (state.attributes.event_type === "ring" && Number.isFinite(when.getTime())) {
      this.lastRingLabel.hidden = false;
      this.lastRingLabel.textContent = "Poslední zazvonění · " + new Intl.DateTimeFormat(undefined, {
        day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
      }).format(when);
    }
    if (this.lastRing === undefined) {
      this.lastRing = state.state;
      return;
    }
    if (!Number.isFinite(Date.parse(state.state)) ||
        Date.parse(state.state) <= Date.parse(this.lastRing)) return;
    this.lastRing = state.state;
    if (state.attributes.event_type !== "ring") return;
    this.playChime();
    this.openVideo(true);
  }

  connectedCallback() {
    if (this.homeAssistant) this.hass = this.homeAssistant;
    this.pageHidden ??= () => {
      if (document.hidden) this.closeVideo();
    };
    this.pageClosed ??= () => this.closeVideo();
    document.addEventListener("visibilitychange", this.pageHidden);
    window.addEventListener("pagehide", this.pageClosed);
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this.pageHidden);
    window.removeEventListener("pagehide", this.pageClosed);
    this.closeVideo();
    this.lastRing = undefined;
    if (this.audio) void this.audio.close().catch(() => {});
    if (this.listener?.context) {
      void this.listener.context.close().catch(() => {});
      this.listener.context = null;
      this.listener.gain = null;
    }
    this.audio = undefined;
    this.syncSoundButton();
  }

  render() {
    const root = this.shadowRoot ?? this.attachShadow({mode: "open"});
    root.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; border-radius: 22px; }
        .heading { display: flex; align-items: center; gap: 12px; padding: 17px 18px; }
        .heading .icon { width: 44px; height: 44px; display: grid; place-items: center;
          background: color-mix(in srgb, var(--primary-color) 14%, var(--card-background-color));
          color: var(--primary-text-color); border-radius: 14px; flex: none; }
        h2 { margin: 0; font-size: 1.18rem; font-weight: 650; line-height: 1.25; }
        .status { margin: 4px 0 0; font-size: .81rem; color: var(--secondary-text-color); }
        .screen { position: relative; isolation: isolate; display: grid; place-items: center;
          aspect-ratio: 4 / 3; max-height: 440px; overflow: hidden; margin: 0 12px;
          border-radius: 16px; background: radial-gradient(ellipse at 48% 24%, #254241, #111f2a 74%);
          color: #f1f8f6; text-align: center; }
        .screen img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
        .screen.live { background: #0e1720; }
        .placeholder { display: grid; justify-items: center; gap: 12px; padding: 14px; }
        .placeholder ha-icon { display: grid; place-items: center; width: 64px; height: 64px;
          --mdc-icon-size: 30px; border-radius: 50%; border: 1px solid #6fa89e70;
          background: #b3f8eb17; color: #a8ebdb; }
        .message { font-size: .91rem; color: #e3efed; }
        .stream-state { position: absolute; left: 10px; top: 10px; display: inline-flex;
          align-items: center; gap: 7px; padding: 5px 9px; border-radius: 999px;
          background: #0e1720e8; color: #f8fcfb; font-size: .77rem; font-weight: 650; }
        .stream-state::before { content: ""; width: 7px; height: 7px;
          border-radius: 50%; background: #8be5ca; }
        .screen[data-phase="ringing"] .stream-state::before { background: #ffd074; }
        .screen[data-phase="connecting"] .stream-state::before { background: #b9c9d3; }
        .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 13px 12px 14px; }
        button { display: flex; align-items: center; justify-content: center; gap: 7px; min-height: 44px;
          padding: 0 12px; font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
          color: var(--primary-text-color); border-radius: 12px;
          border: 1px solid var(--divider-color); background: var(--secondary-background-color); }
        button ha-icon { --mdc-icon-size: 18px; }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button[aria-pressed="true"] { border-color: var(--primary-color); }
        .video { flex-grow: 1; background: #a1ead8; color: #102923; border-color: transparent; }
        .video.secondary { flex-grow: 0; background: var(--secondary-background-color); color: var(--primary-text-color); border-color: var(--divider-color); }
        .hangup { background: #b3253c; color: #fff; border-color: transparent; }
        .conversation { margin: 0; flex-basis: 100%; padding: 0 3px; }
        .conversation:empty, [hidden] { display: none !important; }
        @media (max-width: 360px) { button { padding-inline: 8px; } }
      </style>
      <ha-card>
        <div class="heading">
          <span class="icon"><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon></span>
          <div><h2></h2><p class="status" hidden></p></div>
        </div>
        <div class="screen">
          <img alt="Živý obraz od dveří" hidden>
          <div class="placeholder">
            <ha-icon icon="mdi:video-outline" aria-hidden="true"></ha-icon>
            <span class="message" role="status">Čeká na zazvonění</span>
          </div>
          <span class="stream-state" role="status" hidden></span>
        </div>
        <div class="controls">
          <button type="button" class="video"><ha-icon icon="mdi:video-outline" aria-hidden="true"></ha-icon><span>Otevřít obraz</span></button>
          <button type="button" class="sound" aria-pressed="false"><ha-icon icon="mdi:bell-outline" aria-hidden="true"></ha-icon><span>Zapnout zvonění</span></button>
          <button type="button" class="listen" aria-pressed="false" hidden><ha-icon icon="mdi:volume-high" aria-hidden="true"></ha-icon><span>Poslouchat</span></button>
          <button type="button" class="talk" aria-pressed="false" hidden><ha-icon icon="mdi:microphone-outline" aria-hidden="true"></ha-icon><span>Mluvit</span></button>
          <button type="button" class="hangup" hidden><ha-icon icon="mdi:phone-hangup" aria-hidden="true"></ha-icon><span>Položit</span></button>
          <p class="status conversation" role="status" hidden></p>
        </div>
      </ha-card>`;
    root.querySelector("h2").textContent = this.config.name ?? "U dveří";
    this.lastRingLabel = root.querySelector(".status");
    this.screen = root.querySelector(".screen");
    this.message = root.querySelector(".message");
    this.placeholder = root.querySelector(".placeholder");
    this.streamState = root.querySelector(".stream-state");
    this.image = root.querySelector("img");
    this.videoButton = root.querySelector(".video");
    this.soundButton = root.querySelector(".sound");
    this.listenButton = root.querySelector(".listen");
    this.talkButton = root.querySelector(".talk");
    this.hangupButton = root.querySelector(".hangup");
    this.conversationStatus = root.querySelector(".conversation");
    const showAudio = (notice) => {
      this.conversationStatus.textContent = notice;
      if (!this.listener.socket && !this.listener.pending) this.setListening(false);
    };
    const sign = (mode) => this.signedSocket(mode);
    this.listener = new DoorbellAudio(showAudio, 0.7, sign);
    this.talker = new DoorbellTalk(showAudio, (active) => {
      this.talkButton.querySelector("span").textContent = active ? "Přestat mluvit" : "Mluvit";
      this.talkButton.setAttribute("aria-pressed", String(active));
      if (active && this.listener.gain) this.listener.setVolume(0);
      else this.listener.setVolume(0.7);
    }, sign);
    this.image.addEventListener("load", () => {
      if (!this.videoOpen) return;
      this.image.hidden = false;
      this.placeholder.hidden = true;
      if (!this.passive) {
        this.screen.dataset.phase = "live";
        this.streamState.textContent = "Živě";
      }
    });
    this.image.addEventListener("error", () => {
      if (this.videoOpen) this.closeVideo("Živý obraz teď není dostupný.");
    });
    this.videoButton.addEventListener("click", () => {
      if (this.videoOpen && !this.passive) this.closeVideo();
      else this.openVideo();
    });
    this.listenButton.addEventListener("click", () => {
      if (this.listener.socket || this.listener.pending) {
        this.listener.stop();
        this.setListening(false);
      } else {
        void this.listener.start();
        this.setListening(true);
      }
    });
    this.talkButton.addEventListener("click", () => {
      if (this.talker.press) this.talker.stop();
      else void this.talker.start();
    });
    this.hangupButton.addEventListener("click", () => void this.hangup());
    this.soundButton.addEventListener("click", () => void this.toggleSound());
    this.syncSoundButton();
  }

  setListening(active) {
    this.listenButton.setAttribute("aria-pressed", String(active));
    this.listenButton.querySelector("span").textContent = active ? "Ztlumit poslech" : "Poslouchat";
  }

  async signedSocket(mode) {
    const path = "/api/valore_ringbell/" + this.config.camera + "/" + mode;
    const signed = await this.homeAssistant.callWS({type: "auth/sign_path", path, expires: 60});
    const address = new URL(signed.path, window.location.origin);
    address.protocol = address.protocol === "https:" ? "wss:" : "ws:";
    return address;
  }

  async hangup() {
    this.closeVideo();
    try {
      const response = await this.homeAssistant.fetchWithAuth(
        "/api/valore_ringbell/" + this.config.camera + "/hangup", {method: "POST"}
      );
      if (!response.ok) throw new Error(response.status);
    } catch {
      this.message.textContent = "Náhled se zavřel. Odpojení zvonku se nepodařilo potvrdit.";
    }
  }

  openVideo(passive = false) {
    clearTimeout(this.videoTimer);
    if (!this.videoOpen || (this.passive && !passive)) {
      const token = this.homeAssistant?.states[this.config.camera]?.attributes.access_token;
      if (typeof token !== "string" || !token) {
        this.closeVideo("Živý obraz teď není dostupný.");
        return;
      }
      this.videoOpen = true;
      this.passive = passive;
      this.screen.classList.add("live");
      this.message.textContent = "Připojuji kameru…";
      this.screen.dataset.phase = passive ? "ringing" : "connecting";
      this.streamState.textContent = passive ? "Někdo zvoní" : "Připojuji";
      this.streamState.hidden = false;
      this.placeholder.hidden = false;
      this.videoButton.classList.toggle("secondary", !passive);
      this.videoButton.querySelector("ha-icon").setAttribute("icon", passive ? "mdi:phone" : "mdi:video-off-outline");
      this.videoButton.querySelector("span").textContent = passive ? "Přijmout" : "Zavřít obraz";
      this.hangupButton.hidden = false;
      this.listenButton.hidden = passive;
      this.talkButton.hidden = passive;
      this.conversationStatus.hidden = passive;
      this.image.referrerPolicy = "no-referrer";
      this.image.hidden = false;
      this.image.src = "/api/camera_proxy_stream/" + encodeURIComponent(this.config.camera)
        + "?token=" + encodeURIComponent(token) + (passive ? "&valore_preview=1" : "")
        + "&t=" + Date.now();
    }
    if (this.passive) this.videoTimer = setTimeout(() => this.closeVideo(), 180_000);
  }

  closeVideo(message = "Čeká na zazvonění") {
    clearTimeout(this.videoTimer);
    this.videoOpen = false;
    if (!this.image) return;
    this.listener.stop();
    this.talker.stop();
    this.listenButton.hidden = true;
    this.setListening(false);
    this.talkButton.hidden = true;
    this.hangupButton.hidden = true;
    this.conversationStatus.hidden = true;
    this.image.removeAttribute("src");
    this.image.hidden = true;
    this.message.textContent = message;
    this.placeholder.hidden = false;
    this.streamState.hidden = true;
    this.screen.classList.remove("live");
    delete this.screen.dataset.phase;
    this.videoButton.classList.remove("secondary");
    this.videoButton.querySelector("ha-icon").setAttribute("icon", "mdi:video-outline");
    this.videoButton.querySelector("span").textContent = "Otevřít obraz";
  }

  syncSoundButton() {
    if (!this.soundButton) return;
    const enabled = this.audio?.state === "running";
    this.soundButton.setAttribute("aria-pressed", String(enabled));
    this.soundButton.querySelector("span").textContent = enabled ? "Ztlumit zvonění" : "Zapnout zvonění";
  }

  async toggleSound() {
    const Context = window.AudioContext ?? window.webkitAudioContext;
    if (!Context) {
      this.lastRingLabel.textContent = "Prohlížeč zvuk nepodporuje.";
      return;
    }
    this.audio ??= new Context();
    try {
      if (this.audio.state === "running" && this.soundButton.getAttribute("aria-pressed") === "true") {
        await this.audio.suspend();
      } else {
        await this.audio.resume();
        this.playChime();
      }
    } catch {
      this.lastRingLabel.textContent = "Zvuk se nepodařilo zapnout.";
    }
    this.syncSoundButton();
  }

  playChime() {
    const audio = this.audio;
    if (audio?.state !== "running") return;
    const start = audio.currentTime;
    for (const [offset, frequency] of [[0, 784], [0.26, 587.33]]) {
      const note = audio.createOscillator();
      const gain = audio.createGain();
      note.type = "sine";
      note.frequency.value = frequency;
      const time = start + offset;
      gain.gain.setValueAtTime(0.001, time);
      gain.gain.exponentialRampToValueAtTime(0.18, time + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.001, time + 0.48);
      note.connect(gain).connect(audio.destination);
      note.start(time);
      note.stop(time + 0.49);
      note.onended = () => {
        note.disconnect();
        gain.disconnect();
      };
    }
  }
}

if (!customElements.get("valore-doorbell-card")) {
  customElements.define("valore-doorbell-card", ValoreDoorbellCard);
}
