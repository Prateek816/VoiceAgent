import json
import os
import threading
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
load_dotenv()
import websocket

API_KEY = os.environ["ASSEMBLYAI_API_KEY"]
# A live AAC (ADTS) internet radio stream, so no microphone is needed.
STREAM_URL = "https://14123.live.streamtheworld.com/WBBRAMAAC.aac"
RUN_SECONDS = 25
# AAC is self-describing (ADTS headers carry the sample rate)
CONNECTION_PARAMS = {"speech_model": "universal-3-5-pro", "encoding": "aac"}
API_ENDPOINT = f"wss://streaming.assemblyai.com/v3/ws?{urlencode(CONNECTION_PARAMS)}"

stop = threading.Event()


def on_open(ws):
    print("Connected. Streaming live radio for ~25 seconds.")

    def stream_audio():
        # Pull the live radio stream and forward each chunk as a binary frame.
        response = requests.get(STREAM_URL, stream=True)
        deadline = time.time() + RUN_SECONDS
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if stop.is_set() or time.time() > deadline:
                    break
                ws.send(chunk, websocket.ABNF.OPCODE_BINARY)
        finally:
            response.close()
            if ws.sock and ws.sock.connected:
                # Terminate finalizes the open turn.
                # Keep the connection open long enough to receive the last final.
                ws.send(json.dumps({"type": "Terminate"}))

    threading.Thread(target=stream_audio, daemon=True).start()


def on_message(ws, message):
    data = json.loads(message)
    print(data)
    if data.get("type") == "Turn":
        print(data.get("transcript", ""), end="\n" if data.get("end_of_turn") else "\r")


def on_error(ws, error):
    # On a normal shutdown, websocket-client hands the server's close frame to
    # on_error; ignore it and let on_close report the disconnect. Real failures
    # arrive as exceptions, not close frames.
    if isinstance(error, websocket.ABNF) and error.opcode == websocket.ABNF.OPCODE_CLOSE:
        return
    print(f"\nError: {error}")
    stop.set()


def on_close(ws, status, msg):
    stop.set()
    print("\nDisconnected.")


def main():
    ws = websocket.WebSocketApp(
        API_ENDPOINT,
        header={"Authorization": API_KEY},
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
    ws_thread.start()

    try:
        while ws_thread.is_alive():
            ws_thread.join(0.1)
    except KeyboardInterrupt:
        stop.set()
        if ws.sock and ws.sock.connected:
            ws.send(json.dumps({"type": "Terminate"}))  
        ws.close()


if __name__ == "__main__":
    main()