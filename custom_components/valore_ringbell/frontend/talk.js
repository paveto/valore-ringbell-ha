export class DoorbellTalk {
    constructor(showStatus, setTransmit, signedSocket) {
        this.showStatus = showStatus;
        this.setTransmit = setTransmit;
        this.press = null;
        this.signedSocket = signedSocket;
    }

    async enableMicrophone() {
        if (!navigator.mediaDevices?.getUserMedia) {
            this.showStatus("Mikrofon vyžaduje přístup k Home Assistantu přes HTTPS.");
            return;
        }
        if (this.press || this.requestingPermission) return;
        this.requestingPermission = true;
        try {
            const stream = await navigator.mediaDevices.getUserMedia({audio: true});
            stream.getTracks().forEach(track => track.stop());
            this.showStatus("Mikrofon připraven.");
        } catch {
            this.showStatus("Povol mikrofon v nastavení této stránky.");
        } finally {
            this.requestingPermission = false;
        }
    }

    async start() {
        if (this.press || this.requestingPermission) return;
        if (!navigator.mediaDevices?.getUserMedia) {
            this.showStatus("Mikrofon vyžaduje přístup k Home Assistantu přes HTTPS.");
            return;
        }

        const press = {};
        this.press = press;
        this.showStatus("Čekám na mikrofon…");

        try {
            press.context = new AudioContext();
            const resumed = press.context.resume().then(() => true, () => false);
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
            });
            if (this.press !== press) {
                stream.getTracks().forEach(track => track.stop());
                return;
            }

            press.stream = stream;
            if (!await resumed) throw new Error("Audio context did not resume");
            if (this.press !== press) return;
            await press.context.audioWorklet.addModule("/valore_ringbell/microphone.js");
            if (this.press !== press) return;
            const url = await this.signedSocket("talk");
            if (this.press !== press) return;
            const socket = press.socket = new WebSocket(url);
            this.showStatus("Zapínám reproduktor zvonku…");
            socket.onmessage = ({data}) => {
                if (this.press !== press) return;
                const reply = JSON.parse(data);
                if (reply.ready) {
                    try {
                        this.sendMicrophone(press, reply.sample_rate ?? 8000);
                    } catch {
                        this.stop("Mikrofon se nepodařilo připojit.");
                    }
                } else if (reply.error) {
                    this.stop(reply.error);
                }
            };
            socket.onclose = () => {
                if (this.press === press) this.stop("Přenos řeči skončil.");
            };
            socket.onerror = () => {
                if (this.press === press) this.stop("Otevři živý obraz a zkus to znovu.");
            };
        } catch (error) {
            if (this.press === press) {
                this.stop(error.name === "NotAllowedError" ? "Mikrofon vyžaduje povolení." : "Mikrofon se nepodařilo otevřít.");
            }
        }
    }

    sendMicrophone(press, rate) {
        const context = press.context;
        press.source = context.createMediaStreamSource(press.stream);
        press.processor = new AudioWorkletNode(context, "doorbell-microphone", {
            numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, processorOptions: {rate},
        });
        press.processor.port.onmessage = ({data}) => {
            const socket = press.socket;
            if (this.press === press && socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 1280) {
                socket.send(data);
            }
        };
        this.setTransmit(true);
        this.showStatus("Mluvíš do zvonku");
        press.source.connect(press.processor);
        // Keep the worklet scheduled. It leaves its output silent.
        press.processor.connect(context.destination);
    }

    stop(message = "") {
        const press = this.press;
        if (!press) return;
        this.press = null;
        press.stream?.getTracks().forEach(track => track.stop());
        press.source?.disconnect();
        press.processor?.disconnect();
        press.socket?.close();
        press.context?.close().catch(() => {});
        this.setTransmit(false);
        this.showStatus(message);
    }
}
