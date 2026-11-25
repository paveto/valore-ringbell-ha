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
    if (this.lastRing === undefined) {
      this.lastRing = state.state;
      return;
    }
    if (!Number.isFinite(Date.parse(state.state)) ||
        Date.parse(state.state) <= Date.parse(this.lastRing)) return;
    this.lastRing = state.state;
    if (state.attributes.event_type !== "ring") return;
    this.lastRingLabel.textContent = "Zazvonění v " + new Date(state.state).toLocaleTimeString();
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
        ha-card { overflow: hidden; }
        .heading { display: flex; align-items: center; gap: 12px; padding: 16px; }
        .icon { display: grid; place-items: center; width: 40px; height: 40px;
          background: var(--secondary-background-color); border-radius: 12px;
          color: var(--primary-color); flex-shrink: 0; }
        h2 { margin: 0; font-size: 1.1rem; font-weight: 600; }
        .status { margin: 3px 0 0; font-size: .85rem; color: var(--secondary-text-color); }
        .screen { position: relative; display: grid; place-items: center; min-height: 100px;
          background: var(--secondary-background-color); text-align: center; }
        .screen.live { aspect-ratio: 4 / 3; background: #101519; color: #f2f6f7; }
        img { position: absolute; width: 100%; height: 100%; object-fit: contain; }
        [hidden] { display: none !important; }
        .controls { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px; }
        button { font: inherit; font-size: .9rem; cursor: pointer; min-height: 42px;
          padding: 0 14px; color: var(--primary-text-color); border-radius: 10px;
          border: 1px solid var(--divider-color); background: var(--card-background-color); }
        button[aria-pressed="true"] { border-color: var(--primary-color); }
      </style>
      <ha-card>
        <div class="heading">
          <span class="icon"><ha-icon icon="mdi:doorbell-video"></ha-icon></span>
          <div><h2></h2><p class="status" role="status">Čeká na zazvonění</p></div>
        </div>
        <div class="screen">
          <span class="message" role="status">Živý obraz se otevře po zazvonění.</span>
          <img alt="Živý obraz od dveří" hidden>
        </div>
        <div class="controls">
          <button type="button" class="sound" aria-pressed="false">Zapnout zvuk</button>
          <button type="button" class="video">Otevřít obraz</button>
          <button type="button" class="listen" hidden>Poslouchat</button>
          <button type="button" class="talk" hidden>Mluvit</button>
          <button type="button" class="hangup" hidden>Položit</button>
          <p class="status conversation" role="status" hidden></p>
        </div>
      </ha-card>`;
    root.querySelector("h2").textContent = this.config.name ?? "U dveří";
    this.lastRingLabel = root.querySelector(".status");
    this.screen = root.querySelector(".screen");
    this.message = root.querySelector(".message");
    this.image = root.querySelector("img");
    this.videoButton = root.querySelector(".video");
    this.soundButton = root.querySelector(".sound");
    this.listenButton = root.querySelector(".listen");
    this.talkButton = root.querySelector(".talk");
    this.hangupButton = root.querySelector(".hangup");
    this.conversationStatus = root.querySelector(".conversation");
    const showAudio = (notice) => { this.conversationStatus.textContent = notice; };
    const sign = (mode) => this.signedSocket(mode);
    this.listener = new DoorbellAudio(showAudio, 0.7, sign);
    this.talker = new DoorbellTalk(showAudio, (active) => {
      this.talkButton.textContent = active ? "Přestat mluvit" : "Mluvit";
      if (active && this.listener.gain) this.listener.setVolume(0);
      else this.listener.setVolume(0.7);
    }, sign);
    this.image.addEventListener("load", () => {
      if (!this.videoOpen) return;
      this.image.hidden = false;
      this.message.hidden = true;
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
        this.listenButton.textContent = "Poslouchat";
      } else {
        void this.listener.start();
        this.listenButton.textContent = "Ztlumit poslech";
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
      this.videoButton.textContent = passive ? "Přijmout" : "Zavřít obraz";
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

  closeVideo(message = "Živý obraz se otevře po zazvonění.") {
    clearTimeout(this.videoTimer);
    this.videoOpen = false;
    if (!this.image) return;
    this.listener.stop();
    this.talker.stop();
    this.listenButton.hidden = true;
    this.listenButton.textContent = "Poslouchat";
    this.talkButton.hidden = true;
    this.hangupButton.hidden = true;
    this.conversationStatus.hidden = true;
    this.image.removeAttribute("src");
    this.image.hidden = true;
    this.message.textContent = message;
    this.message.hidden = false;
    this.screen.classList.remove("live");
    this.videoButton.textContent = "Otevřít obraz";
  }

  syncSoundButton() {
    if (!this.soundButton) return;
    const enabled = this.audio?.state === "running";
    this.soundButton.setAttribute("aria-pressed", String(enabled));
    this.soundButton.textContent = enabled ? "Ztlumit zvuk" : "Zapnout zvuk";
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
