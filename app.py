import asyncio
import os
import queue
import re
from concurrent.futures import ThreadPoolExecutor
import uvicorn

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from google.cloud import speech

# Environment variables
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID")

app = FastAPI()

# -------------------- TTS --------------------
def generate_tts(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {
        "accept": "*/*",
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "text": text,
        "voice_settings": {"stability": 0.50, "similarity_boost": 0.30}
    }
    response = requests.post(url, headers=headers, json=data, stream=True)
    response.raise_for_status()
    audio_data = b""
    for chunk in response.iter_content(chunk_size=1024):
        if chunk:
            audio_data += chunk
    return audio_data

# -------------------- STT Loop --------------------
def stt_loop(in_q: queue.Queue, out_q: queue.Queue, sample_rate: int = 16000):
    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate,
        language_code="en-US",
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True
    )

    def generator():
        while True:
            chunk = in_q.get()
            if chunk is None:
                return
            yield chunk

    audio_generator = generator()
    request_generator = (
        speech.StreamingRecognizeRequest(audio_content=content)
        for content in audio_generator
    )

    try:
        responses = client.streaming_recognize(streaming_config, request_generator)
        for response in responses:
            if not response.results:
                continue
            result = response.results[0]
            if not result.alternatives:
                continue
            transcript = result.alternatives[0].transcript
            print("Partial transcript:", transcript)

            if result.is_final:
                print("Final transcript:", transcript)
                if re.search(r"\b(exit|quit)\b", transcript, re.I):
                    print("Exiting...")
                    out_q.put(None)
                    continue
                try:
                    audio = generate_tts(transcript)
                    out_q.put(audio)
                except Exception as e:
                    print(f"TTS error: {e}")
                    out_q.put(None)
        out_q.put(None)
    except Exception as e:
        print(f"STT error: {e}")
        out_q.put(None)

# -------------------- WebSocket --------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    in_q = queue.Queue()
    out_q = queue.Queue()

    # Always use 16kHz to avoid mismatches
    sample_rate = 16000

    # Send a welcome test TTS immediately
    try:
        welcome_audio = generate_tts("Hello! Connection established. Please start speaking.")
        await websocket.send_bytes(welcome_audio)
        print("Sent welcome TTS")
    except Exception as e:
        print(f"Error sending welcome TTS: {e}")

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(stt_loop, in_q, out_q, sample_rate)
    sent_exit = False

    try:
        while True:
            try:
                data = await websocket.receive_bytes()
                in_q.put(data)
            except WebSocketDisconnect:
                break

            while True:
                try:
                    audio = out_q.get_nowait()
                    if audio is None:
                        sent_exit = True
                        break
                    print(f"Sending audio chunk of size {len(audio)}")
                    await websocket.send_bytes(audio)
                except queue.Empty:
                    break

            if sent_exit:
                break
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        in_q.put(None)
        try:
            future.result(timeout=10)
        except:
            pass
        executor.shutdown(wait=True)
        print("WebSocket closed")

# -------------------- Frontend --------------------
@app.get("/", response_class=HTMLResponse)
async def get():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Voice Conversation App</title>
    </head>
    <body>
        <h1>Real-Time Voice Echo App</h1>
        <button id="startBtn">Start Conversation</button>
        <button id="stopBtn" disabled>Stop</button>
        <p id="status">Status: Idle</p>
        <script>
            let ws = null;
            let audioContext = null;
            let source = null;
            let processor = null;
            let gainNode = null;
            let mediaStream = null;
            const startBtn = document.getElementById('startBtn');
            const stopBtn = document.getElementById('stopBtn');
            const status = document.getElementById('status');

            startBtn.onclick = async function() {
                try {
                    startBtn.disabled = true;
                    status.textContent = 'Status: Connecting...';

                    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    audioContext = new (window.AudioContext || window.webkitAudioContext)();

                    source = audioContext.createMediaStreamSource(mediaStream);
                    processor = audioContext.createScriptProcessor(4096, 1, 1);
                    gainNode = audioContext.createGain();
                    gainNode.gain.value = 0;
                    processor.connect(gainNode);
                    gainNode.connect(audioContext.destination);
                    source.connect(processor);

                    const protocol = location.protocol === "https:" ? "wss" : "ws";
                    ws = new WebSocket(`${protocol}://${location.host}/ws`);
                    ws.binaryType = 'arraybuffer';

                    ws.onopen = async function() {
                        console.log('WebSocket connected');
                        startBtn.textContent = 'Connected';
                        stopBtn.disabled = false;
                        status.textContent = 'Status: Connected';
                    };

                    processor.onaudioprocess = function(e) {
                        if (!ws || ws.readyState !== WebSocket.OPEN) return;
                        const inputData = e.inputBuffer.getChannelData(0);
                        const buffer = new Int16Array(inputData.length);
                        for (let i = 0; i < inputData.length; i++) {
                            let s = Math.max(-1, Math.min(1, inputData[i]));
                            buffer[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                        }
                        ws.send(buffer.buffer);
                    };

                    ws.onmessage = function(event) {
                        console.log("Received audio chunk:", event.data.byteLength);
                        if (event.data instanceof ArrayBuffer && event.data.byteLength > 0) {
                            const blob = new Blob([event.data], { type: 'audio/mpeg' });
                            const url = URL.createObjectURL(blob);
                            const audio = new Audio(url);
                            audio.play().catch(e => console.error('Playback error:', e));
                        }
                    };

                    ws.onclose = function() {
                        console.log('WebSocket closed');
                        cleanup();
                    };

                    ws.onerror = function(error) {
                        console.error('WebSocket error:', error);
                        cleanup();
                        status.textContent = 'Status: Error - WebSocket failed';
                    };
                } catch (err) {
                    console.error('Start error:', err);
                    cleanup();
                    status.textContent = `Status: Error - ${err.message}`;
                }
            };

            stopBtn.onclick = function() {
                if (ws) {
                    ws.close();
                }
                cleanup();
            };

            function cleanup() {
                if (processor && source) {
                    try { source.disconnect(processor); processor.disconnect(); } catch (e) {}
                }
                if (gainNode) {
                    try { gainNode.disconnect(); } catch (e) {}
                }
                if (mediaStream) {
                    mediaStream.getTracks().forEach(track => track.stop());
                }
                if (audioContext) {
                    audioContext.close().catch(e => {});
                }
                startBtn.disabled = false;
                startBtn.textContent = 'Start Conversation';
                stopBtn.disabled = true;
                status.textContent = 'Status: Idle';
                ws = null;
                audioContext = null;
                source = null;
                processor = null;
                gainNode = null;
                mediaStream = null;
            }
        </script>
    </body>
    </html>
    """

# -------------------- Run --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
