#!/usr/bin/env python3
"""Runs inside GitHub Actions: asks the local VoiceStudio container for one MP3 per scene."""
import base64, json, os, subprocess, sys, time, urllib.error, urllib.request

BASE = os.environ.get("VS_BASE", "http://127.0.0.1:3900")
KEY = os.environ.get("VS_KEY", "")
OUT = os.environ.get("OUT_DIR", "out")
job = json.loads(base64.b64decode(os.environ["SCRIPT_B64"]))
AUTH = {"Authorization": f"Bearer {KEY}"} if KEY else {}


def req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(AUTH)
    if data:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def wait_ready(max_s=900):
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            st, _, body = req("GET", "/.well-known/voicestudio-speech", timeout=10)
            if st == 200:
                print("READY:", body[:600].decode(errors="replace"), flush=True)
                return
        except Exception:
            pass
        time.sleep(5)
    sys.exit("VoiceStudio never became ready")


def peek(path):
    try:
        st, _, body = req("GET", path, timeout=30)
        print(f"GET {path} -> {st}: {body[:800].decode(errors='replace')}", flush=True)
        return json.loads(body)
    except Exception as e:
        print(f"GET {path} failed: {e}", flush=True)
        return None


def first_voice(data):
    items = data.get("voices") or data.get("data") if isinstance(data, dict) else data
    if not items:
        return None
    v = items[0]
    if isinstance(v, dict):
        return v.get("id") or v.get("voice_id") or v.get("name")
    return str(v)


def attempts(text, voice, model):
    base = {"input": text, "response_format": "mp3"}
    cands = []
    if voice and model:
        cands.append({**base, "voice": voice, "model": model})
    if voice:
        cands.append({**base, "voice": voice})
    if model:
        cands.append({**base, "model": model})
    cands.append({**base, "voice": voice or "alloy", "model": "tts-1"})
    cands.append(base)
    seen, out = set(), []
    for c in cands:
        k = json.dumps(c, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def speak(text, voice, model):
    last = None
    for payload in attempts(text, voice, model):
        for _ in range(2):
            try:
                st, ct, body = req("POST", "/v1/audio/speech", payload, timeout=1800)
                if st == 200 and len(body) > 2000 and "json" not in ct:
                    return body
                last = f"{st} {ct} {body[:300]!r}"
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code} for keys {sorted(payload)}: {e.read()[:400]!r}"
                print("attempt failed:", last, flush=True)
                break
            except Exception as e:
                last = str(e)
                time.sleep(10)
    sys.exit(f"speech failed: {last}")


def words_for(path):
    if os.environ.get("TRANSCRIBE") != "1":
        return None
    cmd = ["curl", "-s", "-m", "900", "-H", f"Authorization: Bearer {KEY}", "-F", f"file=@{path}",
           "-F", "response_format=verbose_json", "-F", "timestamp_granularities[]=word",
           BASE + "/v1/audio/transcriptions"]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
        ws = data.get("words") or [w for s in data.get("segments", []) for w in s.get("words", [])]
        res = [{"t": w["start"], "d": w["end"] - w["start"], "w": w["word"].strip()} for w in ws]
        return res or None
    except Exception as e:
        print("transcription skipped:", e, flush=True)
        return None


def main():
    os.makedirs(OUT, exist_ok=True)
    wait_ready()
    peek("/v1/models")
    voice = job.get("vs_voice") or first_voice(peek("/v1/audio/voices"))
    model = job.get("vs_model") or os.environ.get("VS_MODEL") or None
    print("USING voice:", voice, "model:", model, flush=True)
    for i, sc in enumerate(job["scenes"]):
        audio = speak(sc["narration"], voice, model)
        path = os.path.join(OUT, f"s{i}.mp3")
        open(path, "wb").write(audio)
        w = words_for(path)
        if w:
            json.dump(w, open(os.path.join(OUT, f"s{i}.json"), "w"))
        print(f"scene {i} done ({len(audio)} bytes)", flush=True)


if __name__ == "__main__":
    main()
