import type {
    BinarySensor, Camera, FFmpegInput, Intercom, MediaObject,
    ResponseMediaStreamOptions, VideoCamera,
} from '@scrypted/sdk';
import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { once } from 'node:events';

// Scrypted injects these values into console scripts at runtime.
declare const sdk: typeof import('@scrypted/sdk').default;
declare const ScryptedDeviceBase: typeof import('@scrypted/sdk').ScryptedDeviceBase;
declare const ScryptedInterface: typeof import('@scrypted/sdk').ScryptedInterface;
declare const ScryptedMimeTypes: typeof import('@scrypted/sdk').ScryptedMimeTypes;
declare const device: { handleTypes(...interfaces: string[]): void };
device.handleTypes(ScryptedInterface.BinarySensor);

const linkFiles = ['/config/.storage/valore_ringbell.scrypted', '/homeassistant/.storage/valore_ringbell.scrypted'];
const stream: ResponseMediaStreamOptions = {
    id: 'valore', name: 'Valore', container: 'mpegts',
    video: { codec: 'h264', width: 640, height: 480 },
    audio: { codec: 'aac', sampleRate: 16000 },
};

async function bridgeLink() {
    for (const file of linkFiles) {
        try {
            const { data } = JSON.parse(await readFile(file, 'utf8'));
            const url = new URL(data.url);
            if (url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.port &&
                /^\/[A-Za-z0-9_-]{32}$/.test(url.pathname) && !url.username && !url.password &&
                !url.search && !url.hash) return url.href;
        } catch {
            // Home Assistant OS may mount its configuration under either directory.
        }
    }
    throw new Error('Zapni Scrypted pro kameru Valore a ověř, že Home Assistant běží.');
}

async function openSocket(path: 'talk' | 'rings') {
    return new WebSocket((await bridgeLink()).replace(/^http:/, 'ws:') + '/' + path);
}

function readyToTalk(socket: WebSocket, signal: AbortSignal) {
    return new Promise<number>((resolve, reject) => {
        const stop = () => reject(new Error('Zvonek nepotvrdil mikrofon.'));
        socket.addEventListener('close', stop, { once: true, signal });
        socket.addEventListener('error', stop, { once: true, signal });
        signal.addEventListener('abort', stop, { once: true });
        socket.addEventListener('message', ({ data }) => {
            try {
                const reply = JSON.parse(String(data));
                if (reply.ready && [8000, 16000].includes(reply.sample_rate)) resolve(reply.sample_rate);
                else stop();
            } catch {
                stop();
            }
        }, { once: true, signal });
    });
}

export default class ValoreDoorbell extends ScryptedDeviceBase implements Camera, VideoCamera, BinarySensor, Intercom {
    private talk?: { socket?: WebSocket; audio?: ChildProcessWithoutNullStreams; controller: AbortController };
    private ringTimeout?: ReturnType<typeof setTimeout>;

    constructor(nativeId: string) {
        super(nativeId);
        this.binaryState = false;
        void this.listenForRings();
    }

    private async listenForRings() {
        let backoff = 1000;
        while (true) {
            let socket: WebSocket | undefined;
            try {
                socket = await openSocket('rings');
                socket.addEventListener('open', () => {
                    backoff = 1000;
                    this.console.info('Valore: připojeno k místnímu mostu.');
                }, { once: true });
                socket.addEventListener('message', ({ data }) => {
                    if (JSON.parse(String(data)).event !== 'ring') return;
                    this.binaryState = true;
                    clearTimeout(this.ringTimeout);
                    this.ringTimeout = setTimeout(() => { this.binaryState = false; }, 700);
                });
                await once(socket, 'close');
            } catch {
                socket?.close();
            }
            await new Promise(resolve => setTimeout(resolve, backoff));
            backoff = Math.min(backoff * 2, 10000);
        }
    }

    async getPictureOptions() {
        return [{ id: 'valore', name: 'Valore', picture: { width: 640, height: 480 } }];
    }

    async takePicture() {
        const response = await fetch(await bridgeLink() + '/snapshot', {
            redirect: 'error', signal: AbortSignal.timeout(7000),
        });
        if (!response.ok) throw new Error('Momentálně není dostupný snímek zvonku.');
        return sdk.mediaManager.createMediaObject(Buffer.from(await response.arrayBuffer()), 'image/jpeg');
    }

    async getVideoStreamOptions() {
        return [stream];
    }

    async getVideoStream() {
        const url = await bridgeLink() + '/av';
        const input: FFmpegInput = {
            url, container: 'mpegts', mediaStreamOptions: stream,
            inputArguments: ['-probesize', '65536', '-analyzeduration', '500000', '-f', 'mpegts', '-i', url],
        };
        return sdk.mediaManager.createMediaObject(Buffer.from(JSON.stringify(input)), ScryptedMimeTypes.FFmpegInput);
    }

    async startIntercom(media: MediaObject) {
        await this.stopIntercom();
        const session = { controller: new AbortController() } as NonNullable<typeof this.talk>;
        this.talk = session;
        try {
            const [input, ffmpeg] = await Promise.all([
                sdk.mediaManager.convertMediaObjectToBuffer(media, ScryptedMimeTypes.FFmpegInput),
                sdk.mediaManager.getFFmpegPath(),
            ]);
            const settings: FFmpegInput = JSON.parse(input.toString());
            if (!settings.inputArguments) throw new Error("Chybí zdroj zvuku.");
            if (this.talk !== session) return;
            const socket = await openSocket('talk');
            if (this.talk !== session) {
                socket.close();
                return;
            }
            session.socket = socket;
            const signal = AbortSignal.any([session.controller.signal, AbortSignal.timeout(8000)]);
            const close = () => { if (this.talk === session) void this.stopIntercom(); };
            socket.addEventListener('close', close, { once: true });
            const rate = await readyToTalk(socket, signal);
            if (this.talk !== session) return;
            const process = session.audio = spawn(ffmpeg, [
                '-loglevel', 'error', '-nostdin', ...settings.inputArguments,
                '-vn', '-ar', String(rate), '-ac', '1', '-f', 's16le', 'pipe:1',
            ]);
            process.stdin.end();
            process.stderr.resume();
            process.once('exit', close);
            process.once('error', close);
            const frameBytes = rate / 25;
            let remainder: Buffer = Buffer.alloc(0);
            process.stdout.on('data', (chunk: Buffer) => {
                if (socket.readyState !== WebSocket.OPEN) return;
                const bytes = remainder.length ? Buffer.concat([remainder, chunk]) : chunk;
                let offset = 0;
                while (offset + frameBytes <= bytes.length) {
                    if (socket.bufferedAmount < frameBytes * 5) socket.send(bytes.subarray(offset, offset + frameBytes));
                    offset += frameBytes;
                }
                remainder = bytes.subarray(offset);
            });
        } catch {
            if (this.talk === session) await this.stopIntercom();
            throw new Error('Přenos řeči do zvonku se nepodařilo spustit.');
        }
    }

    async stopIntercom() {
        const session = this.talk;
        this.talk = undefined;
        session?.controller.abort();
        session?.socket?.close();
        session?.audio?.kill();
    }
}
